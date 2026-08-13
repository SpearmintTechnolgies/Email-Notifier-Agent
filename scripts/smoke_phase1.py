"""Phase 1 smoke checks — IMAP folders, draft/Waiting filing, notify dedupe."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telegram import Bot

from mailops.bot import keyboards as kb
from mailops.config import PRIORITY_FOLDERS, load_accounts, load_runtime
from mailops.db import (
    already_notified,
    clear_draft,
    db,
    get_draft,
    init_db,
    save_draft,
)
from mailops.himalaya_client import Himalaya
from mailops.notify import handle_event, send_to_home


def main() -> int:
    init_db()
    him = Himalaya()
    accounts = load_accounts()
    runtime = load_runtime()

    print("=== FOLDER COUNTS (live IMAP) ===")
    counts: dict[str, int] = {}
    sample = None
    for acc in accounts:
        for folder in PRIORITY_FOLDERS + ["INBOX"]:
            envs = him.envelopes(acc.id, folder, page_size=50)
            key = f"{acc.id}/{folder}"
            counts[key] = len(envs)
            if sample is None and envs and folder.startswith("INBOX.P"):
                sample = (
                    acc.id,
                    folder,
                    str(envs[0].get("id")),
                    str(envs[0].get("subject")),
                )
            if envs:
                print(f"{key}: {len(envs)}")

    print("sample=", sample)

    print("\n=== REPLY CLI ===")
    help_r = him.run(["message", "reply", "--help"])
    blob = (help_r.stdout + help_r.stderr).lower()
    print("reply help ok=", "reply" in blob and "--send" in blob, "code=", help_r.code)

    print("\n=== DRAFT + MOVE TO WAITING (reply filing) ===")
    if sample:
        aid, mailbox, mid, _subj = sample
        uid = 6877637692
        save_draft(uid, aid, mailbox, mid, "Mailops Phase1 reply-flow self-test (not sent).")
        d = get_draft(uid)
        assert d and d["msg_id"] == mid and "self-test" in d["body"]
        print("draft saved ok")
        r = him.move(aid, mid, "INBOX.Waiting", mailbox)
        print("move Waiting:", r.ok, (r.stderr or r.stdout)[:200])
        if not r.ok:
            return 1
        r2 = him.move(aid, mid, mailbox, "INBOX.Waiting")
        print("restore:", r2.ok, (r2.stderr or r2.stdout)[:200])
        clear_draft(uid)
    else:
        print("No P-folder sample to test move")
        return 1

    print("\n=== KEYBOARDS ===")
    markup = kb.main_menu()
    assert len(markup.keyboard) == 3
    assert markup.keyboard[0][0].text == "Critical"
    assert kb.thread_keyboard("sales", "INBOX.P1-Important", "1")
    print("main/thread keyboards ok")

    print("\n=== NOTIFY DEDUPE + PUSH ===")

    async def notify_smoke() -> None:
        bot = Bot(runtime.telegram_token)
        key = "smoke:notify:phase1"
        with db() as conn:
            conn.execute("DELETE FROM notified WHERE key=?", (key,))
        event = {
            "type": "new_mail",
            "key": key,
            "account_id": "sales",
            "email": "sales@coinography.com",
            "msg_id": sample[2],
            "mailbox": sample[1],
            "subject": "Mailops Phase 1 online",
            "sender": "mailops@local",
            "priority": 1,
            "tags": ["ops"],
            "reason": "smoke test — Open / Reply buttons attached",
        }
        await handle_event(bot, event)
        assert already_notified(key)
        await handle_event(bot, event)  # dedupe no-op
        p1 = sum(v for k, v in counts.items() if k.endswith("P1-Important"))
        p2 = sum(v for k, v in counts.items() if k.endswith("P2-Normal"))
        p3 = sum(v for k, v in counts.items() if k.endswith("P3-Noise"))
        p0 = sum(v for k, v in counts.items() if k.endswith("P0-Critical"))
        await send_to_home(
            bot,
            "Inbox dashboard ready.\nMenu: Critical · Needs reply · Inboxes · Search · Settings",
            kb.home_inline({"p0": p0, "p1": p1, "p2": p2, "p3": p3}),
        )
        print("telegram pushes sent")

    asyncio.run(notify_smoke())
    print("\nALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
