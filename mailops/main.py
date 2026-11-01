from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Allow `python -m mailops.main` from mailops root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mailops.config import load_runtime, load_accounts, load_settings
from mailops.paths import DATA_DIR, log_file


def _setup_logging() -> None:
    # Resolve the target FIRST: under pytest this raises for the live log
    # before any existing handler is touched.
    target = log_file()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    # Separate from launcher redirects (bot.stderr.log) to avoid Windows file locks.
    fh = logging.FileHandler(target, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    # httpx logs every request URL at INFO, and Telegram's URL embeds the bot
    # token. Quiet those loggers AND scrub secrets from every record,
    # including exception tracebacks (defence in depth).
    from mailops.redact import install as install_redaction

    install_redaction(root)

    def _redacting_excepthook(exc_type, exc, tb):
        # Uncaught crashes print tracebacks straight to stderr, bypassing the
        # logging handlers; route them through the redacting logger instead.
        logging.getLogger("mailops").critical("uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = _redacting_excepthook


# NOTE: logging is deliberately NOT configured at import time. Importing this
# module (tests, tooling) must never attach a handler to the live
# data/mailops.log. Real launches go through ``cli()`` below.
log = logging.getLogger("mailops")


async def sync_loop(app) -> None:
    from mailops.config import load_settings
    from mailops.notify import make_notifier
    from mailops.digest import build_and_send_digest
    from mailops.sync_worker import retry_pending_notifications, sync_once

    bot = app.bot
    notify = make_notifier(bot)
    # Let Telegram polling come up before the first heavy IMAP pass
    await asyncio.sleep(3)
    while True:
        try:
            # Drain any leftover per-email alerts queued by older versions
            # into the new digest table. This never sends a Telegram message
            # on its own (see sync_worker.retry_pending_notifications) so it
            # cannot replay the Alien FI flood on restart.
            migrated = await retry_pending_notifications(notify)
            if migrated:
                log.info("migrated %s legacy pending notification(s) into digest", migrated)
            processed = await sync_once(notify)
            if processed:
                log.info("classified %s messages", len(processed))
            else:
                log.info("sync idle: 0 new messages")
            # Auto-refresh IMAP metadata cache after each check (off event loop)
            from mailops.cache import refresh_all

            await asyncio.to_thread(refresh_all)
            # ONE consolidated digest for the whole account roster. This is
            # the sole automatic Telegram notification path for new mail
            # (points 1, 2, 11 of the redesign) - no per-email send, no
            # per-mailbox fan-out, and only one message when at least one
            # mailbox actually has new P0/P1.
            try:
                diag = await build_and_send_digest(bot, reason="scheduled")
                if diag.get("sent"):
                    log.info(
                        "digest sent: mailboxes=%s actionable=%s covered=%s",
                        diag.get("mailboxes"),
                        diag.get("actionable"),
                        diag.get("covered_keys"),
                    )
                elif diag.get("skipped"):
                    log.info("digest skipped: %s", diag.get("skipped"))
            except Exception:  # noqa: BLE001
                log.exception("digest emission failed; rows stay pending for next tick")
            # Consolidated operational-health alert (October 2026 incident
            # fix). Completely separate message from the mail digest above —
            # new-email counts and sync-error incidents are never mixed into
            # the same Telegram message. Sends nothing when no account's
            # health state changed since the last alert (see mailops.health).
            #
            # MAILOPS activation redesign (phase 3, hard rule): with 33+
            # mailboxes sharing a handful of SiteGround hosts, "no recurring
            # identical health-error alerts" additionally means this
            # STANDALONE exceptional alert defaults to OFF
            # (``ops_alert_standalone_enabled``) - the unhealthy count and
            # per-mailbox detail already live in the one 15-minute control
            # card's "Check Unhealthy Accounts" view, which is always
            # current without ever sending its own Telegram push. Flipping
            # the setting on restores the old behaviour for an operator who
            # explicitly wants a separate, deduplicated incident ping.
            if bool(load_settings().get("ops_alert_standalone_enabled", False)):
                try:
                    from mailops.health import build_and_send_health_alert

                    hdiag = await build_and_send_health_alert(bot, reason="scheduled")
                    if hdiag.get("sent"):
                        log.info("ops health alert sent: %s account(s) changed", hdiag.get("changed"))
                    elif hdiag.get("skipped"):
                        log.info("ops health alert skipped: %s", hdiag.get("skipped"))
                except Exception:  # noqa: BLE001
                    log.exception("ops health alert emission failed; incidents stay pending for next tick")
        except asyncio.CancelledError:
            # Shutdown requested — exit cleanly instead of looping.
            log.info("sync loop cancelled; shutting down")
            raise
        except Exception:  # noqa: BLE001
            # Recoverable sync/cache/Telegram error: log and keep the loop
            # alive so one bad pass never terminates the bot.
            log.exception("sync loop error")
        try:
            interval = int(load_settings().get("sync_interval_seconds", 900))
            wait = max(60, interval)
            log.info("next IMAP check in %ss", wait)
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            log.info("sync loop cancelled during sleep; shutting down")
            raise


# Track the background task so shutdown can cancel and await it.
_SYNC_TASK_NAME = "mailops-sync-loop"
_WEB_RUNNER_NAME = "mailops-web-runner"
_BULK_TASK_NAME = "mailops-bulk-resume"


async def post_init(app) -> None:
    # Loudly warn if Telegram privacy mode is still enabled — in group
    # chats this causes plain text (including the Quick Add Email paste)
    # to be invisible to the bot. Operators then see Quick Add "do
    # nothing" with no sign of the message in logs. Nothing is changed
    # programmatically; privacy mode is only toggleable through
    # @BotFather /setprivacy.
    try:
        me = await app.bot.get_me()
        can_read = bool(getattr(me, "can_read_all_group_messages", False))
        if not can_read:
            log.warning(
                "telegram privacy mode is ON (can_read_all_group_messages=False) "
                "— plain text in groups will not reach the bot. Quick Add Email "
                "paste requires either (a) /setprivacy Disable in @BotFather "
                "and re-adding the bot to the group, or (b) the /qadd command "
                "fallback."
            )
        else:
            log.info("telegram privacy mode is OFF — group text updates will be delivered")
    except Exception:  # noqa: BLE001
        log.debug("privacy-mode startup check failed", exc_info=True)
    # Create after the application is running so PTB awaits the task.
    task = asyncio.create_task(sync_loop(app), name=_SYNC_TASK_NAME)
    app.bot_data[_SYNC_TASK_NAME] = task
    log.info("sync loop started")
    # Restore the last known dashboard counts, then warm in the background.
    #
    # The metadata cache is in memory, so a restart used to leave the
    # dashboard with nothing until a full-fleet IMAP pass finished - 6-13
    # minutes on this 39-mailbox fleet, during which /dashboard had nothing
    # to show. hydrate() reloads the persisted snapshot (one small SQLite
    # read) so the dashboard is useful immediately with an honest age stamp,
    # and the refresh below replaces those numbers mailbox by mailbox as it
    # progresses. Single-flight, so it never races the sync loop's refresh.
    try:
        from mailops.cache import ensure_refresh_running, hydrate, seed_from_db

        if hydrate() or seed_from_db():
            log.info("dashboard counts available immediately; refreshing in background")
        ensure_refresh_running()
    except Exception:  # noqa: BLE001 - startup warm is best effort
        log.exception("startup cache warm could not be started")
    # Bulk mailbox import worker — processes any CSV job that was running
    # or confirmed at crash/restart. Nothing is done if no job is pending.
    bulk_task = asyncio.create_task(bulk_resume_loop(app), name=_BULK_TASK_NAME)
    app.bot_data[_BULK_TASK_NAME] = bulk_task
    # Optional secure onboarding web server (loopback HTTPS). Enabled when
    # MAILOPS_WEB_ENABLE=1 is set in the environment. We NEVER bind to a
    # public address; a tunnel in front terminates the public TLS. See
    # docs/REMOTE_ONBOARDING.md.
    if (os.environ.get("MAILOPS_WEB_ENABLE") or "").strip() == "1":
        try:
            from mailops.web.app import start_web_server

            runner = await start_web_server()
            app.bot_data[_WEB_RUNNER_NAME] = runner
        except Exception:  # noqa: BLE001
            log.exception("web onboarding server failed to start; continuing without it")


async def bulk_resume_loop(app) -> None:
    """Resume any bulk CSV job that survived a crash/restart.

    Scans ``bulk_jobs`` every minute. Picks up jobs in state 'confirmed' or
    'running' and runs the validator in-process. Idempotent: already-done
    rows are skipped, in-flight rows that were 'running' at crash time are
    retried by a fresh ``claim_row`` call (status is reset in the DB only
    once the worker finishes a row).
    """
    from mailops.bulk import jobs as bulk_jobs, validator as bulk_validator

    # Reset any 'running' row from a previous process back to 'pending' so a
    # restart doesn't leave rows stuck in 'running'. The claim_row path
    # re-arms them under a new worker's atomic UPDATE.
    try:
        from mailops.db import db as _db

        with _db() as conn:
            conn.execute(
                "UPDATE bulk_rows SET status='pending', started_at=NULL "
                "WHERE status='running'"
            )
    except Exception:  # noqa: BLE001
        log.exception("bulk restart: could not reset in-flight rows")

    tasks: dict[str, asyncio.Task] = {}
    while True:
        try:
            for job in bulk_jobs.runnable_jobs():
                existing = tasks.get(job.job_id)
                if existing is not None and not existing.done():
                    continue

                async def _run_one(jid=job.job_id):
                    try:
                        await bulk_validator.run_job(jid)
                    except Exception:  # noqa: BLE001
                        log.exception("bulk validator for %s crashed", jid)

                tasks[job.job_id] = asyncio.create_task(
                    _run_one(), name=f"bulk-resume-{job.job_id}"
                )
        except asyncio.CancelledError:
            log.info("bulk resume loop cancelled")
            raise
        except Exception:  # noqa: BLE001
            log.exception("bulk resume loop error")
        # Drop finished tasks so the dict doesn't grow unbounded.
        tasks = {k: v for k, v in tasks.items() if not v.done()}
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise


async def post_shutdown(app) -> None:
    """Cancel and await the background sync task so the event loop never
    closes with it still pending ('Task was destroyed but it is pending!')."""
    # Ask the cache warm thread to stop at the next mailbox boundary, and
    # persist whatever it has published, so a restart still comes back warm
    # and we don't leave orphaned Himalaya subprocesses behind.
    try:
        from mailops.cache import request_abort, save_counts

        request_abort()
        save_counts()
    except Exception:  # noqa: BLE001
        log.debug("cache shutdown hook failed", exc_info=True)
    task = app.bot_data.get(_SYNC_TASK_NAME)
    if task is not None:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("sync task raised during shutdown")
        log.info("sync loop stopped")
    bulk_task = app.bot_data.get(_BULK_TASK_NAME)
    if bulk_task is not None:
        if not bulk_task.done():
            bulk_task.cancel()
        try:
            await bulk_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("bulk resume task raised during shutdown")
    runner = app.bot_data.get(_WEB_RUNNER_NAME)
    if runner is not None:
        try:
            await runner.cleanup()
        except Exception:  # noqa: BLE001
            log.exception("web onboarding server shutdown error")
        log.info("web onboarding server stopped")


def cmd_bootstrap() -> int:
    from mailops.db import init_db
    from mailops.taxonomy import bootstrap_all

    init_db()
    print(bootstrap_all())
    return 0


def cmd_sync_once() -> int:
    from mailops.db import init_db
    from mailops.sync_worker import sync_once

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
    from mailops.db import init_db
    from mailops.sync_worker import rescan_all
    from mailops.taxonomy import bootstrap_all

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


def cmd_backfill_locations() -> int:
    """One-time repair: record message_locations for emails processed before
    that table existed. Read-only against Gmail (no moves/copies/deletes),
    sends no notifications, and is safe to rerun."""
    from mailops.db import init_db
    from mailops.sync_worker import backfill_all_locations

    init_db()

    async def _run():
        return await backfill_all_locations()

    results = asyncio.run(_run())
    total = 0
    for acc_id, n in results.items():
        print(f"{acc_id}: {n} location(s) recorded")
        total += n
    print(f"backfill complete: {total} message_locations written")
    return 0


def cmd_run() -> int:
    from mailops.db import init_db
    from mailops.singleton import acquire_or_exit
    from mailops.taxonomy import bootstrap_all

    acquire_or_exit()
    init_db()
    runtime = load_runtime()
    accounts = load_accounts()
    log.info("accounts=%s", [a.id for a in accounts])
    log.info("active account: %s", ", ".join(f"{a.id} <{a.email}>" for a in accounts) or "none")
    if not accounts:
        log.error("no active accounts: nothing to manage. Check himalaya/accounts.json")
    from mailops import access as access_mod

    access_mod.ensure()
    log.info(
        "home_channels=%s (groups=%s) env_allowed=%s access_users=%s access_groups=%s bootstrap_owner=%s",
        runtime.home_channels,
        runtime.home_groups,
        runtime.allowed_users,
        [(u.user_id, u.role) for u in access_mod.users()],
        access_mod.group_ids(),
        access_mod.bootstrap_owner_id(),
    )
    from mailops.config import load_dotenv, parse_id_list

    _env = load_dotenv()
    for _key in ("TELEGRAM_ALLOWED_USERS", "TELEGRAM_HOME_CHANNEL"):
        _, _bad = parse_id_list(_env.get(_key))
        if _bad:
            log.warning("%s has unparsable entries (ignored): %s", _key, _bad)
    if runtime.home_channel is None:
        log.warning("TELEGRAM_HOME_CHANNEL empty: alerts go to the first allowed user only")
    if not access_mod.users():
        log.warning("access store empty: all Telegram access is denied (fail closed)")
    print(bootstrap_all())
    from mailops.bot.app import build_application

    app = build_application(post_init=post_init, post_shutdown=post_shutdown)
    from mailops.safe_mode import test_mode_enabled

    if test_mode_enabled():
        log.info("TEST MODE active: mailbox mutations and sends are blocked")
    key_state = "present" if runtime.openai_api_key.strip() else "MISSING"
    log.info(
        "llm provider=%s model=%s key=%s",
        runtime.openai_base_url,
        runtime.model,
        key_state,
    )
    if not runtime.openai_api_key.strip():
        log.warning(
            "OPENAI_API_KEY is empty: classification uses keyword rules then P2 default; AI drafts will fail"
        )
    if runtime.model == "auto" and "openai.com" in runtime.openai_base_url.lower():
        log.warning(
            "OPENAI_MODEL is unset; api.openai.com rejects model=auto. Set OPENAI_MODEL in .env"
        )
    interval = int(load_settings().get("sync_interval_seconds", 900))
    log.info("sync interval %ss (sleep floor 60s)", interval)
    log.info("Starting Telegram button bot")
    app.run_polling(allowed_updates=["message", "callback_query", "my_chat_member"])
    return 0


def cmd_accounts() -> int:
    accounts = load_accounts()
    if not accounts:
        print(
            "No accounts registered. Use: python -m mailops webmail-add <id> <email> "
            "--imap-host <host> --smtp-host <host>"
        )
        return 0
    for a in accounts:
        marker = " (default)" if a.default else ""
        print(f"{a.id}: {a.email}{marker}")
    return 0


def cmd_policy() -> int:
    """Show what each account may do. Read-only: never contacts a mailbox."""
    from mailops.policy import policy_for
    from mailops.safe_mode import test_mode_enabled

    print(f"test mode: {'ON (every mutation/send is a logged no-op)' if test_mode_enabled() else 'off'}")
    for a in load_accounts():
        p = policy_for(a.id)
        print(
            f"{a.id}: organize={'yes' if p.organize else 'NO'} "
            f"send={'yes (per-email approval)' if p.send else 'NO'}"
            f"{' [locked read-only]' if p.locked else ''}"
        )
    return 0


def cmd_gmail_setup(account_id: str, email: str, display_name: str = "") -> int:
    from mailops.config import GMAIL_ENABLED
    from mailops.gmail_oauth import GmailAccountSpec, ensure_config, oauth_credentials_present

    if not GMAIL_ENABLED:
        print(
            "Gmail is disabled for this deployment: nothing was written. "
            "Onboard business mailboxes with: python -m mailops webmail-add"
        )
        return 1
    if not oauth_credentials_present():
        print(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set in .env.\n"
            "Create a Google Cloud OAuth 'Desktop app' client first - see README "
            "section 'Gmail OAuth setup'. No config written."
        )
        return 1
    spec = GmailAccountSpec(
        account_id=account_id,
        email=email,
        display_name=display_name or email.split("@", 1)[0],
        default=not load_accounts(),
    )
    print(ensure_config(spec))
    print(f"Next: python -m mailops gmail-login {account_id}")
    return 0


def cmd_gmail_login(account_id: str) -> int:
    from mailops.gmail_oauth import run_login

    return run_login(account_id)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Webmail onboarding commands own their option parsing (--imap-host ...).
    if argv and argv[0] == "webmail-add":
        from mailops.webmail_onboarding import cli_add

        return cli_add(argv[1:])
    if argv and argv[0] == "webmail-check":
        from mailops.webmail_onboarding import cli_check

        if len(argv) < 2:
            print("usage: python -m mailops webmail-check <account_id>")
            return 2
        return cli_check(argv[1])
    if argv and argv[0] == "webmail-set-password":
        from mailops.webmail_onboarding import cli_set_password

        return cli_set_password(argv[1:])
    if argv and argv[0] == "webmail-run-pending":
        from mailops.webmail_onboarding import cli_run_pending

        return cli_run_pending(argv[1:])
    if argv and argv[0] == "get-secret":
        from mailops.webmail_onboarding import cli_get_secret

        if len(argv) < 3:
            return 2
        return cli_get_secret(argv[1], argv[2])
    parser = argparse.ArgumentParser(
        description="Mailops email notifier agent. Webmail onboarding: "
        "webmail-add [id email --imap-host H --smtp-host H ...] | webmail-check <id> | "
        "webmail-set-password <id>"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "bootstrap", "sync", "rescan", "backfill-locations", "accounts", "policy", "gmail-setup", "gmail-login", "get-token"],
    )
    parser.add_argument("args", nargs="*", help="command arguments")
    args = parser.parse_args(argv)
    if args.command == "bootstrap":
        return cmd_bootstrap()
    if args.command == "sync":
        return cmd_sync_once()
    if args.command == "rescan":
        return cmd_rescan()
    if args.command == "backfill-locations":
        return cmd_backfill_locations()
    if args.command == "accounts":
        return cmd_accounts()
    if args.command == "policy":
        return cmd_policy()
    if args.command == "gmail-setup":
        if len(args.args) < 2:
            print("usage: python -m mailops gmail-setup <account_id> <email> [display name]")
            return 2
        return cmd_gmail_setup(args.args[0], args.args[1], " ".join(args.args[2:]))
    if args.command == "gmail-login":
        if len(args.args) < 1:
            print("usage: python -m mailops gmail-login <account_id>")
            return 2
        return cmd_gmail_login(args.args[0])
    if args.command == "get-token":
        if len(args.args) < 1:
            print("usage: python -m mailops get-token <account_id>")
            return 2
        from mailops.gmail_oauth import get_access_token

        tok = get_access_token(args.args[0])
        if not tok:
            return 1
        sys.stdout.write(tok)
        return 0
    return cmd_run()


def cli(argv: list[str] | None = None) -> int:
    """Process entry point: configure logging (file + redaction), then run."""
    _setup_logging()
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(cli())
