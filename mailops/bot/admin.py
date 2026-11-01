"""Private-chat Admin / Access Management UI.

Only owners see or use this. Mutating buttons carry single-use tokens so an
old invitation or confirmation cannot be replayed. Group chats never get this
menu, and mailbox passwords are never requested here.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .. import access
from ..config import load_accounts
from .callbacks import Ack

log = logging.getLogger("mailops.admin")

PREFIX = "ac:"
_PRIVATE_ONLY = "Access management is private-chat only. Open the bot in a private chat."
_OWNERS_ONLY = "That command is for administrators only."


def _ago(ts: float) -> str:
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _markup(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(t, d) for t, d in row] for row in rows])


def _tok(actor_id: int, action: str, **payload: Any) -> str:
    return f"{PREFIX}tok:{access.issue_action(actor_id, action, payload)}"


def main_screen(actor_id: int) -> tuple[str, InlineKeyboardMarkup]:
    people = access.users()
    groups = access.groups()
    n_owner = sum(1 for u in people if u.role == "owner")
    n_op = sum(1 for u in people if u.role == "operator")
    n_view = sum(1 for u in people if u.role == "viewer")
    text = (
        "Access management\n\n"
        f"Owners: {n_owner}\n"
        f"Operators: {n_op}\n"
        f"Viewers: {n_view}\n"
        f"Whitelisted groups: {len(groups)}\n\n"
        "Whitelisting a group does not authorize its members. "
        "Group chats stay read-only (counts and subjects only).\n\n"
        "Changes are stored in the local database and survive restarts. "
        "No .env edit is required."
    )
    rows = [
        [("Users", f"{PREFIX}u"), ("Groups", f"{PREFIX}g")],
        [("Add user", f"{PREFIX}au"), ("Add group", f"{PREFIX}ag")],
        [("Audit log", f"{PREFIX}audit")],
        [("Mailbox onboarding", f"{PREFIX}mb")],
        [("My permissions", f"{PREFIX}me")],
        [("Close", f"{PREFIX}x")],
    ]
    return text, _markup(rows)


def users_screen(actor_id: int) -> tuple[str, InlineKeyboardMarkup]:
    people = access.users()
    if not people:
        return "No authorized users.", _markup([[("Back", f"{PREFIX}m")]])
    lines = ["Authorized users", ""]
    rows: list[list[tuple[str, str]]] = []
    for u in people:
        send = "send" if access.P_SEND in u.perms else "no-send"
        boot = " · bootstrap" if u.is_bootstrap else ""
        label = u.name or (f"@{u.username}" if u.username else str(u.user_id))
        lines.append(f"• {label} · {u.user_id} · {u.role} · {send}{boot}")
        rows.append([(f"{u.role} {u.user_id}", f"{PREFIX}user:{u.user_id}")])
    rows.append([("Add user", f"{PREFIX}au"), ("Back", f"{PREFIX}m")])
    return "\n".join(lines)[:3900], _markup(rows)


def groups_screen(actor_id: int) -> tuple[str, InlineKeyboardMarkup]:
    items = access.groups()
    if not items:
        return (
            "No whitelisted groups.\n\n"
            "Add the bot to a Telegram group, send /chatid there as an authorized "
            "user, then tap Add group and paste the numeric chat id (negative).",
            _markup([[("Add group", f"{PREFIX}ag"), ("Back", f"{PREFIX}m")]]),
        )
    lines = ["Whitelisted groups", "", "Members are not auto-authorized.", ""]
    rows: list[list[tuple[str, str]]] = []
    for g in items:
        title = g.title or str(g.chat_id)
        lines.append(f"• {title} · {g.chat_id} · added {_ago(g.added_at)}")
        rows.append([(f"Remove {g.chat_id}", _tok(actor_id, "remove_group", chat_id=g.chat_id))])
    rows.append([("Add group", f"{PREFIX}ag"), ("Back", f"{PREFIX}m")])
    return "\n".join(lines)[:3900], _markup(rows)


def user_screen(actor_id: int, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    text = access.describe_perms(user_id)
    u = access.get_user(user_id)
    rows: list[list[tuple[str, str]]] = []
    if u is None:
        return "That user is not authorized.", _markup([[("Back", f"{PREFIX}u")]])
    if not u.is_bootstrap:
        role_row: list[tuple[str, str]] = []
        for role, label in (("owner", "Owner"), ("operator", "Operator"), ("viewer", "Viewer")):
            mark = "· " if u.role == role else ""
            role_row.append(
                (f"{mark}{label}", _tok(actor_id, "set_role", user_id=user_id, role=role))
            )
        rows.append(role_row)
        if u.role == "operator":
            if u.can_send:
                rows.append(
                    [
                        (
                            "Revoke send approval",
                            _tok(actor_id, "set_send", user_id=user_id, enabled=False),
                        )
                    ]
                )
            else:
                rows.append(
                    [
                        (
                            "Grant send approval",
                            _tok(actor_id, "set_send", user_id=user_id, enabled=True),
                        )
                    ]
                )
        rows.append([("Remove user", _tok(actor_id, "remove_user", user_id=user_id))])
    else:
        rows.append([("(Bootstrap owner cannot be removed or demoted)", f"{PREFIX}noop")])
    rows.append([("Back", f"{PREFIX}u")])
    return text, _markup(rows)


def audit_screen() -> tuple[str, InlineKeyboardMarkup]:
    rows_data = access.audit_log(40)
    if not rows_data:
        return "No access-management changes recorded yet.", _markup(
            [[("Back", f"{PREFIX}m")]]
        )
    lines = ["Access audit (most recent first)", ""]
    for r in rows_data:
        target = f" {r.target_type}:{r.target_id}" if r.target_id is not None else ""
        extra = f" {r.detail}" if r.detail else ""
        lines.append(f"{_ago(r.created_at)}  actor {r.actor_id}  {r.action}{target}{extra}")
    return "\n".join(lines)[:3900], _markup([[("Back", f"{PREFIX}m")]])


def mailbox_screen() -> tuple[str, InlineKeyboardMarkup]:
    accounts = load_accounts()
    active = ", ".join(f"{a.id} <{a.email}>" for a in accounts) or "none"
    text = (
        "Mailbox onboarding\n\n"
        f"Active mailboxes: {active}\n\n"
        "Open the full Mailboxes panel to view every registered account, run "
        "a connection health check, enable/disable an account or onboard a "
        "new business mailbox. Passwords are never collected in this chat "
        "by default - the Mailboxes wizard shows a one-shot server-side "
        "command that reads the password at a hidden terminal prompt."
    )
    return text, _markup(
        [
            [("Open Mailboxes panel", "mb:m")],
            [("Back", f"{PREFIX}m")],
        ]
    )


def _close_markup() -> InlineKeyboardMarkup:
    return _markup([[("Open admin", f"{PREFIX}m")]])


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


async def _guard_admin(update: Update, *, private_only: bool = True) -> bool:
    user = update.effective_user
    chat = getattr(update, "effective_chat", None)
    chat_type = getattr(chat, "type", None) if chat is not None else "private"
    q = getattr(update, "callback_query", None)
    if user is None:
        return False
    if private_only and chat_type in ("group", "supergroup"):
        if q is not None:
            await Ack(q).alert(_PRIVATE_ONLY)
        elif update.effective_message:
            await update.effective_message.reply_text(_PRIVATE_ONLY)
        return False
    if not access.is_owner(user.id):
        log.info("ignored admin access user=%s chat=%s", user.id, getattr(chat, "id", None))
        if q is not None:
            await Ack(q).alert(_OWNERS_ONLY)
            return False
        if chat_type in ("group", "supergroup"):
            return False
        if update.effective_message:
            await update.effective_message.reply_text(_OWNERS_ONLY)
        return False
    return True


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_admin(update):
        return
    text, markup = main_screen(update.effective_user.id)
    await _show(update, text, markup)


def _args(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    return [str(a) for a in (getattr(context, "args", None) or [])]


async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_admin(update):
        return
    args = _args(context)
    if not args or not args[0].lstrip("+").isdigit():
        access.set_awaiting(update.effective_user.id, "add_user")
        await update.effective_message.reply_text(
            "Usage: /adduser <telegram user id>\nOr send the numeric id as your next message."
        )
        return
    actor_id = update.effective_user.id
    try:
        added = access.add_user(actor_id, int(args[0]), "operator", can_send=False)
    except (access.AccessDenied, access.LastOwnerError, access.InvalidTargetError) as exc:
        await update.effective_message.reply_text(str(exc))
        return
    text, markup = user_screen(actor_id, added.user_id)
    await update.effective_message.reply_text(
        f"Authorized {added.user_id} as {added.role}.", reply_markup=markup
    )


async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_admin(update):
        return
    args = _args(context)
    if not args or not args[0].lstrip("+").isdigit():
        await update.effective_message.reply_text("Usage: /removeuser <telegram user id>")
        return
    try:
        removed = access.remove_user(update.effective_user.id, int(args[0]))
    except (access.AccessDenied, access.LastOwnerError, access.InvalidTargetError) as exc:
        await update.effective_message.reply_text(str(exc))
        return
    if removed:
        await update.effective_message.reply_text(f"Removed {args[0]}. They can no longer use the bot.")
    else:
        await update.effective_message.reply_text("That id is not an authorized user.")


async def cmd_allowgroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_admin(update, private_only=False):
        return
    chat = getattr(update, "effective_chat", None)
    actor_id = update.effective_user.id
    args = _args(context)
    target = None
    title = ""
    if chat is not None and getattr(chat, "type", None) in ("group", "supergroup"):
        target = int(chat.id)
        title = str(getattr(chat, "title", "") or "")
    elif args:
        raw = args[0].replace(" ", "")
        if raw.startswith("-") and raw[1:].isdigit():
            target = int(raw)
    if target is None:
        await update.effective_message.reply_text(
            "Send /allowgroup inside the group, or /allowgroup <negative chat id> in a private chat."
        )
        return
    try:
        group = access.add_group(actor_id, target, title)
    except (access.AccessDenied, access.InvalidTargetError) as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text(
        f"Whitelisted group {group.chat_id}. Members are not auto-authorized. "
        "Group chats stay read-only."
    )


async def cmd_removegroup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_admin(update, private_only=False):
        return
    chat = getattr(update, "effective_chat", None)
    args = _args(context)
    target = None
    if chat is not None and getattr(chat, "type", None) in ("group", "supergroup"):
        target = int(chat.id)
    elif args:
        raw = args[0].replace(" ", "")
        if raw.lstrip("-").isdigit() and raw.count("-") <= 1:
            target = int(raw)
    if target is None:
        await update.effective_message.reply_text(
            "Send /removegroup inside the group, or /removegroup <chat id> in a private chat."
        )
        return
    try:
        removed = access.remove_group(update.effective_user.id, target)
    except (access.AccessDenied, access.InvalidTargetError) as exc:
        await update.effective_message.reply_text(str(exc))
        return
    if removed:
        await update.effective_message.reply_text(f"Removed group {target}. The bot will ignore it now.")
    else:
        await update.effective_message.reply_text("That group is not whitelisted.")


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Anyone in a private chat; authorized users in groups. Not self-registration."""
    user = update.effective_user
    chat = getattr(update, "effective_chat", None)
    if user is None or chat is None:
        return
    chat_type = getattr(chat, "type", None)
    if chat_type in ("group", "supergroup"):
        if not access.authorized(
            user.id, chat.id, chat_type, allow_unconfigured_group=True
        ):
            return
    await update.effective_message.reply_text(
        f"Your Telegram ID: {user.id}\nChat id: {chat.id}\nType: {chat.type}"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True when this message was consumed as admin input."""
    user = update.effective_user
    if user is None or not access.is_owner(user.id):
        return False
    chat = getattr(update, "effective_chat", None)
    if getattr(chat, "type", None) in ("group", "supergroup"):
        return False
    pending = access.get_awaiting(user.id)
    if not pending:
        return False
    raw = (update.effective_message.text or "").strip()
    if raw.lower() == "cancel":
        access.clear_awaiting(user.id)
        await update.effective_message.reply_text("Cancelled.")
        text, markup = main_screen(user.id)
        await update.effective_message.reply_text(text, reply_markup=markup)
        return True
    mode = pending.get("mode")
    if mode == "add_user":
        access.clear_awaiting(user.id)
        tok = raw.lstrip("+")
        if not tok.isdigit():
            await update.effective_message.reply_text(
                "Send a numeric Telegram user id (positive), or Cancel."
            )
            access.set_awaiting(user.id, "add_user")
            return True
        try:
            added = access.add_user(user.id, int(tok), "operator", can_send=False)
        except (access.AccessDenied, access.LastOwnerError, access.InvalidTargetError) as exc:
            await update.effective_message.reply_text(str(exc))
            return True
        text, markup = user_screen(user.id, added.user_id)
        await update.effective_message.reply_text(
            f"Authorized {added.user_id} as {added.role}. "
            "They can use the bot now. Grant send approval from this card if needed.",
        )
        await update.effective_message.reply_text(text, reply_markup=markup)
        return True
    if mode == "add_group":
        access.clear_awaiting(user.id)
        tok = raw.replace(" ", "")
        if not (tok.startswith("-") and tok[1:].isdigit()):
            await update.effective_message.reply_text(
                "Send a numeric Telegram group id (negative, e.g. -100…), or Cancel."
            )
            access.set_awaiting(user.id, "add_group")
            return True
        try:
            group = access.add_group(user.id, int(tok))
        except (access.AccessDenied, access.InvalidTargetError) as exc:
            await update.effective_message.reply_text(str(exc))
            return True
        await update.effective_message.reply_text(
            f"Whitelisted group {group.chat_id}. Members are not auto-authorized. "
            "The group stays read-only."
        )
        text, markup = groups_screen(user.id)
        await update.effective_message.reply_text(text, reply_markup=markup)
        return True
    access.clear_awaiting(user.id)
    return False


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True when this callback was an admin action (consumed)."""
    q = update.callback_query
    data = (q.data or "") if q is not None else ""
    if not data.startswith(PREFIX):
        return False
    if not await _guard_admin(update):
        return True
    actor_id = update.effective_user.id
    # Acknowledgment is owned by ``app.on_callback`` (exactly once per query).

    if data in (f"{PREFIX}m", f"{PREFIX}admin"):
        text, markup = main_screen(actor_id)
        await _show(update, text, markup)
        return True
    if data == f"{PREFIX}u":
        text, markup = users_screen(actor_id)
        await _show(update, text, markup)
        return True
    if data == f"{PREFIX}g":
        text, markup = groups_screen(actor_id)
        await _show(update, text, markup)
        return True
    if data == f"{PREFIX}au":
        access.set_awaiting(actor_id, "add_user")
        await _show(
            update,
            "Send the numeric Telegram user id to authorize.\n"
            "They will be added as Operator without send approval.\n"
            "Send Cancel to abort.",
            _markup([[("Back", f"{PREFIX}m")]]),
        )
        return True
    if data == f"{PREFIX}ag":
        access.set_awaiting(actor_id, "add_group")
        await _show(
            update,
            "Send the numeric Telegram group/supergroup id (negative).\n"
            "Members are not authorized by this. Group chats stay read-only.\n"
            "Send Cancel to abort.",
            _markup([[("Back", f"{PREFIX}m")]]),
        )
        return True
    if data == f"{PREFIX}audit":
        text, markup = audit_screen()
        await _show(update, text, markup)
        return True
    if data == f"{PREFIX}mb":
        text, markup = mailbox_screen()
        await _show(update, text, markup)
        return True
    if data == f"{PREFIX}me":
        await _show(
            update,
            access.describe_perms(actor_id),
            _markup([[("Back", f"{PREFIX}m")]]),
        )
        return True
    if data == f"{PREFIX}x":
        await _show(update, "Access management closed.", None)
        return True
    if data == f"{PREFIX}noop":
        return True
    if data.startswith(f"{PREFIX}user:"):
        try:
            uid = int(data.rsplit(":", 1)[-1])
        except ValueError:
            return True
        text, markup = user_screen(actor_id, uid)
        await _show(update, text, markup)
        return True
    if data.startswith(f"{PREFIX}tok:"):
        token = data[len(f"{PREFIX}tok:") :]
        try:
            payload = access.consume_action(actor_id, token)
        except (access.ReplayError, access.AccessDenied) as exc:
            await _notice(q, str(exc))
            return True
        await _apply_token(update, actor_id, payload)
        return True
    return True


async def _notice(q: Any, text: str) -> None:
    """Refusal/status text that never double-answers the callback query."""
    if q is None:
        return
    await Ack(q).notice(text)


async def _apply_token(update: Update, actor_id: int, payload: dict[str, Any]) -> None:
    action = str(payload.get("_action") or "")
    try:
        if action == "remove_user":
            access.remove_user(actor_id, int(payload["user_id"]))
            text, markup = users_screen(actor_id)
            await _show(update, "User removed.\n\n" + text, markup)
            return
        if action == "remove_group":
            access.remove_group(actor_id, int(payload["chat_id"]))
            text, markup = groups_screen(actor_id)
            await _show(update, "Group removed.\n\n" + text, markup)
            return
        if action == "set_role":
            user = access.set_role(actor_id, int(payload["user_id"]), str(payload["role"]))
            text, markup = user_screen(actor_id, user.user_id)
            await _show(update, text, markup)
            return
        if action == "set_send":
            enabled = bool(payload.get("enabled"))
            user = access.set_can_send(actor_id, int(payload["user_id"]), enabled)
            text, markup = user_screen(actor_id, user.user_id)
            await _show(update, text, markup)
            return
    except (access.LastOwnerError, access.AccessDenied, access.InvalidTargetError, KeyError, ValueError) as exc:
        q = update.callback_query
        await _notice(q, str(exc) or "That action is not valid.")
        return
    await _notice(update.callback_query, "Unknown access action.")


def required_perm(data: str) -> str:
    """Permission a callback requires. Enforced in handlers, not only by hiding buttons."""
    if data.startswith(PREFIX):
        return access.P_ADMIN
    if data.startswith("mb:"):
        return access.P_ADMIN
    if data.startswith(("go:", "nogo:", "send:")):
        return access.P_SEND
    if data.startswith(("reply:", "writeme:", "aidraft:")):
        return access.P_DRAFT
    if data.startswith(("open:", "n:", "browse:")):
        return access.P_READ
    if data.startswith(
        ("done:", "mute:", "prio:", "setprio:", "tags:", "settag:", "snooze:")
    ):
        return access.P_ORGANIZE
    if data == "nav:settings" or data.startswith("set:"):
        return access.P_SETTINGS
    if data.startswith("gdl:") or data.startswith("g:all"):
        # The deep-link minter and the read-only all-emails listing require a
        # confirmed user. Fine-grained perm (draft / send / admin / settings)
        # is re-checked inside _mint_deep_link so unauthorized members never
        # receive a token - whitelisting a group never grants access.
        return access.P_VIEW
    if data.startswith("gr:"):
        # Group-read namespace (see GROUP_CALLBACK_PREFIXES in app.py).
        # Mirrors the private perm for the same underlying action so a
        # group tap can never be LESS strictly checked than its DM twin.
        sub = data[len("gr:") :]
        if sub.startswith(("go:", "nogo:", "send:")):
            return access.P_SEND
        if sub.startswith(("reply:", "writeme:", "aidraft:")):
            return access.P_DRAFT
        if sub.startswith(("n:", "o:", "b:", "bp:", "restricted:")) or sub in ("back", "search"):
            return access.P_READ
        return access.P_READ
    return access.P_VIEW
