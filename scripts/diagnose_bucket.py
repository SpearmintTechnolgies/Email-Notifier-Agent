"""READ-ONLY diagnosis of one mailbox's priority bucket.

Compares three things for a given account + priority:

  1. what ``message_locations`` (the database) believes
  2. what the mailbox actually contains over IMAP
  3. what ``mailops.cache.get_bucket`` would hand the Telegram screen

Strictly read-only: the database is opened with ``mode=ro`` and the only
IMAP verb used is ``envelope list``. Nothing is sent, moved, flagged,
archived, deleted or written.

Usage:  python scripts/diagnose_bucket.py [account_id] [priority]
"""
from __future__ import annotations

import os
import sys
import tempfile

os.environ["MAILOPS_LOG_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="diag-bucket-"), "probe.log"
)

from mailops import cache as cache_mod  # noqa: E402
from mailops.config import PRIORITY_TO_FOLDER, load_accounts  # noqa: E402
from mailops.himalaya_client import Himalaya  # noqa: E402
from mailops.paths import OPS_DB  # noqa: E402
from mailops.policy import organizes_physically, test_mode_enabled  # noqa: E402

ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "coinography"
PRIORITY = int(sys.argv[2]) if len(sys.argv) > 2 else 1


def db_rows(account_id: str):
    import sqlite3

    conn = sqlite3.connect(f"file:{OPS_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT msg_id, mailbox, subject, sender, priority "
                "FROM message_locations WHERE account_id=? ORDER BY priority",
                (account_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


def main() -> int:
    folder = PRIORITY_TO_FOLDER[PRIORITY]
    print(f"account        : {ACCOUNT}")
    print(f"priority       : P{PRIORITY} -> {folder}")
    print(f"test mode      : {test_mode_enabled()}")
    print(f"organizes phys : {organizes_physically(ACCOUNT)}")

    ids = [a.id for a in load_accounts()]
    print(f"in registry    : {ACCOUNT in ids}")
    if ACCOUNT not in ids:
        print(f"registry ids   : {ids}")
        return 1

    # ---------------------------------------------------------------- 1. DB
    rows = db_rows(ACCOUNT)
    by_pri: dict[int, list[dict]] = {}
    for r in rows:
        by_pri.setdefault(int(r["priority"] or -1), []).append(r)
    print("\n--- 1. DATABASE (message_locations) ---")
    print(f"total rows for account : {len(rows)}")
    for p in sorted(by_pri):
        print(f"  P{p}: {len(by_pri[p])}")
    target = by_pri.get(PRIORITY, [])
    print(f"rows at P{PRIORITY}            : {len(target)}")
    for r in target[:6]:
        print(f"    msg_id={r['msg_id']!r:>10} mailbox={r['mailbox']!r} subj={str(r['subject'])[:40]!r}")

    # -------------------------------------------------------------- 2. IMAP
    print("\n--- 2. LIVE MAILBOX (read-only envelope list) ---")
    him = Himalaya()
    try:
        boxes = him.mailbox_list(ACCOUNT)
        print(f"mailboxes on server : {boxes}")
    except Exception as exc:  # noqa: BLE001
        print(f"mailbox_list FAILED : {type(exc).__name__}: {exc}")
        boxes = []

    live: dict[str, list[dict]] = {}
    for box in ["INBOX", *PRIORITY_TO_FOLDER.values()]:
        if boxes and box != "INBOX" and box.lower() not in {b.lower() for b in boxes}:
            print(f"  {box:<22} (absent on server)")
            continue
        try:
            envs = him.envelopes(ACCOUNT, box, page_size=500)
            live[box] = envs
            print(f"  {box:<22} {len(envs)} envelopes")
        except Exception as exc:  # noqa: BLE001
            print(f"  {box:<22} FAILED {type(exc).__name__}: {str(exc)[:90]}")

    # ------------------------------------------------- 3. ID correlation
    print("\n--- 3. ID CORRELATION (db msg_id vs live envelope id) ---")
    db_ids = {str(r["msg_id"]) for r in rows}
    for box, envs in live.items():
        live_ids = {str(e.get("id") or e.get("uid") or "") for e in envs}
        overlap = db_ids & live_ids
        print(
            f"  {box:<22} live={len(live_ids):<4} overlap_with_db={len(overlap)}"
        )
        if envs and not overlap:
            sample_live = sorted(live_ids)[:5]
            print(f"      sample live ids : {sample_live}")
            print(f"      sample db  ids : {sorted(db_ids)[:5]}")

    # ----------------------------------------------- 4. What the UI gets
    print("\n--- 4. WHAT THE TELEGRAM SCREEN WOULD GET ---")
    cache_mod.hydrate() or cache_mod.seed_from_db()
    counts, cold = cache_mod.counts_now()
    mine = [c for c in counts if c.account_id == ACCOUNT]
    if mine:
        c = mine[0]
        print(f"count shown on dashboard : p0={c.p0} p1={c.p1} p2={c.p2} p3={c.p3}")
        print(f"  -> button would read   : {ACCOUNT} Important ({c.p1})")
    print(f"cache cold               : {cold}")
    print(f"cache stamp              : {cache_mod.cache_stamp()}")

    items = cache_mod.get_bucket(PRIORITY, ACCOUNT)
    print(f"get_bucket() items       : {len(items)}  <-- the list screen")
    for it in items[:5]:
        print(f"    {it['subject'][:50]!r} from={it['from'][:28]!r}")

    expected = len(target)
    print("\n--- VERDICT ---")
    print(f"database says {expected} at P{PRIORITY}; screen would show {len(items)}")
    if expected and not items:
        print("MISMATCH: non-zero count but empty list (the reported bug)")
        return 1
    print("consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
