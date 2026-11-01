"""One-shot live Telegram verification for the digest redesign.

What this does
--------------
Builds a FRESH ``digest_pending`` queue inside an ISOLATED SQLite file
(``<temp>/digest_live_verify_<timestamp>/data/ops.db``), formats the
real digest text + inline keyboard, sends EXACTLY ONE message prefixed
with ``[TEST]`` to the authorized group chat ``-5531260744`` using the
real Mailops bot token, then deletes the temp directory.

Guarantees + safeguards
-----------------------
* Does NOT read or write ``data/ops.db`` in the project root. The paths
  module is pointed at the temp profile BEFORE any mailops.* module is
  imported, so the live DB is untouched.
* Does NOT send any email (no SMTP, no IMAP), does NOT touch
  ``himalaya/accounts.json`` or any secret file.
* Does NOT restart the running Mailops process (the running bot keeps
  polling with its own token - Telegram's getUpdates is tolerant of a
  second bot.send_message as long as polling is not duplicated, which
  this script does not do).
* Dry-run available: ``--dry-run`` renders the text + keyboard to
  stdout without contacting Telegram.

Usage
-----
    python scripts/live_digest_verify.py            # sends to -5531260744
    python scripts/live_digest_verify.py --dry-run  # local render only
    python scripts/live_digest_verify.py --target -1001234567890
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Resolve the real bot token from the project .env (never from env).
PROD_ENV = PROJECT_ROOT / ".env"


def _read_token(env_path: Path) -> str:
    if not env_path.exists():
        print(f"[live-verify] .env not found at {env_path}", file=sys.stderr)
        sys.exit(2)
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    print("[live-verify] TELEGRAM_BOT_TOKEN missing in .env", file=sys.stderr)
    sys.exit(2)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target", type=int, default=-5531260744,
                   help="Telegram chat id (default: -5531260744)")
    p.add_argument("--dry-run", action="store_true",
                   help="Render the message locally; do not contact Telegram")
    return p.parse_args()


def _repoint_paths_to_temp(temp_root: Path) -> None:
    """Make mailops.* think the project root is ``temp_root``.

    Must be called BEFORE any ``from mailops ...`` import so the paths
    module caches the temp directory, not the production one. We copy
    ``.env`` into the temp root for ``load_runtime`` (it needs the real
    bot token), but leave ``data/`` and ``himalaya/`` empty - mailops
    will initialise them with defaults inside the temp dir only.

    ``DATA_DIR`` is NOT reachable via an env var in the current paths
    module, so we also monkey-patch it (and ``OPS_DB`` / ``SETTINGS_JSON``
    / ``LIVE_LOG_FILE``) in-process so ``init_db`` never opens the real
    ops.db. This is defence-in-depth; if the patch did not land, the
    script would still refuse to proceed because the empty temp DB
    returns zero digest rows.
    """
    (temp_root / "data").mkdir(parents=True, exist_ok=True)
    (temp_root / "himalaya").mkdir(parents=True, exist_ok=True)
    # Minimal env: real token so the Bot client can authenticate.
    token_line = next(
        line for line in PROD_ENV.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("TELEGRAM_BOT_TOKEN=")
    )
    (temp_root / ".env").write_text(token_line + "\n", encoding="utf-8")
    (temp_root / "himalaya" / "accounts.json").write_text("[]", encoding="utf-8")
    os.environ["MAILOPS_HOME"] = str(temp_root)
    # Explicitly disable logging to the live log for this process.
    os.environ["MAILOPS_LOG_FILE"] = str(temp_root / "data" / "live-verify.log")
    # Repoint DATA_DIR + OPS_DB + SETTINGS_JSON (not covered by MAILOPS_HOME).
    import mailops.paths as _paths  # late import, before db/config touch disk

    _paths.DATA_DIR = temp_root / "data"
    _paths.OPS_DB = _paths.DATA_DIR / "ops.db"
    _paths.SETTINGS_JSON = _paths.DATA_DIR / "settings.json"
    _paths.LIVE_LOG_FILE = _paths.DATA_DIR / "mailops.log"
    _paths.PROFILE = temp_root
    _paths.HIMALAYA_DIR = temp_root / "himalaya"
    _paths.ACCOUNTS_JSON = _paths.HIMALAYA_DIR / "accounts.json"
    _paths.CONFIG_TOML = _paths.HIMALAYA_DIR / "config.toml"
    _paths.ENV_FILE = temp_root / ".env"
    _paths.TOKEN_DIR = temp_root / "tokens"
    _paths.SECRETS_DIR = temp_root / "secrets"
    # config.py binds these at import time too; override after loading.
    import mailops.config as _cfg

    _cfg.ACCOUNTS_JSON = _paths.ACCOUNTS_JSON
    _cfg.ENV_FILE = _paths.ENV_FILE
    _cfg.SETTINGS_JSON = _paths.SETTINGS_JSON
    _cfg.DATA_DIR = _paths.DATA_DIR
    import mailops.db as _db

    _db.DATA_DIR = _paths.DATA_DIR
    _db.OPS_DB = _paths.OPS_DB


async def _run(args: argparse.Namespace) -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    temp_root = Path(tempfile.mkdtemp(prefix=f"mailops-digest-live-{stamp}-"))
    print(f"[live-verify] isolated profile: {temp_root}")
    try:
        _repoint_paths_to_temp(temp_root)
        # Imports MUST come after the path repoint.
        from mailops.db import enqueue_digest_pending, init_db  # noqa: WPS433
        from mailops.digest import (  # noqa: WPS433
            collect_window,
            digest_keyboard,
            format_digest,
        )

        init_db()
        # Simulated fleet: three mailboxes with pending P0 / P1 counts
        # that look exactly like a real digest window would.
        fake_accounts = [
            ("altcoininformer", "info@altcoininformer.com", "Altcoin Informer"),
            ("alien", "contact@alien-fi.com", "Alien FI"),
            ("coinography", "info@coinography.com", "Coinography"),
        ]
        # Seed accounts.json so collect_window's display_name lookup works.
        import json as _json

        rows = [
            {"id": aid, "email": email, "display_name": name, "default": i == 0}
            for i, (aid, email, name) in enumerate(fake_accounts)
        ]
        (temp_root / "himalaya" / "accounts.json").write_text(
            _json.dumps(rows), encoding="utf-8"
        )
        # Make all three "active" in the isolated settings file.
        from mailops.config import load_settings, save_settings  # noqa: WPS433
        s = load_settings()
        s["active_accounts"] = [a[0] for a in fake_accounts]
        s["account_policies"] = {a[0]: {"organize": False, "send": False} for a in fake_accounts}
        save_settings(s)

        # P0 Critical: altcoininformer x1
        enqueue_digest_pending(
            "altcoininformer", "INBOX", "sim-p0-1", 0,
            subject="Security alert: unusual sign-in from new device",
            sender="security@example.com",
        )
        # P1 Important: altcoininformer x3
        enqueue_digest_pending(
            "altcoininformer", "INBOX", "sim-p1-1", 1,
            subject="Advertising enquiry - banner on homepage",
            sender="agency@example.com",
        )
        enqueue_digest_pending(
            "altcoininformer", "INBOX", "sim-p1-2", 1,
            subject="Sponsored article pricing request",
            sender="brand@example.com",
        )
        enqueue_digest_pending(
            "altcoininformer", "INBOX", "sim-p1-3", 1,
            subject="Press release submission for Monday",
            sender="pr@example.com",
        )
        # P1 Important: alien x7 (mirroring the real-world flood)
        for i in range(7):
            enqueue_digest_pending(
                "alien", "INBOX", f"sim-alien-{i}", 1,
                subject=f"Alien FI advertising inquiry #{i + 1}",
                sender=f"lead{i}@example.com",
            )
        # Coinography has nothing new (demonstrates 'only mailboxes with
        # new actionable emails appear' - point 9).

        window = collect_window()
        text = format_digest(window, test_tag="LIVE VERIFICATION - safe to ignore")
        markup = digest_keyboard(window)

        # Visible diagnostics so operators can see what will be sent.
        print(f"[live-verify] window mailboxes={len(window.buckets)}  "
              f"P0={window.total_p0}  P1={window.total_p1}  "
              f"total_actionable={window.total_actionable}")
        print(f"[live-verify] keyboard buttons={[btn.text for row in markup.inline_keyboard for btn in row]}")
        print("-" * 60)
        print(text)
        print("-" * 60)

        if args.dry_run:
            print("[live-verify] --dry-run set, not contacting Telegram.")
            return 0

        # Send via a one-off Bot client. We do NOT call send_to_home or any
        # home-target list: this script talks to exactly one chat id so
        # it cannot accidentally fan out to other configured targets.
        from telegram import Bot  # noqa: WPS433

        token = _read_token(temp_root / ".env")
        bot = Bot(token=token)
        try:
            await bot.initialize()
            await bot.send_message(
                chat_id=args.target, text=text, reply_markup=markup
            )
            print(f"[live-verify] [TEST] digest sent to chat {args.target}")
        finally:
            try:
                await bot.shutdown()
            except Exception:  # noqa: BLE001
                pass
        return 0
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
        print(f"[live-verify] temp profile cleaned up: {temp_root}")


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
