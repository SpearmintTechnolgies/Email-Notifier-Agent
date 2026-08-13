from __future__ import annotations

import os
from pathlib import Path

PROFILE = Path(__file__).resolve().parents[2]
MAILOPS_ROOT = Path(__file__).resolve().parents[1]
HIMALAYA_DIR = PROFILE / "home" / ".config" / "himalaya"
CONFIG_TOML = HIMALAYA_DIR / "config.toml"
ACCOUNTS_JSON = HIMALAYA_DIR / "accounts.json"
ENV_FILE = PROFILE / ".env"
DATA_DIR = MAILOPS_ROOT / "data"
OPS_DB = DATA_DIR / "ops.db"
SETTINGS_JSON = DATA_DIR / "settings.json"


def himalaya_bin() -> str:
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "bin" / "himalaya.exe"
    if local.exists():
        return str(local)
    return "himalaya"
