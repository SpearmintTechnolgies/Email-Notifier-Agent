from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from .config import active_account_ids, load_accounts, load_settings
from .dashboard import collect_counts, format_dashboard
from .db import already_notified, mark_notified, pop_batch
from .bot import keyboards as kb

log = logging.getLogger("mailops.notify")


def _prio_label(p: int) -> str:
    return {0: "P0 Critical", 1: "P1 Important", 2: "P2 Normal", 3: "P3 Noise"}.get(p, f"P{p}")


def mail_keyboard(account_id: str, mailbox: str, msg_id: str) -> InlineKeyboardMarkup:
    cb = f"{account_id}|{mailbox}|{msg_id}"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Open", callback_data=f"open:{cb}"),
                InlineKeyboardButton("Reply", callback_data=f"reply:{cb}"),
            ],
            [
                InlineKeyboardButton("Priority", callback_data=f"prio:{cb}"),
                InlineKeyboardButton("Done", callback_data=f"done:{cb}"),
            ],
            [
                InlineKeyboardButton("Snooze 1h", callback_data=f"snooze:{cb}|1h"),
            ],
        ]
    )


def _bot_username(bot: Any) -> str | None:
    # Bot.username raises RuntimeError before the bot is initialized.
    try:
        name = getattr(bot, "username", None)
    except Exception:  # noqa: BLE001
        return None
    return name if isinstance(name, str) and name else None


async def send_to_home(
    bot: Bot,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    group_markup: InlineKeyboardMarkup | None = None,
    group_text: str | None = None,
) -> None:
    """Deliver a notification to every configured home target.

    TELEGRAM_HOME_CHANNEL may list several ids (``user_id,-group_id``).
    Private chats get ``reply_markup`` (full actions); group chats never get
    the private action buttons - they get ``group_markup`` (or a small
    read-only "Dashboard / Open privately" keyboard).

    One unreachable target (e.g. bot not yet added to the group) must not
    suppress delivery to the others. Delivery counts as successful if at least
    one target accepted the message; only if EVERY target fails is the
    exception re-raised so the caller keeps the alert queued for retry.
    """
    from .access import effective_notify_targets

    targets = effective_notify_targets()
    if not targets:
        log.warning("No TELEGRAM_HOME_CHANNEL / allowed user for notifications")
        return
    delivered = 0
    last_exc: Exception | None = None
    for chat_id in targets:
        if chat_id < 0:
            markup = group_markup or kb.group_alert_keyboard(_bot_username(bot))
            body = group_text if group_text is not None else text
        else:
            markup = reply_markup
            body = text
        try:
            await bot.send_message(chat_id=chat_id, text=body, reply_markup=markup)
            delivered += 1
        except Exception as exc:  # noqa: BLE001 - isolate per-target failures
            last_exc = exc
            new_id = getattr(exc, "new_chat_id", None)
            hint = f" (group migrated; new id {new_id})" if new_id else ""
            log.warning(
                "notification to chat %s failed: %s: %s%s",
                chat_id,
                type(exc).__name__,
                exc,
                hint,
            )
    if delivered == 0 and last_exc is not None:
        raise last_exc


def _account_label(account_id: Any, email: Any = "") -> str:
    """Display name for an active account ("Altcoin Informer"), else the id."""
    for acc in load_accounts():
        if acc.id == account_id:
            return acc.display_name or acc.id
    return str(account_id)


async def send_or_edit_home(
    bot: Bot,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    group_markup: InlineKeyboardMarkup | None = None,
    group_text: str | None = None,
    state_key: str,
    edit_only: bool = False,
) -> dict[str, Any]:
    """Deliver the ONE consolidated control card, editing a previous card in
    place when possible instead of sending a brand-new Telegram message.

    Why: Telegram does not push a notification to the recipient when a
    message is *edited* (``editMessageText``), only when one is newly
    *sent*. Maintaining one living card per chat and editing it every
    15-minute tick therefore satisfies "at most one scheduled notification
    per window" even on windows where the card's numbers changed, while
    never leaving a stale card on screen.

    Per-target message ids are persisted in ``digest_state`` under
    ``state_key`` (JSON ``{"<chat_id>": <message_id>}``) so this survives a
    process restart exactly like the rest of the digest state.

    ``edit_only=True`` is for a cosmetic refresh of an otherwise-empty
    window: never falls back to sending a brand-new message (that would be
    an unnecessary new notification for "nothing happened"), and never
    raises - a failed cosmetic refresh just leaves the old card as-is.
    ``edit_only=False`` (default) guarantees delivery the same way
    ``send_to_home`` does: if editing fails (message too old, deleted, or
    there was nothing to edit yet) it falls back to sending a new message,
    and only raises if EVERY target failed both ways.
    """
    import json as _json

    from .access import effective_notify_targets
    from .db import digest_state_get, digest_state_set

    targets = effective_notify_targets()
    if not targets:
        log.warning("No TELEGRAM_HOME_CHANNEL / allowed user for notifications")
        return {"delivered": 0, "edited": 0, "sent": 0}
    try:
        refs: dict[str, int] = {
            str(k): int(v) for k, v in (_json.loads(digest_state_get(state_key, "{}") or "{}") or {}).items()
        }
    except Exception:  # noqa: BLE001
        refs = {}
    new_refs = dict(refs)
    delivered = edited = sent = 0
    last_exc: Exception | None = None
    for chat_id in targets:
        if chat_id < 0:
            markup = group_markup or kb.group_alert_keyboard(_bot_username(bot))
            body = group_text if group_text is not None else text
        else:
            markup = reply_markup
            body = text
        key = str(chat_id)
        prior_id = refs.get(key)
        if prior_id:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=prior_id, text=body, reply_markup=markup
                )
                delivered += 1
                edited += 1
                new_refs[key] = prior_id
                continue
            except Exception as exc:  # noqa: BLE001
                if "not modified" in str(exc).lower():
                    delivered += 1
                    edited += 1
                    new_refs[key] = prior_id
                    continue
                log.info(
                    "digest card edit failed for chat %s (%s); %s",
                    chat_id,
                    exc,
                    "will not create a new message (cosmetic refresh)"
                    if edit_only
                    else "falling back to a new send",
                )
                if edit_only:
                    continue
        if edit_only:
            # Nothing to refresh for this target and we must not create a
            # brand-new message just to say "no change".
            continue
        try:
            msg = await bot.send_message(chat_id=chat_id, text=body, reply_markup=markup)
            delivered += 1
            sent += 1
            new_id = getattr(msg, "message_id", None)
            if new_id is not None:
                new_refs[key] = int(new_id)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning(
                "control card delivery to chat %s failed: %s: %s",
                chat_id,
                type(exc).__name__,
                exc,
            )
    if new_refs != refs:
        try:
            digest_state_set(state_key, _json.dumps(new_refs))
        except Exception:  # noqa: BLE001
            log.debug("failed to persist control-card message refs", exc_info=True)
    if delivered == 0 and not edit_only and last_exc is not None:
        raise last_exc
    return {"delivered": delivered, "edited": edited, "sent": sent}


async def handle_event(bot: Bot, event: dict[str, Any]) -> None:
    """Deliver one event to the authorized home targets.

    After the October 2026 digest redesign this function is NO LONGER called
    automatically by the sync loop for P1 Important mail. The sync worker
    now routes P0/P1 into ``digest_pending`` (see mailops.digest) and
    delivers ONE consolidated summary every 15 minutes. ``handle_event`` is
    kept for:

    * ``sync_error`` / ``sent`` / ``send_failed`` operational pings;
    * the opt-in ``digest_p0_immediate`` fast path (true P0 only), which
      the sync worker invokes directly;
    * any manual admin trigger that still wants a per-email card.

    This is important: individual Open/Reply/Priority/Done/Snooze actions
    live on the per-email card produced here, so the card itself must
    stay reachable - it is just not fired automatically for every P1
    anymore (that was the Alien FI 12-13 message flood).
    """
    etype = event.get("type")
    key = event.get("key") or ""
    account_id = event.get("account_id")
    if account_id is not None and account_id not in active_account_ids():
        # Event for an account Mailops no longer manages (e.g. an old queued
        # Gmail alert). Never alert about it; the DB row is left untouched.
        log.warning("dropped %s event for inactive account %r", etype, account_id)
        return
    if key and already_notified(key) and etype != "new_mail":
        # new_mail may mark before send
        pass
    if etype == "sync_error":
        # LEGACY/DEPRECATED (October 2026 incident fix): this per-call path
        # used to fire one Telegram message EVERY poll cycle for EVERY
        # failing account with a generic "Check credentials / IMAP" line —
        # that is exactly the 24-message flood this redesign removes.
        # ``sync_worker.sync_once`` no longer calls notify() with this event
        # type; failures now go through ``mailops.health`` which persists
        # an incident and sends ONE consolidated, deduplicated alert per
        # state change (new/changed/recovered) via
        # ``health.build_and_send_health_alert``. This branch is kept only
        # so an old queued ``pending_notifications`` row (pre-redesign,
        # already migrated by ``retry_pending_notifications``) can never
        # resurrect the flood if it is somehow replayed.
        if key and already_notified(key):
            return
        if key:
            mark_notified(key)
        await send_to_home(
            bot,
            f"Sync issue on `{event.get('account_id')}` ({event.get('email')}). Check credentials / IMAP.",
        )
        return
    if etype == "sent":
        await send_to_home(
            bot,
            f"Sent via `{event.get('account_id')}`\nTo thread `{event.get('msg_id')}`",
        )
        return
    if etype == "send_failed":
        await send_to_home(
            bot,
            f"Send FAILED on `{event.get('account_id')}`\n{event.get('error')}",
        )
        return
    if etype == "new_mail":
        if key and already_notified(key):
            # Delivered-and-confirmed earlier — never alert twice.
            return
        prio = int(event.get("priority", 2))
        text = (
            f"{_prio_label(prio)}\n"
            f"{event.get('subject') or '(no subject)'}\n"
            f"From: {event.get('sender')}\n"
            f"Inbox: {_account_label(event.get('account_id'))} ({event.get('email')})\n"
            f"Why: {event.get('reason') or '—'}"
        )
        await send_to_home(
            bot,
            text,
            mail_keyboard(event["account_id"], event["mailbox"], event["msg_id"]),
        )
        # Mark notified ONLY after Telegram accepted the message, so a failed
        # send leaves the alert eligible for retry from the pending queue.
        if key:
            mark_notified(key)


async def flush_p2_batch(bot: Bot) -> None:
    settings = load_settings()
    seconds = float(settings.get("p2_batch_seconds", 900))
    rows = pop_batch(seconds, active_account_ids())  # retired accounts' rows stay put
    if not rows:
        return
    # group
    lines = [f"New P2 mail ({len(rows)})"]
    buttons = []
    single = len(load_accounts()) == 1
    for r in rows[:20]:
        mark_notified(r["key"])
        lines.append(f"• {r['subject']}" if single else f"• [{r['account_id']}] {r['subject']}")
        cb = f"{r['account_id']}|{r['mailbox']}|{r['msg_id']}"
        buttons.append([InlineKeyboardButton(r["subject"][:40] or r["msg_id"], callback_data=f"open:{cb}")])
    await send_to_home(bot, "\n".join(lines), InlineKeyboardMarkup(buttons) if buttons else None)


async def send_dashboard_digest(bot: Bot, *, note: str = "") -> None:
    counts = collect_counts()
    # Private targets get the full dashboard; group targets get the full
    # control panel (every button; sensitive ones mint a private deep link
    # when tapped, after a server-side permission check).
    text = format_dashboard(counts)
    group_text = format_dashboard(counts, group=True)
    if note:
        text = note.rstrip() + "\n\n" + text
        group_text = note.rstrip() + "\n\n" + group_text
    # Admins are shown the admin / audit rows too; non-admins who later tap
    # a hidden control through a stale message still get server-side denial.
    group_markup = kb.group_dashboard_keyboard(
        counts,
        is_admin=True,
        can_settings=True,
        can_draft=True,
        can_send=True,
    )
    await send_to_home(
        bot,
        text,
        kb.dashboard_keyboard(counts),
        group_markup=group_markup,
        group_text=group_text,
    )


def make_notifier(bot: Bot) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Build a notifier that PROPAGATES delivery failures.

    Callers (sync worker / pending-notification retry loop) must see the
    exception so the alert stays queued for retry instead of being lost.
    """
    async def _notify(event: dict[str, Any]) -> None:
        await handle_event(bot, event)

    return _notify
