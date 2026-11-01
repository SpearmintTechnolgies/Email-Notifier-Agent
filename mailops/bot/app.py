from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Conflict, NetworkError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .. import access
from ..config import (
    PRIORITY_TO_FOLDER,
    TAG_TO_FOLDER,
    active_account_ids,
    load_accounts,
    load_runtime,
    load_settings,
    save_settings,
)
from ..dashboard import (
    collect_bucket,
    collect_counts,
    format_dashboard,
    format_page,
    page_slice,
)
from ..db import clear_draft, get_draft, get_message_locations, save_draft
from .. import group_links
from ..himalaya_client import Himalaya
from ..identity import STALE_TEXT, capture, locate, recall, remember, verify
from ..outbound import (
    body_sha256,
    cancel_approval,
    claim_approval,
    classify_smtp,
    create_pending,
    finish_approval,
    format_confirmation,
    plan_reply,
    plans_match,
)
from ..policy import PRIORITY_FOLDERS, can_organize, can_send, describe, group_shareable
from ..redact import redact_text
from ..mail_format import format_card, format_card_page
from . import admin, callbacks as cbq, keyboards as kb, mailboxes, nav
from .callbacks import Ack

log = logging.getLogger("mailops.bot")


# --------------------------------------------------------------- ack plumbing
#: The ``Ack`` created for the update currently being handled. Refusal helpers
#: deep in the call tree (``_notice``, ``_deny_read_only``, ``_refuse_stale``)
#: only receive the ``CallbackQuery``, not the handler's local ``Ack``; this
#: registry lets them reuse it so a refusal can never double-answer the query.
#: Keyed by callback-query id and cleared when the handler returns, so two
#: owners tapping at the same time never share an entry.
_ACKS: dict[str, Ack] = {}


def _ack_for(q: Any) -> Ack | None:
    """The in-flight ``Ack`` for this query, or a fresh one if unknown."""
    if q is None:
        return None
    qid = str(getattr(q, "id", "") or "")
    existing = _ACKS.get(qid)
    if existing is not None:
        return existing
    return Ack(q)

WAITING_TEXT: dict[int, dict[str, str]] = {}
# How long a WAITING_TEXT prompt (Search, Type my reply, VIP, Context, KB
# note, Purpose, Rule editing) stays armed before it is treated as expired.
# Without this, an admin who taps a prompt button and then sends an
# unrelated message hours/days later (e.g. casual chat with the bot) would
# have that unrelated text silently consumed as the reply draft / setting
# value for a stale workflow. Matches the TTL pattern already used by
# ``access.ACTION_TTL_S`` / ``AWAITING_TTL_S`` for the newer onboarding flows.
WAITING_TEXT_TTL_S = 15 * 60


def _set_waiting(uid: int, data: dict) -> bool:
    """Install a WAITING_TEXT slot for ``uid``.

    Refuses to arm over a pending DM/group password-capture slot in
    ``mailboxes.AWAITING`` — the same invariant ``mailboxes._set_awaiting``
    already enforces for its own slots, so a password can never be
    silently swallowed by an unrelated later screen regardless of which
    dict the race lands in.

    Otherwise, clears any (non-secret) ``mailboxes.AWAITING`` slot for
    this uid. Found during the pre-deployment audit: the two input-state
    dicts never touched each other, so an admin who had, say, "Edit
    purpose" (mailboxes.AWAITING) armed from one tap and then tapped
    "Search" (this dict) here would have their next message consumed by
    whichever dict ``mailboxes.handle_text``/the WAITING_TEXT fallthrough
    happens to check first — not necessarily the prompt they just tapped
    — and the loser would sit there to confusingly misfire on some LATER
    unrelated message instead. Arming one now always clears the other.
    """
    protected = (mailboxes._DM_PW_FIELD, mailboxes._GROUP_PW_FIELD)
    current = mailboxes.AWAITING.get(int(uid))
    if current is not None and current.get("field") in protected:
        return False
    mailboxes.AWAITING.pop(int(uid), None)
    WAITING_TEXT[uid] = {**data, "expires_at": time.time() + WAITING_TEXT_TTL_S}
    return True


def _get_waiting(uid: int) -> dict | None:
    """Pending WAITING_TEXT state for ``uid``, or None if absent/expired.

    An expired entry is popped so it never lingers to be matched later."""
    pending = WAITING_TEXT.get(uid)
    if pending is None:
        return None
    expires_at = pending.get("expires_at")
    if expires_at is not None and time.time() > float(expires_at):
        WAITING_TEXT.pop(uid, None)
        return None
    return pending


def _allowed(user_id: int) -> bool:
    return access.is_authorized_user(user_id)


def _menu(update: Update):
    uid = getattr(update.effective_user, "id", None)
    return kb.main_menu(owner=access.is_owner(uid))


def _unauthorized_text(user_id: int | None) -> str:
    uid = user_id if user_id is not None else "?"
    return (
        "Unauthorized.\n\n"
        f"Your Telegram ID: {uid}\n"
        "An administrator must add this numeric ID. Self-registration is disabled."
    )


def _role_denied_text(perm: str) -> str:
    return f"Your role cannot do that ({perm}). Nothing was changed."


_MAILBOX_NAMES: dict[str, tuple[float, set[str]]] = {}
_MAILBOX_NAMES_TTL = 300.0


def _resolve_mailbox(account_id: str, mailbox: str) -> str:
    """Map a stale virtual priority folder to the real mailbox.

    Read-only / not-yet-approved accounts never get mail moved into the
    INBOX.P* folders, but dashboards built by older versions put those names
    in button refs. If such a folder doesn't exist, the message is in INBOX.
    Current dashboards already carry the physical mailbox, so this is rare.
    """
    if mailbox not in PRIORITY_FOLDERS or can_organize(account_id):
        return mailbox
    now = time.monotonic()
    cached = _MAILBOX_NAMES.get(account_id)
    if cached is None or now - cached[0] > _MAILBOX_NAMES_TTL:
        try:
            names = {str(n).strip().lower() for n in Himalaya().mailbox_list(account_id)}
        except Exception:  # noqa: BLE001
            names = set()
        cached = (now, names)
        _MAILBOX_NAMES[account_id] = cached
    names = cached[1]
    if names and mailbox.lower() not in names:
        return "INBOX"
    return mailbox


class InactiveAccountError(ValueError):
    """A button/ref points at an account Mailops no longer manages."""


def _require_active(account_id: str) -> str:
    """Refuse any account that is not in the active account list.

    Old Telegram buttons (or crafted callback data) may still carry a retired
    account id such as the former Gmail inbox. Nothing may be read, drafted or
    sent for it, so this is checked before any mailbox call is made.
    """
    if account_id not in active_account_ids():
        raise InactiveAccountError(account_id)
    return account_id


def _parse_ref(ref: str) -> tuple[str, str, str]:
    parts = ref.split("|", 2)
    if len(parts) != 3:
        raise ValueError("bad ref")
    _require_active(parts[0])
    return parts[0], _resolve_mailbox(parts[0], parts[1]), parts[2]


async def _notice(q: Any, text: str) -> None:
    """Tell the user why an action was refused.

    The callback query may already be answered (``on_callback`` answers
    every authorized tap up front, before any slow work). Issuing a second
    ``answerCallbackQuery`` is rejected by Telegram with "Query is too old
    and response timeout expired or query id is invalid" AND shows the user
    nothing — the October 2026 "buttons do nothing" reports. Route the text
    through the exactly-once ``Ack`` instead: it shows an alert only if the
    answer is still unclaimed, otherwise it posts a message (with a
    standalone fallback when the quoted message was deleted).
    """
    ack = _ack_for(q)
    if ack is not None:
        await ack.notice(text)
        return
    await cbq.safe_reply(getattr(q, "message", None), text)


async def _deny_read_only(q: Any, account_id: str, action: str) -> bool:
    """True (and tells the user) if the account may not change its mailbox."""
    if can_organize(account_id):
        return False
    log.info("blocked %s on read-only account %s", action, account_id)
    await _notice(
        q,
        f"{account_id} is read-only ({describe(account_id)}): "
        f"{action} is disabled. Nothing in the mailbox was changed.",
    )
    return True


async def _refuse_stale(
    q: Any, him: Any, account_id: str, mailbox: str, msg_id: str, action: str
) -> bool:
    """True (and tells the user) when the referenced message is not verifiably
    the one that was recorded for this account/folder/id. Every mailbox
    mutation and approved reply passes through here before it acts."""
    verdict = verify(him, account_id, mailbox, msg_id)
    if verdict.ok:
        return False
    log.warning(
        "refused %s on %s %s/%s: stale or mismatched message (%s)",
        action, account_id, mailbox, msg_id, verdict.reason,
    )
    await _notice(q, STALE_TEXT.format(why=verdict.reason[:1].upper() + verdict.reason[1:]))
    return True


def _moved_id(him: Any, account_id: str, mailbox: str, msg_id: str, dest: str) -> str:
    """Id the moved message has in ``dest`` (the destination folder assigns a
    new one); falls back to the old id, which later checks will then judge."""
    try:
        ident = recall(account_id, mailbox, msg_id)
        moved = locate(him, ident, dest) if ident else None
    except Exception:  # noqa: BLE001
        moved = None
    if moved is None:
        return msg_id
    remember(moved)
    return moved.msg_id


def _from_label(envelope: dict[str, Any]) -> str:
    frm = envelope.get("from") or []
    if isinstance(frm, list) and frm and isinstance(frm[0], dict):
        return str(frm[0].get("email") or frm[0].get("name") or "?")
    return str(envelope.get("from") or "?")


def _prio_title(priority: int, account_id: str | None = None) -> str:
    name = {0: "Critical", 1: "Important", 2: "Normal", 3: "Noise"}.get(priority, f"P{priority}")
    if account_id:
        return f"{account_id} · {name}"
    return name


GROUP_CHAT_TYPES = ("group", "supergroup")

# Callback data a GROUP chat may trigger directly: read-only dashboard
# navigation (counts, subject/sender lists, paging, refresh, all-emails list)
# and the deep-link minter ``gdl:*`` which does NOT act on mail - it mints a
# single-use token and shows a private-chat URL button. Everything that would
# leak email bodies, drafts, credentials or admin internals is still private.
GROUP_CALLBACK_EXACT = frozenset(
    {
        "dash",
        "nav:home",
        "dash:force",
        "page:force",
        "pg:n",
        "pg:p",
        "noop",
        "g:all",
        "g:all:n",
        "g:all:p",
    }
)
GROUP_CALLBACK_PREFIXES = (
    "b:p", "ab:", "pj:", "gdl:", "dg:", "health:",
    # "gr:" = GROUP-READ namespace (Oct 2026 group-first reading fix). This
    # is the ONLY entry point for bodies/replies/drafts/sends inside a
    # group - each sub-action re-checks mailops.policy.group_shareable for
    # the specific account before any content is rendered, and the
    # existing access.can()/required_perm() checks below still run on
    # every tap exactly like the private "open:"/"reply:"/"send:" family.
    # See mailops/policy.py's "Group-read sharing policy" section and
    # mailops/bot/app.py's ``open_thread`` for the restricted-account path.
    "gr:",
)

GROUP_DENIED_ALERT = "Private chat only - open the bot directly for this."

# Narrow allow-list of Mailboxes-screen (``mb:*``) callbacks that may run in
# a whitelisted group. The rest (view, enable/disable, health, purpose,
# audit, errors, dm_prompt) stay private-only because they either expose
# an existing mailbox's details or capture credentials, both of which must
# never surface in a group where multiple members can read the message.
#
# Credentials themselves are NEVER accepted in a group even for these
# allowed callbacks: `mb:arm:*` in a group deliberately HANDS OFF to a
# private DM so the password step leaves the group entirely.
GROUP_MB_EXACT = frozenset(
    {
        "mb:m",       # list mailboxes (names + states; no stored secrets)
        "mb:add",     # provider pick screen
        "mb:qadd",    # Quick Add Email entry (paste prompt + preset list)
        "mb:switch",  # Switch Inbox / Account status screen
        "mb:bulk",    # Bulk CSV import entry (preview + confirm + status)
        "mb:preset",  # Manage Server Presets entry (list screen)
        "mb:smart",   # Smart Add Email entry (email-first flow)
    }
)
GROUP_MB_PREFIXES = (
    "mb:add:",     # mb:add:hostinger / mb:add:siteground / mb:add:google / mb:add:custom
    "mb:arm:",     # confirm & arm credentials -> triggers private handoff
    "mb:cancel:",  # cancel the owner's wizard
    "mb:edit:",    # reset non-secret fields and restart wizard
    "mb:skip:",    # skip a skippable field
    "mb:poll:",    # refresh the group wizard view
    "mb:credsvia:",  # choose how to supply credentials (DM / server CLI)
    "mb:qadd:",    # quick-add sub-actions (preset pick, edit, save preset)
    "mb:bulk:",    # bulk CSV sub-actions (preview, confirm, status, pause/resume/cancel/retry)
    # Interactive mailbox manager (Switch Inbox / Account Status). Every
    # sub-action here is read-only: pagination, filter clear, per-mailbox
    # control screen, on-demand IMAP+SMTP health check, Refresh. The
    # ``mb:mbx:*:settings`` tap in a group alerts the admin and does not
    # expose settings in the group; see mailboxes.handle_callback.
    "mb:switch:",
    "mb:mbx:",
    # Manage Server Presets (Phase 4) - group-safe VIEW/pagination; the
    # module itself re-checks owner permission + confirmation tokens for
    # any create/edit/delete mutation, same defence-in-depth pattern as
    # bulk_mailboxes above.
    "mb:preset:",
    # Smart Add Email (Phase 5) - group-first server-resolution + review
    # flow; the credential step still only ever reaches DM or (opt-in,
    # owner-only, single-pinned-group) mb:group_pw:.
    "mb:smart:",
    # Opt-in, owner-only, single-pinned-group password entry (Phase: Group
    # Password Entry). Disabled by default; see mailops.group_credentials.
    "mb:group_pw:",
)


def group_mailbox_callback_allowed(data: str) -> bool:
    return data in GROUP_MB_EXACT or data.startswith(GROUP_MB_PREFIXES)


def group_callback_allowed(data: str) -> bool:
    return data in GROUP_CALLBACK_EXACT or data.startswith(GROUP_CALLBACK_PREFIXES)


def _chat(update: Update):
    return getattr(update, "effective_chat", None)


def _is_group(update: Update) -> bool:
    chat = _chat(update)
    return bool(chat is not None and getattr(chat, "type", None) in GROUP_CHAT_TYPES)


def _group_configured(update: Update) -> bool:
    chat = _chat(update)
    return bool(chat is not None and access.group_allowed(chat.id))


async def guard(update: Update, *, allow_unconfigured_group: bool = False) -> bool:
    """Authorize the sender (and, in groups, the chat).

    Private chats: the sender must be in the persisted access store (seeded
    once from TELEGRAM_ALLOWED_USERS / TELEGRAM_OWNER_ID). Empty store fails
    closed; there is no self-registration. Groups: the sender must be an
    authorized user AND the group must be allowlisted (except /start + /chatid
    helpers via ``allow_unconfigured_group``). Whitelisting a group does not
    authorize its members. Strangers in a group are ignored silently;
    callback presses get a private alert only.
    """
    user = update.effective_user
    in_group = _is_group(update)
    chat = _chat(update)
    chat_id = getattr(chat, "id", None)
    chat_type = getattr(chat, "type", None) or "private"

    ok = access.authorized(
        getattr(user, "id", None),
        chat_id,
        chat_type,
        allow_unconfigured_group=allow_unconfigured_group,
    )

    if ok:
        return True

    q = getattr(update, "callback_query", None)
    if in_group:
        log.info(
            "ignored unauthorized group access chat=%s user=%s",
            chat_id,
            getattr(user, "id", None),
        )
        if q is not None:
            # First and only answer for this query (nothing authorized ran).
            await _ack_for(q).alert("Unauthorized.")
        return False

    if update.effective_message:
        await update.effective_message.reply_text(
            _unauthorized_text(getattr(user, "id", None))
        )
    return False


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Metadata-only trace of every update (no message text) so we can prove
    whether the bot actually receives traffic from a given chat."""
    try:
        chat = _chat(update)
        user = update.effective_user
        kind = "update"
        if getattr(update, "callback_query", None) is not None:
            kind = "callback:" + str(update.callback_query.data or "")[:24]
        elif getattr(update, "message", None) is not None:
            text = update.message.text or ""
            kind = "command:" + text.split()[0][:40] if text.startswith("/") else "message"
        elif getattr(update, "my_chat_member", None) is not None:
            kind = "my_chat_member"
        log.info(
            "update kind=%s chat=%s type=%s user=%s",
            kind,
            getattr(chat, "id", None),
            getattr(chat, "type", None),
            getattr(user, "id", None),
        )
    except Exception:  # noqa: BLE001 - tracing must never break handling
        log.debug("log_update failed", exc_info=True)


def traced_command(name: str):
    """Instrument a slash-command handler end to end.

    Why this exists (October 25 live incident)
    -----------------------------------------
    The log proved ``/dashboard`` updates arrived::

        21:37:16 update kind=command:/dashboard chat=-5531260744 ...
        21:38:21 update kind=command:/dashboard chat=-5531260744 ...

    …and then showed NOTHING. No reply, no guard refusal, no Telegram error,
    no traceback. ``log_update`` (a ``TypeHandler`` at group=-1) had traced
    the *receipt* of the update, but nothing traced handler entry, exit,
    duration or outcome, so the single most useful fact — that the handler
    had been entered and never returned — was invisible. The reliability
    work done for inline buttons (``mailops.bot.callbacks.Ack``, which logs
    route/ack_ms/total_ms for every tap) covered ONLY ``CallbackQueryHandler``
    routes; slash commands had no equivalent, which is why a hung command
    looked identical to an undelivered one.

    Every command registered through this wrapper now logs entry, and on
    exit its outcome (``ok`` / ``refused`` / ``error``) plus elapsed ms. A
    command that is slow or wedged is visible as an ``entered`` line with no
    matching ``finished`` line, and a command that raises is logged with a
    traceback here instead of relying on the global error handler alone.
    """

    def decorate(func):
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat = _chat(update)
            chat_id = getattr(chat, "id", None)
            user_id = getattr(getattr(update, "effective_user", None), "id", None)
            started = time.monotonic()
            log.info(
                "command %s entered chat=%s type=%s user=%s",
                name,
                chat_id,
                getattr(chat, "type", None),
                user_id,
            )
            try:
                await func(update, context)
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "command %s failed chat=%s user=%s after=%sms category=%s",
                    name,
                    chat_id,
                    user_id,
                    int((time.monotonic() - started) * 1000),
                    cbq.classify(exc),
                    exc_info=exc,
                )
                raise
            else:
                log.info(
                    "command %s finished chat=%s user=%s after=%sms",
                    name,
                    chat_id,
                    user_id,
                    int((time.monotonic() - started) * 1000),
                )

        return wrapper

    return decorate


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log when the bot is added to / removed from a chat (shows the chat id
    needed for TELEGRAM_HOME_CHANNEL). Replies nothing."""
    cm = update.my_chat_member
    if cm is None:
        return
    log.info(
        "bot membership changed chat=%s type=%s title=%r %s -> %s by user=%s",
        cm.chat.id,
        cm.chat.type,
        cm.chat.title,
        cm.old_chat_member.status,
        cm.new_chat_member.status,
        cm.from_user.id if cm.from_user else None,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Classify Telegram failures accurately.

    ``telegram.error.BadRequest`` SUBCLASSES ``NetworkError`` in PTB, so the
    previous ``isinstance(err, NetworkError)`` branch logged expired callback
    queries, missing reply targets and un-editable messages as "Telegram
    network error" — an application bug disguised as connectivity. Classify
    first, then log at a level that matches what actually happened.
    """
    err = context.error
    if isinstance(err, Conflict):
        log.warning(
            "Telegram Conflict: another getUpdates consumer is using this bot "
            "token (only one poller may run). PTB will keep retrying."
        )
        return
    category = cbq.classify(err)
    if category == cbq.NOT_MODIFIED:
        log.debug("Telegram edit was a no-op (identical content re-tapped)")
        return
    if category == cbq.EXPIRED:
        # Not a network fault: the query was answered too late or twice.
        log.warning(
            "callback query no longer answerable (expired or already "
            "answered): %s",
            err,
        )
        return
    if category == cbq.REPLY_TARGET_MISSING:
        log.warning(
            "reply target no longer exists (message deleted); handlers fall "
            "back to a standalone message: %s",
            err,
        )
        return
    if category in (cbq.MESSAGE_MISSING, cbq.RATE_LIMITED, cbq.FORBIDDEN, cbq.BAD_REQUEST):
        log.warning("Telegram request rejected category=%s: %s", category, err)
        return
    if category in (cbq.NETWORK, cbq.TIMED_OUT):
        log.warning("Telegram transport error category=%s: %s", category, err)
        return
    log.error("unhandled handler error category=%s", category, exc_info=err)
    # An internal crash in a COMMAND used to be invisible to the user: the
    # traceback went to the log and the chat stayed silent, which is
    # indistinguishable from "the bot never got my message". Tell them, in
    # the same chat, that the command failed and was logged.
    await _report_internal_failure(update, category)


async def _report_internal_failure(update: object, category: str) -> None:
    """Best-effort 'that command failed' notice in the originating chat.

    Only for message/command updates: an inline tap already gets feedback
    through ``mailops.bot.callbacks.Ack``. Never leaks the exception text
    (it can contain mailbox names, addresses or tokens) — the operator reads
    the traceback in the log, the user just learns it failed.
    """
    message = getattr(update, "effective_message", None)
    if message is None or getattr(update, "callback_query", None) is not None:
        return
    try:
        await cbq.safe_reply(
            message,
            "That command failed with an internal error and has been logged. "
            "Nothing was sent and no mailbox was changed. Please try again.",
        )
    except Exception:  # noqa: BLE001 - the error handler must never raise
        log.debug("could not deliver internal-failure notice", exc_info=True)


async def _edit_or_reply(update: Update, text: str, markup=None) -> int | None:
    """Render a response into the tapped message, else post a new one.

    Returns the id of the message that now shows ``text``, so list renderers
    can bind their refs to it (see ``mailops.bot.nav``). Distinguishes
    callback-answer failure (handled by ``Ack``) from genuine message-
    rendering failure: an identical re-tap is a success, a deleted or
    un-editable message falls back to a fresh message in the SAME chat so
    the user's tap is never silently lost.
    """
    if len(text) > 3900:
        text = text[:3900] + "\n…"
    q = update.callback_query
    if q is not None:
        category = await cbq.safe_edit(q, text, markup)
        if category in (cbq.OK, cbq.NOT_MODIFIED):
            # NOT_MODIFIED = same content re-tapped (esp. shared group
            # dashboards); the message already shows what we wanted.
            return getattr(getattr(q, "message", None), "message_id", None)
    message = update.effective_message
    if message is not None:
        return await cbq.safe_reply(message, text, markup)
    return None


#: How long a dashboard render may wait for a cold cache before it answers
#: with whatever is known and a "warming" note.
#:
#: The October 25 incident: after a restart the in-memory cache is empty, so
#: ``collect_counts`` called ``cache.refresh_all()`` inline — a full-fleet
#: IMAP pass that the live log measured at 6-13 MINUTES for 39 accounts.
#: ``/dashboard`` therefore produced no Telegram message at all: the handler
#: was running the whole time, no exception was raised, nothing was logged,
#: and the user reasonably concluded the bot was dead. A command must always
#: answer; a slow mailbox fleet is a reason for a partial answer, never for
#: silence.
#: The dashboard NEVER waits for IMAP. It renders from whatever counts are
#: already known (memory, or the snapshot persisted across restarts) and lets
#: a background pass fill them in.
#:
#: History, because this was got wrong twice:
#:
#: 1. Originally ``show_dashboard`` called ``collect_counts()``, which
#:    treated an empty cache as "rebuild now" and ran a full-fleet IMAP pass
#:    INLINE. After a restart that is 6-13 minutes on this 39-mailbox fleet,
#:    so ``/dashboard`` produced no message at all (Oct 25, 21:21-21:39).
#: 2. The first fix replaced that with a 6-second wait. Better, but still a
#:    wait: the live log then showed ``total_ms=24891`` and
#:    ``category=callback_expired`` - several queued ``asyncio.to_thread``
#:    hops plus the budget pushed every Dashboard tap past Telegram's
#:    callback window, re-breaking the very thing the Ack work fixed.
#:
#: So the budget is now ZERO. Reading cached counts is a dict copy plus (once
#: per process) one small SQLite row; that is event-loop safe and needs no
#: thread hop at all. The controls render immediately in every case.
DASHBOARD_BUDGET_SECONDS = 0.0

#: How long an explicit Manual refresh waits for the rebuild it asked for.
#: Bounded for the same reason: a repeated tap must not pin a thread-pool
#: worker for the full length of a fleet-wide pass.
MANUAL_REFRESH_BUDGET_SECONDS = 90.0


def _warming_note(scanned: int, total: int) -> str:
    """Honest progress line for a dashboard rendered during a refresh."""
    if total and scanned:
        return (
            f"Refreshing from IMAP: {scanned}/{total} mailboxes scanned. "
            "Counts update as each mailbox completes — tap Dashboard again."
        )
    return (
        "Refreshing from IMAP in the background. Counts appear as mailboxes "
        "complete — tap Dashboard again to update."
    )


async def show_dashboard(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, force: bool = False
) -> None:
    from .. import cache as cache_mod

    note = ""
    if force:
        # Full IMAP rebuild across every account: many Himalaya subprocesses,
        # minutes of work. It runs off the event loop (running it inline froze
        # the whole bot, including answerCallbackQuery for other taps, which
        # is what made unrelated buttons report "Query is too old"), is
        # single-flight so repeated taps share one pass, and is bounded so the
        # handler always returns and reports what actually happened.
        await asyncio.to_thread(cache_mod.ensure_refresh_running)
        finished = await asyncio.to_thread(
            cache_mod.wait_for_refresh, MANUAL_REFRESH_BUDGET_SECONDS
        )
        note = (
            "Manual refresh done — cache rebuilt from IMAP."
            if finished
            else "Manual refresh is still running in the background; "
            "showing the latest cached counts."
        )
        # Don't claim a clean rebuild when mailboxes could not be reached:
        # the whole point of this incident was a refresh that looked
        # successful while 32 hosts were unreachable.
        down = cache_mod.unreachable_accounts()
        if finished and down:
            shown = ", ".join(down[:4]) + (" ..." if len(down) > 4 else "")
            note = (
                f"Manual refresh finished, but {len(down)} mailbox(es) could "
                f"not be reached ({shown}). Their counts are last-known, not "
                "live; everything else was rebuilt from IMAP."
            )
    # No thread hop, no waiting: cached counts only.
    counts, cold = cache_mod.counts_now()
    if cold or cache_mod.is_stale():
        # Nothing cached, or the mailbox list changed since the last pass.
        # Start a background refresh and render immediately regardless.
        cache_mod.ensure_refresh_running()
    if not force and cache_mod.refresh_in_progress():
        scanned, total = cache_mod.refresh_progress()
        note = note or _warming_note(scanned, total)
    if not counts:
        # Genuinely nothing to show yet (first ever run, or a wiped db). The
        # real controls still render so the group is interactive.
        await _edit_or_reply(
            update,
            "Priority dashboard\n"
            "Cache: building\n\n"
            + _warming_note(*cache_mod.refresh_progress()),
            kb.group_dashboard_keyboard(
                [],
                is_admin=access.can(
                    getattr(update.effective_user, "id", None), access.P_ADMIN
                ),
            )
            if _is_group(update)
            else kb.dashboard_keyboard([]),
        )
        log.info("dashboard rendered with controls (no counts cached yet)")
        return
    uid = getattr(update.effective_user, "id", None)
    if _is_group(update):
        await _edit_or_reply(
            update,
            format_dashboard(counts, force_note=note, group=True),
            kb.group_dashboard_keyboard(
                counts,
                is_admin=access.can(uid, access.P_ADMIN),
                can_settings=access.can(uid, access.P_SETTINGS),
                can_draft=access.can(uid, access.P_DRAFT),
                can_send=access.can(uid, access.P_SEND),
            ),
        )
        return
    summaries_only = not access.can(uid, access.P_READ)
    text = format_dashboard(
        counts, force_note=note, summaries_only=summaries_only
    )
    await _edit_or_reply(
        update,
        text,
        kb.dashboard_keyboard(
            counts, show_settings=access.can(uid, access.P_SETTINGS)
        ),
    )


def _current_view(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> dict[str, Any]:
    """The view the tapped message is showing.

    Prefers the message-scoped snapshot so pagination/Back follow the list
    the user is actually looking at, even when another owner rendered it.
    Falls back to the tapper's own ``user_data`` for pre-deploy keyboards.
    """
    snap = _snapshot_for(update)
    if snap is not None:
        return dict(snap.view)
    return dict(context.user_data.get("view") or {})


def _snapshot_for(update: Update) -> nav.Snapshot | None:
    """The nav snapshot bound to the message this callback came from."""
    q = getattr(update, "callback_query", None)
    message = getattr(q, "message", None) if q is not None else None
    if message is None:
        return None
    chat = getattr(message, "chat", None)
    return nav.load(
        getattr(chat, "id", None), getattr(message, "message_id", None)
    )


def _snapshot_items(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> list[dict[str, str]]:
    """Already-fetched items for this message, avoiding a fresh IMAP pass."""
    snap = _snapshot_for(update)
    if snap is not None and snap.items:
        return list(snap.items)
    return list(context.user_data.get("view_items") or [])


async def _load_items(
    priority: int, account_id: str | None, *, force: bool = False
) -> list[dict[str, str]]:
    """Fetch a priority bucket WITHOUT blocking the event loop.

    ``collect_bucket`` can trigger a full ``cache.refresh_all``, which spawns
    one Himalaya subprocess per folder per account (39 accounts here). On the
    event loop that stalls every other callback — including the
    ``answerCallbackQuery`` that stops a button spinning — long enough for
    Telegram to invalidate the query.
    """
    return await asyncio.to_thread(
        collect_bucket, priority, account_id, force=force
    )


async def show_bucket_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    priority: int,
    account_id: str | None = None,
    page: int = 0,
    reload: bool = True,
    force: bool = False,
) -> None:
    prev = _current_view(update, context)
    view = {
        "priority": priority,
        "account_id": account_id,
        "page": page,
        "flagged": prev.get("flagged") if prev.get("priority") == priority else False,
        "search": prev.get("search"),
    }
    if force:
        items = await _load_items(priority, account_id, force=True)
    elif reload:
        items = await _load_items(priority, account_id, force=False)
    else:
        items = _snapshot_items(update, context)
        if not items:
            items = await _load_items(priority, account_id, force=False)
    slice_items, page, total_pages = page_slice(items, page)
    view["page"] = page
    if view.get("flagged"):
        title = "Needs reply"
    elif view.get("search"):
        title = f"Search: {view['search']}"
    else:
        title = _prio_title(priority, account_id)
    await _render_list(
        update,
        context,
        title=title,
        items=items,
        slice_items=slice_items,
        page=page,
        total_pages=total_pages,
        view=view,
    )


_ALL_EMAIL_PRIORITIES = (0, 1, 2, 3)
_GROUP_DEEP_LINKS = {
    "reply_pick": (access.P_DRAFT, "Reply"),
    "ai_drafts": (access.P_DRAFT, "AI Drafts"),
    "approvals": (access.P_SEND, "Send / Approvals"),
    "admin_users": (access.P_ADMIN, "Manage Users"),
    "admin_groups": (access.P_ADMIN, "Whitelist Groups"),
    "settings": (access.P_SETTINGS, "Settings"),
    "audit": (access.P_ADMIN, "Audit Logs"),
    "open_pick": (access.P_READ, "Open email"),
    "open_ref": (access.P_READ, "Open email"),
    "mailboxes": (access.P_ADMIN, "Mailboxes"),
}


def _collect_all_emails() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for priority in _ALL_EMAIL_PRIORITIES:
        for row in collect_bucket(priority, None, force=False):
            ref = row.get("ref")
            if not ref or ref in seen:
                continue
            seen.add(ref)
            items.append(row)
    return items


async def show_all_emails(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, page: int = 0
) -> None:
    """Group-safe listing of every priority bucket (subjects + senders only)."""
    items = await asyncio.to_thread(_collect_all_emails)
    slice_items, page, total_pages = page_slice(items, page)
    in_group = _is_group(update)
    uid = getattr(update.effective_user, "id", None)
    can_read = access.can(uid, access.P_READ)
    token = nav.new_token()
    text = format_page(
        "All Emails",
        slice_items,
        page,
        total_pages,
        len(items),
        group=in_group,
        can_open=can_read,
    )
    if not can_read:
        markup = kb.page_keyboard(page, total_pages, 0, group=False)
    elif in_group:
        restricted = {
            i
            for i, it in enumerate(slice_items, start=1)
            if not group_shareable(str(it.get("account_id") or ""))
        }
        markup = kb.group_all_emails_keyboard(
            page, total_pages, len(slice_items), restricted=restricted, token=token
        )
    else:
        markup = kb.page_keyboard(
            page, total_pages, len(slice_items), token=token
        )
    message_id = await _edit_or_reply(update, text, markup)
    view = {"priority": 0, "account_id": None, "page": page, "all": True}
    page_refs = [it["ref"] for it in slice_items]
    browse_refs = [it["ref"] for it in items]
    chat = _chat(update)
    nav.save(
        getattr(chat, "id", None),
        message_id,
        token=token,
        view=view,
        page_refs=page_refs,
        browse_refs=browse_refs,
        items=items,
        rendered_by=uid,
    )
    context.user_data["view"] = view
    context.user_data["view_items"] = items
    context.user_data["browse_refs"] = browse_refs
    context.user_data["page_refs"] = page_refs


def _bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    bot = getattr(context, "bot", None)
    for attr in ("username", "_bot_user"):
        value = getattr(bot, attr, None) if bot is not None else None
        if attr == "_bot_user" and value is not None:
            value = getattr(value, "username", None)
        if isinstance(value, str) and value:
            return value
    return "ourmailagent_bot"


async def _mint_deep_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str
) -> None:
    """Called when the user taps a sensitive button in the group.

    Verifies the required permission server-side (whitelisting a group never
    grants its members access), mints a single-use token tied to this user
    and chat, and shows a URL button that opens a private bot session.
    """
    q = update.callback_query
    if q is None:
        return
    cfg = _GROUP_DEEP_LINKS.get(action)
    if cfg is None:
        await _notice(q, "Unknown control.")
        return
    perm, label = cfg
    uid = getattr(update.effective_user, "id", None)
    chat = _chat(update)
    chat_id = getattr(chat, "id", None)
    if uid is None or chat_id is None:
        return
    if not access.can(uid, perm):
        await _notice(q, _role_denied_text(perm))
        return
    try:
        token = group_links.issue(int(uid), int(chat_id), action)
    except group_links.LinkError as exc:
        log.warning("group deep-link mint failed: %s", exc)
        await _notice(q, "Could not create a private link. Try again.")
        return
    url = group_links.deep_link(_bot_username(context), token)
    await _edit_or_reply(
        update,
        f"{label} — continue privately with the bot.\n"
        "This link opens a one-shot private session for the admin who tapped. "
        "It expires in 5 minutes and cannot be re-used.",
        kb.group_private_link_keyboard(url, label),
    )


async def _show_private(update: Update, text: str, markup=None) -> None:
    """Reply in a private chat (deep links always start from /start)."""
    msg = getattr(update, "effective_message", None)
    if msg is None:
        return
    if len(text) > 3900:
        text = text[:3900] + "\n…"
    await msg.reply_text(text, reply_markup=markup)


async def _consume_deep_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE, token: str
) -> None:
    """Handle ``/start mdl_<token>`` in a private chat.

    Verifies the token belongs to this user and has not expired or been
    used, then routes to the matching private screen. Group whitelisting
    never grants access on its own: the caller still has to be an
    authorized user with the right permission.
    """
    uid = getattr(update.effective_user, "id", None)
    if uid is None:
        return
    try:
        link = group_links.consume(int(uid), token)
    except group_links.LinkError as exc:
        await _show_private(
            update,
            f"That link cannot be used.\n{exc}\n\n"
            "Open the group dashboard again and tap the control once more.",
        )
        return
    cfg = _GROUP_DEEP_LINKS.get(link.action)
    if cfg is None:
        await _show_private(update, "That link points at an unknown action.")
        return
    perm, label = cfg
    if not access.can(uid, perm):
        log.info(
            "deep-link denied by permission user=%s action=%s perm=%s",
            uid, link.action, perm,
        )
        await _show_private(update, _role_denied_text(perm))
        return
    log.info(
        "deep-link consumed user=%s chat=%s action=%s", uid, link.chat_id, link.action
    )
    await _dispatch_deep_link(update, context, link.action, label, link.payload)


async def _dispatch_deep_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    label: str,
    payload: dict,
) -> None:
    """Run the private-chat screen the deep link named.

    Every branch reuses an existing private handler. Nothing here reads
    the mailbox before permission has been re-verified in ``_consume``.
    """
    if action in ("reply_pick", "ai_drafts", "open_pick"):
        await _show_private_pick(update, context, action, label)
        return
    if action == "open_ref":
        ref = str(payload.get("ref") or "")
        try:
            account_id, mailbox, msg_id = _parse_ref(ref)
        except (ValueError, InactiveAccountError):
            await _show_private(update, "That email reference is no longer valid.")
            return
        await open_thread(update, context, account_id, mailbox, msg_id)
        return
    if action == "approvals":
        await _show_private_approvals(update, context)
        return
    if action == "admin_users":
        text, markup = admin.users_screen(update.effective_user.id)
        await _show_private(update, text, markup)
        return
    if action == "admin_groups":
        text, markup = admin.groups_screen(update.effective_user.id)
        await _show_private(update, text, markup)
        return
    if action == "settings":
        settings = load_settings()
        await _show_private(update, "Settings", kb.settings_keyboard(settings))
        return
    if action == "audit":
        text, markup = admin.audit_screen()
        await _show_private(update, text, markup)
        return
    if action == "mailboxes":
        text, markup = mailboxes.main_screen()
        await _show_private(update, text, markup)
        return
    await _show_private(update, f"{label} is not available yet.")


async def _show_private_pick(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    label: str,
) -> None:
    """Private picker: list recent P0/P1/P2 emails so the admin can continue."""
    uid = update.effective_user.id
    items = _collect_all_emails()[:10]
    context.user_data["view"] = {
        "priority": 1,
        "account_id": None,
        "page": 0,
        "all": True,
    }
    context.user_data["view_items"] = items
    context.user_data["browse_refs"] = [it["ref"] for it in items]
    context.user_data["page_refs"] = [it["ref"] for it in items]
    if not items:
        await _show_private(
            update,
            f"{label}\n\nNo emails to act on right now. Pull Manual Refresh "
            "in the group dashboard and try again.",
        )
        return
    lines = [f"{label} — pick an email to continue privately.", ""]
    rows: list[list[Any]] = []
    for i, it in enumerate(items, start=1):
        subject = (it.get("subject") or "(no subject)")[:60]
        sender = it.get("from") or "?"
        lines.append(f"{i}. {subject}")
        lines.append(f"    from {sender}")
        rows.append(
            [
                InlineKeyboardButton(
                    f"{i}. {subject[:40]}",
                    callback_data=f"open:{it['ref']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("Back to dashboard", callback_data="dash")])
    await _show_private(update, "\n".join(lines), InlineKeyboardMarkup(rows))


async def _show_private_approvals(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Private list of pending send approvals for this operator."""
    from ..db import db as _db

    uid = update.effective_user.id
    with _db() as conn:
        rows = conn.execute(
            "SELECT token, account_id, mailbox, msg_id, recipient, subject, created_at "
            "FROM send_approvals WHERE user_id=? AND status='pending' "
            "ORDER BY created_at DESC LIMIT 10",
            (int(uid),),
        ).fetchall()
    if not rows:
        await _show_private(
            update,
            "Send / Approvals\n\nNo pending approvals right now.\n\n"
            "Approvals are created when you preview a draft with Confirm send; "
            "they expire the moment the exact reply is sent, cancelled, or "
            "rejected.",
        )
        return
    lines = ["Send / Approvals", ""]
    buttons: list[list[Any]] = []
    for r in rows:
        subject = (r["subject"] or "(no subject)")[:60]
        lines.append(f"• {subject}")
        lines.append(f"    to {r['recipient']} via {r['account_id']}")
        buttons.append(
            [
                InlineKeyboardButton(
                    f"Review: {subject[:40]}",
                    callback_data=f"open:{r['account_id']}|{r['mailbox']}|{r['msg_id']}",
                )
            ]
        )
    buttons.append([InlineKeyboardButton("Back to dashboard", callback_data="dash")])
    await _show_private(update, "\n".join(lines), InlineKeyboardMarkup(buttons))


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Authorized users only: report this chat's id so an owner can whitelist it.
    Works in not-yet-configured groups."""
    if not await guard(update, allow_unconfigured_group=True):
        return
    chat = _chat(update)
    if chat is None:
        return
    configured = access.group_allowed(chat.id) or chat.id in load_runtime().home_channels
    if configured:
        status = "Configured as a notification target / whitelisted group."
    else:
        status = (
            "Not whitelisted yet — an owner can add this id in Admin → Groups "
            "(private chat). No .env edit is required."
        )
    await update.effective_message.reply_text(
        f"Chat id: {chat.id}\nType: {chat.type}\n" + status
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, allow_unconfigured_group=True):
        return
    args = getattr(context, "args", None) or []
    if args and not _is_group(update):
        token = group_links.start_token(str(args[0]))
        if token is not None:
            await _consume_deep_link(update, context, token)
            return
    if _is_group(update):
        if not _group_configured(update):
            chat = _chat(update)
            await update.effective_message.reply_text(
                "This group is not configured for Mailops yet.\n"
                f"Chat id: {chat.id}\n"
                "An owner can whitelist this id in Admin → Groups (private chat). "
                "Whitelisting the group does not authorize its members."
            )
            return
        await show_dashboard(update, context)
        return
    accounts = load_accounts()
    heading = (
        f"{accounts[0].display_name} Email Agent ready."
        if len(accounts) == 1
        else "Mail dashboard ready."
    )
    await update.effective_message.reply_text(
        f"{heading}\n"
        "Hourly auto-check caches your folders.\n"
        "Manual refresh = pull IMAP now.\n"
        "Buttons: Open 1…Open 10 · Previous/Next page · Reply.\n"
        "First: Set inbox purposes so ranking knows what matters.",
        reply_markup=_menu(update),
    )
    await show_dashboard(update, context)


async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dashboard - inline dashboard in private chats and configured groups."""
    if not await guard(update):
        return
    await show_dashboard(update, context)


_PRIORITY_FOLDER_INV = {v: k for k, v in PRIORITY_TO_FOLDER.items()}


def _priority_for_ref(account_id: str, mailbox: str, msg_id: str) -> int | None:
    """Best-effort priority for the group-reading card header.

    Physical accounts: the mailbox IS the priority folder. Logical/
    virtual accounts (read-only, test mode): the physical mailbox is
    usually plain INBOX, so fall back to the recorded classification
    (``mailops.db.get_message_locations``) the same way the dashboard
    cache does.
    """
    if mailbox in _PRIORITY_FOLDER_INV:
        return _PRIORITY_FOLDER_INV[mailbox]
    try:
        loc = get_message_locations(account_id).get(str(msg_id))
    except Exception:  # noqa: BLE001
        return None
    if loc and loc.get("priority") is not None:
        try:
            return int(loc["priority"])
        except (TypeError, ValueError):
            return None
    return None


def _resolve_row(
    update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str
) -> nav.Resolution:
    """Resolve a positional Open button to the ref it was rendered for.

    Resolution is anchored to the MESSAGE the button lives on, not to the
    tapper's ``user_data``, so a shared group list behaves the same for
    every owner and a superseded keyboard is reported as stale instead of
    quietly opening whatever now sits at that position.
    """
    q = getattr(update, "callback_query", None)
    message = getattr(q, "message", None) if q is not None else None
    chat = getattr(message, "chat", None) if message is not None else None
    return nav.resolve(
        getattr(chat, "id", None),
        getattr(message, "message_id", None) if message is not None else None,
        payload,
        fallback_refs=list(context.user_data.get("page_refs") or []),
    )


async def _recover_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    ack: Ack,
    res: nav.Resolution,
) -> None:
    """Explain an unresolvable Open tap and leave a WORKING list on screen.

    The tap is never silently dropped: the user is told what happened and
    the list is re-rendered from the current cache, so the next tap uses
    fresh buttons. Nothing is opened, because the only safe answer to "which
    email was position N?" is "re-list and let the user choose again".
    """
    index = 0
    try:
        parsed = nav.parse_position(
            (update.callback_query.data or "").split(":", 2)[-1]
        )
        index = parsed.index if parsed else 0
    except Exception:  # noqa: BLE001 - message text only
        index = 0
    await ack.notice(res.message(index))
    view = _current_view(update, context)
    await show_bucket_page(
        update,
        context,
        priority=int(view.get("priority", 1)),
        account_id=view.get("account_id"),
        page=int(view.get("page", 0)),
        reload=True,
    )


def _browse_refs(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> list[str]:
    """The full result set backing Previous/Next email for this message."""
    snap = _snapshot_for(update)
    if snap is not None and snap.browse_refs:
        return list(snap.browse_refs)
    return list(context.user_data.get("browse_refs") or [])


async def _mint_ref_deep_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ref: str, label: str = "Open email"
) -> None:
    """A specific email's mailbox opted out of group sharing
    (``mailops.policy.group_shareable``). Mint a one-shot private deep
    link bound to THIS exact message (not just a generic picker), so the
    tapper lands straight on it in their DM instead of having to search."""
    q = update.callback_query
    uid = getattr(update.effective_user, "id", None)
    chat = _chat(update)
    chat_id = getattr(chat, "id", None)
    if uid is None or chat_id is None:
        return
    try:
        token = group_links.issue(int(uid), int(chat_id), "open_ref", {"ref": ref})
    except group_links.LinkError as exc:
        log.warning("group ref deep-link mint failed: %s", exc)
        if q is not None:
            await _notice(q, "Could not create a private link. Try again.")
        return
    url = group_links.deep_link(_bot_username(context), token)
    await _edit_or_reply(
        update,
        "This mailbox is marked restricted from group display (sharing "
        "policy), so its content is not shown in this chat's history.\n\n"
        f"{label} — continue privately with the bot. This link is "
        "one-shot for you only and expires in 5 minutes.",
        kb.group_private_link_keyboard(url, label),
    )


async def _handle_stale_open(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    him: Any,
    account_id: str,
    mailbox: str,
    msg_id: str,
    verdict: Any,
) -> None:
    """Recover from "the message is no longer at <folder>" without guessing.

    This is the live ``refused open of memecoinist INBOX/61`` case. The id in
    a persisted ref is a Himalaya envelope id, which equals the IMAP sequence
    number on these servers: it shifts whenever anything earlier in the
    folder is expunged, and a moved message gets a different id in its new
    folder. The identity guard correctly refuses rather than open whatever is
    now at 61 — but it then dead-ended, so the button looked broken.

    What we do instead, in order:

    1. Try to RELOCATE the same message by its stable identity (RFC 5322
       Message-ID, else subject+sender+date) in the folders it could have
       moved to. If found, open it at its real id there — never at the old
       sequence number.
    2. Otherwise tell the user plainly that the email moved or was deleted,
       and re-render the list so the next tap has working buttons.

    The identity check is never bypassed: a relocated message is only opened
    after ``mailops.identity.locate`` matched it, and the reopen runs the
    normal ``open_thread`` path (which verifies again at the new id).
    """
    log.warning(
        "refused open of %s %s/%s: %s", account_id, mailbox, msg_id, verdict.reason
    )
    q = update.callback_query
    identity = recall(account_id, mailbox, msg_id)
    relocated = None
    if identity is not None:
        relocated = await asyncio.to_thread(
            _relocate, him, identity, mailbox
        )
    if relocated is not None:
        new_mailbox, found = relocated
        log.info(
            "relocated %s %s/%s -> %s/%s by stable identity",
            account_id,
            mailbox,
            msg_id,
            new_mailbox,
            found.msg_id,
        )
        remember(found)
        if q is not None:
            await _notice(
                q,
                "That email had moved since the list was built "
                f"({mailbox} → {new_mailbox}). Opening the same email at its "
                "new location.",
            )
        await open_thread(update, context, account_id, new_mailbox, found.msg_id)
        return
    text = (
        "That email is no longer in "
        f"{mailbox} — it was moved or deleted after this list was built, so "
        "nothing was opened (the bot will not open a different email that "
        "took its place).\n\nThe list below has been refreshed."
    )
    if q is not None:
        await _notice(q, text)
        view = _current_view(update, context)
        await show_bucket_page(
            update,
            context,
            priority=int(view.get("priority", 1)),
            account_id=view.get("account_id"),
            page=int(view.get("page", 0)),
            reload=True,
        )
        return
    message = getattr(update, "effective_message", None)
    if message is not None:
        await cbq.safe_reply(message, text)


#: Folders a message may have been moved into between listing and opening.
#: INBOX first (the common "moved back" case), then the priority buckets the
#: organizer uses, then Waiting/Archive.
_RELOCATE_FOLDERS = (
    "INBOX",
    *PRIORITY_FOLDERS,
    "INBOX.Waiting",
    "INBOX.Archive",
    "Archive",
)


def _relocate(him: Any, identity: Any, origin: str) -> tuple[str, Any] | None:
    """Find the SAME message elsewhere, by stable identity. Read-only.

    Returns ``(folder, identity_at_that_folder)``. Only an identity match
    (Message-ID, or subject+sender+date) counts — a position never does — so
    this can never substitute a different email for the one the user tapped.
    """
    if not (identity.message_id or identity.subject or identity.sender):
        return None  # nothing stable to match on: refuse rather than guess
    for folder in _RELOCATE_FOLDERS:
        if folder == origin:
            continue
        try:
            found = locate(him, identity, folder)
        except Exception:  # noqa: BLE001 - unreadable folder: try the next
            continue
        if found is not None and found.msg_id:
            return folder, found
    return None


async def open_thread(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    account_id: str,
    mailbox: str,
    msg_id: str,
    *,
    body_page: int = 0,
) -> None:
    him = Himalaya()
    # A recorded identity must still match what is at this position; otherwise
    # the button/list is stale and could show (and later act on) another email.
    # Both the verification and the body read are IMAP round trips through a
    # Himalaya subprocess, so they run off the event loop: on it they stalled
    # every other user's callbacks (and their acknowledgments) for seconds.
    if recall(account_id, mailbox, msg_id) is not None:
        verdict = await asyncio.to_thread(verify, him, account_id, mailbox, msg_id)
        if not verdict.ok:
            await _handle_stale_open(
                update, context, him, account_id, mailbox, msg_id, verdict
            )
            return
    in_group = _is_group(update)
    if in_group and not group_shareable(account_id):
        # Group-first reading (Phase 1) is content-exposure-gated: a mailbox
        # an admin marked restricted must NEVER have its body rendered into
        # shared group history, no matter which button path got here. The
        # admin gets a one-shot private deep link straight to this email.
        await _mint_ref_deep_link(update, context, f"{account_id}|{mailbox}|{msg_id}")
        return
    # record what the user is about to see
    await asyncio.to_thread(capture, him, account_id, mailbox, msg_id)
    card = await asyncio.to_thread(him.read_card, account_id, msg_id, mailbox)
    ref = f"{account_id}|{mailbox}|{msg_id}"
    refs = _browse_refs(update, context)
    index = refs.index(ref) if ref in refs else None
    total = len(refs) if refs else None
    has_prev = bool(index is not None and index > 0)
    has_next = bool(index is not None and index < len(refs) - 1)
    if in_group:
        priority = _priority_for_ref(account_id, mailbox, msg_id)
        text, body_page, total_body_pages = format_card_page(
            account_id,
            mailbox,
            card,
            index=index,
            total=total,
            priority=priority,
            body_page=body_page,
        )
        markup = kb.thread_keyboard(
            account_id,
            mailbox,
            msg_id,
            has_prev=has_prev,
            has_next=has_next,
            group=True,
            body_page=body_page,
            total_body_pages=total_body_pages,
        )
    else:
        text = format_card(account_id, mailbox, card, index=index, total=total)
        markup = kb.thread_keyboard(
            account_id, mailbox, msg_id, has_prev=has_prev, has_next=has_next
        )
    await _edit_or_reply(update, text, markup)


async def browse_step(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    account_id: str,
    mailbox: str,
    msg_id: str,
    direction: str,
) -> None:
    ref = f"{account_id}|{mailbox}|{msg_id}"
    refs = _browse_refs(update, context)
    if ref not in refs:
        await open_thread(update, context, account_id, mailbox, msg_id)
        return
    i = refs.index(ref)
    if direction == "n" and i + 1 < len(refs):
        a, m, mid = _parse_ref(refs[i + 1])
        await open_thread(update, context, a, m, mid)
        return
    if direction == "p" and i > 0:
        a, m, mid = _parse_ref(refs[i - 1])
        await open_thread(update, context, a, m, mid)
        return
    await open_thread(update, context, account_id, mailbox, msg_id)


async def ai_draft(account_id: str, mailbox: str, msg_id: str) -> str:
    from ..reply_kb import build_draft_system_prompt

    him = Himalaya()
    runtime = load_runtime()
    if not runtime.openai_api_key.strip():
        raise RuntimeError(
            "OPENAI_API_KEY is not set. AI drafts cannot run. "
            "Classification is using keyword rules only."
        )
    if runtime.model == "auto" and "openai.com" in runtime.openai_base_url.lower():
        raise RuntimeError(
            "OPENAI_MODEL is not set. api.openai.com does not accept model=auto. "
            "Add OPENAI_MODEL to .env (for example gpt-4o-mini)."
        )
    card = him.read_card(account_id, msg_id, mailbox)
    settings = load_settings()
    acc = next((a for a in load_accounts() if a.id == account_id), None)
    purposes = settings.get("account_purposes") or {}
    system = build_draft_system_prompt(
        account_id=account_id,
        account_email=(acc.email if acc else ""),
        inbox_context=str(settings.get("inbox_context") or ""),
        account_purpose=str(purposes.get(account_id) or ""),
        display_name=(acc.display_name if acc else account_id),
    )
    prompt = {
        "model": runtime.model,
        "temperature": 0.35,
        "max_tokens": 1500,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    "Draft a reply to this inbound email.\n"
                    f"From: {card.from_line}\n"
                    f"Subject: {card.subject}\n\n"
                    f"{card.body[:3000]}"
                ),
            },
        ],
    }
    if "groq.com" in runtime.openai_base_url.lower() and "gpt-oss" in (runtime.model or ""):
        prompt["reasoning_effort"] = "low"
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{runtime.openai_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {runtime.openai_api_key}",
                "Content-Type": "application/json",
            },
            json=prompt,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


def _list_render(
    update: Update,
    title: str,
    slice_items: list[dict[str, str]],
    page: int,
    total_pages: int,
    total: int,
    token: str = "",
) -> tuple[str, Any]:
    """Shared text+keyboard rendering for EVERY email-list screen.

    Every list renderer must go through this so a new screen cannot ship
    with private-only ``n:<N>`` buttons (or group copy that contradicts its
    own keyboard) ever again - the Oct 25, 2026 live group-reading failure
    was exactly that: ``show_flagged``/``show_list_clean`` had no group
    branch at all, so in a group they emitted private ``n:`` buttons that
    the dispatcher then refused with GROUP_DENIED_ALERT.

    ``token`` binds the Open buttons to this specific render (see
    ``mailops.bot.nav``).
    """
    in_group = _is_group(update)
    uid = getattr(update.effective_user, "id", None)
    can_read = access.can(uid, access.P_READ)
    text = format_page(
        title, slice_items, page, total_pages, total, group=in_group, can_open=can_read
    )
    if not can_read:
        return text, kb.page_keyboard(page, total_pages, 0, group=False)
    if in_group:
        restricted = {
            i
            for i, it in enumerate(slice_items, start=1)
            if not group_shareable(str(it.get("account_id") or ""))
        }
        return text, kb.page_keyboard(
            page,
            total_pages,
            len(slice_items),
            group=True,
            restricted=restricted,
            token=token,
        )
    return text, kb.page_keyboard(
        page, total_pages, len(slice_items), token=token
    )


async def _render_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    title: str,
    items: list[dict[str, str]],
    slice_items: list[dict[str, str]],
    page: int,
    total_pages: int,
    view: dict[str, Any],
) -> None:
    """Render an email list and bind its refs to the message that shows it.

    Ordering is the fix for the live "wrong/empty Open button" reports: the
    refs are stored only AFTER the edit succeeds, and against the message id
    that now displays them. Previously they were written to per-user state
    *before* the render, so two quick taps (``ab:coinography2|p1`` then
    ``ab:memecoinist|p1``, one second apart in the incident log) could leave
    the message showing one mailbox while the stored refs described the
    other, and a second owner tapping the shared group list resolved
    positions against their own unrelated state.

    ``context.user_data`` is still updated for backward compatibility with
    keyboards that were already on screen before this deploy.
    """
    token = nav.new_token()
    text, markup = _list_render(
        update, title, slice_items, page, total_pages, len(items), token=token
    )
    message_id = await _edit_or_reply(update, text, markup)
    page_refs = [it["ref"] for it in slice_items]
    browse_refs = [it["ref"] for it in items]
    chat = _chat(update)
    nav.save(
        getattr(chat, "id", None),
        message_id,
        token=token,
        view=view,
        page_refs=page_refs,
        browse_refs=browse_refs,
        items=items,
        rendered_by=getattr(update.effective_user, "id", None),
    )
    # Legacy fallback for keyboards rendered before message-scoped nav.
    context.user_data["view"] = view
    context.user_data["view_items"] = items
    context.user_data["browse_refs"] = browse_refs
    context.user_data["page_refs"] = page_refs


async def show_flagged(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Reuse page UI over flagged items across folders
    him = Himalaya()
    items: list[dict[str, str]] = []
    for acc in load_accounts():
        for folder in [
            "INBOX.P0-Critical",
            "INBOX.P1-Important",
            "INBOX.P2-Normal",
            "INBOX.Waiting",
        ]:
            for e in him.envelopes(acc.id, folder, page_size=30):
                flags = e.get("flags") or []
                flagged = any(
                    (isinstance(f, dict) and f.get("iana") == "flagged")
                    or (isinstance(f, str) and "flag" in f.lower())
                    for f in flags
                )
                if not flagged:
                    continue
                mid = str(e.get("id") or "")
                items.append(
                    {
                        "ref": f"{acc.id}|{folder}|{mid}",
                        "subject": str(e.get("subject") or "(no subject)"),
                        "from": _from_label(e),
                        "account_id": acc.id,
                        "mailbox": folder,
                        "msg_id": mid,
                    }
                )
    slice_items, page, total_pages = page_slice(items, 0)
    await _render_list(
        update,
        context,
        title="Needs reply",
        items=items,
        slice_items=slice_items,
        page=page,
        total_pages=total_pages,
        view={"priority": 1, "account_id": None, "page": 0, "flagged": True},
    )


async def show_list_clean(
    update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: str, mailbox: str
) -> None:
    # Map folder to priority browse when possible
    inv = {v: k for k, v in PRIORITY_TO_FOLDER.items()}
    if mailbox in inv:
        await show_bucket_page(
            update, context, priority=inv[mailbox], account_id=account_id, page=0
        )
        return
    him = Himalaya()
    envs = him.envelopes(account_id, mailbox, page_size=50)
    items = []
    for e in envs:
        mid = str(e.get("id") or "")
        items.append(
            {
                "ref": f"{account_id}|{mailbox}|{mid}",
                "subject": str(e.get("subject") or "(no subject)"),
                "from": _from_label(e),
                "account_id": account_id,
                "mailbox": mailbox,
                "msg_id": mid,
            }
        )
    slice_items, page, total_pages = page_slice(items, 0)
    await _render_list(
        update,
        context,
        title=f"{account_id} · {mailbox}",
        items=items,
        slice_items=slice_items,
        page=page,
        total_pages=total_pages,
        view={
            "priority": 2,
            "account_id": account_id,
            "page": 0,
            "mailbox": mailbox,
        },
    )


async def do_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    q = query.lower()
    him = Himalaya()
    items: list[dict[str, str]] = []
    for acc in load_accounts():
        for folder in [
            "INBOX.P0-Critical",
            "INBOX.P1-Important",
            "INBOX.P2-Normal",
            "INBOX",
            "INBOX.Waiting",
        ]:
            for e in him.envelopes(acc.id, folder, page_size=25):
                blob = f"{e.get('subject')} {_from_label(e)}".lower()
                if q not in blob:
                    continue
                mid = str(e.get("id") or "")
                items.append(
                    {
                        "ref": f"{acc.id}|{folder}|{mid}",
                        "subject": str(e.get("subject") or "(no subject)"),
                        "from": _from_label(e),
                        "account_id": acc.id,
                        "mailbox": folder,
                        "msg_id": mid,
                    }
                )
                if len(items) >= 100:
                    break
            if len(items) >= 100:
                break
        if len(items) >= 100:
            break
    slice_items, page, total_pages = page_slice(items, 0)
    await _render_list(
        update,
        context,
        title=f"Search: {query}",
        items=items,
        slice_items=slice_items,
        page=page,
        total_pages=total_pages,
        view={"priority": 1, "account_id": None, "page": 0, "search": query},
    )


async def _handle_group_waiting_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Privacy-mode-safe consumption of a group ForceReply prompt armed by
    ``gr:writeme:`` (typed reply) or ``gr:search`` (inbox search) in
    ``_on_callback``.

    Telegram privacy mode means a bare group message may never reach the
    bot at all, but a reply to the bot's OWN message always does
    (documented, mode-independent Bot API behaviour - the same invariant
    ``mailops.bot.mailboxes._handle_group_password`` already relies on).
    We therefore only ever accept a message that:
      * is a reply to the EXACT prompt message id we sent,
      * arrives in the EXACT chat the prompt was armed in,
      * comes from the EXACT user it was armed for (WAITING_TEXT is keyed
        by user id already, so this is implicit, but the chat/message
        checks below are the binding that stops a stray reply - e.g. to
        some other old message, or copy-pasted into a different group -
        from ever being treated as live input).
    Returns True once the message has been handled (answered one way or
    another) so the caller does not fall through to anything else.
    """
    user = update.effective_user
    uid = getattr(user, "id", None)
    if uid is None:
        return False
    pending = _get_waiting(uid)
    if not pending or pending.get("mode") not in ("write", "search") or not pending.get("group"):
        return False
    msg = update.effective_message
    chat = _chat(update)
    chat_id = getattr(chat, "id", None)
    pending_chat = pending.get("chat_id")
    if pending_chat is None or chat_id is None or int(pending_chat) != int(chat_id):
        return False
    reply_to = getattr(msg, "reply_to_message", None)
    reply_id = getattr(reply_to, "message_id", None) if reply_to is not None else None
    prompt_id = pending.get("prompt_message_id")
    if reply_to is None or prompt_id is None or reply_id != prompt_id:
        # Not a reply to our prompt: leave the slot armed (TTL still
        # governs it) and let other group handling ignore this message.
        return False
    mode = pending["mode"]
    WAITING_TEXT.pop(uid, None)
    if not access.is_authorized_user(uid) or not access.group_allowed(chat_id):
        # Revoked between arming and reply: fail closed, nothing is used.
        return True
    text = (msg.text or "").strip()
    if mode == "search":
        if not access.can(uid, access.P_READ):
            await msg.reply_text(_role_denied_text(access.P_READ))
            return True
        if not text:
            await msg.reply_text("Empty search ignored.")
            return True
        if text.lower() == "cancel":
            await msg.reply_text("Search cancelled.")
            return True
        await do_search(update, context, text)
        return True
    # mode == "write"
    if not access.can(uid, access.P_DRAFT):
        await msg.reply_text(_role_denied_text(access.P_DRAFT))
        return True
    account_id, mailbox, msg_id = pending["account_id"], pending["mailbox"], pending["msg_id"]
    if not text:
        await msg.reply_text("Empty reply ignored. Nothing was saved.")
        return True
    if text.lower() == "cancel":
        await msg.reply_text("Cancelled. Nothing was saved.")
        return True
    save_draft(uid, account_id, mailbox, msg_id, text)
    await msg.reply_text(
        f"Draft saved:\n\n{text[:3000]}",
        reply_markup=kb.draft_keyboard(
            account_id,
            mailbox,
            msg_id,
            can_send=access.can(uid, access.P_SEND),
            group=True,
        ),
    )
    return True


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_group(update):
        # Groups are navigation + inline buttons only... EXCEPT when the
        # mailbox-onboarding wizard is actively waiting on a non-secret field
        # from an admin whose slot is bound to this exact group chat. The
        # mailbox handler checks AWAITING["chat_id"] against the real chat
        # id and refuses mismatches, so free text in an unrelated group is
        # still ignored. The group-first "Type my reply" prompt (gr:writeme:)
        # is the same shape, bound via WAITING_TEXT + a ForceReply message
        # id instead of mailboxes.AWAITING.
        if await mailboxes.handle_text(update, context):
            return
        if await _handle_group_waiting_text(update, context):
            return
        return
    if await mailboxes.handle_text(update, context):
        return
    if await admin.handle_text(update, context):
        return
    if not await guard(update):
        return
    text = (update.effective_message.text or "").strip()
    uid = update.effective_user.id

    if text == "Admin":
        await admin.cmd_admin(update, context)
        return
    if text == "Dashboard":
        await show_dashboard(update, context, force=False)
        return
    if text in ("Manual refresh", "Refresh"):
        await show_dashboard(update, context, force=True)
        return
    if text == "Critical":
        await show_bucket_page(update, context, priority=0, page=0)
        return
    if text == "Important":
        await show_bucket_page(update, context, priority=1, page=0)
        return
    if text == "Needs reply":
        await show_flagged(update, context)
        return
    if text == "Inboxes":
        accounts = [(a.id, a.email) for a in load_accounts()]
        if len(accounts) <= 1:
            # Stale persistent keyboard from the multi-inbox layout: there is
            # nothing to pick. Refresh the menu and show the one dashboard.
            only = accounts[0][0] if accounts else "none"
            name = next((a.display_name for a in load_accounts() if a.id == only), only)
            await update.effective_message.reply_text(
                f"{name} is the only inbox.", reply_markup=_menu(update)
            )
            await show_dashboard(update, context, force=False)
            return
        await update.effective_message.reply_text(
            "Accounts", reply_markup=kb.accounts_keyboard(accounts)
        )
        return
    if text == "Settings":
        if not access.can(uid, access.P_SETTINGS):
            await update.effective_message.reply_text(_role_denied_text(access.P_SETTINGS))
            return
        settings = load_settings()
        ctx = (settings.get("inbox_context") or "")[:400]
        await update.effective_message.reply_text(
            "Settings\n\nCurrent context:\n" + (ctx or "(empty — tap Context to set)"),
            reply_markup=kb.settings_keyboard(settings),
        )
        return
    if text == "Search":
        _set_waiting(uid, {"mode": "search"})
        await update.effective_message.reply_text(
            "Type a search query (from/subject). Send Cancel to abort."
        )
        return
    if text.lower() == "cancel":
        WAITING_TEXT.pop(uid, None)
        await update.effective_message.reply_text("Cancelled.", reply_markup=_menu(update))
        return

    pending = _get_waiting(uid)
    if pending and pending.get("mode") == "search":
        WAITING_TEXT.pop(uid, None)
        await do_search(update, context, text)
        return
    if pending and pending.get("mode") == "write":
        if not access.can(uid, access.P_DRAFT):
            WAITING_TEXT.pop(uid, None)
            await update.effective_message.reply_text(_role_denied_text(access.P_DRAFT))
            return
        account_id, mailbox, msg_id = pending["account_id"], pending["mailbox"], pending["msg_id"]
        WAITING_TEXT.pop(uid, None)
        save_draft(uid, account_id, mailbox, msg_id, text)
        await update.effective_message.reply_text(
            f"Draft saved:\n\n{text[:3000]}",
            reply_markup=kb.draft_keyboard(
                account_id,
                mailbox,
                msg_id,
                can_send=access.can(uid, access.P_SEND),
            ),
        )
        return
    if pending and pending.get("mode") in ("vip", "context", "kb_note", "purpose", "rule"):
        if not access.can(uid, access.P_SETTINGS):
            WAITING_TEXT.pop(uid, None)
            await update.effective_message.reply_text(_role_denied_text(access.P_SETTINGS))
            return
    if pending and pending.get("mode") == "vip":
        WAITING_TEXT.pop(uid, None)
        settings = load_settings()
        vips = list(settings.get("vip_senders") or [])
        email = text.strip().lower()
        if email and email not in vips:
            vips.append(email)
            settings["vip_senders"] = vips
            save_settings(settings)
        await update.effective_message.reply_text(
            f"VIP list ({len(vips)}):\n" + "\n".join(vips[:30] or ["(none)"]),
            reply_markup=kb.settings_keyboard(settings),
        )
        return
    if pending and pending.get("mode") == "context":
        WAITING_TEXT.pop(uid, None)
        settings = load_settings()
        settings["inbox_context"] = text.strip()
        settings["context_confirmed"] = False
        save_settings(settings)
        await update.effective_message.reply_text(
            "Overall ranking rules saved.\n"
            "Still set each inbox purpose, then tap Mark purposes complete.\n\n"
            + text.strip()[:1200],
            reply_markup=kb.purposes_keyboard(),
        )
        return
    if pending and pending.get("mode") == "kb_note":
        from ..reply_kb import append_account_kb_note, format_kb_card

        account_id = pending["account_id"]
        WAITING_TEXT.pop(uid, None)
        append_account_kb_note(account_id, text.strip())
        await update.effective_message.reply_text(
            f"Added to {account_id} reply KB.\n\n" + format_kb_card(account_id)[:3500],
            reply_markup=kb.kb_keyboard(),
        )
        return
    if pending and pending.get("mode") == "purpose":
        account_id = pending.get("account_id") or ""
        WAITING_TEXT.pop(uid, None)
        settings = load_settings()
        purposes = dict(settings.get("account_purposes") or {})
        purposes[account_id] = text.strip()
        settings["account_purposes"] = purposes
        settings["context_confirmed"] = False
        save_settings(settings)
        await update.effective_message.reply_text(
            f"Purpose saved for {account_id}.\n\n{text.strip()[:1200]}\n\n"
            "Set the other inboxes, then tap Mark purposes complete.",
            reply_markup=kb.purposes_keyboard(),
        )
        return
    if pending and pending.get("mode") == "rule":
        from ..priority_rules import parse_rule_message, save_bucket_rules, format_rules_card

        bucket = pending.get("bucket") or "p1"
        WAITING_TEXT.pop(uid, None)
        patch = parse_rule_message(text)
        if not patch:
            await update.effective_message.reply_text(
                "Empty rules — try again.", reply_markup=kb.rules_keyboard()
            )
            return
        rules = save_bucket_rules(bucket, patch)
        settings = load_settings()
        settings["rules_confirmed"] = False
        save_settings(settings)
        kws = rules[bucket].get("keywords") or []
        await update.effective_message.reply_text(
            f"Saved {bucket.upper()} · {len(kws)} keywords.\n\n"
            + format_rules_card(rules)[:3000],
            reply_markup=kb.rules_keyboard(),
        )
        return

    await update.effective_message.reply_text(
        "Use the menu buttons below.", reply_markup=_menu(update)
    )


def _stale_account_text() -> str:
    names = ", ".join(a.display_name or a.id for a in load_accounts()) or "none"
    return (
        "That button belongs to an inbox Mailops no longer manages. "
        f"Active inboxes: {names} - tap Dashboard for a fresh view."
    )


def _account_from(account_id: str) -> str:
    acc = next((a for a in load_accounts() if a.id == account_id), None)
    return (acc.email if acc else "").strip()


async def _show_confirmation(q: Any, text: str, markup) -> None:
    """Show the full reply. Split only when Telegram's edit limit requires it."""
    if len(text) <= 3900:
        await q.edit_message_text(text, reply_markup=markup)
        return
    await q.edit_message_text(
        "Final confirmation — nothing has been sent.\n"
        "The complete message follows in the next message."
    )
    await q.message.reply_text(text, reply_markup=markup)


def _release_unsent(token: str, note: str) -> None:
    """The approval was claimed but SMTP was not called. Allow a later review."""
    finish_approval(token, "rejected", None, note)


async def _preview_send(
    q: Any, him: Any, uid: int, account_id: str, mailbox: str, msg_id: str, *, group: bool = False
) -> None:
    """Show the exact reply. This step never submits SMTP."""
    if not access.can(uid, access.P_SEND):
        await _notice(q, _role_denied_text(access.P_SEND))
        return
    draft = get_draft(uid)
    if not draft or draft.get("msg_id") != msg_id:
        await _notice(q, "No draft — write or AI draft first")
        return
    if draft.get("account_id") != account_id:
        await _notice(q, "This draft belongs to a different account. Nothing was sent.")
        return
    if draft.get("mailbox") and draft.get("mailbox") != mailbox:
        await _notice(q, "This draft was written for a different folder. Nothing was sent.")
        return
    if not can_send(account_id):
        await _notice(
            q,
            f"Sending is disabled for {account_id} ({describe(account_id)}). Nothing was sent.",
        )
        return
    if await _refuse_stale(q, him, account_id, mailbox, msg_id, "send"):
        clear_draft(uid)
        return
    from ..safe_mode import test_mode_enabled

    if test_mode_enabled():
        await q.edit_message_text(
            "TEST MODE is ON — send blocked, nothing was delivered.\n"
            "Disable with test_mode=false in data/settings.json "
            "or MAILOPS_TEST_MODE=0."
        )
        return
    from_addr = _account_from(account_id)
    plan, why = plan_reply(him, account_id, mailbox, msg_id, draft.get("body") or "", from_addr)
    if plan is None:
        log.info("send preview refused account=%s msg=%s", account_id, msg_id)
        await _notice(q, f"Nothing was sent. {why[:1].upper() + why[1:] if why else 'The reply could not be verified.'}")
        return
    token, blocked = create_pending(uid, plan)
    if not token:
        await _notice(
            q,
            "This exact reply already has a send in progress, was already accepted, "
            "or had an uncertain result. It was not sent again.",
        )
        return
    await _show_confirmation(q, format_confirmation(plan), kb.final_send_keyboard(token, group=group))


async def _commit_send(q: Any, him: Any, uid: int, token: str) -> None:
    """Claim one approval and submit that exact reply at most once."""
    from ..safe_mode import test_mode_enabled

    if not access.can(uid, access.P_SEND):
        await _notice(q, _role_denied_text(access.P_SEND))
        return

    if test_mode_enabled():
        await q.edit_message_text(
            "TEST MODE is ON — send blocked, nothing was delivered.\n"
            "Disable with test_mode=false in data/settings.json "
            "or MAILOPS_TEST_MODE=0."
        )
        return
    claimed = claim_approval(token, uid)
    if not claimed.ok or claimed.row is None:
        if claimed.reason == "blocked":
            text = (
                "This reply already has a send in progress, was already accepted, "
                "or had an uncertain result. Nothing was sent."
            )
        else:
            text = "This approval was already used or is not valid. Nothing was sent."
        await _notice(q, text)
        return
    row = claimed.row
    account_id = str(row["account_id"])
    mailbox = str(row["mailbox"])
    msg_id = str(row["msg_id"])
    log.info(
        "send claimed user=%s account=%s mailbox=%s msg=%s approval=%s",
        uid,
        account_id,
        mailbox,
        msg_id,
        token[:8],
    )

    def _stop(note: str) -> None:
        _release_unsent(token, note)
        log.info("send precheck refused account=%s msg=%s note=%s", account_id, msg_id, note)

    if account_id not in active_account_ids() or not can_send(account_id):
        _stop("policy")
        await _notice(q, f"Sending is disabled for {account_id}. Nothing was sent.")
        return
    draft = get_draft(uid)
    body = (draft or {}).get("body") or ""
    if (
        not draft
        or draft.get("account_id") != account_id
        or str(draft.get("msg_id")) != msg_id
        or (draft.get("mailbox") and draft.get("mailbox") != mailbox)
        or body_sha256(body) != row["body_sha256"]
    ):
        _stop("draft_mismatch")
        await _notice(q, "The reviewed draft no longer matches this approval. Nothing was sent.")
        return
    plan, _why = plan_reply(him, account_id, mailbox, msg_id, body, _account_from(account_id))
    if plan is None or not plans_match(plan, row):
        _stop("identity_mismatch")
        await _notice(
            q,
            "The original message, recipient, or threading no longer matches the reviewed reply. "
            "Nothing was sent.",
        )
        return

    await q.edit_message_text("Sending…")
    try:
        result = him.reply_send(
            account_id, msg_id, plan.body, mailbox, plan.from_addr, approved=True
        )
    except Exception as exc:  # noqa: BLE001 - outcome is unknown, never retry
        finish_approval(token, "ambiguous", None, "exception")
        log.warning(
            "smtp result account=%s msg=%s code=%s status=%s error=%s",
            account_id,
            msg_id,
            None,
            "ambiguous",
            type(exc).__name__,
        )
        await q.edit_message_text(
            "The send result is unclear, so it was not retried. "
            "Check the mailbox before writing another reply. Nothing else was sent."
        )
        return

    status = classify_smtp(result.code, result.stdout, result.stderr)
    finish_approval(token, status, result.code, status)
    log.info(
        "smtp result account=%s msg=%s code=%s status=%s",
        account_id,
        msg_id,
        result.code,
        status,
    )
    if status == "accepted":
        filed = can_organize(account_id)
        if filed:
            him.move(account_id, msg_id, "INBOX.Waiting", mailbox)
        clear_draft(uid)
        await q.edit_message_text(
            f"Sent via {plan.from_addr}.\n"
            + (
                "Copy saved to Sent · original filed to Waiting."
                if filed
                else "Copy saved to Sent · original left in place (no mailbox changes)."
            )
        )
        return
    if status == "rejected":
        hint = redact_text(f"{result.stderr or ''}\n{result.stdout or ''}")
        if plan.body:
            hint = hint.replace(plan.body, "")
        hint = " ".join(hint.split())[:240]
        await q.edit_message_text(
            "Send failed before the server accepted it. The draft was kept and nothing was retried.\n"
            + (f"\n{hint}" if hint else "")
        )
        return
    await q.edit_message_text(
        "The send result is unclear, so it was not retried. "
        "This exact reply cannot be sent again. Check the mailbox before writing another reply."
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for every inline-button tap.

    Owns the acknowledgment lifecycle so no inner layer has to guess whether
    the query was already answered:

    * one ``Ack`` per query, registered in ``_ACKS`` for refusal helpers;
    * the query is answered as early as the authorization decision allows,
      never after IMAP/rendering work (that is what let queries expire
      before they were ever acknowledged);
    * exactly one answer per query, so Telegram never reports the bogus
      "Query is too old" caused by a second answer;
    * one structured log line per tap with route, ack latency and outcome.
    """
    q = update.callback_query
    if q is None:
        return
    ack = Ack(q)
    qid = ack.query_id
    _ACKS[qid] = ack
    # Guarantee an answer even if this handler runs long (IMAP, cache
    # rebuild). A fast refusal still answers with its own popup inside the
    # watchdog window; a slow handler gets acknowledged on its behalf so the
    # query can never expire unanswered.
    ack.arm()
    outcome = "handled"
    category: str | None = None
    try:
        await _on_callback(update, context, ack)
    except InactiveAccountError as exc:
        outcome = "refused_inactive_account"
        log.info("refused callback for inactive account %r", str(exc))
        await ack.notice(_stale_account_text())
    except Exception as exc:  # noqa: BLE001 - log, tell the user, keep polling
        outcome = "error"
        category = cbq.classify(exc)
        # The tap must never look like it silently did nothing.
        log.exception("callback route=%s failed", ack.route)
        try:
            await ack.notice(
                "That action could not be completed. Nothing was changed. "
                "Open a fresh dashboard and try again."
            )
        except Exception:  # noqa: BLE001
            pass
    finally:
        ack.disarm()
        # A handler that returned without refusing anything still owes the
        # user an answer (e.g. a pure render path that finished fast).
        if not ack.answered:
            await ack.ok()
        _ACKS.pop(qid, None)
        ack.done(outcome, category=category)


async def _on_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ack: Ack | None = None
) -> None:
    q = update.callback_query
    data = q.data or ""
    if ack is None:  # direct call (tests): still answer exactly once
        ack = _ack_for(q)
        _ACKS.setdefault(ack.query_id, ack)
    if data.startswith(admin.PREFIX):
        await admin.handle_callback(update, context)
        return
    if data.startswith(mailboxes.PREFIX):
        if _is_group(update):
            # Only a narrow allow-list of mb:* actions is safe in a group.
            # Credentials still never cross the group boundary — mb:arm:
            # hands off to the admin's private chat.
            if not group_mailbox_callback_allowed(data):
                await ack.alert(GROUP_DENIED_ALERT)
                return
        await mailboxes.handle_callback(update, context)
        return
    if not await guard(update):
        return
    if _is_group(update) and not group_callback_allowed(data):
        # Bodies, replies, drafts, sends, moves and settings are private-only.
        await ack.alert(GROUP_DENIED_ALERT)
        return
    uid = update.effective_user.id
    need = admin.required_perm(data)
    if not access.can(uid, need):
        await ack.alert(_role_denied_text(need))
        return
    # Authorized. Everything past this point may touch IMAP (a Himalaya
    # subprocess) or rebuild the cache, which can take tens of seconds;
    # the armed watchdog answers the query long before Telegram would
    # invalidate it, while a fast in-handler refusal below can still show a
    # real popup.
    him = Himalaya()

    if data in ("dash", "nav:home"):
        await show_dashboard(update, context, force=False)
        return
    if data == "dash:force":
        await show_dashboard(update, context, force=True)
        return
    if data == "page:force":
        view = _current_view(update, context)
        await show_bucket_page(
            update,
            context,
            priority=int(view.get("priority", 1)),
            account_id=view.get("account_id"),
            page=int(view.get("page", 0)),
            force=True,
        )
        return
    if data == "noop":
        return
    if data == "g:all":
        await show_all_emails(update, context, page=0)
        return
    if data == "g:all:n":
        view = _current_view(update, context)
        if view.get("all"):
            await show_all_emails(
                update, context, page=int(view.get("page", 0)) + 1
            )
        else:
            await show_all_emails(update, context, page=0)
        return
    if data == "g:all:p":
        view = _current_view(update, context)
        page = max(0, int(view.get("page", 0)) - 1) if view.get("all") else 0
        await show_all_emails(update, context, page=page)
        return
    if data.startswith("gdl:"):
        await _mint_deep_link(update, context, data[len("gdl:") :])
        return
    if data == "nav:settings":
        settings = load_settings()
        await q.edit_message_text(
            "Settings", reply_markup=kb.settings_keyboard(settings)
        )
        return
    if data == "set:rules":
        from ..priority_rules import ensure_priority_rules, format_rules_card

        rules = ensure_priority_rules()
        await q.edit_message_text(
            format_rules_card(rules)[:3900], reply_markup=kb.rules_keyboard()
        )
        return
    if data.startswith("set:rule:"):
        from ..priority_rules import ensure_priority_rules, format_edit_prompt

        bucket = data.split(":")[-1]
        if bucket not in ("p0", "p1", "p2", "p3"):
            await ack.notice("Unknown bucket")
            return
        _set_waiting(uid, {"mode": "rule", "bucket": bucket})
        rules = ensure_priority_rules()
        await q.edit_message_text(format_edit_prompt(bucket, rules)[:3900])
        return
    if data == "set:rules_done":
        from ..priority_rules import ensure_priority_rules

        settings = load_settings()
        rules = ensure_priority_rules(settings)
        empty = [k for k in ("p0", "p1", "p3") if not (rules[k].get("keywords") or [])]
        if empty:
            await ack.notice("Still empty: " + ", ".join(empty))
            return
        settings["rules_confirmed"] = True
        save_settings(settings)
        await q.edit_message_text(
            "Priority rules confirmed. Keyword matcher is active "
            "(Critical → Noise → Important → Normal, then AI if no hit).",
            reply_markup=kb.dashboard_keyboard(collect_counts(force=False)),
        )
        return
    if data == "set:purposes":
        settings = load_settings()
        purposes = settings.get("account_purposes") or {}
        lines = ["Set what each inbox is for.", ""]
        for acc in load_accounts():
            cur = (purposes.get(acc.id) or "").strip()
            lines.append(f"{acc.id} <{acc.email}>")
            lines.append(f"  {(cur[:160] + '…') if len(cur) > 160 else (cur or '(not set)')}")
            lines.append("")
        overall = (settings.get("inbox_context") or "").strip()
        lines.append("Overall notes:")
        lines.append(overall[:400] if overall else "(not set)")
        lines.append("")
        lines.append("Tap an inbox → send one message describing its purpose.")
        lines.append("Also set Priority rules (P0–P3 keywords) — that drives sorting.")
        lines.append("Reply knowledge base drives AI draft links/rates.")
        await q.edit_message_text("\n".join(lines)[:3900], reply_markup=kb.purposes_keyboard())
        return
    if data == "set:kb":
        await q.edit_message_text(
            "Reply knowledge base\n\n"
            "AI drafts use this KB for essential links, packages/prices, tone, and signature.\n"
            "Files live in mailops/data/kb/ — Telegram notes append; full edits can be in those files.\n"
            "Hermes Phase 2 can deepen learning on top of this KB.",
            reply_markup=kb.kb_keyboard(),
        )
        return
    if data.startswith("set:kb:view:"):
        from ..reply_kb import format_kb_card

        account_id = _require_active(data.split(":")[-1])
        await q.edit_message_text(
            format_kb_card(account_id)[:3900],
            reply_markup=kb.kb_keyboard(),
        )
        return
    if data.startswith("set:kb:note:"):
        account_id = _require_active(data.split(":")[-1])
        _set_waiting(uid, {"mode": "kb_note", "account_id": account_id})
        await q.edit_message_text(
            f"Send ONE message to append to the {account_id} reply KB.\n"
            "Examples:\n"
            "- Listing fee starts at $X\n"
            "- Always CC advertising@…\n"
            "- New media kit URL: https://…\n\n"
            "Send Cancel to abort."
        )
        return
    if data.startswith("set:purpose:"):
        account_id = _require_active(data.split(":", 2)[2])
        acc = next((a for a in load_accounts() if a.id == account_id), None)
        _set_waiting(uid, {"mode": "purpose", "account_id": account_id})
        settings = load_settings()
        cur = ((settings.get("account_purposes") or {}).get(account_id) or "").strip()
        await q.edit_message_text(
            f"Purpose for {account_id}"
            + (f" <{acc.email}>" if acc else "")
            +             "\n\nSend ONE message describing ONLY this inbox: what the business is, what "
            "makes a message critical / important / normal / noise here, and what to ignore. "
            "Do not paste another brand's details.\n\n"
            "Current:\n"
            + (cur or "(empty)")
        )
        return
    if data == "set:purposes_done":
        settings = load_settings()
        purposes = settings.get("account_purposes") or {}
        missing = [
            a.id for a in load_accounts() if not (purposes.get(a.id) or "").strip()
        ]
        if missing or not (settings.get("inbox_context") or "").strip():
            await ack.notice(
                "Still missing: " + (", ".join(missing) or "overall rules"),
            )
            return
        settings["context_confirmed"] = True
        save_settings(settings)
        await q.edit_message_text(
            "Purposes confirmed. New mail will be ranked with your rules.",
            reply_markup=kb.dashboard_keyboard(collect_counts(force=False)),
        )
        return
    if data.startswith("b:p"):
        priority = int(data.split("p", 1)[1])
        await show_bucket_page(update, context, priority=priority, page=0)
        return
    # -----------------------------------------------------------------
    # GROUP-READ namespace (``gr:*``) - Oct 2026 group-first reading fix.
    #
    # This is the complete email-reading (and reply/draft/send) journey
    # inside an authorized Telegram GROUP: Dashboard -> list -> gr:n:<N>
    # (open) -> gr:b:<ref>|p/n (Previous/Next email) -> gr:bp:<ref>|<page>
    # (paginate a long body) -> gr:reply:<ref> -> gr:aidraft:/gr:writeme:
    # -> gr:send:<ref> -> gr:go:/gr:nogo:<token> -> gr:back (to the list)
    # / dash (Home). Every sub-action re-checks the SAME permission its
    # private twin requires (admin.required_perm's "gr:" branch) and the
    # SAME per-account group_shareable() gate that ``open_thread`` applies,
    # so a restricted mailbox can never leak its body into group history
    # via any of these taps either.
    # -----------------------------------------------------------------
    if data == "gr:search":
        if not access.can(uid, access.P_READ):
            await ack.notice(_role_denied_text(access.P_READ))
            return
        from telegram import ForceReply

        sent = await q.message.reply_text(
            "Send your search query (from/subject) as a REPLY to this "
            "message (tap Reply, then type). Group privacy mode means a "
            "bare message may never reach the bot otherwise. Send Cancel "
            "to abort.",
            reply_markup=ForceReply(selective=True),
        )
        chat = _chat(update)
        _set_waiting(
            uid,
            {
                "mode": "search",
                "group": True,
                "chat_id": getattr(chat, "id", None),
                "prompt_message_id": getattr(sent, "message_id", None),
            },
        )
        return
    if data == "gr:back":
        view = _current_view(update, context)
        if view.get("flagged"):
            await show_flagged(update, context)
            return
        if view.get("all"):
            await show_all_emails(update, context, page=int(view.get("page", 0)))
            return
        await show_bucket_page(
            update,
            context,
            priority=int(view.get("priority", 1)),
            account_id=view.get("account_id"),
            page=int(view.get("page", 0)),
            reload=True,
        )
        return
    if data.startswith("gr:restricted:"):
        # Tapped a locked row in a group list (mailbox opted out of group
        # sharing). Resolve against the list THIS message shows, then mint
        # the deep link straight to that exact email.
        res = _resolve_row(update, context, data[len("gr:restricted:"):])
        if not res.ok:
            await ack.notice(res.message(0))
            return
        await _mint_ref_deep_link(update, context, res.ref)
        return
    if data.startswith("gr:n:"):
        res = _resolve_row(update, context, data[len("gr:n:"):])
        if not res.ok:
            await _recover_list(update, context, ack, res)
            return
        account_id, mailbox, msg_id = _parse_ref(res.ref)
        await open_thread(update, context, account_id, mailbox, msg_id)
        return
    if data.startswith("gr:o:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 2)[2])
        await open_thread(update, context, account_id, mailbox, msg_id)
        return
    if data.startswith("gr:b:"):
        payload, direction = data.split(":", 2)[2].rsplit("|", 1)
        account_id, mailbox, msg_id = _parse_ref(payload)
        await browse_step(update, context, account_id, mailbox, msg_id, direction)
        return
    if data.startswith("gr:bp:"):
        payload, page_s = data.split(":", 2)[2].rsplit("|", 1)
        account_id, mailbox, msg_id = _parse_ref(payload)
        await open_thread(
            update, context, account_id, mailbox, msg_id, body_page=int(page_s)
        )
        return
    if data.startswith("gr:reply:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 2)[2])
        if not group_shareable(account_id):
            await _mint_ref_deep_link(update, context, f"{account_id}|{mailbox}|{msg_id}", "Reply")
            return
        card = him.read_card(account_id, msg_id, mailbox)
        await q.edit_message_text(
            f"Reply to:\n{card.subject}\nFrom: {card.from_line}",
            reply_markup=kb.reply_keyboard(account_id, mailbox, msg_id, group=True),
        )
        return
    if data.startswith("gr:writeme:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 2)[2])
        if not group_shareable(account_id):
            await _mint_ref_deep_link(update, context, f"{account_id}|{mailbox}|{msg_id}", "Reply")
            return
        # Telegram privacy mode: a plain group message may never reach the
        # bot, so the typed-reply prompt is sent as a ForceReply and bound
        # to (user, chat, account/mailbox/msg) - the SAME chat-pinning
        # invariant mailops.bot.mailboxes already applies to its own
        # group-typed-input slots (_set_awaiting's chat_id check). Only a
        # reply to THIS exact prompt message, from THIS exact user, in
        # THIS exact chat, is ever consumed as the draft text.
        from telegram import ForceReply

        sent = await q.message.reply_text(
            "Send your reply text as a REPLY to this message "
            "(tap Reply, then type). Group privacy mode means a bare "
            "message may never reach the bot otherwise.",
            reply_markup=ForceReply(selective=True),
        )
        chat = _chat(update)
        _set_waiting(
            uid,
            {
                "mode": "write",
                "account_id": account_id,
                "mailbox": mailbox,
                "msg_id": msg_id,
                "group": True,
                "chat_id": getattr(chat, "id", None),
                "prompt_message_id": getattr(sent, "message_id", None),
            },
        )
        return
    if data.startswith("gr:aidraft:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 2)[2])
        if not group_shareable(account_id):
            await _mint_ref_deep_link(update, context, f"{account_id}|{mailbox}|{msg_id}", "AI draft")
            return
        await q.edit_message_text("Drafting…")
        try:
            draft = await ai_draft(account_id, mailbox, msg_id)
        except Exception as exc:  # noqa: BLE001
            await q.edit_message_text(f"Draft failed: {exc}")
            return
        save_draft(uid, account_id, mailbox, msg_id, draft)
        await q.edit_message_text(
            f"AI draft:\n\n{draft[:3500]}",
            reply_markup=kb.draft_keyboard(
                account_id,
                mailbox,
                msg_id,
                can_send=access.can(uid, access.P_SEND),
                group=True,
            ),
        )
        return
    if data.startswith("gr:send:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 2)[2])
        if not group_shareable(account_id):
            await _mint_ref_deep_link(update, context, f"{account_id}|{mailbox}|{msg_id}", "Send")
            return
        await _preview_send(q, him, uid, account_id, mailbox, msg_id, group=True)
        return
    if data.startswith("gr:nogo:"):
        token = data.split(":", 2)[2]
        if cancel_approval(token, uid):
            await q.edit_message_text("Cancelled. Nothing was sent.")
        else:
            await _notice(q, "This approval can no longer be cancelled. Nothing was sent.")
        return
    if data.startswith("gr:go:"):
        await _commit_send(q, him, uid, data.split(":", 2)[2])
        return
    # -----------------------------------------------------------------
    # 15-minute digest callbacks (see mailops/digest.py). These power the
    # four buttons on the consolidated summary (Open Inbox / View
    # Critical / Select Mailbox / Refresh Summary). All work in groups
    # (added to GROUP_CALLBACK_PREFIXES above); sensitive drilldowns
    # (opening a specific email) still require either a private chat or
    # the existing deep-link hand-off used by other group buttons.
    # -----------------------------------------------------------------
    if data == "dg:crit":
        # 'View Critical' - jump straight to the all-accounts P0 bucket.
        await show_bucket_page(update, context, priority=0, page=0)
        return
    if data == "dg:imp":
        # 'View Important' - all-accounts P1 bucket.
        await show_bucket_page(update, context, priority=1, page=0)
        return
    if data == "dg:norm":
        # 'View Normal' - all-accounts P2 bucket.
        await show_bucket_page(update, context, priority=2, page=0)
        return
    if data == "dg:noise":
        # 'View Noise' - all-accounts P3 bucket.
        await show_bucket_page(update, context, priority=3, page=0)
        return
    if data.startswith("dg:all"):
        # 'View All Mailboxes' - paginated full-fleet breakdown (every
        # monitored mailbox, Critical/Important first, including those
        # with zero new mail this window). Browsing never sends more
        # than ONE edited/sent message, regardless of fleet size.
        from ..digest import all_mailboxes_keyboard

        parts = data.split(":")
        page = 0
        if len(parts) >= 3 and parts[2].isdigit():
            page = int(parts[2])
        text, markup = all_mailboxes_keyboard(page)
        await _edit_or_reply(update, text, markup)
        return
    if data == "dg:refresh":
        # 'Refresh Summary' - re-build and re-emit the digest to the home
        # targets. force=True bypasses the interval gate but the dedupe is
        # unchanged: any email already covered by the last successful
        # digest is NOT re-counted (its row is already delivered).
        from ..digest import build_and_send_digest

        try:
            diag = await build_and_send_digest(
                context.bot, force=True, reason="manual"
            )
        except Exception:  # noqa: BLE001
            log.exception("manual digest refresh failed")
            await ack.notice("Refresh failed; next tick will retry")
            return
        if diag.get("sent"):
            await ack.notice(
                f"Digest resent: {diag.get('mailboxes', 0)} inbox(es)",
            )
        elif diag.get("skipped") == "empty":
            await ack.notice("No new actionable emails right now.")
        else:
            await ack.notice(
                f"Digest skipped: {diag.get('skipped') or 'unknown'}",
            )
        return
    if data.startswith("dg:pick"):
        # 'Select Mailbox' - paginated list of mailboxes with pending counts.
        from ..digest import select_mailbox_keyboard

        parts = data.split(":")
        page = 0
        if len(parts) >= 3 and parts[2].isdigit():
            page = int(parts[2])
        text, markup = select_mailbox_keyboard(page)
        await _edit_or_reply(update, text, markup)
        return
    if data.startswith("dg:open:"):
        # 'Open Inbox' - drill into one mailbox's priority view.
        aid = data.split(":", 2)[2]
        if aid == "_top":
            # Fallback when the digest had no top mailbox (empty window
            # at the time the keyboard was built): send to Dashboard.
            await show_dashboard(update, context)
            return
        try:
            _require_active(aid)
        except InactiveAccountError:
            await _notice(q, _stale_account_text())
            return
        # Prefer P0 if the account has P0 pending, otherwise P1. The
        # 'Select Mailbox' screen (``dg:pick:*``) now lists every
        # registered mailbox - not only those with pending digest rows -
        # so a tap here may land on an inbox with an empty window. That
        # is intentional: the admin asked for THIS inbox, so we open
        # the Important bucket (empty is also a valid answer and the
        # group-safe page_keyboard already renders the "0 of 0" state).
        from ..digest import collect_window

        window = collect_window()
        p0 = next(
            (b.p0 for b in window.buckets if b.account_id == aid),
            0,
        )
        target_priority = 0 if p0 > 0 else 1
        await show_bucket_page(
            update,
            context,
            priority=target_priority,
            account_id=aid,
            page=0,
        )
        return
    # -----------------------------------------------------------------
    # Operational health alert callbacks (October 2026 incident fix).
    # Completely separate from the digest callbacks above: this view shows
    # which MAILBOXES ARE CURRENTLY UNHEALTHY (sync errors), never new-mail
    # counts. "Retry Health Check" is rate-limited per account inside
    # mailops.health.run_manual_health_check so mashing it cannot turn into
    # a bad-password hammer against a real mail provider.
    # -----------------------------------------------------------------
    if data == "health:view":
        from ..health import format_unhealthy_dashboard, unhealthy_dashboard_keyboard

        await _edit_or_reply(
            update,
            format_unhealthy_dashboard(),
            unhealthy_dashboard_keyboard(),
        )
        return
    if data.startswith("health:retry:"):
        from ..health import run_manual_health_check, format_unhealthy_dashboard, unhealthy_dashboard_keyboard

        target = data.split(":", 2)[2]
        account_ids = None if target == "_all" else [target]
        results = await asyncio.to_thread(run_manual_health_check, account_ids)
        ok = sum(1 for r in results if r.ok)
        limited = sum(1 for r in results if r.rate_limited)
        failed = len(results) - ok - limited
        parts = [f"{ok} healthy"]
        if failed:
            parts.append(f"{failed} still failing")
        if limited:
            parts.append(f"{limited} rate-limited (retry later)")
        await ack.notice("Retry Health Check: " + ", ".join(parts))
        await _edit_or_reply(
            update,
            format_unhealthy_dashboard(),
            unhealthy_dashboard_keyboard(),
        )
        return
    if data.startswith("ab:"):
        rest = data.split(":", 1)[1]
        account_id, pcode = rest.split("|", 1)
        _require_active(account_id)
        priority = int(pcode[1]) if pcode.startswith("p") else int(pcode)
        await show_bucket_page(
            update, context, priority=priority, account_id=account_id, page=0
        )
        return
    if data == "pg:n":
        view = _current_view(update, context)
        await show_bucket_page(
            update,
            context,
            priority=int(view.get("priority", 1)),
            account_id=view.get("account_id"),
            page=int(view.get("page", 0)) + 1,
            reload=False,
        )
        return
    if data == "pg:p":
        view = _current_view(update, context)
        await show_bucket_page(
            update,
            context,
            priority=int(view.get("priority", 1)),
            account_id=view.get("account_id"),
            page=int(view.get("page", 0)) - 1,
            reload=False,
        )
        return
    if data.startswith("pj:"):
        page = int(data.split(":", 1)[1])
        view = _current_view(update, context)
        await show_bucket_page(
            update,
            context,
            priority=int(view.get("priority", 1)),
            account_id=view.get("account_id"),
            page=page,
            reload=False,
        )
        return
    if data.startswith("n:"):
        res = _resolve_row(update, context, data[len("n:"):])
        if not res.ok:
            await _recover_list(update, context, ack, res)
            return
        account_id, mailbox, msg_id = _parse_ref(res.ref)
        await open_thread(update, context, account_id, mailbox, msg_id)
        return
    if data == "backpage":
        view = _current_view(update, context)
        if view.get("flagged"):
            await show_flagged(update, context)
            return
        await show_bucket_page(
            update,
            context,
            priority=int(view.get("priority", 1)),
            account_id=view.get("account_id"),
            page=int(view.get("page", 0)),
            reload=True,
        )
        return
    if data == "nav:flagged":
        await show_flagged(update, context)
        return
    if data == "nav:accounts":
        accounts = [(a.id, a.email) for a in load_accounts()]
        await q.edit_message_text("Accounts", reply_markup=kb.accounts_keyboard(accounts))
        return
    if data.startswith("acct:"):
        aid = _require_active(data.split(":", 1)[1])
        await q.edit_message_text(
            f"Account {aid}", reply_markup=kb.account_folders_keyboard(aid)
        )
        return
    if data.startswith("list:"):
        _, ref = data.split(":", 1)
        account_id, mailbox = ref.split("|", 1)
        _require_active(account_id)
        await show_list_clean(update, context, account_id, mailbox)
        return
    if data.startswith("browse:"):
        payload, direction = data.split(":", 1)[1].rsplit("|", 1)
        account_id, mailbox, msg_id = _parse_ref(payload)
        await browse_step(update, context, account_id, mailbox, msg_id, direction)
        return
    if data.startswith("open:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        await open_thread(update, context, account_id, mailbox, msg_id)
        return
    if data.startswith("prio:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        await q.edit_message_reply_markup(
            reply_markup=kb.priority_keyboard(account_id, mailbox, msg_id)
        )
        return
    if data.startswith("setprio:"):
        payload, prio_s = data.split(":", 1)[1].rsplit("|", 1)
        account_id, mailbox, msg_id = _parse_ref(payload)
        if await _deny_read_only(q, account_id, "changing priority (moving mail)"):
            return
        if await _refuse_stale(q, him, account_id, mailbox, msg_id, "setprio"):
            return
        dest = PRIORITY_TO_FOLDER[int(prio_s)]
        him.move(account_id, msg_id, dest, mailbox)
        await open_thread(
            update, context, account_id, dest, _moved_id(him, account_id, mailbox, msg_id, dest)
        )
        return
    if data.startswith("tags:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        await q.edit_message_reply_markup(
            reply_markup=kb.tags_keyboard(account_id, mailbox, msg_id)
        )
        return
    if data.startswith("settag:"):
        payload, tag = data.split(":", 1)[1].rsplit("|", 1)
        account_id, mailbox, msg_id = _parse_ref(payload)
        if await _deny_read_only(q, account_id, "tagging (copying mail)"):
            return
        if await _refuse_stale(q, him, account_id, mailbox, msg_id, "settag"):
            return
        folder = TAG_TO_FOLDER[tag]
        r = him.copy(account_id, msg_id, folder, mailbox)
        await ack.notice(f"Tagged {tag}" if r.ok else "Tag failed")
        await open_thread(update, context, account_id, mailbox, msg_id)
        return
    if data.startswith("done:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        if await _deny_read_only(q, account_id, "Mark done (moving mail)"):
            return
        if await _refuse_stale(q, him, account_id, mailbox, msg_id, "done"):
            return
        him.move(account_id, msg_id, "INBOX.Handled", mailbox)
        # The destination folder assigned a new id: unflag THAT message only.
        handled_id = _moved_id(him, account_id, mailbox, msg_id, "INBOX.Handled")
        if verify(him, account_id, "INBOX.Handled", handled_id).ok:
            him.flag_remove(account_id, handled_id, "flagged", "INBOX.Handled")
        else:
            log.warning("done: skipped unflag, cannot confirm %s/INBOX.Handled/%s", account_id, handled_id)
        ref = f"{account_id}|{mailbox}|{msg_id}"
        refs = [x for x in (context.user_data.get("browse_refs") or []) if x != ref]
        context.user_data["browse_refs"] = refs
        items = [it for it in (context.user_data.get("view_items") or []) if it.get("ref") != ref]
        context.user_data["view_items"] = items
        if refs:
            # open next remaining, or return to page
            a, m, mid = _parse_ref(refs[0])
            view = _current_view(update, context)
            await show_bucket_page(
                update,
                context,
                priority=int(view.get("priority", 1)),
                account_id=view.get("account_id"),
                page=int(view.get("page", 0)),
                reload=False,
            )
        else:
            await show_dashboard(update, context)
        return
    if data.startswith("mute:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        # Muting derives the sender from this message: never from a stale ref.
        if await _refuse_stale(q, him, account_id, mailbox, msg_id, "mute"):
            return
        card = him.read_card(account_id, msg_id, mailbox)
        email = (card.from_email or "").lower()
        settings = load_settings()
        muted = list(settings.get("muted_senders") or [])
        if email:
            if email not in muted:
                muted.append(email)
                settings["muted_senders"] = muted
                save_settings(settings)
            if can_organize(account_id):
                him.move(account_id, msg_id, "INBOX.P3-Noise", mailbox)
                await q.edit_message_text(f"Muted {email} → Noise")
            else:
                # Read-only account: the mute rule is local; the mailbox is untouched.
                await q.edit_message_text(
                    f"Muted {email}: future mail is ranked Noise. "
                    f"{account_id} is read-only, so this message was not moved."
                )
        else:
            await ack.notice("Could not detect sender email")
        return
    if data.startswith("snooze:"):
        rest = data.split(":", 1)[1]
        try:
            _account_id, _mailbox, _msg_id, _how = rest.rsplit("|", 3)
        except ValueError:
            await ack.notice("Bad snooze payload")
            return
        await q.edit_message_text(
            "Snooze noted. Mailops will not send this alert again. "
            "Nothing in the mailbox was changed."
        )
        return
    if data.startswith("reply:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        card = him.read_card(account_id, msg_id, mailbox)
        await q.edit_message_text(
            f"Reply to:\n{card.subject}\nFrom: {card.from_line}",
            reply_markup=kb.reply_keyboard(account_id, mailbox, msg_id),
        )
        return
    if data.startswith("writeme:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        _set_waiting(
            uid,
            {
                "mode": "write",
                "account_id": account_id,
                "mailbox": mailbox,
                "msg_id": msg_id,
            },
        )
        await q.edit_message_text("Send your reply text as the next message.")
        return
    if data.startswith("aidraft:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        await q.edit_message_text("Drafting…")
        try:
            draft = await ai_draft(account_id, mailbox, msg_id)
        except Exception as exc:  # noqa: BLE001
            await q.edit_message_text(f"Draft failed: {exc}")
            return
        save_draft(uid, account_id, mailbox, msg_id, draft)
        await q.edit_message_text(
            f"AI draft:\n\n{draft[:3500]}",
            reply_markup=kb.draft_keyboard(
                account_id,
                mailbox,
                msg_id,
                can_send=access.can(uid, access.P_SEND),
            ),
        )
        return
    if data.startswith("nogo:"):
        token = data.split(":", 1)[1]
        if cancel_approval(token, uid):
            await q.edit_message_text("Cancelled. Nothing was sent.")
        else:
            await _notice(q, "This approval can no longer be cancelled. Nothing was sent.")
        return
    if data.startswith("go:"):
        await _commit_send(q, him, uid, data.split(":", 1)[1])
        return
    if data.startswith("send:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        await _preview_send(q, him, uid, account_id, mailbox, msg_id)
        return
    if data == "set:alertcycle":
        settings = load_settings()
        cur = int(settings.get("min_alert_priority", 1))
        settings["min_alert_priority"] = (cur + 1) % 4
        save_settings(settings)
        await q.edit_message_reply_markup(reply_markup=kb.settings_keyboard(settings))
        return
    # -------------------------------------------------------------------
    # Notification policy screen (digest redesign point 8).
    # -------------------------------------------------------------------
    if data == "set:notify":
        settings = load_settings()
        from ..digest import last_run_summary
        lr = last_run_summary()
        last_ts = lr.get("last_run_at") or 0
        if last_ts:
            age = int(time.time() - float(last_ts))
            last_line = f"Last digest: {age}s ago, covered {lr.get('last_count', 0)} actionable."
        else:
            last_line = "Last digest: never (first tick pending)."
        await q.edit_message_text(
            "Notification Policy\n\n"
            "Mailops sends ONE consolidated Telegram digest covering ALL "
            "mailboxes every 15 minutes, grouped by inbox. Individual email "
            "cards (Open / Reply / Priority / Done / Snooze) are shown only "
            "when an admin opens an inbox from the digest.\n\n"
            f"{last_line}",
            reply_markup=kb.notifications_keyboard(settings),
        )
        return
    if data == "set:notify:toggle":
        settings = load_settings()
        settings["digest_enabled"] = not bool(settings.get("digest_enabled", True))
        save_settings(settings)
        await q.edit_message_reply_markup(
            reply_markup=kb.notifications_keyboard(settings)
        )
        return
    if data == "set:notify:interval":
        # cycle 5m -> 15m -> 30m -> 60m -> 5m
        settings = load_settings()
        cur = int(settings.get("digest_interval_seconds", 900))
        nxt = {300: 900, 900: 1800, 1800: 3600, 3600: 300}.get(cur, 900)
        settings["digest_interval_seconds"] = nxt
        save_settings(settings)
        await q.edit_message_reply_markup(
            reply_markup=kb.notifications_keyboard(settings)
        )
        return
    if data == "set:notify:p0imm":
        settings = load_settings()
        settings["digest_p0_immediate"] = not bool(
            settings.get("digest_p0_immediate", False)
        )
        save_settings(settings)
        await q.edit_message_reply_markup(
            reply_markup=kb.notifications_keyboard(settings)
        )
        return
    if data == "set:notify:p2":
        settings = load_settings()
        settings["digest_include_p2"] = not bool(
            settings.get("digest_include_p2", False)
        )
        save_settings(settings)
        await q.edit_message_reply_markup(
            reply_markup=kb.notifications_keyboard(settings)
        )
        return
    if data == "set:notify:silent":
        settings = load_settings()
        settings["digest_initial_sync_silent"] = not bool(
            settings.get("digest_initial_sync_silent", True)
        )
        save_settings(settings)
        await q.edit_message_reply_markup(
            reply_markup=kb.notifications_keyboard(settings)
        )
        return
    if data.startswith("set:notify:mb:toggle:"):
        rest = data[len("set:notify:mb:toggle:") :]
        try:
            aid, page_s = rest.rsplit(":", 1)
            page = int(page_s)
        except ValueError:
            await ack.notice("Bad toggle")
            return
        settings = load_settings()
        disabled = [str(x).strip() for x in (settings.get("digest_mailboxes_disabled") or [])]
        if aid in disabled:
            disabled.remove(aid)
        else:
            disabled.append(aid)
        settings["digest_mailboxes_disabled"] = disabled
        save_settings(settings)
        text, markup = kb.notifications_mailboxes_keyboard(page, settings)
        await _edit_or_reply(update, text, markup)
        return
    if data.startswith("set:notify:mb:"):
        page_s = data.split(":", 3)[3]
        try:
            page = int(page_s)
        except ValueError:
            page = 0
        settings = load_settings()
        text, markup = kb.notifications_mailboxes_keyboard(page, settings)
        await _edit_or_reply(update, text, markup)
        return
    if data == "set:noisecycle":
        settings = load_settings()
        settings["noise_push"] = "daily" if settings.get("noise_push") == "off" else "off"
        save_settings(settings)
        await q.edit_message_reply_markup(reply_markup=kb.settings_keyboard(settings))
        return
    if data == "set:synccycle":
        settings = load_settings()
        # cycle 15m → 30m → 60m → 15m
        cur = int(settings.get("sync_interval_seconds", 3600))
        nxt = {900: 1800, 1800: 3600, 3600: 900}.get(cur, 3600)
        settings["sync_interval_seconds"] = nxt
        save_settings(settings)
        await q.edit_message_reply_markup(reply_markup=kb.settings_keyboard(settings))
        return
    if data == "set:vip":
        _set_waiting(uid, {"mode": "vip"})
        settings = load_settings()
        vips = settings.get("vip_senders") or []
        await q.edit_message_text(
            "Send a VIP email to add.\nCurrent:\n" + ("\n".join(vips) if vips else "(none)")
        )
        return
    if data == "set:context":
        _set_waiting(uid, {"mode": "context"})
        settings = load_settings()
        cur = settings.get("inbox_context") or ""
        await q.edit_message_text(
            "Send ONE message describing what mail matters to you.\n\nCurrent:\n"
            + (cur[:1500] if cur else "(empty)")
        )
        return


#: How many Telegram updates may be handled at the same time.
#:
#: python-telegram-bot defaults this to 1, i.e. STRICTLY SEQUENTIAL update
#: processing. That default is the amplifier behind the October 2026 incident:
#: one handler that waited on IMAP (a Himalaya subprocess, up to its 60s
#: timeout) prevented PTB from even STARTING any other update, so every other
#: owner's tap sat in the queue until Telegram had invalidated its callback
#: query. The live log shows exactly that shape — a 22-second ``gr:n:1`` open
#: followed immediately by an "expired" answer for the tap queued behind it.
#:
#: Making mailbox work non-blocking (``asyncio.to_thread``) is necessary but
#: not sufficient on its own: with a limit of 1, awaiting still serialises
#: updates. A bounded pool keeps taps independent while capping how many
#: Himalaya subprocesses the bot can spawn at once across a 39-mailbox fleet.
#:
#: Note on shared state: concurrent handlers are safe here because navigation
#: state is keyed per (chat, message) in ``mailops.bot.nav`` and written only
#: after a render succeeds, and callback acknowledgment is claimed
#: exactly-once under a lock in ``mailops.bot.callbacks``.
CONCURRENT_UPDATES = 24


def build_application(post_init=None, post_shutdown=None) -> Application:
    runtime = load_runtime()
    builder = (
        Application.builder()
        .token(runtime.telegram_token)
        .concurrent_updates(CONCURRENT_UPDATES)
    )
    if post_init is not None:
        builder = builder.post_init(post_init)
    if post_shutdown is not None:
        builder = builder.post_shutdown(post_shutdown)
    app = builder.build()
    # group=-1 runs before the real handlers and never stops propagation.
    app.add_handler(TypeHandler(Update, log_update), group=-1)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    # Every slash command is wrapped in traced_command so handler entry,
    # exit, duration and exceptions are all in the log. Inline taps already
    # got this from mailops.bot.callbacks.Ack; commands did not, which is why
    # a wedged /dashboard logged only its arrival.
    for command, handler in (
        ("start", cmd_start),
        ("dashboard", cmd_dashboard),
        ("chatid", cmd_chatid),
        ("admin", admin.cmd_admin),
        ("myid", admin.cmd_myid),
        ("adduser", admin.cmd_adduser),
        ("removeuser", admin.cmd_removeuser),
        ("allowgroup", admin.cmd_allowgroup),
        ("removegroup", admin.cmd_removegroup),
        ("qadd", mailboxes.handle_qadd_command),
    ):
        app.add_handler(
            CommandHandler(command, traced_command(f"/{command}")(handler))
        )
    app.add_error_handler(on_error)
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    return app


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route an uploaded document to the bulk-import handler.

    This handler must authorize BEFORE the file is downloaded: an
    unauthenticated upload (stranger in a group, or a non-admin) must
    not consume our Telegram download quota or let anyone probe the
    bot's reachability through side-channel timing.
    """
    chat = _chat(update)
    user = update.effective_user
    chat_id = getattr(chat, "id", None)
    chat_type = getattr(chat, "type", None) or "private"
    user_id = getattr(user, "id", None)
    if user_id is None:
        return
    # Reuse the same authorization gate as everything else: strangers get
    # silent-ignored in groups and a 'Unauthorized.' in a DM.
    if not access.authorized(user_id, chat_id, chat_type):
        if chat_type == "private":
            # Private DM from an unknown user: fall back to the standard
            # unauthorized reply so the sender knows why nothing happens.
            await guard(update)
        return
    try:
        from . import bulk_mailboxes

        await bulk_mailboxes.handle_document(update, context)
    except Exception:  # noqa: BLE001
        log.exception("bulk document handler crashed")
