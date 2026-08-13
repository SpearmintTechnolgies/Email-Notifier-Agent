from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Allow `python -m mailops.main` from mailops root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mailops.bot.app import build_application
from mailops.config import load_runtime, load_accounts
from mailops.db import init_db
from mailops.notify import flush_p2_batch, make_notifier
from mailops.paths import DATA_DIR
from mailops.sync_worker import sync_once
from mailops.taxonomy import bootstrap_all


def _setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    # Separate from launcher redirects (bot.stderr.log) to avoid Windows file locks.
    fh = logging.FileHandler(DATA_DIR / "mailops.log", encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


_setup_logging()
log = logging.getLogger("mailops")


async def sync_loop(app) -> None:
    from mailops.config import load_settings
    from mailops.notify import send_dashboard_digest

    bot = app.bot
    notify = make_notifier(bot)
    # Let Telegram polling come up before the first heavy IMAP pass
    await asyncio.sleep(3)
    while True:
        try:
            processed = await sync_once(notify)
            if processed:
                log.info("classified %s messages", len(processed))
            await flush_p2_batch(bot)
            # Auto-refresh IMAP metadata cache after each check (off event loop)
            from mailops.cache import refresh_all

            await asyncio.to_thread(refresh_all)
            note = "Auto-check complete — cache refreshed."
            if processed:
                p01 = sum(1 for x in processed if x.priority <= 1)
                note = f"Sorted {len(processed)} new · {p01} priority. Cache refreshed."
            await send_dashboard_digest(bot, note=note)
        except Exception:  # noqa: BLE001
            log.exception("sync loop error")
        interval = int(load_settings().get("sync_interval_seconds", 3600))
        await asyncio.sleep(max(60, interval))


async def post_init(app) -> None:
    # Create after the application is running so PTB awaits the task.
    asyncio.create_task(sync_loop(app), name="mailops-sync-loop")
    log.info("sync loop started")


def cmd_bootstrap() -> int:
    init_db()
    print(bootstrap_all())
    return 0


def cmd_sync_once() -> int:
    init_db()

    async def _run():
        items = await sync_once(None)
        for it in items:
            print(
                f"[{it.account_id}] P{it.priority} -> {it.folder} | {it.subject} ({it.reason})"
            )
        print(f"processed={len(items)}")

    asyncio.run(_run())
    return 0


def cmd_rescan() -> int:
    """One-shot: classify + sort every message in INBOX and P0–P3 across all accounts."""
    from mailops.sync_worker import rescan_all

    init_db()
    print(bootstrap_all())
    print("Full rescan starting (INBOX + P0-P3)...")

    async def _run():
        items = await rescan_all()
        items_sorted = sorted(items, key=lambda x: (x.account_id, x.priority, x.subject.lower()))
        print("")
        print("=== ALL EMAILS (sorted by account, priority, subject) ===")
        for it in items_sorted:
            print(
                f"[{it.account_id}] P{it.priority} {it.folder} | {it.subject} | {it.sender} | {it.reason}"
            )
        print("")
        by_prio: dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0}
        by_acc: dict[str, dict[int, int]] = {}
        for it in items:
            by_prio[it.priority] = by_prio.get(it.priority, 0) + 1
            bucket = by_acc.setdefault(it.account_id, {0: 0, 1: 0, 2: 0, 3: 0})
            bucket[it.priority] = bucket.get(it.priority, 0) + 1
        print("=== SUMMARY ===")
        for acc, counts in by_acc.items():
            print(
                f"  {acc}: P0={counts.get(0, 0)} P1={counts.get(1, 0)} "
                f"P2={counts.get(2, 0)} P3={counts.get(3, 0)} total={sum(counts.values())}"
            )
        print(
            f"  ALL: P0={by_prio[0]} P1={by_prio[1]} P2={by_prio[2]} P3={by_prio[3]} "
            f"processed={len(items)}"
        )

    asyncio.run(_run())
    return 0


def cmd_run() -> int:
    from mailops.singleton import acquire_or_exit

    acquire_or_exit()
    init_db()
    runtime = load_runtime()
    accounts = load_accounts()
    log.info("accounts=%s", [a.id for a in accounts])
    log.info("home_channel=%s allowed=%s", runtime.home_channel, runtime.allowed_users)
    print(bootstrap_all())
    app = build_application(post_init=post_init)
    log.info("Starting Telegram button bot (Hermes gateway should be stopped for this token)")
    app.run_polling(allowed_updates=["message", "callback_query"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mailops Phase 1")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "bootstrap", "sync", "rescan"],
    )
    args = parser.parse_args(argv)
    if args.command == "bootstrap":
        return cmd_bootstrap()
    if args.command == "sync":
        return cmd_sync_once()
    if args.command == "rescan":
        return cmd_rescan()
    return cmd_run()


if __name__ == "__main__":
    raise SystemExit(main())
