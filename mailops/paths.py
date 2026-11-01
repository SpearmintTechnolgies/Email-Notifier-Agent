from __future__ import annotations

import os
import shutil
from pathlib import Path

# Repo root = parent of the mailops package. Works wherever the repo is cloned.
MAILOPS_ROOT = Path(__file__).resolve().parents[1]

# A "profile" directory holds machine-local state that must NOT be committed:
#   .env, himalaya config, OAuth token cache.
# Defaults to the repo itself; override with MAILOPS_HOME for a deployed layout.
PROFILE = Path(os.environ.get("MAILOPS_HOME") or MAILOPS_ROOT).resolve()

# Himalaya config directory. Override with MAILOPS_HIMALAYA_DIR.
HIMALAYA_DIR = Path(
    os.environ.get("MAILOPS_HIMALAYA_DIR") or (PROFILE / "himalaya")
).resolve()
CONFIG_TOML = HIMALAYA_DIR / "config.toml"
ACCOUNTS_JSON = HIMALAYA_DIR / "accounts.json"

ENV_FILE = PROFILE / ".env"

DATA_DIR = MAILOPS_ROOT / "data"
OPS_DB = DATA_DIR / "ops.db"
SETTINGS_JSON = DATA_DIR / "settings.json"

# The live application log. Only a real launch (``mailops.main.cli``) may open
# it; tests and tooling must never append to it (see ``log_file``).
LIVE_LOG_FILE = DATA_DIR / "mailops.log"


def log_file() -> Path:
    """Where this process logs. ``MAILOPS_LOG_FILE`` redirects it (tests,
    experiments). Under pytest the live log is refused outright, so a test can
    never append simulated errors or test events to ``data/mailops.log``."""
    override = os.environ.get("MAILOPS_LOG_FILE", "").strip()
    target = Path(override).resolve() if override else LIVE_LOG_FILE
    if "PYTEST_CURRENT_TEST" in os.environ and target == LIVE_LOG_FILE.resolve():
        raise RuntimeError(
            "refusing to log to the live data/mailops.log from a test; "
            "set MAILOPS_LOG_FILE to a temporary path"
        )
    return target

# Machine-local OAuth token cache (keyring-backed by Himalaya; this dir only
# holds any file-based fallback). Never committed (see .gitignore).
TOKEN_DIR = PROFILE / "tokens"

# Machine-local mailbox passwords for password-based (webmail) accounts, read
# by Himalaya through `python -m mailops get-secret`. Never committed.
SECRETS_DIR = PROFILE / "secrets"


def _env_file_value(key: str) -> str:
    """Read one key straight from the profile .env (no side effects)."""
    try:
        for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def himalaya_bin() -> str:
    """Resolve the himalaya executable.

    Order: HIMALAYA_BIN env -> HIMALAYA_BIN in .env -> PATH lookup -> common
    Windows install dirs. (The .env fallback means a launcher that did not
    inherit the variable from a shell - Task Scheduler, double-click - still
    finds the binary instead of silently reading zero mail.)
    """
    for override in (os.environ.get("HIMALAYA_BIN"), _env_file_value("HIMALAYA_BIN")):
        if override and Path(override).exists():
            return override
    found = shutil.which("himalaya")
    if found:
        return found
    local_app = os.environ.get("LOCALAPPDATA", "")
    user_profile = os.environ.get("USERPROFILE", "")
    candidates = [
        Path(local_app) / "Programs" / "himalaya" / "himalaya.exe",
        Path(user_profile) / "scoop" / "shims" / "himalaya.exe",
        Path(user_profile) / ".local" / "bin" / "himalaya.exe",
        Path("C:/ProgramData/chocolatey/bin/himalaya.exe"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return "himalaya"  # let subprocess raise a clear error
