"""Securely replace TELEGRAM_BOT_TOKEN in the local .env.

Run in YOUR OWN terminal (needs a real TTY - the token is typed at a hidden
prompt, never accepted via arguments, pipes or files, and never printed):

    .\\.venv\\Scripts\\python.exe scripts\\set_telegram_token.py

What it does:
  1. Reads the new token with getpass (hidden).
  2. Validates the format, then asks Telegram `getMe` to confirm it is a live
     token for the expected bot username (default: ourmailagent_bot).
  3. Backs up .env to backups/<timestamp>/.env.before-token-change.
  4. Rewrites ONLY the TELEGRAM_BOT_TOKEN line (all other lines, order and
     line endings are preserved) via an atomic replace.
Only the bot username/id are printed - never the token.
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mailops.redact import redact_text  # noqa: E402

EXPECTED_USERNAME = os.environ.get("MAILOPS_EXPECTED_BOT", "ourmailagent_bot")
ENV_FILE = Path(os.environ.get("MAILOPS_HOME") or ROOT) / ".env"


def looks_like_token(tok: str) -> bool:
    import re

    return bool(re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", tok))


def get_me(token: str) -> dict:
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:  # noqa: BLE001
            return {"ok": False, "description": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "description": redact_text(f"{type(e).__name__}: {e}", (token,))}


def replace_token_line(text: str, token: str) -> tuple[str, int]:
    """Replace only TELEGRAM_BOT_TOKEN=... lines, keeping line endings."""
    out, n = [], 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("TELEGRAM_BOT_TOKEN="):
            eol = line[len(line.rstrip("\r\n")):]
            out.append(f"TELEGRAM_BOT_TOKEN={token}{eol}")
            n += 1
        else:
            out.append(line)
    return "".join(out), n


def main() -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Needs an interactive terminal (hidden prompt). Nothing changed.")
        return 2
    if not ENV_FILE.exists():
        print(f"{ENV_FILE} not found. Nothing changed.")
        return 2
    token = getpass.getpass("New Telegram bot token (hidden, paste then Enter): ").strip().strip('"').strip("'")
    if not looks_like_token(token):
        print("That does not look like a Telegram bot token (expected <digits>:<secret>). Nothing changed.")
        return 2
    info = get_me(token)
    if not info.get("ok"):
        print("Telegram rejected the token:", redact_text(str(info.get("description")), (token,)), "- nothing changed.")
        return 1
    me = info["result"]
    print(f"Telegram says: @{me.get('username')} (id {me.get('id')})")
    if me.get("username") != EXPECTED_USERNAME:
        print(f"Expected @{EXPECTED_USERNAME}. Refusing to write a token for a different bot. Nothing changed.")
        return 1

    raw = ENV_FILE.read_bytes().decode("utf-8")
    new_text, n = replace_token_line(raw, token)
    if n != 1:
        print(f"Expected exactly one TELEGRAM_BOT_TOKEN line, found {n}. Nothing changed.")
        return 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = ROOT / "backups" / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ENV_FILE, backup_dir / ".env.before-token-change")
    tmp = ENV_FILE.with_name(".env.tmp")
    tmp.write_bytes(new_text.encode("utf-8"))
    os.replace(tmp, ENV_FILE)
    print(f"Updated TELEGRAM_BOT_TOKEN in {ENV_FILE.name}. Previous .env saved to backups/{stamp}/.")
    print("Tell the assistant it is done; it will restart the poller and verify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
