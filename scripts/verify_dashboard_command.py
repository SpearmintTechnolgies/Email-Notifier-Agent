"""Offline verification of the /dashboard fix at production scale.

Loads the REAL 39-account registry and runs the REAL command dispatcher, but
with a stub mail provider (no IMAP, no subprocess, no mailbox touched) and a
captured Telegram transport (nothing is sent). Proves that a cold cache now
answers /dashboard inside the budget instead of blocking on a full-fleet
refresh.

Read-only: no mailbox, no settings file, no secret and no AWS call.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

# Keep this probe out of the live log before mailops is imported.
_PROBE_DIR = tempfile.mkdtemp(prefix="dashboard-verify-")
os.environ["MAILOPS_LOG_FILE"] = os.path.join(_PROBE_DIR, "probe.log")

from telegram import Bot, Update, User  # noqa: E402

import mailops.bot.app as app_mod  # noqa: E402
import mailops.db as db_mod  # noqa: E402
import mailops.paths as paths_mod  # noqa: E402
from mailops import cache as cache_mod  # noqa: E402
from mailops.config import load_accounts, load_runtime  # noqa: E402

# Read-only guarantee: the probe gets its own throw-away SQLite file, so the
# live data/ops.db (and its history) is never opened, let alone written.
_PROBE_DB = os.path.join(_PROBE_DIR, "probe.db")
db_mod.OPS_DB = _PROBE_DB
paths_mod.OPS_DB = _PROBE_DB

GROUP = -5531260744
USER = 8925736576


class StubProvider:
    """Stands in for Himalaya. Simulates a slow fleet; touches nothing."""

    def __init__(self, latency: float) -> None:
        self.latency = latency
        self.calls = 0

    def envelopes(self, account, mailbox="INBOX", page_size=25, page=1):
        self.calls += 1
        time.sleep(self.latency)
        return []

    def mailbox_list(self, account):
        self.calls += 1
        time.sleep(self.latency)
        return ["INBOX"]

    def uid_validity(self, account, mailbox="INBOX"):
        return ""


class Captured:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.markups: list = []

    async def send_message(self, chat_id, text, **kw):
        from telegram import Chat, Message

        self.messages.append({"chat_id": chat_id, "text": text})
        self.markups.append(kw.get("reply_markup"))
        return Message(
            message_id=1, date=None, chat=Chat(id=chat_id, type="group"), text=text
        )


async def main() -> int:
    accounts = load_accounts()
    print(f"real account registry loaded: {len(accounts)} accounts")
    runtime = load_runtime()
    print(f"authorized groups: {runtime.home_groups}")

    provider = StubProvider(latency=2.0)
    cache_mod.set_provider(provider)
    # Cold, exactly like a fresh restart.
    cache_mod._counts = []
    cache_mod._buckets = {}
    cache_mod._updated_at = 0.0
    cache_mod._sf_event = None
    print(f"cache cold? {cache_mod.is_cold()}")

    bot_user = User(id=7653116966, is_bot=True, first_name="bot", username="ourmailagent_bot")

    async def fake_get_me(self, *a, **kw):
        self._bot_user = bot_user
        return bot_user

    captured = Captured()
    Bot.get_me = fake_get_me
    Bot.send_message = lambda self, **kw: captured.send_message(**kw)

    application = app_mod.build_application()
    application.bot._bot_user = bot_user

    text = "/dashboard"
    update = Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1700000000,
                "chat": {"id": GROUP, "type": "group"},
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

    print(f"\nelapsed: {elapsed:.2f}s (budget {app_mod.DASHBOARD_BUDGET_SECONDS}s)")
    print(f"messages sent to Telegram transport: {len(captured.messages)}")
    for m in captured.messages:
        print(f"  -> chat_id={m['chat_id']}")
        print("     " + m["text"].replace("\n", "\n     ")[:600])
    print(f"\nstub IMAP calls made (background warm, not blocking): {provider.calls}")
    mk = captured.markups[0] if captured.markups else None
    rows = len(mk.inline_keyboard) if mk is not None else 0
    labels = (
        [b.text for row in mk.inline_keyboard for b in row] if mk is not None else []
    )
    print(f"keyboard rows: {rows}")
    print(f"controls: {labels}")

    # The dashboard must answer effectively instantly and carry its real
    # interactive controls, even with nothing cached.
    ok = (
        bool(captured.messages)
        and elapsed < 2.0
        and rows >= 3
        and any("Critical" in l for l in labels)
    )
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
