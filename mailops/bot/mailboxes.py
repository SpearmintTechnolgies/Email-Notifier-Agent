"""Mailboxes section of the Telegram group control panel.

A single private-chat screen tree where an authorized admin can:

* see every email account Mailops knows about and its state
* run a non-sending IMAP+SMTP health check on an active account
* enable or disable an existing mailbox (credentials are preserved)
* edit the per-account purpose (keyed strictly by account id so brand
  context never bleeds between inboxes)
* start onboarding a NEW mailbox (Hostinger / Custom IMAP+SMTP)
* see recent onboarding errors (last mailbox_audit rows)

Everything mutating goes through ``mailops.mailbox_admin`` or
``mailops.webmail_onboarding``; this module is UI-only.

Group-first onboarding
----------------------
The non-secret wizard (provider pick → account id → email → hosts/ports/
users → review → arm) may run inside a *whitelisted* group where the
tapper has ``P_ADMIN``. Nothing credential-bearing crosses the group
boundary: the arm step always HANDS OFF to the admin's private DM, where
the existing credential-capture paths (CLI or DM-mode) take over. Error
text shown in the group is scrubbed through ``redact_text`` so stray
server responses that could echo a username, host or token never land in
group history.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .. import access, mailbox_admin
from .. import group_credentials as gc
from .. import onboarding_session as os_mod
from .. import server_resolution
from ..redact import redact_text
from .callbacks import Ack
from . import mb_quick
from . import presets_ui

log = logging.getLogger("mailops.bot.mailboxes")

PREFIX = "mb:"
AWAITING_INPUT_PREFIX = "mb_input:"

# Order matters: _collect_steps is the single source of truth for the add-
# mailbox wizard, so changes made here flow into the UI, validation and tests.
STEPS: list[tuple[str, str, str]] = [
    ("account_id", "Account id", "A short lowercase id, e.g. coinography"),
    ("email", "Email address", "The business email (e.g. info@coinography.com)"),
    ("display_name", "Display name", "Shown in the bot UI; press skip to use the email"),
    ("imap_host", "IMAP host", "Hostname from your email provider, e.g. imap.hostinger.com"),
    ("imap_port", "IMAP port", "993 for Hostinger / most providers (implicit TLS)"),
    ("imap_user", "IMAP username", "Usually your full email; press skip to accept"),
    ("smtp_host", "SMTP host", "Hostname from your email provider, e.g. smtp.hostinger.com"),
    ("smtp_port", "SMTP port", "465 for Hostinger / most providers (implicit TLS)"),
    ("smtp_user", "SMTP username", "Press skip to use the IMAP username"),
]

HOSTINGER_DEFAULTS = {
    "imap_host": "imap.hostinger.com",
    "imap_port": 993,
    "smtp_host": "smtp.hostinger.com",
    "smtp_port": 465,
}

# SiteGround server names vary per tenant (gcam1108.siteground.biz,
# gcam1092.siteground.biz, ...). Only the standard implicit-TLS ports are
# safe to prefill; the hostname must come from the admin (or from a saved
# custom preset for a specific SiteGround server used by several brands).
SITEGROUND_DEFAULTS = {
    "imap_port": 993,
    "smtp_port": 465,
}

# Google/Gmail is NOT accepted as a password-flow provider. The existing
# ``mailops.gmail_oauth`` module owns Gmail accounts and only through the
# authorized OAuth 'Desktop app' client defined in GOOGLE_CLIENT_ID /
# GOOGLE_CLIENT_SECRET. The wizard shows an informative screen rather
# than an input prompt so a CEO cannot accidentally type a Gmail password
# into Telegram.
GOOGLE_PROVIDER_ID = "google"

SKIPPABLE = frozenset({"display_name", "imap_user", "smtp_user"})


# --------------------------------------------------------------------------- helpers


def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _markup(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[_btn(t, d) for t, d in row] for row in rows]
    )


def _url_markup(url_label: str, url: str, extra: list[list[tuple[str, str]]] | None = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(url_label, url=url)]
    ]
    for row in extra or []:
        rows.append([_btn(t, d) for t, d in row])
    return InlineKeyboardMarkup(rows)


def _is_private(update: Update) -> bool:
    chat = getattr(update, "effective_chat", None)
    return getattr(chat, "type", None) in (None, "private")


def _is_group_chat(update: Update) -> bool:
    chat = getattr(update, "effective_chat", None)
    return getattr(chat, "type", None) in ("group", "supergroup")


def _effective_chat_id(update: Update) -> int | None:
    chat = getattr(update, "effective_chat", None)
    cid = getattr(chat, "id", None)
    try:
        return int(cid) if cid is not None else None
    except (TypeError, ValueError):
        return None


# Mailbox-screen callbacks that are safe to run in a WHITELISTED group when
# the tapper has P_ADMIN. Must match app.GROUP_MB_EXACT / GROUP_MB_PREFIXES —
# the authoritative allow-list lives in app.py; this is the local check that
# refuses credential/lifecycle screens even if the dispatcher is bypassed.
_GROUP_SAFE_SUFFIX_EXACT = frozenset({"", "m", "add", "qadd", "switch", "bulk", "preset", "smart"})
_GROUP_SAFE_SUFFIX_PREFIX = (
    "add:",
    "arm:",
    "cancel:",
    "edit:",
    "skip:",
    "poll:",
    "credsvia:",
    "qadd:",
    "bulk:",
    # Opt-in, owner-only, single-pinned-group password entry (see
    # mailops.group_credentials). The arm screen itself re-checks the
    # feature flag + exact group id + is_owner before doing anything.
    "group_pw:",
    # New interactive mailbox manager. All sub-bodies here are read-only
    # (pagination, filter clear, per-mailbox control screen, on-demand
    # health check). The ``mbx:*:settings`` tap routes to the private DM
    # instead of exposing settings in the group - handle_callback does
    # the alert + deep-link, not this allow-list.
    "switch:",
    "mbx:",
    # Manage Server Presets - group-safe VIEWING; mutating actions
    # (create/edit/delete) re-check owner + confirmation inside presets_ui.
    "preset:",
    # Smart Add Email - group-first flow; the credential step still only
    # ever hands off to DM or (opt-in) the group_pw: path above.
    "smart:",
)
_GROUP_UNSAFE_SUFFIX_PREFIX = (
    "view:",
    "enable:",
    "disable:",
    "health:",
    "purpose:",
    "audit:",
    "dm_prompt:",
)


def _group_safe_body(body: str) -> bool:
    if body in _GROUP_SAFE_SUFFIX_EXACT:
        return True
    for p in _GROUP_UNSAFE_SUFFIX_PREFIX:
        if body.startswith(p):
            return False
    for p in _GROUP_SAFE_SUFFIX_PREFIX:
        if body.startswith(p):
            return True
    return False


async def _notice(q: Any, text: str) -> None:
    """Refusal/status text that never double-answers the callback query.

    See ``mailops.bot.callbacks``: a second ``answerCallbackQuery`` is
    rejected by Telegram ("Query is too old ...") AND shows the user
    nothing, which is how mailbox buttons came to look unresponsive.
    """
    if q is None:
        return
    await Ack(q).notice(text)


async def _ensure_session_owner(update: Update, token: str) -> bool:
    """Alert + refuse when a non-owner taps a session-bound button.

    The underlying ``onboarding_session`` already refuses writes bound
    to a different user; this layer just turns that into a visible
    "started by someone else" Telegram alert so the UX in a group
    never silently fails for a second admin.
    """
    uid = getattr(update.effective_user, "id", None)
    if uid is None:
        return False
    try:
        session = os_mod.get_session(token, int(uid))
    except os_mod.SessionError as exc:
        q = getattr(update, "callback_query", None)
        if q is not None:
            await Ack(q).alert(str(exc)[:180])
        return False
    return session is not None


async def _show(update: Update, text: str, markup: InlineKeyboardMarkup | None) -> None:
    if len(text) > 3900:
        text = text[:3900] + "\n…"
    q = getattr(update, "callback_query", None)
    if q is not None:
        try:
            await q.edit_message_text(text, reply_markup=markup)
            return
        except Exception as exc:  # noqa: BLE001
            if "not modified" in str(exc).lower():
                return
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(text, reply_markup=markup)


async def _guard_admin(update: Update, *, body: str = "") -> bool:
    """Authorize the tapper for a mailbox screen.

    Allows:
      * Private chats (always), for every mb:* subcommand.
      * Whitelisted groups, only for ``_group_safe_body`` subcommands.

    Any other combination raises a Telegram alert and returns False.
    Admin permission is re-checked on every tap.
    """
    user = update.effective_user
    uid = getattr(user, "id", None)
    q = getattr(update, "callback_query", None)
    if uid is None:
        return False
    in_group = _is_group_chat(update)
    if in_group:
        # Whitelisted group only. (If a non-whitelisted group somehow
        # reaches this point, access.can below will fail safe.)
        chat_id = _effective_chat_id(update)
        if chat_id is None or not access.group_allowed(chat_id):
            if q is not None:
                await Ack(q).alert(
                    "This group is not whitelisted for Mailops onboarding."
                )
            return False
        if body and not _group_safe_body(body):
            if q is not None:
                await Ack(q).alert(
                    "That screen is private only. Open the bot in a "
                    "DM to continue."
                )
            return False
    elif not _is_private(update):
        if q is not None:
            await Ack(q).alert(
                "Mailboxes are managed in a private chat with the bot "
                "or inside the whitelisted group."
            )
        return False
    if not access.can(uid, access.P_ADMIN):
        if q is not None:
            await Ack(q).alert("Only administrators can manage mailboxes.")
        elif update.effective_message is not None:
            await update.effective_message.reply_text(
                "Only administrators can manage mailboxes."
            )
        return False
    return True


# --------------------------------------------------------------------------- screens


def _list_text(
    mailboxes: list[mailbox_admin.MailboxView], *, in_group: bool = False
) -> str:
    if not mailboxes:
        return (
            "Mailboxes\n\nNo email accounts registered yet.\n\n"
            "Tap Add email to onboard the first business mailbox. "
            "Credentials never travel through this chat."
        )
    lines = ["Mailboxes", ""]
    for m in mailboxes:
        marker = "·" if m.active else "○"
        lines.append(f"{marker} {m.account_id} <{m.email}>")
        lines.append(f"    {m.status_label()}")
        if not m.has_credentials and not m.locked:
            lines.append("    ⚠ no stored credentials — needs onboarding")
    lines.append("")
    if in_group:
        lines.append(
            "Non-secret onboarding steps run here. Credentials are collected "
            "in a private DM with the bot — passwords never appear in this group."
        )
    else:
        lines.append(
            "Active mailboxes are polled by the sync loop; disabled ones are "
            "preserved but ignored. Credentials never travel through Telegram."
        )
    return "\n".join(lines)


def main_screen(*, in_group: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    mailboxes = mailbox_admin.list_mailboxes()
    rows: list[list[tuple[str, str]]] = []
    for m in mailboxes:
        label = f"{m.account_id} ({'active' if m.active else 'off'})"
        if m.locked:
            label = f"{m.account_id} (locked)"
        # In a group, the per-mailbox detail screen is private-only
        # (shows email, display name, audit). Omit the per-mailbox
        # buttons so a group tap never reaches a private-only screen.
        if not in_group:
            rows.append([(label, f"{PREFIX}view:{m.account_id}")])
    import os as _os

    public_url = (_os.environ.get("MAILOPS_WEB_PUBLIC_URL") or "").strip()
    onboard_row: list[InlineKeyboardButton] = [
        _btn("Smart Add Email", f"{PREFIX}smart"),
        _btn("Add email", f"{PREFIX}add"),
        _btn("Quick Add Email", f"{PREFIX}qadd"),
    ]
    # Bulk CSV import — a group-safe entry point. The ingest handler itself
    # re-checks MAILOPS_BULK_ALLOW_GROUP_UPLOAD + MAILOPS_BULK_ALLOWED_GROUP
    # before accepting any document.
    tools_row: list[InlineKeyboardButton] = [
        _btn("Bulk Add (CSV)", f"{PREFIX}bulk"),
        _btn("Manage Presets", f"{PREFIX}preset"),
    ]
    if not in_group:
        # Audit/error history can leak hostnames in failure strings; only
        # show it on the private screen.
        tools_row.append(_btn("Onboarding errors", f"{PREFIX}errors"))
    inline_rows: list[list[InlineKeyboardButton]] = [[_btn(t, d) for t, d in row] for row in rows]
    # Switch Inbox is useful in both private and group chats; the screen
    # itself hides per-mailbox detail buttons in a group so it never
    # routes a group tap to a private-only view.
    if mailboxes:
        inline_rows.append([_btn("Switch Inbox / Status", f"{PREFIX}switch")])
    inline_rows.append(onboard_row)
    inline_rows.append(tools_row)
    if (not in_group) and public_url.startswith("https://"):
        # WebAppInfo must be https. The server verifies the Telegram-signed
        # ``initData`` on arrival — this URL alone grants no access. Not
        # shown in a group: a WebApp button in a group opens for whoever
        # taps it, not necessarily the admin.
        inline_rows.append(
            [InlineKeyboardButton("Secure Setup (phone)", web_app=WebAppInfo(url=public_url))]
        )
    # Back button must point to a chat-appropriate destination. The ``ac:*``
    # Access Management screens are PRIVATE-ONLY by design (they expose user
    # ids, audit log, bootstrap owner, group whitelist). Pointing a group
    # Back button at ``ac:m`` would land the admin on a dead-end popup
    # ("Access management is private-chat only."). In a group we route to
    # ``nav:home`` instead — the shared group dashboard, which IS the
    # group's control-center home and is already in GROUP_CALLBACK_EXACT.
    if in_group:
        inline_rows.append([_btn("Back to dashboard", "nav:home")])
    else:
        inline_rows.append([_btn("Back to admin", "ac:m")])
    text = _list_text(mailboxes, in_group=in_group)
    return text, InlineKeyboardMarkup(inline_rows)


def _conn_badge(m: mailbox_admin.MailboxView) -> str:
    if m.locked:
        return "locked"
    if not m.has_credentials:
        return "no creds"
    if not m.enabled_registry:
        return "registry: off"
    if not m.in_allowlist:
        return "allowlist: off"
    return "ready"


def switch_screen(*, in_group: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """Backward-compatible 'Switch Inbox' entry point.

    Delegates to the interactive paginated manager (``manager_screen``)
    for page 0, no search filter. The in-group variant now renders
    SELECTABLE per-mailbox buttons (routed to the group-safe
    ``mb:mbx:<aid>`` detail screen) rather than a plain-text report, so
    an admin can manage 90+ accounts directly from the whitelisted
    group. All sensitive write actions still require a private chat;
    they appear in the per-mailbox screen as 'Settings' which deep-links
    to the DM.

    This wrapper preserves the pre-October-2026 signature used by the
    existing screen-level tests (which assert on the text + the
    ``mb:view:*`` buttons produced in the private-chat path).
    """
    return manager_screen(page=0, query="", in_group=in_group)


# --------------------------------------------------------------------------- interactive mailbox manager
#
# ``manager_screen`` + ``mailbox_screen`` replace the former plain-text
# status report with a paginated, selectable mailbox grid that works
# inside the authorized Telegram group AND in a private chat. Design
# constraints:
#
#   * Lists EVERY registered mailbox (active, disabled, locked), not just
#     mailboxes with pending emails. The spec requires 90-100+ accounts
#     to be navigable, so pagination is a hard requirement.
#   * Shows derived-from-reality status only. ``_conn_badge`` reads the
#     local on-disk registry + settings + credentials; it does NOT touch
#     the mail server. The 'last sync' line uses ``cache.last_updated``
#     which is the real global cache stamp (null until the sync loop or
#     a manual refresh has ever completed). A separate 'Connection
#     Health' button runs the actual IMAP+SMTP handshake on demand and
#     caches the result in-process so re-rendering is cheap.
#   * The group-safe surface is READ-ONLY (view, refresh, health check,
#     open priority buckets). Sensitive write actions (enable/disable,
#     purpose, audit) stay in the private-only ``mb:view:*`` tree; the
#     per-mailbox screen in a group surfaces a 'Settings (DM)' button
#     that alerts the admin to open a private chat.
#   * No credentials or tokens appear in the UI. Status text is scrubbed
#     through ``redact_text`` before being shown in a group so a provider
#     error banner cannot land in group history.

_MANAGER_PAGE_SIZE = 10  # one page = 10 mailboxes + nav + action bar

# {user_id: {"page": int, "query": str, "chat_id": int}}
# Transient per-admin filter + page so pagination survives taps without
# needing a long callback_data payload. Dropped on bot restart.
_MANAGER_STATE: dict[int, dict[str, Any]] = {}

# {account_id: {"ok": bool, "imap": bool, "smtp": bool, "detail": str, "at": float}}
# Populated by the on-demand 'Check connection' button only. Never
# fabricated: an entry here means a real health_check() ran at ``at``.
_HEALTH_CACHE: dict[str, dict[str, Any]] = {}
_HEALTH_TTL_S = 300  # 5 minutes; older results shown with an age hint

_MB_SEARCH_FIELD = "__mb_search__"


def _fmt_age(ts: float | None) -> str:
    if not ts:
        return "never"
    try:
        delta = max(0, int(time.time() - float(ts)))
    except (TypeError, ValueError):
        return "unknown"
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _global_sync_label() -> str:
    """Human text for the real last cache refresh stamp.

    This is a GLOBAL stamp (one timestamp for the whole fleet, set by
    ``cache.refresh_all``). We surface it explicitly rather than
    pretending it is per-mailbox so the admin isn't misled.
    """
    try:
        from .. import cache as _cache  # local import: cache touches state

        ts = _cache.last_updated()
    except Exception:  # noqa: BLE001 - never crash the UI
        return "unknown"
    if not ts:
        return "never (sync has not run yet)"
    return _fmt_age(ts) + " (fleet cache)"


def _counts_for(account_id: str) -> tuple[int, int, int, int, bool]:
    """Return (p0, p1, p2, p3, found) for ``account_id`` from the cache.

    Reads whatever is ALREADY in the in-memory cache; never triggers
    ``cache.refresh_all()``. The sync loop and the explicit Manual Refresh
    button own cache rebuilds — the manager screen must stay responsive
    for a 90-100+ fleet and must not block the Telegram update loop on
    an IMAP round-trip for every mailbox.

    ``found=False`` when the cache has no entry for this account (either
    the fleet cache has never built, or the account was added after the
    last build). We display '—' instead of '0' in that case so the UI
    distinguishes "no mail yet" from "we don't know yet".
    """
    try:
        from .. import cache as _cache

        # Access the already-materialised counts without forcing a rebuild.
        # The module-level ``_counts`` is updated under ``_lock`` by
        # ``refresh_all``; reading it is cheap and safe.
        for c in getattr(_cache, "_counts", []) or []:
            if c.account_id == account_id:
                return c.p0, c.p1, c.p2, c.p3, True
    except Exception:  # noqa: BLE001 - degraded UI, not an outage
        pass
    return 0, 0, 0, 0, False


def _last_error_for(account_id: str) -> tuple[str, float] | None:
    """Most recent error-ish audit row for this mailbox, if any.

    'Error-ish' means an action we recognise as a failure signal:
    onboarding failures, health check failures (recorded by the health
    cache below), and policy denials. Returns (short_message, ts) with
    the message run through ``redact_text``.
    """
    cached = _HEALTH_CACHE.get(account_id)
    if cached and not cached.get("ok"):
        return (
            redact_text(str(cached.get("detail") or "connection check failed"))[:160],
            float(cached.get("at") or 0),
        )
    # Walk the audit log for a limited window of failure actions.
    try:
        rows = mailbox_admin.audit_log(10, account_id=account_id)
    except Exception:  # noqa: BLE001
        return None
    for r in rows:
        act = (r.action or "").lower()
        if any(tok in act for tok in ("fail", "error", "rollback", "denied", "refuse")):
            msg = redact_text(r.detail or r.action)[:160]
            return msg, float(r.created_at)
    return None


def _manager_entries(
    mailboxes: list[mailbox_admin.MailboxView], query: str
) -> list[mailbox_admin.MailboxView]:
    """Filter + stable-sort mailboxes for the manager grid.

    Sort key: pending P0 count (desc) → pending P1 count (desc) → active
    mailboxes before disabled → display name (case-insensitive). This
    keeps the busiest inboxes at the top of page 1 so the admin can
    react first and still find every mailbox via pagination.
    """
    q = (query or "").strip().lower()
    filtered: list[mailbox_admin.MailboxView] = []
    for m in mailboxes:
        if not q:
            filtered.append(m)
            continue
        hay = f"{m.account_id} {m.email} {m.display_name}".lower()
        if q in hay:
            filtered.append(m)

    def _key(m: mailbox_admin.MailboxView) -> tuple[int, int, int, str]:
        p0, p1, _p2, _p3, _ok = _counts_for(m.account_id)
        return (-p0, -p1, 0 if m.active else 1, (m.display_name or m.account_id).lower())

    filtered.sort(key=_key)
    return filtered


def _conn_summary(m: mailbox_admin.MailboxView) -> str:
    """One-word connection status derived from local state + health cache.

    Distinguishes:
      * Ready       - credentials present, enabled, in allowlist (configured
                      and able to be polled; actual reachability is confirmed
                      only by 'Check connection' which fills _HEALTH_CACHE)
      * Checking    - a health check is older than TTL / never run; shown as
                      Ready-pending so the admin knows the online status
                      has not been verified recently
      * Error       - a real health_check() returned ok=False recently
      * Disconnected- credentials missing or registry says disabled
      * Locked      - Gmail/shared inbox, read-only
    """
    if m.locked:
        return "Locked"
    if not m.has_credentials:
        return "Disconnected (no creds)"
    if not m.enabled_registry:
        return "Disconnected (registry off)"
    if not m.in_allowlist:
        return "Disconnected (not in allowlist)"
    cached = _HEALTH_CACHE.get(m.account_id)
    if cached and cached.get("ok"):
        return "Ready"
    if cached and not cached.get("ok"):
        return "Error"
    return "Ready (unverified)"


def manager_screen(
    *, page: int = 0, query: str = "", in_group: bool = False
) -> tuple[str, InlineKeyboardMarkup]:
    """Interactive paginated mailbox manager.

    Replaces the former static Switch Inbox listing with a grid where
    every mailbox is a selectable button. Works in the whitelisted group
    chat and in private chat. See the module header above for the full
    design rationale.
    """
    all_mailboxes = mailbox_admin.list_mailboxes()
    if not all_mailboxes:
        return (
            "Switch Inbox / Account Status\n\n"
            "No mailboxes registered yet. Tap Mailboxes → Add email or "
            "Bulk Add (CSV) to onboard the first one.",
            _markup([[("Back to mailboxes", f"{PREFIX}m")]]),
        )
    entries = _manager_entries(all_mailboxes, query)
    total = len(entries)
    pages = max(1, (total + _MANAGER_PAGE_SIZE - 1) // _MANAGER_PAGE_SIZE)
    page = max(0, min(int(page or 0), pages - 1))
    start = page * _MANAGER_PAGE_SIZE
    slice_ = entries[start : start + _MANAGER_PAGE_SIZE]

    sync_text = _global_sync_label()
    lines = [
        "Switch Inbox / Account Status",
        f"Mailboxes: {total}  ·  Page {page + 1}/{pages}  ·  Last sync: {sync_text}",
    ]
    if query:
        lines.append(f"Filter: {query!r}")
    lines.append("")
    for m in slice_:
        p0, p1, _p2, _p3, found = _counts_for(m.account_id)
        monitoring = "ON" if m.active else "OFF"
        send_state = "Human-approval" if (m.send and not m.locked) else "OFF"
        organize = "ON" if (m.organize and not m.locked) else "OFF"
        conn = _conn_summary(m)
        name = m.display_name or m.account_id
        lines.append(f"· {name}  <{m.email}>")
        lines.append(
            f"    id={m.account_id}  ·  conn: {conn}  ·  monitor: {monitoring}  "
            f"·  send: {send_state}  ·  organize: {organize}"
        )
        counts_text = (
            f"    P0 Critical: {p0}  ·  P1 Important: {p1}"
            if found
            else "    P0 Critical: —  ·  P1 Important: —  (cache not built)"
        )
        lines.append(counts_text)
        err = _last_error_for(m.account_id)
        if err is not None:
            err_msg, err_at = err
            lines.append(f"    ⚠ last error: {err_msg}  ({_fmt_age(err_at)})")
    lines.append("")
    if in_group:
        lines.append(
            "Tap any mailbox to open its control screen. Non-secret "
            "actions run here; credential / registry edits open your "
            "private DM so passwords never cross this group."
        )
    else:
        lines.append(
            "Tap any mailbox for its control screen (open inbox, drill "
            "into priority buckets, run a health check, edit settings)."
        )

    # Build the keyboard: one row per mailbox + nav row + action row.
    rows: list[list[tuple[str, str]]] = []
    for m in slice_:
        p0, p1, _p2, _p3, _ok = _counts_for(m.account_id)
        name = (m.display_name or m.account_id)
        # Keep labels short: Telegram shows ~30 characters per button on
        # a mobile screen before truncating.
        badge = ""
        if m.locked:
            badge = " 🔒"
        elif not m.active:
            badge = " ○"
        elif _HEALTH_CACHE.get(m.account_id) and not _HEALTH_CACHE[m.account_id].get("ok"):
            badge = " ⚠"
        count_badge = ""
        if p0 or p1:
            count_badge = f"  [P0:{p0} P1:{p1}]"
        label = (name[:22] + badge + count_badge)[:40]
        rows.append([(label, f"{PREFIX}mbx:{m.account_id}")])

    nav: list[tuple[str, str]] = []
    if page > 0:
        nav.append(("Prev", f"{PREFIX}switch:p:{page - 1}"))
    nav.append((f"Page {page + 1}/{pages}", "noop"))
    if page + 1 < pages:
        nav.append(("Next", f"{PREFIX}switch:p:{page + 1}"))
    if nav:
        rows.append(nav)

    action_row: list[tuple[str, str]] = [("Refresh", f"{PREFIX}switch:refresh")]
    if query:
        action_row.append(("Clear filter", f"{PREFIX}switch:clear"))
    else:
        # Search is a text-input flow; group taps alert → DM.
        action_row.append(("Search", f"{PREFIX}switch:search"))
    rows.append(action_row)
    rows.append([("Back to mailboxes", f"{PREFIX}m")])

    return "\n".join(lines)[:3900], _markup(rows)


def _get_manager_state(user_id: int | None) -> dict[str, Any]:
    if user_id is None:
        return {"page": 0, "query": ""}
    return _MANAGER_STATE.get(int(user_id), {"page": 0, "query": ""})


def _set_manager_state(user_id: int, *, page: int | None = None, query: str | None = None, chat_id: int | None = None) -> None:
    if user_id is None:
        return
    cur = dict(_MANAGER_STATE.get(int(user_id)) or {})
    if page is not None:
        cur["page"] = int(page)
    if query is not None:
        cur["query"] = str(query)
    if chat_id is not None:
        cur["chat_id"] = int(chat_id)
    _MANAGER_STATE[int(user_id)] = cur


def mailbox_screen(
    account_id: str, *, in_group: bool = False
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Per-mailbox control screen reachable from the manager grid.

    Group-safe surface (read-only):
      * Open Inbox / Critical / Important / Normal / Noise
        → re-use the existing ``ab:<aid>|p<N>`` callbacks, which are
          already in GROUP_CALLBACK_PREFIXES.
      * Refresh Status  → re-render this screen with fresh counts.
      * Connection Health → runs health_check() + caches result.
      * Settings → in group, deep-links to the admin's DM via an alert.
      * Back to Mailboxes → returns to the paginated manager.
    """
    m = mailbox_admin.find_mailbox(account_id)
    if m is None:
        return None

    p0, p1, p2, p3, found = _counts_for(m.account_id)
    conn = _conn_summary(m)
    monitoring = "ON" if m.active else "OFF"
    send_state = "Human-approval required" if (m.send and not m.locked) else "OFF"
    organize = "ON" if (m.organize and not m.locked) else "OFF"
    cached = _HEALTH_CACHE.get(m.account_id)

    lines = [
        f"Mailbox: {m.display_name or m.account_id}",
        f"Email: {m.email}",
        f"Account id: {m.account_id}",
        "",
        f"Connection: {conn}",
        f"Monitoring: {monitoring}",
        f"Sending: {send_state}",
        f"Organize: {organize}",
        f"Stored credentials: {'yes' if m.has_credentials else 'no'}",
        f"Last fleet sync: {_global_sync_label()}",
    ]
    if cached is not None:
        imap_tag = "OK" if cached.get("imap") else "FAIL"
        smtp_tag = "OK" if cached.get("smtp") else "FAIL"
        age = _fmt_age(cached.get("at"))
        lines.append(
            f"Last health check: IMAP {imap_tag} · SMTP {smtp_tag} ({age})"
        )
        detail = redact_text(str(cached.get("detail") or ""))[:200]
        if detail:
            lines.append(f"Health detail: {detail}")
    else:
        lines.append("Last health check: not run (tap Connection Health)")
    lines.append("")
    if found:
        lines.append(
            f"Counts  ·  P0 Critical: {p0}  ·  P1 Important: {p1}  "
            f"·  P2 Normal: {p2}  ·  P3 Noise: {p3}"
        )
    else:
        lines.append("Counts: cache not built yet — Refresh Status after the first sync.")
    err = _last_error_for(m.account_id)
    if err is not None:
        msg, when = err
        lines.append(f"⚠ last error: {msg}  ({_fmt_age(when)})")

    rows: list[list[tuple[str, str]]] = []
    # The priority bucket browser is group-safe (``ab:*`` prefix lives
    # in GROUP_CALLBACK_PREFIXES) and private-safe.
    rows.append([("Open Inbox", f"ab:{m.account_id}|p1")])
    rows.append([
        ("Critical", f"ab:{m.account_id}|p0"),
        ("Important", f"ab:{m.account_id}|p1"),
    ])
    rows.append([
        ("Normal", f"ab:{m.account_id}|p2"),
        ("Noise", f"ab:{m.account_id}|p3"),
    ])
    rows.append([
        ("Refresh Status", f"{PREFIX}mbx:{m.account_id}:refresh"),
        ("Connection Health", f"{PREFIX}mbx:{m.account_id}:health"),
    ])
    # Settings deep-link: group taps alert the admin to open DM; private
    # taps go straight to the credential/enable/disable/purpose/audit tree.
    rows.append([("Settings", f"{PREFIX}mbx:{m.account_id}:settings")])
    rows.append([("Back to Mailboxes", f"{PREFIX}switch")])
    return "\n".join(lines)[:3900], _markup(rows)


def _view_text(m: mailbox_admin.MailboxView) -> str:
    purpose = mailbox_admin.get_purpose(m.account_id)
    lines = [
        f"Mailbox: {m.account_id}",
        f"Email: {m.email}",
        f"Display name: {m.display_name}",
        f"Status: {m.status_label()}",
        f"Stored credentials: {'yes' if m.has_credentials else 'no'}",
        "",
        "Purpose (brand-specific, used only for this inbox):",
        purpose or "(not set — tap Edit purpose)",
    ]
    return "\n".join(lines)


def view_screen(account_id: str) -> tuple[str, InlineKeyboardMarkup] | None:
    m = mailbox_admin.find_mailbox(account_id)
    if m is None:
        return None
    rows: list[list[tuple[str, str]]] = []
    if m.locked:
        rows.append(
            [("(locked read-only; managed by the Sales-Agent)", f"{PREFIX}noop")]
        )
    else:
        if m.active:
            rows.append([("Disable", f"{PREFIX}disable:{m.account_id}")])
        else:
            if m.has_credentials:
                rows.append([("Enable", f"{PREFIX}enable:{m.account_id}")])
            else:
                rows.append(
                    [("(onboard again to re-enable — credentials are missing)",
                      f"{PREFIX}noop")]
                )
        rows.append([("Check connection", f"{PREFIX}health:{m.account_id}")])
        rows.append([("Edit purpose", f"{PREFIX}purpose:{m.account_id}")])
        rows.append([("View audit", f"{PREFIX}audit:{m.account_id}")])
    rows.append([("Back to mailboxes", f"{PREFIX}m")])
    return _view_text(m), _markup(rows)


# --------------------------------------------------------------------------- add-mailbox wizard


def _next_pending_step(fields: dict[str, Any]) -> tuple[str, str, str] | None:
    for key, label, hint in STEPS:
        if not str(fields.get(key) or "").strip():
            return key, label, hint
    return None


def _session_summary(fields: dict[str, Any]) -> str:
    rows = []
    for key, label, _hint in STEPS:
        value = str(fields.get(key) or "").strip() or "(not set)"
        rows.append(f"{label}: {value}")
    return "\n".join(rows)


def add_start_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "Add email account\n\n"
        "Choose the provider.\n\n"
        "• Hostinger — IMAP/SMTP prefilled (imap.hostinger.com:993, "
        "smtp.hostinger.com:465).\n"
        "• SiteGround — standard ports prefilled (993/465); you supply the "
        "server hostname (e.g. gcam1108.siteground.biz) because SiteGround "
        "assigns a different server per tenant.\n"
        "• Google / Gmail — managed by the Sales-Agent through authorized "
        "OAuth. Setup runs on the server, not through Telegram.\n"
        "• Custom IMAP + SMTP — any other provider; you supply all fields.\n\n"
        "Credentials are NEVER collected in this chat by default. After the "
        "non-secret fields are entered, Mailops will show you a one-shot "
        "server-side command to finish onboarding at a hidden terminal "
        "prompt. Nothing is sent to the mailbox during validation."
    )
    rows = [
        [("Hostinger", f"{PREFIX}add:hostinger")],
        [("SiteGround", f"{PREFIX}add:siteground")],
        [("Google / Gmail", f"{PREFIX}add:google")],
        [("Custom IMAP + SMTP", f"{PREFIX}add:custom")],
        [("Back", f"{PREFIX}m")],
    ]
    return text, _markup(rows)


def _google_oauth_available() -> tuple[bool, str]:
    """True when GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are populated."""
    import os as _os

    cid = (_os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
    csec = (_os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()
    if cid and csec:
        return True, ""
    # Try the file .env fallback as well.
    try:
        from ..paths import _env_file_value

        cid = cid or _env_file_value("GOOGLE_CLIENT_ID").strip()
        csec = csec or _env_file_value("GOOGLE_CLIENT_SECRET").strip()
    except Exception:  # noqa: BLE001
        pass
    if cid and csec:
        return True, ""
    return False, (
        "Google OAuth is not configured (GOOGLE_CLIENT_ID / "
        "GOOGLE_CLIENT_SECRET are empty)."
    )


def _google_provider_screen() -> tuple[str, InlineKeyboardMarkup]:
    """Explain the Gmail flow and refuse any Telegram-based onboarding.

    This screen deliberately does NOT open an onboarding session: a
    Gmail mailbox never carries a password-based IMAP login in this
    deployment. Pressing the button only educates the admin and (when
    OAuth is configured) tells them the exact server-side command to
    run next.
    """
    from .. import config

    ready, reason = _google_oauth_available()
    enabled = config.GMAIL_ENABLED
    lines = [
        "Google / Gmail account",
        "",
        "Gmail is managed through the authorized Google OAuth 'Desktop app' "
        "client, NOT through a password pasted into Telegram.",
        "",
    ]
    if not enabled:
        lines.append(
            "⚠ Gmail is DISABLED for this deployment "
            "(mailops.config.GMAIL_ENABLED=False). The former Growth Pilot "
            "Gmail inbox is shared with the Sales-Agent and must not be "
            "reconnected without explicit approval from the owner."
        )
    elif not ready:
        lines.append(f"⚠ Gmail onboarding is NOT available right now: {reason}")
        lines.append("")
        lines.append(
            "An owner can enable it by filling in GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET in .env (see README section 'Gmail OAuth "
            "setup'). After that, run on the Mailops server:"
        )
        lines.append("")
        lines.append("  python -m mailops gmail-setup <account_id> <email>")
        lines.append("  python -m mailops gmail-login <account_id>")
    else:
        lines.append(
            "To onboard a Gmail account, run on the Mailops server:"
        )
        lines.append("")
        lines.append("  python -m mailops gmail-setup <account_id> <email>")
        lines.append("  python -m mailops gmail-login <account_id>")
        lines.append("")
        lines.append(
            "The second command opens the authorized Google consent screen in "
            "a browser on that machine. Mailops never sees the Google "
            "password; only a refresh token is stored."
        )
    rows = [[("Back to providers", f"{PREFIX}add")]]
    return "\n".join(lines), _markup(rows)


def _wizard_screen(session: os_mod.OnboardingSession) -> tuple[str, InlineKeyboardMarkup]:
    nxt = _next_pending_step(session.fields)
    summary = _session_summary(session.fields)
    expiry_min = max(1, int((session.expires_at - os_mod._now()) // 60))
    if nxt is None:
        text = (
            "Review the new mailbox\n\n"
            + summary
            + f"\n\nSession expires in {expiry_min} min. "
            "Confirm to arm credential entry. Nothing has been sent yet."
        )
        rows = [
            [("Confirm & arm credentials", f"{PREFIX}arm:{session.token}:{session.nonce}")],
            [("Edit", f"{PREFIX}edit:{session.token}:{session.nonce}")],
            [("Cancel", f"{PREFIX}cancel:{session.token}:{session.nonce}")],
        ]
        return text, _markup(rows)
    key, label, hint = nxt
    text = (
        "Add email account\n\n"
        + summary
        + f"\n\nNext step: {label}\n{hint}\n\n"
        f"Send ONE message with the value. Session expires in {expiry_min} min."
    )
    rows: list[list[tuple[str, str]]] = []
    if key in SKIPPABLE:
        rows.append([(f"Skip {label}", f"{PREFIX}skip:{session.token}:{session.nonce}")])
    rows.append([("Cancel", f"{PREFIX}cancel:{session.token}:{session.nonce}")])
    return text, _markup(rows)


def _armed_screen(session: os_mod.OnboardingSession) -> tuple[str, InlineKeyboardMarkup]:
    """Credentials-capture step. Two modes:

    * CLI (default): shows the server-side command the admin runs on the
      Mailops host. The password is typed at a hidden terminal prompt.
    * DM (opt-in): the bot asks for the password in this DM, holds it in
      RAM only for the add_account call, then wipes.
    """
    import os as _os

    allow_dm = (_os.environ.get("MAILOPS_ALLOW_DM_CREDENTIALS") or "").strip() == "1"
    summary = _session_summary(session.fields)
    expiry_min = max(1, int((session.expires_at - os_mod._now()) // 60))

    cli_cmd = f"python -m mailops webmail-run-pending {session.token}"
    lines = [
        "Ready to validate",
        "",
        summary,
        "",
        "On the Mailops server, open a terminal (any Windows/Linux shell "
        "that already runs the bot) and run:",
        "",
        cli_cmd,
        "",
        "You will be asked for the mailbox password at a HIDDEN prompt. "
        "Mailops only performs a login handshake (IMAP + SMTP) — no email "
        "is sent during validation. The result appears here when done.",
        f"\nSession expires in {expiry_min} min.",
    ]
    rows: list[list[tuple[str, str]]] = [
        [("Refresh status", f"{PREFIX}poll:{session.token}")],
    ]
    if allow_dm:
        lines.append("")
        lines.append(
            "OPT-IN DM capture is enabled on this server "
            "(MAILOPS_ALLOW_DM_CREDENTIALS=1). You can instead send the "
            "password as your next message here. It will be held in memory "
            "only for the single validation call, then the message is "
            "deleted and the secret is wiped. The password is weaker than "
            "the CLI path because it transits Telegram's servers."
        )
        rows.append([("Send password in this chat", f"{PREFIX}dm_prompt:{session.token}:{session.nonce}")])
    rows.append([("Cancel onboarding", f"{PREFIX}cancel:{session.token}:")])
    return "\n".join(lines), _markup(rows)


def _done_screen(session: os_mod.OnboardingSession) -> tuple[str, InlineKeyboardMarkup]:
    aid = str(session.fields.get("account_id") or "?")
    text = (
        f"Mailbox '{aid}' validated and registered.\n\n"
        "It is read-only with sending disabled until an admin explicitly "
        "turns organization/sending on from the standard Settings screens. "
        "The sync loop will pick it up on the next tick (no restart needed)."
    )
    rows = [
        [("Open mailbox", f"{PREFIX}view:{aid}")],
        [("Back to mailboxes", f"{PREFIX}m")],
    ]
    return text, _markup(rows)


def _failed_screen(session: os_mod.OnboardingSession) -> tuple[str, InlineKeyboardMarkup]:
    # Validation errors come from Himalaya / IMAP / SMTP server responses
    # and can embed the username, host, or server banner. In private chat
    # this is fine; in a group it would leak into message history, so we
    # scrub through redact_text. Scrubbing in private is a no-op in
    # practice because the patterns targeted (emails, hosts, tokens) are
    # the same fields the admin is actively staring at — redact is
    # idempotent on fields the admin already supplied.
    reason = redact_text(session.last_error or "validation failed")
    text = (
        "Onboarding failed and was rolled back.\n\n"
        f"Reason: {reason[:400]}\n\n"
        "Nothing was registered. No existing mailbox was modified. "
        "You can start again from Add email."
    )
    rows = [
        [("Add email", f"{PREFIX}add")],
        [("Back to mailboxes", f"{PREFIX}m")],
    ]
    return text, _markup(rows)


def session_screen(session: os_mod.OnboardingSession) -> tuple[str, InlineKeyboardMarkup]:
    if session.state == os_mod.STATE_DRAFT:
        return _wizard_screen(session)
    if session.state == os_mod.STATE_AWAITING_CREDS:
        return _armed_screen(session)
    if session.state == os_mod.STATE_VALIDATING:
        return (
            "Validating credentials against IMAP and SMTP (no email is sent). "
            "Tap Refresh in a moment.",
            _markup([[("Refresh status", f"{PREFIX}poll:{session.token}")]]),
        )
    if session.state == os_mod.STATE_DONE:
        return _done_screen(session)
    if session.state == os_mod.STATE_FAILED:
        return _failed_screen(session)
    if session.state == os_mod.STATE_CANCELLED:
        return (
            "Onboarding cancelled. Nothing was registered.",
            _markup([[("Back to mailboxes", f"{PREFIX}m")]]),
        )
    return (
        "This onboarding session expired. Open Mailboxes to start again.",
        _markup([[("Back to mailboxes", f"{PREFIX}m")]]),
    )


def errors_screen() -> tuple[str, InlineKeyboardMarkup]:
    rows = mailbox_admin.audit_log(20)
    if not rows:
        return (
            "No onboarding or lifecycle events recorded yet.",
            _markup([[("Back", f"{PREFIX}m")]]),
        )
    import time as _time

    lines = ["Recent mailbox events", ""]
    for r in rows:
        when = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(r.created_at))
        aid = r.account_id or "-"
        detail = (r.detail or "")[:120]
        lines.append(f"{when}  {r.action}  {aid}  {detail}".rstrip())
    return "\n".join(lines)[:3900], _markup([[("Back", f"{PREFIX}m")]])


def audit_screen(account_id: str) -> tuple[str, InlineKeyboardMarkup]:
    import time as _time

    rows = mailbox_admin.audit_log(30, account_id=account_id)
    if not rows:
        return (
            f"No events recorded for {account_id} yet.",
            _markup([[("Back", f"{PREFIX}view:{account_id}")]]),
        )
    lines = [f"Audit · {account_id}", ""]
    for r in rows:
        when = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(r.created_at))
        lines.append(f"{when}  {r.action}  {(r.detail or '')[:120]}".rstrip())
    return "\n".join(lines)[:3900], _markup([[("Back", f"{PREFIX}view:{account_id}")]])


# --------------------------------------------------------------------------- handlers

# Text-input modes the admin can be in. Each entry is (session_token,
# field_key) OR a sentinel for "waiting for DM password" / "editing purpose".
AWAITING: dict[int, dict[str, Any]] = {}

# How long after _arm_dm_prompt a password message may still be accepted.
# Any text arriving later is refused and the slot is cleared. This is a
# TTL *inside* the already-short 20-minute session TTL, so an admin who
# gets distracted after tapping "Send password in this chat" cannot
# accidentally submit something else later as their password.
DM_ARM_TTL_S = 60

# TTL for every AWAITING slot that has NO backing onboarding-session token
# (``payload["token"] == ""``), applied uniformly by ``_set_awaiting``
# (which stamps ``armed_at`` on arm) and enforced in ``handle_text``. These
# slots cannot rely on a session's own 20-minute expiry, and several of
# them (``_PURPOSE_FIELD``, ``_MB_SEARCH_FIELD``, ``presets_ui.CREATE_FIELD``
# / ``EDIT_FIELD``, and the first-message arm of ``QUICK_ADD_FIELD`` /
# ``_SMART_EMAIL_FIELD`` before a session exists to parse into) accept
# arbitrary free text that is applied with little or no validation. Found
# during the pre-deployment audit: these had NO expiry at all, so an admin
# who tapped e.g. "Edit purpose" or "Create preset" and then, hours later,
# sent an unrelated private message to the bot would have had that message
# silently stored as the mailbox's purpose / preset field / search filter.
# Mirrors ``app.py``'s ``WAITING_TEXT_TTL_S``. Password-capture slots are
# NOT covered here — they always carry a real session token and keep their
# own, stricter ``DM_ARM_TTL_S``.
UNTOKENED_FIELD_TTL_S = 15 * 60

# Sentinel field names recognised by handle_text.
_DM_PW_FIELD = "__dm_password__"
_PURPOSE_FIELD = "__purpose__"
# Group-password capture (opt-in, see mailops.group_credentials). Separate
# sentinel from _DM_PW_FIELD so the two code paths can never be confused by
# handle_text, and so a leftover group-armed slot is trivially distinguish-
# able from a DM-armed one in logs/tests.
_GROUP_PW_FIELD = "__group_password__"

# Smart Add Email (Phase 5): email-first server-resolution flow. Two text
# sentinels - the admin's first message is just the email address; the
# second (only reached when no VERIFIED/SUGGESTED match exists and the
# admin picked a provider that still needs a hostname, i.e. SiteGround) is
# a single mail-server hostname applied to BOTH imap_host and smtp_host
# (SiteGround uses the same host for both, just different ports).
_SMART_EMAIL_FIELD = "__smart_email__"
_SMART_HOST_FIELD = "__smart_host__"

_SMART_STATUS_LABELS = {
    server_resolution.STATUS_VERIFIED: "VERIFIED",
    server_resolution.STATUS_SUGGESTED: "SUGGESTED",
    server_resolution.STATUS_UNKNOWN: "UNKNOWN",
}


def _awaiting_user(user_id: int) -> dict[str, Any] | None:
    return AWAITING.get(int(user_id))


def _clear_sibling_waiting_text(user_id: int) -> None:
    """Clear ``app.py``'s ``WAITING_TEXT`` slot for this user.

    Called whenever an ``AWAITING`` slot is successfully installed, so the
    two independent input-state dicts never both believe they own the
    admin's next message at once (see ``app.py``'s ``_set_waiting``
    docstring for the full cross-dict-starvation scenario this closes).
    Import is deferred to call time — ``app.py`` imports this module at
    load time, so importing it back at module scope here would be
    circular; by the time any handler actually runs, both modules are
    already fully loaded and this is just a normal attribute lookup.
    """
    from . import app as _app_mod

    _app_mod.WAITING_TEXT.pop(int(user_id), None)


def _set_awaiting(user_id: int, payload: dict[str, Any]) -> bool:
    """Install an input slot for ``user_id``.

    Refuses to overwrite a pending ``__dm_password__`` slot from any other
    screen (purpose editor, wizard step, ...). This closes a race where an
    admin arms the DM password capture, then — before sending the password
    — taps "Edit purpose" on a different mailbox. Without this guard, the
    next text message would be stored as the mailbox purpose in
    ``settings.json`` (a non-secret file), landing the password on disk in
    plaintext.

    ``payload`` MUST include a ``chat_id`` integer (the chat where the slot
    is valid). ``handle_text`` refuses messages from a different chat so
    one admin's open wizard in a group cannot absorb another admin's DM
    message (or vice versa).

    Returns True when the slot was taken, False when it was refused.
    """
    uid = int(user_id)
    if "chat_id" not in payload:
        # Defensive: force the caller to set chat_id so no caller can
        # install a slot that would silently accept text from anywhere.
        raise ValueError("awaiting slot missing chat_id")
    current = AWAITING.get(uid)
    protected_fields = (_DM_PW_FIELD, _GROUP_PW_FIELD)
    if (
        current is not None
        and current.get("field") in protected_fields
        and payload.get("field") not in protected_fields
    ):
        return False
    if not payload.get("token") and "armed_at" not in payload:
        # No onboarding-session token means nothing else bounds this
        # slot's lifetime (session TTL doesn't apply), so stamp our own
        # arm time. ``handle_text`` enforces UNTOKENED_FIELD_TTL_S against
        # this for every untokened field uniformly — see that constant's
        # docstring. Password-capture slots always carry a real session
        # token (see ``_arm_dm_prompt``/``_arm_group_pw_prompt``), so they
        # never hit this branch and keep their own, stricter DM_ARM_TTL_S.
        payload["armed_at"] = time.time()
    AWAITING[uid] = payload
    _clear_sibling_waiting_text(uid)
    return True


def _force_set_awaiting(user_id: int, payload: dict[str, Any]) -> None:
    """Unconditional install. Only the DM-prompt / cancel paths use this."""
    if "chat_id" not in payload:
        raise ValueError("awaiting slot missing chat_id")
    AWAITING[int(user_id)] = payload
    _clear_sibling_waiting_text(user_id)


def _clear_awaiting(user_id: int) -> None:
    AWAITING.pop(int(user_id), None)


async def _show_session(update: Update, session: os_mod.OnboardingSession) -> None:
    text, markup = session_screen(session)
    await _show(update, text, markup)


async def open_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_admin(update):
        return
    text, markup = main_screen(in_group=_is_group_chat(update))
    await _show(update, text, markup)


async def _open_add(update: Update) -> None:
    text, markup = add_start_screen()
    await _show(update, text, markup)


async def _open_view(update: Update, account_id: str) -> None:
    out = view_screen(account_id)
    if out is None:
        await _show(
            update,
            f"'{account_id}' is not a registered mailbox.",
            _markup([[("Back", f"{PREFIX}m")]]),
        )
        return
    text, markup = out
    await _show(update, text, markup)


async def _start_wizard(
    update: Update, user_id: int, provider: str
) -> None:
    # Gmail/Google: we never open a password-flow session for it. Short-circuit
    # to the OAuth-oriented info screen instead.
    if provider == "google":
        text, markup = _google_provider_screen()
        await _show(update, text, markup)
        return
    try:
        session = os_mod.start_session(user_id, mode=os_mod.MODE_CLI)
    except os_mod.SessionError as exc:
        await _show(update, str(exc), _markup([[("Back", f"{PREFIX}m")]]))
        return
    patch: dict[str, Any] = {}
    if provider == "hostinger":
        patch.update(HOSTINGER_DEFAULTS)
    elif provider == "siteground":
        patch.update(SITEGROUND_DEFAULTS)
    if patch:
        try:
            session = os_mod.set_fields(
                session.token, user_id, nonce=session.nonce, patch=patch
            )
        except os_mod.SessionError as exc:
            await _show(update, str(exc), _markup([[("Back", f"{PREFIX}m")]]))
            return
    mailbox_admin.record(
        user_id, "onboard_start", "", f"mode=cli provider={provider}"
    )
    await _show_session(update, session)
    # Immediately prime the first text-input slot.
    nxt = _next_pending_step(session.fields)
    if nxt is not None:
        key, _label, _hint = nxt
        _set_awaiting(
            user_id,
            {"token": session.token, "field": key, "chat_id": _effective_chat_id(update)},
        )


def _coerce_field(key: str, raw: str) -> tuple[Any | None, str]:
    """Light client-side coerce. Full validation is done in validate_spec
    once all fields are collected."""
    raw = (raw or "").strip()
    if not raw:
        return None, "empty value"
    if key in ("imap_port", "smtp_port"):
        if not raw.isdigit():
            return None, "port must be a number between 1 and 65535"
        n = int(raw)
        if not 1 <= n <= 65535:
            return None, "port must be between 1 and 65535"
        return n, ""
    if len(raw) > 254:
        return None, "value is too long"
    return raw, ""


async def _apply_wizard_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, token: str, field: str, raw: str
) -> None:
    user_id = update.effective_user.id
    value, err = _coerce_field(field, raw)
    if err and raw.strip().lower() != "skip":
        await update.effective_message.reply_text(
            f"Cannot accept that value: {err}\nTry again or press Cancel."
        )
        return
    if raw.strip().lower() == "skip" and field not in SKIPPABLE:
        await update.effective_message.reply_text(
            "That step is required. Please send a value."
        )
        return
    patch: dict[str, Any] = {}
    if raw.strip().lower() != "skip":
        patch[field] = value
    else:
        # Apply the sensible default for skippable fields.
        if field == "display_name":
            patch["display_name"] = ""
        elif field == "imap_user":
            patch["imap_user"] = ""
        elif field == "smtp_user":
            patch["smtp_user"] = ""
    try:
        session = os_mod.get_session(token, user_id)
        session = os_mod.set_fields(token, user_id, nonce=session.nonce, patch=patch)
    except os_mod.SessionError as exc:
        _clear_awaiting(user_id)
        await update.effective_message.reply_text(str(exc))
        return
    nxt = _next_pending_step(session.fields)
    if nxt is not None:
        _set_awaiting(
            user_id,
            {"token": token, "field": nxt[0], "chat_id": _effective_chat_id(update)},
        )
    else:
        _clear_awaiting(user_id)
    await _show_session(update, session)


async def _arm_session(
    update: Update, context: ContextTypes.DEFAULT_TYPE, token: str, nonce: str
) -> None:
    user_id = update.effective_user.id
    try:
        session = os_mod.arm_for_credentials(token, user_id, nonce=nonce)
    except os_mod.SessionError as exc:
        await _show(
            update,
            # Scrub in case the SessionError text somehow echoes a value
            # from the user's own input.
            redact_text(str(exc))[:300],
            _markup([[("Back to mailboxes", f"{PREFIX}m")]]),
        )
        return
    mailbox_admin.record(
        user_id, "onboard_armed", str(session.fields.get("account_id") or ""), ""
    )
    if _is_group_chat(update):
        # GROUP → PRIVATE HANDOFF (default, unchanged). The credential step
        # never happens in a group UNLESS the operator explicitly enabled
        # group password entry for THIS exact group AND the tapper is an
        # owner (mailops.group_credentials.allowed_for_group) - in that one
        # case we also offer an opt-in "enter it here" button alongside the
        # DM option; the DM path remains the default/first-listed choice.
        chat_id = _effective_chat_id(update)
        offer_group_entry = gc.allowed_for_group(chat_id) and access.is_owner(user_id)
        await _handoff_from_group(update, context, session, offer_group_entry=offer_group_entry)
        return
    await _show_session(update, session)


async def _handoff_from_group(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: os_mod.OnboardingSession,
    *,
    offer_group_entry: bool = False,
) -> None:
    """Group arm → private DM takeover (default), with an optional explicit
    in-group password-entry button when the operator enabled that feature
    for this exact group and the tapper is an owner.

    The group message is edited to a bland "credentials step moved to
    your private chat" so no onlooker learns which mailbox the admin is
    about to validate. The admin receives the normal armed screen as a
    DM. If the DM cannot be sent (e.g. the admin has not started a
    private chat with the bot yet), we leave a short, non-revealing
    hint in the group telling them to open the bot DM first.
    """
    user_id = update.effective_user.id
    text, markup = session_screen(session)
    dm_sent = False
    try:
        await context.bot.send_message(
            chat_id=int(user_id),
            text="Credentials step for your pending mailbox onboarding — "
            "finish this in a private chat so passwords never cross a "
            "group boundary.\n\n" + text,
            reply_markup=markup,
        )
        dm_sent = True
    except TelegramError as exc:
        log.info("group→DM handoff could not reach admin %s: %s", user_id, exc)
    group_text = (
        "Non-secret setup is complete. Credentials step continues in a "
        "private chat with the bot — passwords never appear in this group.\n\n"
        "Open the bot in a DM to finish."
    )
    if not dm_sent:
        group_text += (
            "\n\n⚠ The bot could not DM you. Open "
            "@" + (getattr(context.bot, "username", "") or "the bot")
            + " and press Start, then re-tap Confirm & arm in this group."
        )
    rows: list[list[tuple[str, str]]] = []
    if offer_group_entry:
        group_text += (
            "\n\nThis group is explicitly enabled for in-group password "
            "entry (owners only). You may instead tap below to enter the "
            "password HERE as a reply to a bot prompt - this is riskier "
            "than the private DM and shows a full warning before you send "
            "anything."
        )
        rows.append(
            [("Enter password in this group instead (advanced)",
              f"{PREFIX}group_pw:{session.token}:{session.nonce}")]
        )
    rows.append([("Back to mailboxes", f"{PREFIX}m")])
    await _show(update, group_text, _markup(rows))


async def _handoff_credentials(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    token: str,
    choice: str,
) -> None:
    """Not currently used — reserved for future ``mb:credsvia:*`` fan-out.

    The current flow posts the full armed screen (which already offers
    Server CLI and, when enabled, DM capture) directly in the admin's
    private chat; no extra fork is needed in the group. The branch
    exists so the ``mb:credsvia:*`` allow-list entry is non-functional
    today rather than being an accidental hole.
    """
    q = getattr(update, "callback_query", None)
    if q is not None:
        await Ack(q).notice(
            "Open the bot in a DM to finish the credentials step."
        )


async def _cancel_session(update: Update, token: str, nonce: str | None) -> None:
    user_id = update.effective_user.id
    try:
        os_mod.cancel(token, user_id, nonce=nonce or None)
        mailbox_admin.record(user_id, "onboard_cancelled", "", "")
    except os_mod.SessionError as exc:
        await _show(update, redact_text(str(exc))[:300], _markup([[("Back", f"{PREFIX}m")]]))
        return
    finally:
        _clear_awaiting(user_id)
    text, markup = main_screen(in_group=_is_group_chat(update))
    await _show(update, "Cancelled.\n\n" + text, markup)


async def _poll_session(update: Update, token: str) -> None:
    user_id = update.effective_user.id
    try:
        session = os_mod.get_session(token, user_id)
    except os_mod.SessionError as exc:
        await _show(update, str(exc), _markup([[("Back", f"{PREFIX}m")]]))
        return
    await _show_session(update, session)


async def _edit_session(update: Update, token: str, nonce: str) -> None:
    """Clear every field and restart from the first wizard step."""
    user_id = update.effective_user.id
    try:
        session = os_mod.get_session(token, user_id)
        patch = {key: "" for key, _l, _h in STEPS}
        session = os_mod.set_fields(token, user_id, nonce=session.nonce, patch=patch)
    except os_mod.SessionError as exc:
        await _show(update, str(exc), _markup([[("Back", f"{PREFIX}m")]]))
        return
    _set_awaiting(
        user_id,
        {"token": token, "field": STEPS[0][0], "chat_id": _effective_chat_id(update)},
    )
    await _show_session(update, session)


# ---- Quick Add Email (one-message onboarding) ------------------------------

QUICK_ADD_FIELD = mb_quick.QUICK_ADD_FIELD


def _collect_taken_account_ids() -> set[str]:
    """Every id currently registered OR reserved by a non-terminal session.

    Used by the quick-add parser so the auto-derived id does not collide
    with an in-flight onboarding or an already-registered mailbox.
    """
    taken: set[str] = set()
    for m in mailbox_admin.list_mailboxes():
        if m.account_id:
            taken.add(str(m.account_id).lower())
    # Scan onboarding_sessions table directly: we do not know the admin
    # id here, and listing per-user would be racy. Reading only ids and
    # states is safe (no secrets).
    try:
        from ..db import db as _db
        from .. import onboarding_session as _os_mod

        with _db() as conn:
            rows = conn.execute(
                "SELECT fields_json, state FROM onboarding_sessions "
                "WHERE state IN (?,?,?)",
                (_os_mod.STATE_DRAFT, _os_mod.STATE_AWAITING_CREDS, _os_mod.STATE_VALIDATING),
            ).fetchall()
    except Exception:  # noqa: BLE001 - defensive: taken-set is advisory
        rows = []
    import json as _json

    for r in rows:
        try:
            blob = _json.loads(r["fields_json"] or "{}")
        except (ValueError, TypeError):
            continue
        aid = str(blob.get("account_id") or "").strip().lower()
        if aid:
            taken.add(aid)
    return taken


def _quick_add_main_screen(preset_name: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """Entry screen listing presets (if any) and showing the paste prompt."""
    presets = mb_quick.presets_list()
    lines = [
        "Quick Add Email",
        "",
        "Paste ONE message with the mailbox's non-secret details.",
        "Example:",
        "",
        "Email: info@alien.fi",
        "Name: Alien FI",
        "IMAP: gcam1108.siteground.biz",
        "IMAP Port: 993",
        "SMTP: gcam1108.siteground.biz",
        "SMTP Port: 465",
        "",
        "Optional lines: Account ID, IMAP user, SMTP user.",
        "NEVER include a password or token — those are rejected.",
        "",
        "In a group: if the bot does not react to a plain paste, Telegram "
        "privacy mode is hiding your message. Either reply to this prompt "
        "with your paste, or send it as /qadd <paste on next lines>.",
    ]
    if presets:
        lines.append("")
        if preset_name:
            lines.append(f"Preset selected: {preset_name}")
            lines.append(
                "You only need: Email + Name (hosts/ports come from the preset)."
            )
        else:
            lines.append(
                "Or pick a saved server preset to onboard with just an email "
                "and a display name:"
            )
    rows: list[list[tuple[str, str]]] = []
    if presets:
        for name, _blob in presets[:6]:
            marker = "·" if name == preset_name else "○"
            rows.append([(f"{marker} Use preset: {name}", f"{PREFIX}qadd:preset:{name}")])
        if preset_name:
            rows.append([("Clear preset", f"{PREFIX}qadd:preset:")])
    rows.append([("Cancel", f"{PREFIX}m")])
    return "\n".join(lines), _markup(rows)


# Transient per-admin selection for quick-add:
#   {user_id: {"preset": name_or_"", "chat_id": int}}
# Cleared when the session is confirmed/cancelled or the admin leaves the
# quick-add flow. Not persisted — a bot restart just loses the pick.
_QADD_PRESET_CHOICE: dict[int, dict[str, Any]] = {}


def _quick_review_text(session: os_mod.OnboardingSession, result: mb_quick.QuickAddResult, preset_name: str) -> str:
    expiry_min = max(1, int((session.expires_at - os_mod._now()) // 60))
    summary = _session_summary(session.fields)
    header = ["Review the new mailbox (Quick Add)", ""]
    if preset_name:
        header.append(f"Preset: {preset_name}")
    header.append(summary)
    header.append("")
    if result.account_id_overridden:
        header.append("Account ID: supplied by you")
    else:
        header.append(f"Account ID: auto-derived as '{session.fields.get('account_id')}'")
    header.append("")
    header.append(
        f"Session expires in {expiry_min} min. Confirm to continue to "
        "password capture in your private DM. Nothing has been sent yet."
    )
    return "\n".join(header)


def _quick_review_screen(
    session: os_mod.OnboardingSession,
    result: mb_quick.QuickAddResult,
    preset_name: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    rows: list[list[tuple[str, str]]] = [
        [("Confirm", f"{PREFIX}arm:{session.token}:{session.nonce}")],
        [("Edit", f"{PREFIX}qadd:edit:{session.token}:{session.nonce}")],
        [("Cancel", f"{PREFIX}cancel:{session.token}:{session.nonce}")],
    ]
    if result.save_preset_suggested and not preset_name:
        rows.insert(
            1,
            [("Save server as preset", f"{PREFIX}qadd:save_preset:{session.token}:{session.nonce}")],
        )
    return _quick_review_text(session, result, preset_name), _markup(rows)


async def _open_quick_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry: show the paste prompt + preset list and arm a text slot."""
    user_id = update.effective_user.id
    choice = _QADD_PRESET_CHOICE.get(int(user_id), {})
    preset_name = str(choice.get("preset") or "")
    text, markup = _quick_add_main_screen(preset_name)
    await _show(update, text, markup)
    # Install a fresh text-capture slot — the admin's next message is the
    # one that holds all mailbox details. We do not open an onboarding
    # session yet; the parser creates one only after the message parses
    # cleanly so rate limits are respected.
    _set_awaiting(
        user_id,
        {
            "token": "",
            "field": QUICK_ADD_FIELD,
            "preset": preset_name,
            "chat_id": _effective_chat_id(update),
        },
    )


async def _pick_quick_preset(update: Update, raw_name: str) -> None:
    user_id = update.effective_user.id
    name = (raw_name or "").strip()
    if name and mb_quick.preset_get(name) is None:
        await _notice(update.callback_query, "That preset no longer exists.")
        return
    _QADD_PRESET_CHOICE[int(user_id)] = {
        "preset": name,
        "chat_id": _effective_chat_id(update),
    }
    # Keep the slot alive but update the stored preset so the next text
    # message uses it. If no slot exists we re-arm one.
    pending = _awaiting_user(user_id)
    if pending and pending.get("field") == QUICK_ADD_FIELD:
        pending["preset"] = name
    else:
        _set_awaiting(
            user_id,
            {
                "token": "",
                "field": QUICK_ADD_FIELD,
                "preset": name,
                "chat_id": _effective_chat_id(update),
            },
        )
    text, markup = _quick_add_main_screen(name)
    await _show(update, text, markup)


async def _handle_quick_add_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: dict[str, Any],
    raw: str,
) -> None:
    """Receive the one-message paste, parse, open a session, show review."""
    user_id = update.effective_user.id
    msg = update.effective_message
    preset_name = str(pending.get("preset") or "").strip()
    preset = mb_quick.preset_get(preset_name) if preset_name else None
    # Clear the slot UP FRONT so a parse error does not keep the admin
    # trapped — they can retry via the quick-add button.
    _clear_awaiting(user_id)
    try:
        result = mb_quick.parse_quick_add_text(
            raw,
            preset=preset,
            taken_ids=_collect_taken_account_ids(),
        )
    except mb_quick.QuickAddError as exc:
        # redact_text in case a parse echoes anything from the message.
        safe = redact_text(str(exc))[:600]
        await msg.reply_text(
            f"Quick Add refused: {safe}\n\nTap Quick Add Email to try again."
        )
        return

    # Open a session and store ALL non-secret fields in one call. The
    # existing per-field validation runs inside set_fields / arm; adding
    # them atomically means the admin only sees the Review screen next.
    try:
        session = os_mod.start_session(user_id, mode=os_mod.MODE_CLI)
    except os_mod.SessionError as exc:
        await msg.reply_text(f"Could not open onboarding session: {exc}")
        return
    try:
        session = os_mod.set_fields(
            session.token,
            user_id,
            nonce=session.nonce,
            patch=result.fields,
        )
    except os_mod.SessionError as exc:
        # Roll back the just-opened session so the rate-limit counter is
        # not consumed by a parse that we already know is unusable.
        try:
            os_mod.cancel(session.token, user_id, nonce=session.nonce)
        except os_mod.SessionError:
            pass
        await msg.reply_text(
            f"Could not store those fields: {redact_text(str(exc))[:400]}"
        )
        return
    mailbox_admin.record(
        user_id,
        "onboard_start",
        str(session.fields.get("account_id") or ""),
        f"mode=cli provider=quick preset={preset_name or '-'}",
    )
    text, markup = _quick_review_screen(session, result, preset_name)
    await msg.reply_text(text, reply_markup=markup)


async def _reset_quick_session(
    update: Update, token: str, nonce: str
) -> None:
    """Edit = reopen the paste prompt and clear every stored field."""
    user_id = update.effective_user.id
    try:
        session = os_mod.get_session(token, user_id)
        patch = {key: "" for key, _l, _h in STEPS}
        session = os_mod.set_fields(
            token, user_id, nonce=session.nonce, patch=patch
        )
    except os_mod.SessionError as exc:
        await _show(update, redact_text(str(exc))[:300], _markup([[("Back", f"{PREFIX}m")]]))
        return
    # Cancel the empty session rather than leave it dangling — the admin
    # will re-paste and we open a fresh one. This keeps rate limits clean.
    try:
        os_mod.cancel(token, user_id, nonce=session.nonce)
    except os_mod.SessionError:
        pass
    await _open_quick_add(update, None)  # type: ignore[arg-type]


async def _save_preset_from_session(
    update: Update, token: str, nonce: str
) -> None:
    user_id = update.effective_user.id
    try:
        session = os_mod.get_session(token, user_id)
    except os_mod.SessionError as exc:
        await _show(update, str(exc), _markup([[("Back", f"{PREFIX}m")]]))
        return
    # Only valid while the session is still a draft (fields may still change).
    if session.state != os_mod.STATE_DRAFT:
        await _notice(update.callback_query, "Session already past the draft step.")
        return
    # Prime a text-input slot that captures the preset name.
    installed = _set_awaiting(
        user_id,
        {
            "token": token,
            "field": "__quick_preset_name__",
            "nonce": session.nonce,
            "chat_id": _effective_chat_id(update),
        },
    )
    if not installed:
        await _notice(update.callback_query, "Finish the pending password capture first.")
        return
    await _show(
        update,
        "Save server as preset\n\n"
        "Send ONE message with a short name (letters, digits, '-', '_', '.' or space).\n"
        "The preset will remember IMAP/SMTP host and port only — no credentials.\n"
        "Send 'cancel' to abort.",
        _markup([
            [("Back", f"{PREFIX}qadd:back_to_review:" + token)],
        ]),
    )


async def _handle_quick_preset_name_text(
    update: Update, pending: dict[str, Any], raw: str
) -> None:
    user_id = update.effective_user.id
    msg = update.effective_message
    token = str(pending.get("token") or "")
    _clear_awaiting(user_id)
    if raw.strip().lower() == "cancel":
        await msg.reply_text("Cancelled. Nothing was saved.")
        return
    try:
        session = os_mod.get_session(token, user_id)
    except os_mod.SessionError as exc:
        await msg.reply_text(f"Session is no longer valid: {exc}")
        return
    try:
        mb_quick.preset_save(
            raw.strip(),
            {
                "imap_host": session.fields.get("imap_host"),
                "imap_port": session.fields.get("imap_port"),
                "smtp_host": session.fields.get("smtp_host"),
                "smtp_port": session.fields.get("smtp_port"),
            },
        )
    except mb_quick.QuickAddError as exc:
        await msg.reply_text(f"Preset refused: {exc}")
        return
    mailbox_admin.record(user_id, "preset_save", "", f"name={raw.strip()[:32]}")
    # Return to the review screen so the admin can still Confirm/Edit/Cancel.
    await msg.reply_text(f"Preset '{raw.strip()}' saved.")
    # Replay a review-like screen (we don't still have the QuickAddResult;
    # just show a plain review with no extra buttons beyond the standard
    # three).
    expiry_min = max(1, int((session.expires_at - os_mod._now()) // 60))
    summary = _session_summary(session.fields)
    text = (
        "Review the new mailbox (Quick Add)\n\n"
        + summary
        + f"\n\nSession expires in {expiry_min} min. "
        "Confirm to continue to password capture in your private DM."
    )
    rows = [
        [("Confirm", f"{PREFIX}arm:{session.token}:{session.nonce}")],
        [("Edit", f"{PREFIX}qadd:edit:{session.token}:{session.nonce}")],
        [("Cancel", f"{PREFIX}cancel:{session.token}:{session.nonce}")],
    ]
    await msg.reply_text(text, reply_markup=_markup(rows))


async def _back_to_review(update: Update, token: str) -> None:
    user_id = update.effective_user.id
    _clear_awaiting(user_id)
    try:
        session = os_mod.get_session(token, user_id)
    except os_mod.SessionError as exc:
        await _show(update, str(exc), _markup([[("Back", f"{PREFIX}m")]]))
        return
    await _show_session(update, session)


# ---- DM-mode password capture (opt-in) -------------------------------------

def _dm_capture_allowed() -> bool:
    import os as _os

    return (_os.environ.get("MAILOPS_ALLOW_DM_CREDENTIALS") or "").strip() == "1"


async def _arm_dm_prompt(update: Update, token: str, nonce: str) -> None:
    user_id = update.effective_user.id
    if not _dm_capture_allowed():
        await _show(
            update,
            "DM password capture is disabled on this server. "
            "Run the webmail-run-pending command on the Mailops host instead.",
            _markup([[("Back", f"{PREFIX}poll:" + token)]]),
        )
        return
    try:
        session = os_mod.get_session(token, user_id)
    except os_mod.SessionError as exc:
        await _show(update, str(exc), _markup([[("Back", f"{PREFIX}m")]]))
        return
    if session.state != os_mod.STATE_AWAITING_CREDS:
        await _show_session(update, session)
        return
    # Bind the DM capture to the EXACT nonce active at arm time. If the
    # session nonce rotates (e.g. the admin cancels and re-arms the same
    # token), handle_text will refuse to use a stale slot.
    _force_set_awaiting(
        user_id,
        {
            "token": token,
            "field": _DM_PW_FIELD,
            "nonce": session.nonce,
            "armed_at": time.time(),
            "account_id": str(session.fields.get("account_id") or ""),
            "email": str(session.fields.get("email") or ""),
            # DM-mode is PRIVATE-ONLY by construction (the arm button is
            # only reachable from a private chat and _arm_dm_prompt refuses
            # in group). Still pin chat_id so handle_text refuses any text
            # arriving from somewhere else.
            "chat_id": _effective_chat_id(update),
        },
    )
    aid = session.fields.get("account_id") or "?"
    email = session.fields.get("email") or "?"
    await update.effective_message.reply_text(
        "Password capture armed for "
        f"mailbox '{aid}' <{email}>.\n\n"
        "⚠ Important before you send:\n"
        "• Use a dedicated APP PASSWORD generated by your mail provider "
        "(e.g. Hostinger → Email Accounts → the mailbox → Connect Apps / "
        "'App password'). Do NOT paste your regular mailbox login "
        "password; if a password leaks, you can revoke only the app "
        "password without changing your mailbox login.\n"
        "• Telegram's servers process the message before Mailops can "
        "delete it. Any device that received the message before "
        "deletion keeps a local copy. Push notifications may have "
        "already shown the password preview.\n"
        "• Message deletion is best-effort. If Mailops cannot delete "
        "it, you must delete it manually from this chat.\n"
        "• You have 60 seconds. Send 'cancel' to abort, or send the "
        "app password as your very next message.",
    )


async def _handle_dm_password(
    update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict[str, Any], raw: str
) -> None:
    """Receive and consume a DM-mode password send.

    ``pending`` is the AWAITING slot (contains token, nonce, armed_at,
    account_id). We check it here instead of in the caller so the race
    window stays small.
    """
    user_id = update.effective_user.id
    msg = update.effective_message
    token = str(pending.get("token") or "")
    armed_nonce = str(pending.get("nonce") or "")
    armed_at = float(pending.get("armed_at") or 0)
    aid_hint = str(pending.get("account_id") or "")
    # Clear the slot ASAP so a second concurrent message cannot double-fire
    # into add_account.
    _clear_awaiting(user_id)

    # Best-effort: delete the password message right away so it does not
    # sit in Telegram chat history on the admin's device.
    delete_ok = False
    try:
        await msg.delete()
        delete_ok = True
    except Exception as exc:  # noqa: BLE001
        log.warning("DM password: could not delete admin message (%s)", type(exc).__name__)

    stripped = raw.strip()
    # 'cancel' aborts cleanly. Any further validation is below.
    if stripped.lower() == "cancel":
        if not delete_ok:
            await msg.reply_text(
                "⚠ Mailops could not delete your message. Please delete it manually.\n"
                "DM password capture cancelled. No validation ran."
            )
        else:
            await msg.reply_text("DM password capture cancelled. No validation ran.")
        return

    # Enforce the short arm window: any text after DM_ARM_TTL_S is refused.
    now = time.time()
    if armed_at and (now - armed_at) > DM_ARM_TTL_S:
        warn = (
            "That password was received after the 60-second window closed; "
            "it was NOT used. Open Mailboxes → the pending session → "
            "'Send password in this chat' again to arm a fresh window."
        )
        if not delete_ok:
            warn = "⚠ Mailops could not delete your message. Please delete it manually.\n" + warn
        await msg.reply_text(warn)
        return

    # Re-verify the session is still awaiting_credentials with the SAME
    # nonce that was active at arm time. If the nonce has rotated (because
    # the admin cancelled and re-armed, or the session advanced from a
    # different device) we refuse rather than consume the password against
    # a possibly-different target.
    try:
        session = os_mod.get_session(token, user_id)
    except os_mod.SessionError as exc:
        notice = f"That onboarding session is no longer valid: {exc}"
        if not delete_ok:
            notice = "⚠ Mailops could not delete your message. Please delete it manually.\n" + notice
        await msg.reply_text(notice)
        return
    if session.state != os_mod.STATE_AWAITING_CREDS or session.nonce != armed_nonce:
        notice = (
            "The onboarding session has changed since you tapped 'Send "
            "password in this chat'. Your password was NOT used. Open "
            "Mailboxes and arm the capture again."
        )
        if not delete_ok:
            notice = "⚠ Mailops could not delete your message. Please delete it manually.\n" + notice
        await msg.reply_text(notice)
        return

    # Finally: run the blocking validation off-thread so Telegram polling
    # stays responsive and the password string is held in a short-lived
    # worker stack frame rather than the handler frame.
    pw_hint = aid_hint or session.fields.get("account_id") or "?"
    try:
        from .. import webmail_onboarding as wm

        result = await asyncio.to_thread(
            wm.run_pending, token, imap_password=raw
        )
    except Exception as exc:  # noqa: BLE001 - presented safely below
        reason = str(exc)[:400]
        prefix = ""
        if not delete_ok:
            prefix = "⚠ Mailops could not delete your message. Please delete it manually.\n"
        await msg.reply_text(
            prefix
            + "Validation failed and was rolled back.\n\n"
            f"Reason: {reason}\n\nNothing was registered.",
            reply_markup=_markup([[("Back", f"{PREFIX}m")]]),
        )
        return
    finally:
        # Drop every local reference to the plaintext. CPython string
        # interning may still hold a copy; the real guarantee is that
        # add_account's rollback deletes stored secrets on failure.
        raw = ""

    try:
        session = os_mod.get_session(token, user_id)
    except os_mod.SessionError:
        session = None

    if not delete_ok:
        await msg.reply_text(
            "⚠ Mailops could not delete your password message. Please "
            "delete it manually from this chat."
        )

    if session is None:
        await msg.reply_text(
            f"Validation finished for '{pw_hint}' but the session could "
            "not be re-loaded.",
            reply_markup=_markup([[("Back", f"{PREFIX}m")]]),
        )
        return
    await _show_session(update, session)


# ---- Group-mode password capture (opt-in, owner-only, single pinned group) -

async def _arm_group_pw_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, token: str, nonce: str
) -> None:
    """Opt-in, owner-only, single-group password capture.

    Guarded by:
      * ``group_credentials.allowed_for_group`` - the feature flag AND an
        exact pinned group id, both explicit env vars (disabled by default).
      * ``access.is_owner`` - only the two authorized owners, never any
        operator with plain ``P_ADMIN``.
      * the existing onboarding_session token/nonce/user_id binding.

    Telegram privacy mode: with privacy mode ON (unchanged - this feature
    does NOT require disabling it or re-adding the bot), the bot still
    receives any message that is a REPLY to one of its own messages; this
    is documented, mode-independent Telegram Bot API behaviour. We
    therefore send a NEW prompt message and require the admin to reply
    directly to it. A bare (non-reply) message typed in the group may
    never reach the bot at all under privacy mode, so we never accept one
    as a password - only a reply bound to this exact prompt message id.
    """
    user_id = update.effective_user.id
    chat_id = _effective_chat_id(update)
    if not gc.allowed_for_group(chat_id):
        await _notice(update.callback_query, "Group password entry is not enabled for this group.")
        return
    if not access.is_owner(user_id):
        await _notice(update.callback_query, "Only an owner may use in-group password entry.")
        return
    try:
        session = os_mod.get_session(token, user_id)
    except os_mod.SessionError as exc:
        await _show(update, str(exc), _markup([[("Back", f"{PREFIX}m")]]))
        return
    if session.state != os_mod.STATE_AWAITING_CREDS or session.nonce != nonce:
        await _show_session(update, session)
        return

    aid = session.fields.get("account_id") or "?"
    email = session.fields.get("email") or "?"
    warning = (
        "GROUP PASSWORD ENTRY (advanced, owners only)\n\n"
        f"Mailbox '{aid}' <{email}>.\n\n"
        "REPLY DIRECTLY to THIS message (use Telegram's Reply action, do "
        "not just type) with the mailbox password as your very next "
        "message. This is required: a plain message typed in this group "
        "may never reach the bot under Telegram privacy mode, but a reply "
        "to the bot's own message always does.\n\n"
        "Use a dedicated APP PASSWORD if your provider supports one.\n\n"
        + gc.RETENTION_WARNING + "\n\n"
        "Mailops will try to delete your reply the instant it is received, "
        "BEFORE showing any confirmation or starting validation. If "
        "deletion fails, onboarding stops immediately, the captured "
        "password is discarded, and you must delete the message yourself.\n\n"
        f"You have {gc.ARM_TTL_S} seconds. Reply 'cancel' to abort."
    )
    try:
        sent = await context.bot.send_message(chat_id=chat_id, text=warning)
    except TelegramError as exc:
        log.warning("could not post group password prompt: %s", exc)
        await _notice(
            update.callback_query,
            "Could not post the group prompt. Use the private DM option instead.",
        )
        return
    _force_set_awaiting(
        user_id,
        {
            "token": token,
            "field": _GROUP_PW_FIELD,
            "nonce": session.nonce,
            "armed_at": time.time(),
            "account_id": str(session.fields.get("account_id") or ""),
            "email": str(session.fields.get("email") or ""),
            "chat_id": chat_id,
            "prompt_message_id": getattr(sent, "message_id", None),
        },
    )
    if update.callback_query is not None:
        try:
            await update.callback_query.answer("Prompt posted below. Reply to it with the password.")
        except Exception:  # noqa: BLE001
            pass


async def _handle_group_password(
    update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict[str, Any], raw: str
) -> None:
    """Receive and consume a group-mode password reply.

    Stricter than the DM path on one point, per the explicit security
    requirement for this feature: if the password message cannot be
    deleted, onboarding STOPS and the session is invalidated rather than
    continuing with a warning (the DM path continues; the group path does
    not, because the exposure surface - every group member, not just the
    admin's own devices - is larger).
    """
    user_id = update.effective_user.id
    msg = update.effective_message
    token = str(pending.get("token") or "")
    armed_nonce = str(pending.get("nonce") or "")
    armed_at = float(pending.get("armed_at") or 0)
    aid_hint = str(pending.get("account_id") or "")
    chat_id = pending.get("chat_id")
    prompt_message_id = pending.get("prompt_message_id")
    # Clear the slot ASAP so a second concurrent reply cannot double-fire.
    _clear_awaiting(user_id)

    async def _group_notice(text: str) -> None:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception:  # noqa: BLE001
            pass

    # Re-verify the feature is still enabled for this exact group and the
    # sender is still an owner - config or roles may have changed between
    # arming and the reply arriving.
    if not gc.allowed_for_group(chat_id) or not access.is_owner(user_id):
        await _group_notice(
            "Group password entry is no longer enabled for this group or "
            "admin. Nothing was used."
        )
        return

    # Privacy-mode-compatible binding: this MUST be a reply to our own
    # prompt message. We only reach this function at all when Telegram
    # delivered the message to us, but we still refuse to treat anything
    # other than the exact expected reply as a password.
    reply_to = getattr(msg, "reply_to_message", None)
    reply_id = getattr(reply_to, "message_id", None) if reply_to is not None else None
    if reply_to is None or prompt_message_id is None or reply_id != prompt_message_id:
        await _group_notice(
            "That message was not recognised as a reply to the password "
            "prompt, so it was NOT used as a password. If you still need "
            "to onboard this mailbox, reopen the pending session and tap "
            "'Enter password in this group' again, then use Telegram's "
            "Reply action on the new prompt."
        )
        return

    # Immediately request deletion - BEFORE any confirmation text and
    # BEFORE the (potentially slow) IMAP/SMTP validation call.
    delete_ok = False
    try:
        await msg.delete()
        delete_ok = True
    except Exception as exc:  # noqa: BLE001
        log.warning("group password: could not delete message (%s)", type(exc).__name__)

    if not delete_ok:
        raw = ""
        try:
            os_mod.cancel(token, user_id)
        except os_mod.SessionError:
            pass
        await _group_notice(
            "Mailops could NOT delete your password reply in this group. "
            "Onboarding has been STOPPED and the captured password was "
            "discarded for safety - it was never used. Please delete your "
            "message manually right now, then restart onboarding and use "
            "the private DM method instead."
        )
        return

    stripped = raw.strip()
    if stripped.lower() == "cancel":
        raw = ""
        await _group_notice("Group password capture cancelled. No validation ran.")
        return

    now = time.time()
    if armed_at and (now - armed_at) > gc.ARM_TTL_S:
        raw = ""
        await _group_notice(
            "That reply arrived after the short capture window closed; it "
            "was NOT used. Reopen the pending mailbox and tap 'Enter "
            "password in this group' again."
        )
        return

    try:
        session = os_mod.get_session(token, user_id)
    except os_mod.SessionError as exc:
        raw = ""
        await _group_notice(f"That onboarding session is no longer valid: {exc}")
        return
    if session.state != os_mod.STATE_AWAITING_CREDS or session.nonce != armed_nonce:
        raw = ""
        await _group_notice(
            "The onboarding session changed since you armed group password "
            "entry. Your password was NOT used. Reopen the pending mailbox "
            "and try again."
        )
        return

    pw_hint = aid_hint or session.fields.get("account_id") or "?"
    try:
        from .. import webmail_onboarding as wm

        result = await asyncio.to_thread(wm.run_pending, token, imap_password=raw)
    except Exception as exc:  # noqa: BLE001
        reason = redact_text(str(exc))[:400]
        await _group_notice(
            "Validation failed and was rolled back.\n\n"
            f"Reason: {reason}\n\nNothing was registered."
        )
        return
    finally:
        raw = ""

    try:
        session = os_mod.get_session(token, user_id)
    except os_mod.SessionError:
        session = None
    if session is None:
        await _group_notice(
            f"Validation finished for '{pw_hint}' but the session could "
            "not be re-loaded."
        )
        return
    text, markup = session_screen(session)
    try:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    except Exception:  # noqa: BLE001
        pass


# ---- Smart Add Email (Phase 5) ---------------------------------------------
#
# Email-first onboarding: the admin sends ONE email address and Mailops
# resolves the IMAP/SMTP server through ``mailops.server_resolution``
# (verified reuse -> saved preset -> reliable provider default -> opt-in
# MX suggestion -> ask), labels the match VERIFIED / SUGGESTED / UNKNOWN,
# and only asks for anything else when no reliable match exists. Once the
# non-secret fields are complete this reuses the EXACT SAME onboarding
# session / review / arm / credential-capture machinery as the field-by-
# field wizard and Quick Add — nothing about credential handling changes.

# token -> {"status": str, "source": str, "preset_name": str}. Display-only
# metadata that is not part of the onboarding session's own field set (the
# session only ever stores WebmailSpec-shaped fields); losing this cache on
# a restart just means the review card falls back to a generic label, it
# never blocks or corrupts onboarding.
_SMART_RESOLUTION_CACHE: dict[str, dict[str, str]] = {}


def _smart_cache_set(token: str, *, status: str, source: str, preset_name: str = "") -> None:
    _SMART_RESOLUTION_CACHE[token] = {
        "status": status, "source": source, "preset_name": preset_name,
    }


def _smart_cache_get(token: str) -> dict[str, str]:
    return _SMART_RESOLUTION_CACHE.get(token) or {
        "status": server_resolution.STATUS_UNKNOWN, "source": "", "preset_name": "",
    }


def _smart_intro_screen() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "Smart Add Email\n\n"
        "Send the mailbox's email address as your next message, e.g. "
        "info@coinography.com.\n\n"
        "Mailops will try to recognise the mail server automatically — "
        "from an already-verified mailbox on the same domain, a saved "
        "preset, or a known provider — and show you exactly which case "
        "matched before anything is saved. If nothing reliable is found "
        "you will be asked for the missing details once, and can save "
        "them as a reusable preset.\n\n"
        "Send 'cancel' to abort."
    )
    rows: list[list[tuple[str, str]]] = [[("Cancel", f"{PREFIX}m")]]
    return text, _markup(rows)


async def _open_smart_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text, markup = _smart_intro_screen()
    installed = _set_awaiting(
        user_id,
        {"token": "", "field": _SMART_EMAIL_FIELD, "chat_id": _effective_chat_id(update)},
    )
    if not installed:
        await _notice(update.callback_query, "Finish the pending password capture first.")
        return
    await _show(update, text, markup)


def _smart_review_screen(session: os_mod.OnboardingSession) -> tuple[str, InlineKeyboardMarkup]:
    meta = _smart_cache_get(session.token)
    status = str(meta.get("status") or server_resolution.STATUS_UNKNOWN)
    source = str(meta.get("source") or "")
    preset_name = str(meta.get("preset_name") or "")
    label = _SMART_STATUS_LABELS.get(status, status.upper())
    expiry_min = max(1, int((session.expires_at - os_mod._now()) // 60))
    lines = [
        "Smart Add Email — review",
        "",
        f"Server match: {label}" + (f"  (preset: {preset_name})" if preset_name else ""),
    ]
    if source:
        lines.append(source)
    lines.append("")
    lines.append(_session_summary(session.fields))
    lines.append("")
    lines.append(
        f"Session expires in {expiry_min} min. Confirm to continue to "
        "credential capture. Nothing has been sent yet."
    )
    rows: list[list[tuple[str, str]]] = [
        [("Confirm & arm credentials", f"{PREFIX}arm:{session.token}:{session.nonce}")],
        [("Change server", f"{PREFIX}smart:change:{session.token}:{session.nonce}")],
    ]
    if not preset_name:
        rows.append([("Save server as preset", f"{PREFIX}qadd:save_preset:{session.token}:{session.nonce}")])
    rows.append([("Cancel", f"{PREFIX}cancel:{session.token}:{session.nonce}")])
    return "\n".join(lines), _markup(rows)


def _smart_provider_pick_screen(session: os_mod.OnboardingSession) -> tuple[str, InlineKeyboardMarkup]:
    email = str(session.fields.get("email") or "?")
    lines = [
        "Smart Add Email",
        "",
        f"Email: {email}",
        "",
        "No verified or reliable server match was found for this domain. "
        "Pick the provider, or Custom to enter IMAP/SMTP details yourself.",
        "",
        "• Hostinger — IMAP/SMTP filled in automatically.",
        "• SiteGround — ports filled in (993/465); you supply the server "
        "hostname once, used for BOTH IMAP and SMTP (never guessed from "
        "the domain or MX record).",
        "• Custom — enter IMAP + SMTP host and port yourself.",
    ]
    rows: list[list[tuple[str, str]]] = []
    presets = mb_quick.presets_list()
    for name, _blob in presets[:6]:
        rows.append([(f"Preset: {name}", f"{PREFIX}smart:pickpreset:{name}:{session.token}:{session.nonce}")])
    rows.append([("Hostinger", f"{PREFIX}smart:pick:hostinger:{session.token}:{session.nonce}")])
    rows.append([("SiteGround", f"{PREFIX}smart:pick:siteground:{session.token}:{session.nonce}")])
    rows.append([("Custom IMAP + SMTP", f"{PREFIX}smart:pick:custom:{session.token}:{session.nonce}")])
    rows.append([("Cancel", f"{PREFIX}cancel:{session.token}:{session.nonce}")])
    return "\n".join(lines), _markup(rows)


async def _handle_smart_add_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict[str, Any], raw: str
) -> None:
    """First Smart Add message: the email address only."""
    user_id = update.effective_user.id
    msg = update.effective_message
    _clear_awaiting(user_id)
    stripped = raw.strip()
    if stripped.lower() == "cancel":
        await msg.reply_text("Cancelled.")
        return
    from .. import webmail_onboarding as wm

    email = stripped
    if not wm._EMAIL_RE.match(email):
        await msg.reply_text(
            "That email address looks invalid. Send it again, e.g. "
            "info@example.com, or 'cancel' to abort."
        )
        _set_awaiting(
            user_id,
            {"token": "", "field": _SMART_EMAIL_FIELD, "chat_id": _effective_chat_id(update)},
        )
        return

    resolution = server_resolution.resolve_for_email(email)
    try:
        session = os_mod.start_session(user_id, mode=os_mod.MODE_CLI)
    except os_mod.SessionError as exc:
        await msg.reply_text(f"Could not open onboarding session: {exc}")
        return
    try:
        account_id = mb_quick.derive_account_id(email, _collect_taken_account_ids())
    except mb_quick.QuickAddError as exc:
        try:
            os_mod.cancel(session.token, user_id, nonce=session.nonce)
        except os_mod.SessionError:
            pass
        await msg.reply_text(f"Could not derive an account id: {exc}")
        return
    patch: dict[str, Any] = {"account_id": account_id, "email": email}
    if resolution.imap_port:
        patch["imap_port"] = resolution.imap_port
    if resolution.smtp_port:
        patch["smtp_port"] = resolution.smtp_port
    if resolution.complete:
        patch["imap_host"] = resolution.imap_host
        patch["smtp_host"] = resolution.smtp_host
    try:
        session = os_mod.set_fields(session.token, user_id, nonce=session.nonce, patch=patch)
    except os_mod.SessionError as exc:
        try:
            os_mod.cancel(session.token, user_id, nonce=session.nonce)
        except os_mod.SessionError:
            pass
        await msg.reply_text(f"Could not store those fields: {redact_text(str(exc))[:300]}")
        return
    mailbox_admin.record(user_id, "onboard_start", account_id, "mode=cli provider=smart")
    _smart_cache_set(
        session.token, status=resolution.status, source=resolution.source,
        preset_name=resolution.preset_name,
    )
    if resolution.complete:
        text, markup = _smart_review_screen(session)
    else:
        text, markup = _smart_provider_pick_screen(session)
    await msg.reply_text(text, reply_markup=markup)


async def _smart_pick_provider(
    update: Update, context: ContextTypes.DEFAULT_TYPE, provider: str, token: str, nonce: str
) -> None:
    user_id = update.effective_user.id
    try:
        session = os_mod.get_session(token, user_id)
    except os_mod.SessionError as exc:
        await _show(update, redact_text(str(exc))[:300], _markup([[("Back", f"{PREFIX}m")]]))
        return
    if session.state != os_mod.STATE_DRAFT or session.nonce != nonce:
        await _show_session(update, session)
        return
    if provider not in ("hostinger", "siteground", "custom"):
        await _notice(update.callback_query, "Unknown provider.")
        return
    email = str(session.fields.get("email") or "")

    if provider == "custom":
        # Hand off to the existing field-by-field wizard. account_id/email
        # are already filled, so the next pending step is imap_host — the
        # admin is only ever asked for what Smart Add could not resolve.
        text, markup = _wizard_screen(session)
        await _show(update, text, markup)
        nxt = _next_pending_step(session.fields)
        if nxt is not None:
            _set_awaiting(
                user_id,
                {"token": session.token, "field": nxt[0], "chat_id": _effective_chat_id(update)},
            )
        return

    resolution = server_resolution.resolve_for_email(email, provider_hint=provider)
    patch: dict[str, Any] = {}
    if resolution.imap_port:
        patch["imap_port"] = resolution.imap_port
    if resolution.smtp_port:
        patch["smtp_port"] = resolution.smtp_port
    if resolution.complete:
        patch["imap_host"] = resolution.imap_host
        patch["smtp_host"] = resolution.smtp_host
    try:
        session = os_mod.set_fields(token, user_id, nonce=session.nonce, patch=patch)
    except os_mod.SessionError as exc:
        await _show(update, redact_text(str(exc))[:300], _markup([[("Back", f"{PREFIX}m")]]))
        return
    _smart_cache_set(token, status=resolution.status, source=resolution.source)

    if resolution.complete:
        text, markup = _smart_review_screen(session)
        await _show(update, text, markup)
        return

    # SiteGround (or any provider that still needs a hostname): ask ONCE,
    # applied to both IMAP and SMTP — never a separate prompt for each.
    installed = _set_awaiting(
        user_id,
        {
            "token": session.token, "field": _SMART_HOST_FIELD,
            "nonce": session.nonce, "chat_id": _effective_chat_id(update),
        },
    )
    if not installed:
        await _notice(update.callback_query, "Finish the pending password capture first.")
        return
    await _show(
        update,
        "Enter the mail server hostname (used for BOTH IMAP and SMTP), "
        "e.g. gcam1108.siteground.biz. Check your hosting provider's "
        "welcome email or control panel — it is never guessed from the "
        "domain or an MX record.\n\nSend 'cancel' to abort.",
        _markup([[("Cancel", f"{PREFIX}cancel:{session.token}:{session.nonce}")]]),
    )


async def _smart_pick_preset(update: Update, name: str, token: str, nonce: str) -> None:
    user_id = update.effective_user.id
    try:
        session = os_mod.get_session(token, user_id)
    except os_mod.SessionError as exc:
        await _show(update, redact_text(str(exc))[:300], _markup([[("Back", f"{PREFIX}m")]]))
        return
    if session.state != os_mod.STATE_DRAFT or session.nonce != nonce:
        await _show_session(update, session)
        return
    blob = mb_quick.preset_get(name)
    if blob is None:
        await _notice(update.callback_query, "That preset no longer exists.")
        text, markup = _smart_provider_pick_screen(session)
        await _show(update, text, markup)
        return
    patch = {
        "imap_host": blob.get("imap_host"), "imap_port": blob.get("imap_port"),
        "smtp_host": blob.get("smtp_host"), "smtp_port": blob.get("smtp_port"),
    }
    if blob.get("imap_user"):
        patch["imap_user"] = blob["imap_user"]
    if blob.get("smtp_user"):
        patch["smtp_user"] = blob["smtp_user"]
    try:
        session = os_mod.set_fields(token, user_id, nonce=session.nonce, patch=patch)
    except os_mod.SessionError as exc:
        await _show(update, redact_text(str(exc))[:300], _markup([[("Back", f"{PREFIX}m")]]))
        return
    _smart_cache_set(
        token, status=server_resolution.STATUS_VERIFIED,
        source=f"Saved preset '{name}' selected by you.", preset_name=name,
    )
    text, markup = _smart_review_screen(session)
    await _show(update, text, markup)


async def _smart_change_server(update: Update, token: str, nonce: str) -> None:
    user_id = update.effective_user.id
    try:
        session = os_mod.get_session(token, user_id)
    except os_mod.SessionError as exc:
        await _show(update, redact_text(str(exc))[:300], _markup([[("Back", f"{PREFIX}m")]]))
        return
    if session.state != os_mod.STATE_DRAFT or session.nonce != nonce:
        await _show_session(update, session)
        return
    try:
        session = os_mod.set_fields(
            token, user_id, nonce=session.nonce,
            patch={
                "imap_host": "", "imap_user": "",
                "smtp_host": "", "smtp_user": "",
            },
        )
    except os_mod.SessionError as exc:
        await _show(update, redact_text(str(exc))[:300], _markup([[("Back", f"{PREFIX}m")]]))
        return
    _SMART_RESOLUTION_CACHE.pop(token, None)
    text, markup = _smart_provider_pick_screen(session)
    await _show(update, text, markup)


async def _handle_smart_host_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict[str, Any], raw: str
) -> None:
    """Second (optional) Smart Add message: ONE hostname for a provider
    (SiteGround) whose server varies per tenant. Applied to both IMAP and
    SMTP — SiteGround uses the same host for both, only the ports differ,
    and the admin should never have to type the same hostname twice."""
    user_id = update.effective_user.id
    msg = update.effective_message
    token = str(pending.get("token") or "")
    armed_nonce = str(pending.get("nonce") or "")
    _clear_awaiting(user_id)
    stripped = raw.strip()
    if stripped.lower() == "cancel":
        await msg.reply_text("Cancelled.")
        return
    from .. import webmail_onboarding as wm

    host = stripped.lower()
    portal_problem = wm.reject_webmail_portal_url(host, label="mail server host")
    if portal_problem:
        await msg.reply_text(portal_problem + "\n\nSend the hostname again, or 'cancel' to abort.")
        _set_awaiting(user_id, dict(pending, chat_id=_effective_chat_id(update)))
        return
    if not wm._HOST_RE.match(host):
        await msg.reply_text(
            "That does not look like a plain hostname (e.g. mail.example.com). "
            "Send it again, or 'cancel' to abort."
        )
        _set_awaiting(user_id, dict(pending, chat_id=_effective_chat_id(update)))
        return
    try:
        session = os_mod.get_session(token, user_id)
    except os_mod.SessionError as exc:
        await msg.reply_text(f"Session is no longer valid: {exc}")
        return
    if session.state != os_mod.STATE_DRAFT or (armed_nonce and session.nonce != armed_nonce):
        await msg.reply_text("This onboarding session changed. Reopen Smart Add Email.")
        return
    try:
        session = os_mod.set_fields(
            token, user_id, nonce=session.nonce,
            patch={"imap_host": host, "smtp_host": host},
        )
    except os_mod.SessionError as exc:
        await msg.reply_text(f"Could not store that hostname: {redact_text(str(exc))[:300]}")
        return
    meta = _smart_cache_get(token)
    _smart_cache_set(
        token,
        status=server_resolution.STATUS_UNKNOWN,
        source=(meta.get("source") or "")
        + " You entered this hostname manually; it becomes a VERIFIED match "
        "for the next mailbox on this same domain once this one validates "
        "successfully.",
    )
    text, markup = _smart_review_screen(session)
    await msg.reply_text(text, reply_markup=markup)


# ---- Mailbox lifecycle (non-onboarding) ------------------------------------


async def _do_enable(update: Update, account_id: str) -> None:
    user_id = update.effective_user.id
    try:
        mailbox_admin.enable_mailbox(user_id, account_id)
    except mailbox_admin.MailboxAdminError as exc:
        await _show(
            update,
            str(exc),
            _markup([[("Back", f"{PREFIX}view:" + account_id)]]),
        )
        return
    await _open_view(update, account_id)


async def _do_disable(update: Update, account_id: str) -> None:
    user_id = update.effective_user.id
    try:
        mailbox_admin.disable_mailbox(user_id, account_id)
    except mailbox_admin.MailboxAdminError as exc:
        await _show(
            update,
            str(exc),
            _markup([[("Back", f"{PREFIX}view:" + account_id)]]),
        )
        return
    await _open_view(update, account_id)


async def _do_health(update: Update, account_id: str) -> None:
    try:
        status = mailbox_admin.health_check(account_id)
    except mailbox_admin.MailboxAdminError as exc:
        await _show(
            update,
            str(exc),
            _markup([[("Back", f"{PREFIX}view:" + account_id)]]),
        )
        return
    tag = "OK" if status.get("ok") else "FAIL"
    text = (
        f"Health check · {account_id}\n\n"
        f"IMAP: {'OK' if status.get('imap') else 'FAIL'}\n"
        f"SMTP: {'OK' if status.get('smtp') else 'FAIL'}\n"
        f"Overall: {tag}\n"
    )
    detail = (status.get("detail") or "").strip()
    if detail:
        text += "\nDetail: " + detail[:400]
    await _show(update, text, _markup([[("Back", f"{PREFIX}view:" + account_id)]]))


async def _do_purpose_prompt(update: Update, account_id: str) -> None:
    user_id = update.effective_user.id
    current = mailbox_admin.get_purpose(account_id)
    installed = _set_awaiting(
        user_id,
        {
            "token": "",
            "field": _PURPOSE_FIELD,
            "account_id": account_id,
            "armed_at": time.time(),
            # Purpose editing is a PRIVATE-only screen (gated by
            # _group_safe_body); pin chat_id so a group-bound slot can
            # never masquerade as a purpose capture.
            "chat_id": _effective_chat_id(update),
        },
    )
    if not installed:
        # A DM password capture is pending — refuse to accept purpose text
        # as the next message, which would otherwise persist the password
        # into settings.json as a brand description.
        await _show(
            update,
            "A password capture is already armed for this chat. Finish or "
            "send 'cancel' first, then try Edit purpose again.",
            _markup([[("Back", f"{PREFIX}view:" + account_id)]]),
        )
        return
    text = (
        f"Edit purpose for {account_id}\n\n"
        "Send ONE message describing ONLY this inbox. The classifier uses "
        "this field for this account only — do NOT reference other brands.\n\n"
        f"Current:\n{current or '(empty)'}"
    )
    await _show(
        update,
        text,
        _markup([[("Back", f"{PREFIX}view:" + account_id)]]),
    )


async def _handle_purpose_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: str, raw: str
) -> None:
    user_id = update.effective_user.id
    _clear_awaiting(user_id)
    if raw.strip().lower() == "cancel":
        await update.effective_message.reply_text("Cancelled.")
        return
    try:
        mailbox_admin.set_purpose(user_id, account_id, raw)
    except mailbox_admin.MailboxAdminError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await _open_view(update, account_id)


# --------------------------------------------------------------------------- public dispatch


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True when this callback belonged to the Mailboxes screens."""
    q = update.callback_query
    data = (q.data or "") if q is not None else ""
    if not data.startswith(PREFIX):
        return False
    body = data[len(PREFIX):]
    if not await _guard_admin(update, body=body):
        return True
    # The query's acknowledgment is owned by the caller
    # (``app.on_callback`` arms a watchdog and guarantees exactly one answer),
    # so these screens must NOT answer it themselves — that duplicate answer
    # is what Telegram reported as "Query is too old".

    if body in ("", "m"):
        text, markup = main_screen(in_group=_is_group_chat(update))
        await _show(update, text, markup)
        return True
    if body == "switch":
        user_id = getattr(update.effective_user, "id", None)
        state = _get_manager_state(user_id)
        text, markup = manager_screen(
            page=int(state.get("page") or 0),
            query=str(state.get("query") or ""),
            in_group=_is_group_chat(update),
        )
        await _show(update, text, markup)
        return True
    if body.startswith("switch:"):
        rest = body[len("switch:"):]
        user_id = int(update.effective_user.id)
        state = _get_manager_state(user_id)
        if rest.startswith("p:"):
            try:
                page = int(rest.split(":", 1)[1])
            except ValueError:
                page = 0
            _set_manager_state(user_id, page=page, chat_id=_effective_chat_id(update))
            text, markup = manager_screen(
                page=page,
                query=str(state.get("query") or ""),
                in_group=_is_group_chat(update),
            )
            await _show(update, text, markup)
            return True
        if rest == "refresh":
            text, markup = manager_screen(
                page=int(state.get("page") or 0),
                query=str(state.get("query") or ""),
                in_group=_is_group_chat(update),
            )
            await _show(update, text, markup)
            return True
        if rest == "clear":
            _set_manager_state(user_id, page=0, query="", chat_id=_effective_chat_id(update))
            text, markup = manager_screen(
                page=0, query="", in_group=_is_group_chat(update)
            )
            await _show(update, text, markup)
            return True
        if rest == "search":
            # Search is a text-input flow. In a group we refuse (admin's
            # typing attracts attention and we do not want group noise);
            # in private we arm an _MB_SEARCH_FIELD slot.
            if _is_group_chat(update):
                await _notice(
                    q,
                    "Open the bot in a DM to search mailboxes. "
                    "In the group, use Prev/Next to page.",
                )
                return True
            installed = _set_awaiting(
                user_id,
                {
                    "token": "",
                    "field": _MB_SEARCH_FIELD,
                    "armed_at": time.time(),
                    "chat_id": _effective_chat_id(update),
                },
            )
            if not installed:
                await _notice(q, "Finish the pending password capture first.")
                return True
            await _show(
                update,
                "Search mailboxes\n\n"
                "Send ONE message with a substring to match against the "
                "account id, email or display name. Send 'cancel' to abort.",
                _markup([[("Back", f"{PREFIX}switch")]]),
            )
            return True
        await _notice(q, "Unknown Switch Inbox action.")
        return True
    if body.startswith("mbx:"):
        rest = body[len("mbx:"):]
        if ":" in rest:
            aid, action = rest.split(":", 1)
        else:
            aid, action = rest, ""
        in_group = _is_group_chat(update)
        if action == "":
            out = mailbox_screen(aid, in_group=in_group)
            if out is None:
                await _show(
                    update,
                    f"'{aid}' is not a registered mailbox.",
                    _markup([[("Back", f"{PREFIX}switch")]]),
                )
                return True
            text, markup = out
            await _show(update, text, markup)
            return True
        if action == "refresh":
            out = mailbox_screen(aid, in_group=in_group)
            if out is None:
                await _notice(q, "That mailbox is no longer registered.")
                return True
            text, markup = out
            await _show(update, text, markup)
            return True
        if action == "health":
            # Real IMAP+SMTP handshake. The result is scrubbed before
            # being surfaced in the group (health_check already returns
            # a short detail; redact_text catches a provider banner that
            # echoes the user name or host).
            m = mailbox_admin.find_mailbox(aid)
            if m is None:
                await _notice(q, "That mailbox is no longer registered.")
                return True
            if m.locked:
                await _notice(q, "Health checks are disabled for the shared Gmail account.")
                return True
            try:
                status = mailbox_admin.health_check(aid)
            except Exception as exc:  # noqa: BLE001 - never crash the UI
                log.exception("mb:mbx:%s:health failed", aid)
                status = {"ok": False, "imap": False, "smtp": False,
                          "detail": f"check failed: {type(exc).__name__}"}
            _HEALTH_CACHE[aid] = {
                "ok": bool(status.get("ok")),
                "imap": bool(status.get("imap")),
                "smtp": bool(status.get("smtp")),
                "detail": str(status.get("detail") or ""),
                "at": time.time(),
            }
            out = mailbox_screen(aid, in_group=in_group)
            if out is not None:
                text, markup = out
                await _show(update, text, markup)
            return True
        if action == "settings":
            # Enable/disable, purpose edit and audit live under
            # ``mb:view:<aid>`` which is already private-only via the
            # _group_safe_body check. In a group we surface a clear alert
            # so the admin knows to open the DM; in private we route
            # straight to that existing screen.
            if in_group:
                await _notice(
                    q,
                    "Mailbox settings (enable/disable, purpose, audit) are "
                    "private-only. Open the bot in a DM to continue.",
                )
                return True
            await _open_view(update, aid)
            return True
        await _notice(q, "Unknown mailbox action.")
        return True
    if body == "bulk" or body.startswith("bulk:"):
        from . import bulk_mailboxes

        await bulk_mailboxes.handle_callback(update, context, body)
        return True
    if body == "preset" or body.startswith("preset:"):
        await presets_ui.handle_callback(update, context, body)
        return True
    if body == "smart":
        await _open_smart_add(update, context)
        return True
    if body.startswith("smart:"):
        rest = body[len("smart:"):]
        if rest.startswith("pick:"):
            _, provider, token, nonce = rest.split(":", 3)
            if not await _ensure_session_owner(update, token):
                return True
            await _smart_pick_provider(update, context, provider, token, nonce)
            return True
        if rest.startswith("pickpreset:"):
            _, name, token, nonce = rest.split(":", 3)
            if not await _ensure_session_owner(update, token):
                return True
            await _smart_pick_preset(update, name, token, nonce)
            return True
        if rest.startswith("change:"):
            _, token, nonce = rest.split(":", 2)
            if not await _ensure_session_owner(update, token):
                return True
            await _smart_change_server(update, token, nonce)
            return True
        await _notice(q, "Unknown Smart Add action.")
        return True
    if body == "noop":
        return True
    if body == "errors":
        text, markup = errors_screen()
        await _show(update, text, markup)
        return True
    if body == "add":
        await _open_add(update)
        return True
    if body == "qadd":
        await _open_quick_add(update, context)
        return True
    if body.startswith("qadd:"):
        rest = body[len("qadd:"):]
        if rest.startswith("preset:"):
            name = rest.split(":", 1)[1]
            await _pick_quick_preset(update, name)
            return True
        if rest.startswith("edit:"):
            _, token, nonce = rest.split(":", 2)
            if not await _ensure_session_owner(update, token):
                return True
            await _reset_quick_session(update, token, nonce)
            return True
        if rest.startswith("save_preset:"):
            _, token, nonce = rest.split(":", 2)
            if not await _ensure_session_owner(update, token):
                return True
            await _save_preset_from_session(update, token, nonce)
            return True
        if rest.startswith("back_to_review:"):
            token = rest.split(":", 1)[1]
            if not await _ensure_session_owner(update, token):
                return True
            await _back_to_review(update, token)
            return True
        await _notice(q, "Unknown quick-add action.")
        return True
    if body.startswith("add:"):
        provider = body.split(":", 1)[1]
        if provider not in ("hostinger", "siteground", "google", "custom"):
            await _notice(q, "Unknown provider.")
            return True
        await _start_wizard(update, update.effective_user.id, provider)
        return True
    if body.startswith("view:"):
        await _open_view(update, body.split(":", 1)[1])
        return True
    if body.startswith("enable:"):
        await _do_enable(update, body.split(":", 1)[1])
        return True
    if body.startswith("disable:"):
        await _do_disable(update, body.split(":", 1)[1])
        return True
    if body.startswith("health:"):
        await _do_health(update, body.split(":", 1)[1])
        return True
    if body.startswith("purpose:"):
        await _do_purpose_prompt(update, body.split(":", 1)[1])
        return True
    if body.startswith("audit:"):
        text, markup = audit_screen(body.split(":", 1)[1])
        await _show(update, text, markup)
        return True
    if body.startswith("arm:"):
        _, token, nonce = body.split(":", 2)
        if not await _ensure_session_owner(update, token):
            return True
        await _arm_session(update, context, token, nonce)
        return True
    if body.startswith("cancel:"):
        parts = body.split(":", 2)
        token = parts[1] if len(parts) > 1 else ""
        nonce = parts[2] if len(parts) > 2 else ""
        if token and not await _ensure_session_owner(update, token):
            return True
        await _cancel_session(update, token, nonce or None)
        return True
    if body.startswith("edit:"):
        _, token, nonce = body.split(":", 2)
        if not await _ensure_session_owner(update, token):
            return True
        await _edit_session(update, token, nonce)
        return True
    if body.startswith("poll:"):
        token = body.split(":", 1)[1]
        if not await _ensure_session_owner(update, token):
            return True
        await _poll_session(update, token)
        return True
    if body.startswith("skip:"):
        _, token, nonce = body.split(":", 2)
        user_id = update.effective_user.id
        if not await _ensure_session_owner(update, token):
            return True
        pending = _awaiting_user(user_id) or {}
        field = pending.get("field")
        if not field or pending.get("token") != token:
            await _notice(q, "Nothing to skip right now.")
            return True
        await _apply_wizard_input(update, context, token, field, "skip")
        return True
    if body.startswith("credsvia:"):
        _, token, choice = body.split(":", 2)
        if not await _ensure_session_owner(update, token):
            return True
        await _handoff_credentials(update, context, token, choice)
        return True
    if body.startswith("dm_prompt:"):
        _, token, nonce = body.split(":", 2)
        if not await _ensure_session_owner(update, token):
            return True
        await _arm_dm_prompt(update, token, nonce)
        return True
    if body.startswith("group_pw:"):
        _, token, nonce = body.split(":", 2)
        if not await _ensure_session_owner(update, token):
            return True
        await _arm_group_pw_prompt(update, context, token, nonce)
        return True
    await _notice(q, "Unknown mailbox control.")
    return True


async def handle_qadd_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/qadd`` command handler.

    Privacy-mode-safe fallback for Quick Add in groups. Telegram always
    delivers commands to the bot even when privacy mode is enabled, so
    this path works whether or not ``@BotFather`` /setprivacy is
    disabled. The command body (everything after ``/qadd``) is passed to
    the normal Quick Add parser so the UX is identical to the paste-flow.

    Authorization rules match the inline-button path:
      * caller must be a mailbox admin
      * in a group, the group must be whitelisted
      * secrets are refused by the parser (same as the paste path)
    """
    user = update.effective_user
    msg = update.effective_message
    if user is None or msg is None:
        return
    uid = int(user.id)
    if not access.can(uid, access.P_ADMIN):
        try:
            await msg.reply_text("Unauthorized.")
        except Exception:  # noqa: BLE001
            pass
        return
    in_group = not _is_private(update)
    chat_id = _effective_chat_id(update)
    if in_group:
        if chat_id is None or not access.group_allowed(chat_id):
            try:
                await msg.reply_text("Unauthorized group.")
            except Exception:  # noqa: BLE001
                pass
            return
    raw = (msg.text or "")
    # Strip the leading "/qadd" (optionally with "@botname") token.
    body = raw.split(None, 1)[1] if " " in raw or "\n" in raw else ""
    # The split above only handles space; a /qadd\nEmail: ... form keeps
    # everything after the first newline.
    if not body:
        nl = raw.find("\n")
        body = raw[nl + 1 :] if nl >= 0 else ""
    body = body.strip()
    if not body:
        await msg.reply_text(
            "Usage: /qadd then on the NEXT lines paste the mailbox details, e.g.\n\n"
            "/qadd\n"
            "Email: info@alien.fi\n"
            "Name: Alien FI\n"
            "IMAP: gcam1108.siteground.biz\n"
            "IMAP Port: 993\n"
            "SMTP: gcam1108.siteground.biz\n"
            "SMTP Port: 465"
        )
        return
    # Install a fresh slot so _handle_quick_add_text's parser + session
    # bookkeeping fire exactly as they do for the paste path.
    _set_awaiting(
        uid,
        {
            "token": "",
            "field": QUICK_ADD_FIELD,
            "preset": "",
            "chat_id": chat_id,
        },
    )
    pending = _awaiting_user(uid) or {}
    await _handle_quick_add_text(update, context, pending, body)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True when this message was consumed by a mailbox input slot."""
    user = update.effective_user
    uid = getattr(user, "id", None)
    if uid is None:
        return False
    pending = _awaiting_user(uid)
    if not pending:
        return False
    # Chat-pinning: the slot was installed in some specific chat. Refuse
    # text from a different chat so that (a) a group-wizard slot cannot be
    # advanced by a DM message that happens to arrive in parallel, and
    # (b) a DM password-capture slot can never be fed from a group.
    pending_chat = pending.get("chat_id")
    actual_chat = _effective_chat_id(update)
    if not _is_private(update):
        # In a group, REQUIRE an explicit chat_id pin on the slot. This
        # closes two holes: (1) a legacy slot installed by older code
        # without a chat_id must never be consumable from a group it
        # didn't originate in, and (2) stale AWAITING state from other
        # tests or edge cases cannot be absorbed by an unrelated group
        # message. The group-first wizard always sets chat_id, so this
        # does not regress the intended flow.
        if pending_chat is None:
            return False
    if pending_chat is not None and actual_chat is not None and int(pending_chat) != int(actual_chat):
        return False
    field = pending.get("field")
    if not pending.get("token") and field not in (_DM_PW_FIELD, _GROUP_PW_FIELD):
        # Every untokened slot (no onboarding-session backing) is subject
        # to UNTOKENED_FIELD_TTL_S, stamped at arm time by _set_awaiting.
        # See that constant's docstring for the full list of affected
        # fields and why this matters.
        armed_at = pending.get("armed_at")
        if armed_at is None or (time.time() - float(armed_at)) > UNTOKENED_FIELD_TTL_S:
            _clear_awaiting(uid)
            return False
    if field == _DM_PW_FIELD and not _is_private(update):
        # Defense in depth: the DM capture arm path refuses in group, but
        # if a stale slot somehow faces a group message, refuse hard. We
        # do NOT process this text as a password even accidentally.
        return False
    if field == _GROUP_PW_FIELD and _is_private(update):
        # Mirror image of the above: this slot is only ever armed inside
        # the one pinned group, never in a DM.
        return False
    if not _is_private(update):
        # In a group, only non-secret wizard field input is accepted.
        # Credentials and purpose-editing are private-only.
        if field in (_DM_PW_FIELD, _PURPOSE_FIELD):
            return False
        # Also require the group be whitelisted (defense in depth —
        # the slot can only have been installed from a whitelisted
        # group in the first place, but we re-check so a slot that
        # outlived a whitelist revocation stops working immediately).
        if actual_chat is None or not access.group_allowed(actual_chat):
            return False
    if not access.can(uid, access.P_ADMIN):
        _clear_awaiting(uid)
        return False
    raw = (update.effective_message.text or "").strip()
    if field == _DM_PW_FIELD:
        await _handle_dm_password(update, context, pending, raw)
        return True
    if field == _GROUP_PW_FIELD:
        await _handle_group_password(update, context, pending, raw)
        return True
    if field == _PURPOSE_FIELD:
        account_id = str(pending.get("account_id") or "")
        await _handle_purpose_text(update, context, account_id, raw)
        return True
    if field == QUICK_ADD_FIELD:
        # Quick Add: one message, many fields. Group-safe when the slot
        # was installed in a whitelisted group (chat-pin check above).
        if raw.lower() == "cancel":
            _clear_awaiting(uid)
            await update.effective_message.reply_text("Cancelled.")
            return True
        await _handle_quick_add_text(update, context, pending, raw)
        return True
    if field == "__quick_preset_name__":
        await _handle_quick_preset_name_text(update, pending, raw)
        return True
    if field in (presets_ui.CREATE_FIELD, presets_ui.EDIT_FIELD):
        await presets_ui.handle_text(update, context, pending, raw)
        return True
    if field == _SMART_EMAIL_FIELD:
        await _handle_smart_add_text(update, context, pending, raw)
        return True
    if field == _SMART_HOST_FIELD:
        await _handle_smart_host_text(update, context, pending, raw)
        return True
    if field == _MB_SEARCH_FIELD:
        _clear_awaiting(uid)
        if raw.lower() == "cancel":
            await update.effective_message.reply_text("Search cancelled.")
            # Re-show the current manager page as a courtesy.
            state = _get_manager_state(uid)
            text, markup = manager_screen(
                page=int(state.get("page") or 0),
                query=str(state.get("query") or ""),
                in_group=_is_group_chat(update),
            )
            await update.effective_message.reply_text(text, reply_markup=markup)
            return True
        # Store the filter and jump back to page 0 with it applied.
        _set_manager_state(uid, page=0, query=raw[:60], chat_id=_effective_chat_id(update))
        text, markup = manager_screen(
            page=0, query=raw[:60], in_group=_is_group_chat(update)
        )
        await update.effective_message.reply_text(text, reply_markup=markup)
        return True
    if raw.lower() == "cancel":
        token = pending.get("token") or ""
        _clear_awaiting(uid)
        if token:
            try:
                os_mod.cancel(token, uid)
                mailbox_admin.record(uid, "onboard_cancelled", "", "")
            except os_mod.SessionError:
                pass
        await update.effective_message.reply_text("Cancelled.")
        return True
    token = str(pending.get("token") or "")
    if not token or not field:
        _clear_awaiting(uid)
        return False
    await _apply_wizard_input(update, context, token, field, raw)
    return True







