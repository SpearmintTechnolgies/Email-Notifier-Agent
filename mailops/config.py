from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import ACCOUNTS_JSON, ENV_FILE, SETTINGS_JSON, DATA_DIR

# cPanel/Dovecot hosts often require the INBOX. namespace (delimiter ".").
# Gmail also accepts these as label-like folder paths via IMAP.
PRIORITY_FOLDERS = [
    "INBOX.P0-Critical",
    "INBOX.P1-Important",
    "INBOX.P2-Normal",
    "INBOX.P3-Noise",
    "INBOX.Waiting",
    "INBOX.Handled",
]

TAG_FOLDERS = [
    "INBOX.Tag.Sales-Lead",
    "INBOX.Tag.Partnership",
    "INBOX.Tag.Support",
    "INBOX.Tag.Billing",
    "INBOX.Tag.Legal",
    "INBOX.Tag.Security",
    "INBOX.Tag.Ops",
    "INBOX.Tag.Recruiting",
]

TAG_TO_FOLDER = {
    "sales-lead": "INBOX.Tag.Sales-Lead",
    "partnership": "INBOX.Tag.Partnership",
    "support": "INBOX.Tag.Support",
    "billing": "INBOX.Tag.Billing",
    "legal": "INBOX.Tag.Legal",
    "security": "INBOX.Tag.Security",
    "ops": "INBOX.Tag.Ops",
    "recruiting": "INBOX.Tag.Recruiting",
}

PRIORITY_TO_FOLDER = {
    0: "INBOX.P0-Critical",
    1: "INBOX.P1-Important",
    2: "INBOX.P2-Normal",
    3: "INBOX.P3-Noise",
}

DEFAULT_INBOX_CONTEXT = (
    "I run two crypto news/media sites for revenue: Coinography (coinography.com) "
    "and Memecoinist (memecoinist.com). Primary income is advertising and paid "
    "placements — banners, sponsored articles, newsletter sponsorships, press "
    "releases, and (Memecoinist) paid memecoin/token listing & promo posts. "
    "CRITICAL: security and payment failures. IMPORTANT: anyone asking rates/"
    "pricing, how much to post/publish/advertise, media kit, sponsorship, "
    "listing fees, or ready to buy. NORMAL: vague collab intros. NOISE: cold "
    "RFQ/procurement spam, manufacturer outreach, SEO/guest-post packages, "
    "newsletters, no-reply digests."
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "quiet_hours_start": 23,
    "quiet_hours_end": 8,
    "timezone": "Asia/Kolkata",
    "min_alert_priority": 1,  # instant notify P0-P1; P2 batched
    "noise_push": "off",  # off | daily
    "p2_batch_seconds": 900,
    "vip_senders": [],
    "muted_senders": [],
    "muted_domains": [],
    "sync_interval_seconds": 3600,  # hourly sort + dashboard digest
    # Free-text brief used by classifier / drafts — user must confirm
    "inbox_context": "",
    "account_purposes": {},
    "context_confirmed": False,
    "rules_confirmed": False,
    # Structured buckets — see priority_rules.DEFAULT_PRIORITY_RULES
    "priority_rules": {},
    # legacy (migrated into priority_rules on first load)
    "important_keywords": [],
    "noise_keywords": [],
}


def load_dotenv(path: Path = ENV_FILE) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), out[k.strip()])
    return out


@dataclass
class Account:
    id: str
    email: str
    display_name: str = ""
    default: bool = False


def load_accounts() -> list[Account]:
    if not ACCOUNTS_JSON.exists():
        return []
    raw = json.loads(ACCOUNTS_JSON.read_text(encoding="utf-8-sig"))
    if isinstance(raw, dict):
        raw = [raw]
    accounts: list[Account] = []
    for row in raw:
        accounts.append(
            Account(
                id=str(row["id"]),
                email=str(row["email"]),
                display_name=str(row.get("display_name") or row["email"]),
                default=bool(row.get("default")),
            )
        )
    return accounts


def load_settings() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SETTINGS_JSON.exists():
        save_settings(dict(DEFAULT_SETTINGS))
        return dict(DEFAULT_SETTINGS)
    data = json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data)
    return merged


def save_settings(settings: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_JSON.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


@dataclass
class RuntimeConfig:
    telegram_token: str
    allowed_users: list[int] = field(default_factory=list)
    home_channel: int | None = None
    openai_api_key: str = ""
    openai_base_url: str = "https://desktop-fknb11s.tail8809da.ts.net/v1"
    model: str = "auto"


def load_runtime() -> RuntimeConfig:
    env = load_dotenv()
    allowed = []
    for part in (env.get("TELEGRAM_ALLOWED_USERS") or "").split(","):
        part = part.strip()
        if part.isdigit():
            allowed.append(int(part))
    home = env.get("TELEGRAM_HOME_CHANNEL") or ""
    home_channel = int(home) if home.strip().lstrip("-").isdigit() else None
    # Prefer config.yaml freellmapi URL if present
    base = env.get("OPENAI_BASE_URL") or "https://desktop-fknb11s.tail8809da.ts.net/v1"
    cfg_path = ENV_FILE.parent / "config.yaml"
    if cfg_path.exists():
        text = cfg_path.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            if "base_url:" in line and "http" in line:
                base = line.split("base_url:", 1)[1].strip().strip('"').strip("'")
                break
    token = env.get("TELEGRAM_BOT_TOKEN") or ""
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN missing in inbox .env")
    return RuntimeConfig(
        telegram_token=token,
        allowed_users=allowed,
        home_channel=home_channel,
        openai_api_key=env.get("OPENAI_API_KEY") or "",
        openai_base_url=base.rstrip("/"),
        model="auto",
    )
