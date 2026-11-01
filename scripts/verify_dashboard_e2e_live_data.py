"""End-to-end /dashboard proof using the REAL dispatcher and REAL live data.

Reads the production account registry and the production ops.db (read-only
SQLite connection), runs the genuine telegram.ext Application dispatcher, and
captures the outbound Telegram call instead of performing it.

What this proves that a bot-sent probe cannot: Telegram does not deliver a
bot's own messages back through getUpdates, so the running poller can never
be triggered by us - only by a human. This harness injects the identical
Update the group would produce and shows exactly what the handler returns.

No IMAP, no Telegram send, no writes of any kind.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

_DIR = tempfile.mkdtemp(prefix="e2e-dash-")
os.environ["MAILOPS_LOG_FILE"] = os.path.join(_DIR, "probe.log")

from telegram import Bot, Update, User  # noqa: E402

import mailops.bot.app as app_mod  # noqa: E402
from mailops import cache as cache_mod  # noqa: E402
from mailops.config import load_accounts, load_runtime  # noqa: E402

USER = 8925736576


class Captured:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(self, chat_id, text, **kw):
        from telegram import Chat, Message

        self.calls.append(
            {"chat_id": chat_id, "text": text, "markup": kw.get("reply_markup")}
        )
        return Message(
            message_id=1, date=None, chat=Chat(id=chat_id, type="group"), text=text
        )


async def main() -> int:
    runtime = load_runtime()
    group = runtime.home_groups[0]
    print(f"accounts : {len(load_accounts())}")
    print(f"group    : {group}")

    # Exactly what the live process does at startup.
    seeded = cache_mod.hydrate() or cache_mod.seed_from_db()
    counts, cold = cache_mod.counts_now()
    print(f"seeded   : {seeded}  counts={len(counts)}  cold={cold}")

    bot_user = User(
        id=7653116966, is_bot=True, first_name="bot", username="ourmailagent_bot"
    )

    async def fake_get_me(self, *a, **kw):
        self._bot_user = bot_user
        return bot_user

    cap = Captured()
    Bot.get_me = fake_get_me
    Bot.send_message = lambda self, **kw: cap.send_message(**kw)

    application = app_mod.build_application()
    application.bot._bot_user = bot_user

    text = "/dashboard"
    update = Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1700000000,
                "chat": {"id": group, "type": "group"},
                "from": {"id": USER, "is_bot": False, "first_name": "owner"},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
            },
        },
        application.bot,
    )

    started = time.monotonic()
    async with application:
        await application.process_update(update)
    elapsed = time.monotonic() - started

    print(f"\nhandler elapsed : {elapsed * 1000:.0f}ms")
    print(f"telegram calls  : {len(cap.calls)}")
    if not cap.calls:
        print("RESULT: FAIL - no message produced")
        return 1
    call = cap.calls[0]
    markup = call["markup"]
    rows = len(markup.inline_keyboard) if markup else 0
    labels = [b.text for r in markup.inline_keyboard for b in r] if markup else []
    print(f"-> chat_id      : {call['chat_id']}")
    print(f"keyboard rows   : {rows}")
    print("\n--- message body ---")
    print(call["text"][:700])
    print("\n--- key controls ---")
    print([l for l in labels if "Critical" in l or "Important" in l][:6])

    ok = (
        call["chat_id"] == group
        and elapsed < 2.0
        and rows >= 3
        and "Priority dashboard" in call["text"]
    )
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
