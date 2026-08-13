from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from .config import load_runtime, load_settings
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


async def send_to_home(bot: Bot, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    runtime = load_runtime()
    chat_id = runtime.home_channel or (runtime.allowed_users[0] if runtime.allowed_users else None)
    if chat_id is None:
        log.warning("No TELEGRAM_HOME_CHANNEL / allowed user for notifications")
        return
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


async def handle_event(bot: Bot, event: dict[str, Any]) -> None:
    etype = event.get("type")
    key = event.get("key") or ""
    if key and already_notified(key) and etype != "new_mail":
        # new_mail may mark before send
        pass
    if etype == "sync_error":
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
            return
        if key:
            mark_notified(key)
        prio = int(event.get("priority", 2))
        text = (
            f"{_prio_label(prio)}\n"
            f"{event.get('subject') or '(no subject)'}\n"
            f"From: {event.get('sender')}\n"
            f"Inbox: {event.get('account_id')} ({event.get('email')})\n"
            f"Why: {event.get('reason') or '—'}"
        )
        await send_to_home(
            bot,
            text,
            mail_keyboard(event["account_id"], event["mailbox"], event["msg_id"]),
        )


async def flush_p2_batch(bot: Bot) -> None:
    settings = load_settings()
    seconds = float(settings.get("p2_batch_seconds", 900))
    rows = pop_batch(seconds)
    if not rows:
        return
    # group
    lines = [f"New P2 mail ({len(rows)})"]
    buttons = []
    for r in rows[:20]:
        mark_notified(r["key"])
        lines.append(f"• [{r['account_id']}] {r['subject']}")
        cb = f"{r['account_id']}|{r['mailbox']}|{r['msg_id']}"
        buttons.append([InlineKeyboardButton(r["subject"][:40] or r["msg_id"], callback_data=f"open:{cb}")])
    await send_to_home(bot, "\n".join(lines), InlineKeyboardMarkup(buttons) if buttons else None)


async def send_dashboard_digest(bot: Bot, *, note: str = "") -> None:
    counts = collect_counts()
    text = format_dashboard(counts)
    if note:
        text = note.rstrip() + "\n\n" + text
    await send_to_home(bot, text, kb.dashboard_keyboard(counts))


def make_notifier(bot: Bot) -> Callable[[dict[str, Any]], Awaitable[None]]:
    async def _notify(event: dict[str, Any]) -> None:
        try:
            await handle_event(bot, event)
        except Exception:  # noqa: BLE001
            log.exception("notify failed")

    return _notify
