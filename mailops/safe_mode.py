"""Safe test mode: blocks every mutating mailbox operation.

When active, move/copy/flag/send/create-mailbox become log-only no-ops that
report success, so the full pipeline (classify, dashboard, Telegram buttons,
drafts) can be exercised without touching a real mailbox.

Enable via:
  - environment variable MAILOPS_TEST_MODE=1 (or true/yes/on), or
  - settings.json key "test_mode": true

  - MAILOPS_TEST_MODE in the .env file (read directly, so every CLI command
    sees it regardless of whether .env was loaded into the environment yet)

Precedence: process env var, then .env file, then the settings flag.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("mailops.safe_mode")

_TRUE = {"1", "true", "yes", "on"}


def _dotenv_value(key: str) -> str | None:
    """Read ``key`` straight from the .env file (never mutates os.environ).

    ``config.load_dotenv`` only runs inside ``load_runtime``, so a CLI command
    that asks "is test mode on?" before anything loaded .env (``policy``,
    ``sync``, ``rescan`` ...) would wrongly see it as off. Reading the file
    here makes the answer independent of call order.
    """
    try:
        from . import config

        path = config.ENV_FILE
        if not path.exists():
            return None
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001 - unreadable .env: fall through to settings
        return None
    return None


def test_mode_enabled() -> bool:
    # Precedence: process env var > .env file > settings.json "test_mode".
    raw = os.environ.get("MAILOPS_TEST_MODE")
    if raw is None or raw.strip() == "":
        raw = _dotenv_value("MAILOPS_TEST_MODE")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in _TRUE
    try:
        from .config import load_settings

        return bool(load_settings().get("test_mode"))
    except Exception:  # noqa: BLE001 - settings unreadable -> stay safe? No: default off.
        return False


def guard_mutation(op: str, detail: str = "") -> bool:
    """Return True if the mutation is BLOCKED (test mode). Logs the blocked op."""
    if test_mode_enabled():
        log.info("[TEST MODE] blocked %s %s", op, detail)
        return True
    return False
