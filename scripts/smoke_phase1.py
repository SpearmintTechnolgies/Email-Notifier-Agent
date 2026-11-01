"""Live smoke check against the REAL mailbox — manual use only.

WARNING: This script touches the real inbox (creates folders, moves one
message, sends Telegram pushes). It is NOT part of the automated test suite.
Run the safe suite instead:  python -m pytest tests

Only run this after Gmail OAuth login works and you explicitly want an
end-to-end live check. Requires MAILOPS_TEST_MODE=0 and a configured .env.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mailops.config import load_accounts, load_runtime, load_settings
from mailops.db import init_db
from mailops.himalaya_client import Himalaya
from mailops.notify import handle_event
from mailops.sync_worker import process_account
from mailops.taxonomy import bootstrap_all


def require_confirm() -> None:
    print("This will MOVE one real message and SEND Telegram pushes.")
    if os.environ.get("MAILOPS_LIVE_SMOKE") != "1":
        print("Refusing to run. Set MAILOPS_LIVE_SMOKE=1 to confirm.")
        raise SystemExit(2)


async def main() -> int:
    require_confirm()
    runtime = load_runtime()
    if not runtime.allowed_users:
        print("TELEGRAM_ALLOWED_USERS is empty; set your Telegram user id in .env")
        return 2
    user_id = runtime.allowed_users[0]

    init_db()
    print("bootstrap:", bootstrap_all())

    accounts = load_accounts()
    if not accounts:
        print("No accounts configured.")
        return 1

    events: list[dict] = []

    async def notify(ev: dict) -> None:
        events.append(ev)

    him = Himalaya()
    for acc in accounts:
        items = await process_account(him, acc.id, acc.email, notify)
        print(f"{acc.id}: processed {len(items)}")

    from telegram import Bot

    bot = Bot(token=runtime.telegram_token)
    for ev in events:
        await handle_event(bot, ev)
    print(f"pushed {len(events)} notifications to Telegram user {user_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
