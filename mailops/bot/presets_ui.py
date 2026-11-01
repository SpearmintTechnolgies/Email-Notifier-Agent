"""Manage Server Presets - Telegram UI (Phase 4).

Presets are named, reusable IMAP/SMTP server definitions stored in
``settings.json["mailbox_presets"]`` (owned by ``mailops.bot.mb_quick``,
which this module treats as the single source of truth - no duplicate
storage, no duplicate validation rules). This module is UI-only: screen
builders + the ``mb:preset:*`` callback/text dispatch.

Design constraints (see the CEO's Phase 4 spec):
  * List, paginate (>6 presets), view, create, edit, delete-with-confirm.
  * NEVER store a password inside a preset (enforced by ``mb_quick`` itself
    - ``preset_save`` only accepts host/port/username fields).
  * Editing/deleting a preset must not modify or break already-registered
    mailboxes - presets are a convenience template consumed only at
    onboarding time; nothing re-reads a preset after a mailbox is created.
  * Group-safe VIEWING; mutations (create/edit/delete) still require
    ``P_ADMIN`` (the same bar as every other Mailboxes write action) plus
    an explicit delete confirmation step.
  * Backward compatible with presets saved before this screen existed
    (missing optional metadata is simply shown as "custom" / "unverified").
"""
from __future__ import annotations

import re
import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .. import access
from . import mb_quick

PREFIX = "preset:"
PAGE_SIZE = 6

# Text-input sentinels consumed by mailops.bot.mailboxes.handle_text via a
# lazy import (keeps this module import-cycle-free).
CREATE_FIELD = "__preset_create__"
EDIT_FIELD = "__preset_edit__"

# {user_id: {"name": str, "expires_at": float}} - single-use, short-lived
# delete-confirmation binding. Not a DB table: deleting a preset is not
# security-critical enough to need the full access.issue_action machinery,
# but we still require a fresh explicit tap (no bare "are you sure?" with a
# callback that could be replayed from an old message).
_DELETE_CONFIRM: dict[int, dict[str, Any]] = {}
_DELETE_CONFIRM_TTL_S = 120


def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _markup(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(t, d) for t, d in row] for row in rows])


def _fmt_age(ts: float) -> str:
    if not ts:
        return "never verified"
    delta = max(0, int(time.time() - float(ts)))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


# --------------------------------------------------------------------------- screens


def list_screen(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    presets = mb_quick.presets_list()
    if not presets:
        return (
            "Manage Server Presets\n\n"
            "No saved presets yet. Create one from here, or save one "
            "automatically from Quick Add / Smart Add after a successful "
            "onboarding.",
            _markup([[("Create new preset", "mb:preset:new")], [("Back", "mb:m")]]),
        )
    total = len(presets)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    slice_ = presets[start : start + PAGE_SIZE]
    lines = ["Manage Server Presets", f"{total} saved - page {page + 1}/{pages}", ""]
    rows: list[list[tuple[str, str]]] = []
    for name, blob in slice_:
        provider = str(blob.get("provider") or "custom")
        verified = "✓" if blob.get("verified", True) else "?"
        lines.append(f"{verified} {name}  [{provider}]  {blob.get('imap_host', '')}")
        rows.append([(f"{verified} {name}", f"mb:preset:view:{name}")])
    nav: list[tuple[str, str]] = []
    if page > 0:
        nav.append(("Prev", f"mb:preset:p:{page - 1}"))
    nav.append((f"Page {page + 1}/{pages}", "mb:noop"))
    if page + 1 < pages:
        nav.append(("Next", f"mb:preset:p:{page + 1}"))
    rows.append(nav)
    rows.append([("Create new preset", "mb:preset:new")])
    rows.append([("Back to mailboxes", "mb:m")])
    return "\n".join(lines), _markup(rows)


def view_screen(name: str) -> tuple[str, InlineKeyboardMarkup] | None:
    blob = mb_quick.preset_get(name)
    if blob is None:
        return None
    provider = str(blob.get("provider") or "custom")
    verified = bool(blob.get("verified", True))
    verified_at = float(blob.get("verified_at") or 0)
    username_pattern = str(blob.get("username_pattern") or "") or "(defaults to the mailbox's own email address)"
    domains = blob.get("domains") or []
    mailboxes = mb_quick.associated_mailboxes(name)
    lines = [
        f"Preset: {name}",
        "",
        f"Provider family: {provider}",
        f"IMAP: {blob.get('imap_host', '')}:{blob.get('imap_port', '')}",
        f"SMTP: {blob.get('smtp_host', '')}:{blob.get('smtp_port', '')}",
        f"Username pattern: {username_pattern}",
        f"Status: {'VERIFIED' if verified else 'UNVERIFIED'} ({_fmt_age(verified_at)})",
    ]
    if domains:
        lines.append(f"Tagged domains (auto-VERIFIED match): {', '.join(domains)}")
    if mailboxes:
        lines.append("")
        lines.append(f"Mailboxes using this preset's host ({len(mailboxes)}): " + ", ".join(mailboxes[:15]))
    else:
        lines.append("")
        lines.append("No currently-registered mailbox matches this preset's host yet.")
    rows: list[list[tuple[str, str]]] = [
        [("Edit", f"mb:preset:edit:{name}")],
        [("Delete", f"mb:preset:delask:{name}")],
        [("Back to list", "mb:preset")],
    ]
    return "\n".join(lines), _markup(rows)


def _create_prompt_text() -> str:
    return (
        "Create a new server preset\n\n"
        "Paste ONE message with the preset details, one 'Key: value' per line:\n\n"
        "Name: siteground-gcam1108\n"
        "Provider: siteground\n"
        "IMAP: gcam1108.siteground.biz\n"
        "IMAP Port: 993\n"
        "SMTP: gcam1108.siteground.biz\n"
        "SMTP Port: 465\n\n"
        "Optional lines: Username Pattern, Domains (comma-separated).\n"
        "NEVER include a password - presets never store credentials.\n"
        "Send 'cancel' to abort."
    )


def _edit_prompt_text(name: str, blob: dict[str, Any]) -> str:
    return (
        f"Edit preset '{name}'\n\n"
        "Paste the FULL replacement definition (same format as Create); "
        "the name stays fixed. Current values:\n\n"
        f"Provider: {blob.get('provider', 'custom')}\n"
        f"IMAP: {blob.get('imap_host', '')}\n"
        f"IMAP Port: {blob.get('imap_port', '')}\n"
        f"SMTP: {blob.get('smtp_host', '')}\n"
        f"SMTP Port: {blob.get('smtp_port', '')}\n"
        f"Username Pattern: {blob.get('username_pattern', '')}\n"
        f"Domains: {', '.join(blob.get('domains', []))}\n\n"
        "Editing this preset does NOT change any already-registered "
        "mailbox. Send 'cancel' to abort."
    )


_LINE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z \-_]{0,30})\s*[:=]\s*(.+?)\s*$")
_FORBIDDEN = frozenset({"password", "pass", "pwd", "secret", "token"})


def parse_preset_text(raw: str, *, name_override: str = "") -> tuple[str, dict[str, Any]]:
    """Parse the Create/Edit paste into (name, blob) for ``mb_quick.preset_save``.

    Raises ``mb_quick.QuickAddError`` (admin-safe message) on bad input -
    same error type the rest of the preset/quick-add stack already uses.
    """
    pairs: list[tuple[str, str]] = []
    for line in (raw or "").splitlines():
        m = _LINE_RE.match(line)
        if m:
            pairs.append((m.group(1).strip().lower(), m.group(2).strip()))
    if not pairs:
        raise mb_quick.QuickAddError(
            "No 'Key: value' lines found. See the example in the prompt above."
        )
    fields: dict[str, Any] = {}
    name = name_override
    for key, value in pairs:
        if key in _FORBIDDEN or key.endswith(" password"):
            raise mb_quick.QuickAddError(
                "Passwords are never accepted here - presets never store credentials."
            )
        if key == "name" and not name_override:
            name = value
        elif key == "provider":
            fields["provider"] = value.lower()
        elif key in ("imap", "imap host", "imap server"):
            fields["imap_host"] = value
        elif key in ("imap port",):
            fields["imap_port"] = value
        elif key in ("smtp", "smtp host", "smtp server"):
            fields["smtp_host"] = value
        elif key in ("smtp port",):
            fields["smtp_port"] = value
        elif key in ("username pattern",):
            fields["username_pattern"] = value
        elif key in ("domains",):
            fields["domains"] = [d.strip().lower() for d in value.split(",") if d.strip()]
    if not name:
        raise mb_quick.QuickAddError("Missing required field: Name.")
    if "imap_host" not in fields or "smtp_host" not in fields:
        raise mb_quick.QuickAddError("Missing required field(s): IMAP and/or SMTP host.")
    fields.setdefault("imap_port", 993)
    fields.setdefault("smtp_port", 465)
    return name, fields


def delete_ask_screen(name: str) -> tuple[str, InlineKeyboardMarkup] | None:
    blob = mb_quick.preset_get(name)
    if blob is None:
        return None
    mailboxes = mb_quick.associated_mailboxes(name)
    warn = ""
    if mailboxes:
        warn = (
            f"\n\n{len(mailboxes)} registered mailbox(es) currently match this "
            "preset's host; they are NOT affected by deleting the preset "
            "itself (their own stored server config is independent)."
        )
    text = f"Delete preset '{name}'? This cannot be undone.{warn}"
    rows = [
        [("Confirm delete", f"mb:preset:delconfirm:{name}")],
        [("Cancel", f"mb:preset:view:{name}")],
    ]
    return text, _markup(rows)


# --------------------------------------------------------------------------- dispatch


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, body: str) -> bool:
    """``body`` is already stripped of the leading ``mb:`` by the caller.
    Returns True iff this was a ``preset`` (or ``preset:*``) action."""
    from . import mailboxes as mb  # lazy: avoid import cycle

    if body != "preset" and not body.startswith("preset:"):
        return False
    q = update.callback_query
    user = update.effective_user
    uid = getattr(user, "id", None)
    if uid is None or not access.can(uid, access.P_ADMIN):
        if q is not None:
            await mb._notice(q, "Only administrators can manage presets.")
        return True

    if body == "preset":
        text, markup = list_screen(0)
        await mb._show(update, text, markup)
        return True

    rest = body[len("preset:") :]
    if rest.startswith("p:"):
        try:
            page = int(rest.split(":", 1)[1])
        except ValueError:
            page = 0
        text, markup = list_screen(page)
        await mb._show(update, text, markup)
        return True
    if rest.startswith("view:"):
        name = rest.split(":", 1)[1]
        out = view_screen(name)
        if out is None:
            await mb._show(update, "That preset no longer exists.", _markup([[("Back", "mb:preset")]]))
            return True
        text, markup = out
        await mb._show(update, text, markup)
        return True
    if rest == "new":
        installed = mb._set_awaiting(
            uid, {"token": "", "field": CREATE_FIELD, "chat_id": mb._effective_chat_id(update)}
        )
        if not installed:
            await mb._notice(q, "Finish the pending password capture first.")
            return True
        await mb._show(update, _create_prompt_text(), _markup([[("Cancel", "mb:preset")]]))
        return True
    if rest.startswith("edit:"):
        name = rest.split(":", 1)[1]
        blob = mb_quick.preset_get(name)
        if blob is None:
            await mb._show(update, "That preset no longer exists.", _markup([[("Back", "mb:preset")]]))
            return True
        installed = mb._set_awaiting(
            uid,
            {
                "token": "",
                "field": EDIT_FIELD,
                "name": name,
                "chat_id": mb._effective_chat_id(update),
            },
        )
        if not installed:
            await mb._notice(q, "Finish the pending password capture first.")
            return True
        await mb._show(update, _edit_prompt_text(name, blob), _markup([[("Cancel", f"mb:preset:view:{name}")]]))
        return True
    if rest.startswith("delask:"):
        name = rest.split(":", 1)[1]
        out = delete_ask_screen(name)
        if out is None:
            await mb._show(update, "That preset no longer exists.", _markup([[("Back", "mb:preset")]]))
            return True
        _DELETE_CONFIRM[int(uid)] = {"name": name, "expires_at": time.time() + _DELETE_CONFIRM_TTL_S}
        text, markup = out
        await mb._show(update, text, markup)
        return True
    if rest.startswith("delconfirm:"):
        name = rest.split(":", 1)[1]
        pending = _DELETE_CONFIRM.get(int(uid))
        if (
            not pending
            or pending.get("name") != name
            or float(pending.get("expires_at") or 0) < time.time()
        ):
            await mb._notice(q, "Delete confirmation expired. Tap Delete again.")
            return True
        _DELETE_CONFIRM.pop(int(uid), None)
        removed = mb_quick.preset_delete(name)
        text, markup = list_screen(0)
        prefix = f"Preset '{name}' deleted.\n\n" if removed else "That preset was already gone.\n\n"
        await mb._show(update, prefix + text, markup)
        return True
    if q is not None:
        await mb._notice(q, "Unknown preset action.")
    return True


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict[str, Any], raw: str) -> None:
    """Handles ``CREATE_FIELD`` / ``EDIT_FIELD`` text slots. Caller
    (mailops.bot.mailboxes.handle_text) already did chat-pin / admin-perm
    checks before routing here."""
    from . import mailboxes as mb  # lazy: avoid import cycle

    uid = update.effective_user.id
    msg = update.effective_message
    field = pending.get("field")
    mb._clear_awaiting(uid)
    if raw.strip().lower() == "cancel":
        await msg.reply_text("Cancelled. No preset was changed.")
        return
    try:
        if field == CREATE_FIELD:
            name, fields = parse_preset_text(raw)
        else:
            name, fields = parse_preset_text(raw, name_override=str(pending.get("name") or ""))
    except mb_quick.QuickAddError as exc:
        await msg.reply_text(f"Preset refused: {exc}\n\nTap Manage Presets to try again.")
        return
    try:
        mb_quick.preset_save(name, fields)
    except mb_quick.QuickAddError as exc:
        await msg.reply_text(f"Preset refused: {exc}")
        return
    verb = "updated" if field == EDIT_FIELD else "created"
    out = view_screen(name)
    if out is None:
        await msg.reply_text(f"Preset '{name}' {verb}.")
        return
    text, markup = out
    await msg.reply_text(f"Preset '{name}' {verb}.\n\n" + text, reply_markup=markup)
