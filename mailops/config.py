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

# Company-wide context shared by EVERY inbox. Deliberately brand-neutral: it must
# never carry one brand's pricing, links, identity or policies. Brand facts live
# in ``account_purposes`` (per inbox) and ``data/kb/<account_id>.md`` (per inbox).
DEFAULT_INBOX_CONTEXT = (
    "Company crypto news/media operation with several separate brands, each with "
    "its own business mailbox (Altcoin Informer, Coinography, Memecoinist). Each "
    "mailbox has its own purpose, reply knowledge and sender identity: judge and "
    "answer a message ONLY with that mailbox's own context, and never reuse one "
    "brand's pricing, links, signature, services or policies for another."
)

# Gmail (the former Growth Pilot inbox) is shared with the Sales-Agent and is
# disabled for this deployment: it is never an active account, whatever the
# registry or the allowlist says. Registry rows and tokens are kept on disk.
GMAIL_ENABLED = False
GMAIL_ACCOUNT_IDS = frozenset({"gmail"})
GMAIL_EMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def is_gmail_account(account_id: str, email: str = "") -> bool:
    """True for the Gmail inbox, identified by id or by a Gmail address."""
    if str(account_id).strip().lower() in GMAIL_ACCOUNT_IDS:
        return True
    email = str(email or "").strip().lower()
    return "@" in email and email.rsplit("@", 1)[1] in GMAIL_EMAIL_DOMAINS

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
    "sync_interval_seconds": 900,  # 15-minute IMAP check; digest only on new mail
    # ------------------------------------------------------------------
    # Consolidated notification digest (replaces per-email alerts).
    # ``digest_enabled``: master switch for the 15-minute summary.
    # ``digest_interval_seconds``: how often the summary is built. Must be
    #     a multiple of or equal to ``sync_interval_seconds`` to avoid
    #     empty-but-expensive runs. 900 (15 minutes) matches today's IMAP
    #     poll so the digest piggybacks on existing passes - no second
    #     scheduler is created (point 11).
    # ``digest_p0_immediate``: optional immediate P0 Critical alert in
    #     addition to the next digest. Default OFF because 90+ mailboxes
    #     turning on at once would still flood if each one fired at the
    #     same moment.
    # ``digest_include_p2``: fold P2 Normal into the digest counts too
    #     (default off; P2 is "possibly useful later" and not actionable
    #     enough to summon the team to Telegram every 15 minutes).
    # ``digest_mailboxes_disabled``: list of account ids whose counts are
    #     EXCLUDED from the digest, even when they are active (per-mailbox
    #     notification off switch - point 8).
    # ``digest_initial_sync_silent``: when ON (default), a mailbox's very
    #     first sync marks every classified P0/P1 as "seeded" (counted on
    #     the dashboard but never alerted). This is what stops Alien FI
    #     from firing 12-13 individual alerts the moment it is connected,
    #     and what makes bulk CSV imports of 90 inboxes safe.
    # ``digest_mailbox_page_size``: how many mailboxes per Select Mailbox
    #     page (point 9 - paginate long lists). 20 fits a Telegram screen
    #     comfortably on both mobile and desktop.
    # ``digest_delivered_retention_seconds``: delivered rows survive this
    #     long after send (used for audit + 'why was X in that digest?').
    # ------------------------------------------------------------------
    "digest_enabled": True,
    "digest_interval_seconds": 900,
    "digest_p0_immediate": False,
    "digest_include_p2": False,
    "digest_mailboxes_disabled": [],
    "digest_initial_sync_silent": True,
    "digest_mailbox_page_size": 20,
    "digest_delivered_retention_seconds": 7 * 24 * 3600,
    # ``digest_edit_in_place``: when a window has no new actionable mail but
    # a card from a previous window is still on screen, refresh that same
    # Telegram message (edit) instead of sending a brand-new one. Editing is
    # not a push notification, so this satisfies "at most one scheduled
    # notification per window" while keeping the card's pending/unhealthy
    # numbers current. Default ON; the very first-ever send (nothing to
    # edit yet) always falls back to a normal send.
    "digest_edit_in_place": True,
    # ``ops_alert_standalone_enabled``: the operational sync-health alert
    # (mailops.health) is folded into the consolidated digest card's
    # "Unhealthy mailboxes" line by default and does NOT fire as its own
    # scheduled Telegram message (that would be a second notification in
    # the same 15-minute window). Flipping this ON lets an admin opt back
    # into the separate, already-deduplicated ops alert for a genuine
    # incident that needs its own explicit, configurable exception to the
    # "one message per window" rule. Default OFF.
    "ops_alert_standalone_enabled": False,
    # ``sync_account_stagger_seconds``: optional delay inserted between two
    # consecutive accounts inside one sync pass. 0 (default) = no stagger,
    # used by every unit test. A production fleet concentrated on a few
    # shared SiteGround/cPanel hosts (many of this deployment's 33 inboxes
    # share 2-3 physical mail servers) should set this to a couple of
    # seconds so a 15-minute poll never opens dozens of IMAP sessions against
    # the same host back-to-back.
    "sync_account_stagger_seconds": 0,
    # Free-text brief used by classifier / drafts — user must confirm
    "inbox_context": "",
    "account_purposes": {},
    # Per-inbox additions to the company-wide priority_rules, e.g.
    # {"altcoininformer": {"p0": {"keywords": [...], "description": "..."}}}.
    # Keywords/domains are ADDED to the baseline for that inbox only.
    "account_priority_rules": {},
    "context_confirmed": False,
    "rules_confirmed": False,
    # Safe test mode: block all mutating mailbox ops (also MAILOPS_TEST_MODE=1)
    "test_mode": False,
    # Per-account capabilities, e.g. {"altcoininformer": {"organize": false,
    # "send": false}}. Missing = off. See mailops/policy.py (gmail is locked).
    "account_policies": {},
    # Allowlist of account ids Mailops manages (empty = every account in
    # accounts.json). Others are never synced, cached, notified or shown.
    "active_accounts": [],
    # Structured buckets — see priority_rules.DEFAULT_PRIORITY_RULES
    "priority_rules": {},
    # legacy (migrated into priority_rules on first load)
    "important_keywords": [],
    "noise_keywords": [],
}


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    if path is None:
        path = ENV_FILE
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


def _active_allowlist() -> list[str] | None:
    """Account ids Mailops may manage, from ``active_accounts`` in settings.json.

    ``None`` (key missing/empty/unreadable) means "no allowlist": every account
    in accounts.json is active. Read straight from disk with no side effects
    (``load_settings`` would create a default file).
    """
    try:
        if not SETTINGS_JSON.exists():
            return None
        raw = json.loads(SETTINGS_JSON.read_text(encoding="utf-8")).get("active_accounts")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(raw, list):
        return None
    ids = [str(x).strip() for x in raw if str(x).strip()]
    return ids or None


def load_accounts() -> list[Account]:
    """The ACTIVE accounts: what sync, cache, dashboards and notifications use.

    A registry row is skipped when it is the (disabled) Gmail inbox, has
    ``"enabled": false`` or is not in the ``active_accounts`` allowlist.
    Skipped accounts are never read, cached, notified about or shown, but
    nothing is removed from disk.
    """
    if not ACCOUNTS_JSON.exists():
        return []
    raw = json.loads(ACCOUNTS_JSON.read_text(encoding="utf-8-sig"))
    if isinstance(raw, dict):
        raw = [raw]
    allow = _active_allowlist()
    accounts: list[Account] = []
    for row in raw:
        if row.get("enabled") is False:
            continue
        if not GMAIL_ENABLED and is_gmail_account(str(row["id"]), str(row.get("email") or "")):
            continue
        if allow is not None and str(row["id"]) not in allow:
            continue
        accounts.append(
            Account(
                id=str(row["id"]),
                email=str(row["email"]),
                display_name=str(row.get("display_name") or row["email"]),
                default=bool(row.get("default")),
            )
        )
    return accounts


def registered_email(account_id: str) -> str:
    """Email of ``account_id`` from the registry, active or not ("" if absent
    or unreadable). Policy uses this so a retired/disabled Gmail row is still
    recognised as Gmail."""
    try:
        raw = json.loads(ACCOUNTS_JSON.read_text(encoding="utf-8-sig"))
        for row in raw if isinstance(raw, list) else [raw]:
            if isinstance(row, dict) and str(row.get("id")) == account_id:
                return str(row.get("email") or "")
    except (OSError, ValueError):
        pass
    return ""


def active_account_ids() -> set[str]:
    return {a.id for a in load_accounts()}


def digest_mailbox_enabled(account_id: str, settings: dict[str, Any] | None = None) -> bool:
    """True iff ``account_id`` participates in the 15-minute digest.

    Point 8 of the redesign: operators can mute specific inboxes without
    disabling them entirely (they still get synced + classified; they just
    do not contribute to the Telegram summary). The off-switch is stored as
    a flat list in ``digest_mailboxes_disabled`` for easy inspection.
    """
    s = settings if settings is not None else load_settings()
    disabled = {str(x).strip() for x in (s.get("digest_mailboxes_disabled") or [])}
    return account_id not in disabled


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


def parse_id_list(raw: str | None) -> tuple[list[int], list[str]]:
    """Parse a list of numeric Telegram ids (users or chats).

    Accepts comma / semicolon / whitespace separators, optional surrounding
    quotes, a trailing ``# comment`` and negative ids (groups, supergroups,
    channels). De-duplicates while keeping order.

    Returns ``(ids, invalid_tokens)`` so callers can warn about typos instead
    of silently dropping them (a single unparsable value used to make the
    whole setting load as ``None``).
    """
    text = (raw or "").split("#", 1)[0]
    ids: list[int] = []
    invalid: list[str] = []
    for tok in text.replace(";", ",").replace(" ", ",").replace("\t", ",").split(","):
        tok = tok.strip().strip('"').strip("'")
        if not tok:
            continue
        if tok.lstrip("-").isdigit() and tok.count("-") <= 1 and not tok.endswith("-"):
            value = int(tok)
            if value not in ids:
                ids.append(value)
        else:
            invalid.append(tok)
    return ids, invalid


@dataclass
class RuntimeConfig:
    telegram_token: str
    allowed_users: list[int] = field(default_factory=list)
    # First configured notification target (kept for backward compatibility).
    home_channel: int | None = None
    # Every configured notification target: positive = private chat with a
    # user, negative = group / supergroup / channel.
    home_channels: list[int] = field(default_factory=list)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    model: str = "auto"

    @property
    def home_groups(self) -> list[int]:
        """Configured group chats (negative ids) the dashboard may be used in."""
        return [c for c in self.home_channels if c < 0]

    @property
    def notify_targets(self) -> list[int]:
        """Where push notifications go: configured targets, else first user."""
        if self.home_channels:
            return list(self.home_channels)
        return [self.allowed_users[0]] if self.allowed_users else []


def _llm_api_key(file_env: dict[str, str], base_url: str) -> str:
    """Resolve the chat-completions bearer token without copying extra secrets.

    Preference: non-empty OPENAI_API_KEY in .env, then the process
    OPENAI_API_KEY. If the configured host is Groq and both are empty, use
    GROQ_API_KEY from the process environment (user-level, never written back
    to Mailops .env).
    """
    file_key = (file_env.get("OPENAI_API_KEY") or "").strip()
    if file_key:
        return file_key
    proc_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if proc_key:
        return proc_key
    if "groq.com" in (base_url or "").lower():
        return (os.environ.get("GROQ_API_KEY") or "").strip()
    return ""


def load_runtime() -> RuntimeConfig:
    env = load_dotenv()
    allowed_raw, _ = parse_id_list(env.get("TELEGRAM_ALLOWED_USERS"))
    allowed = [u for u in allowed_raw if u > 0]  # user ids are always positive
    home_channels, _ = parse_id_list(env.get("TELEGRAM_HOME_CHANNEL"))
    home_channel = home_channels[0] if home_channels else None
    base = env.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
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
    base = base.rstrip("/")
    return RuntimeConfig(
        telegram_token=token,
        allowed_users=allowed,
        home_channel=home_channel,
        home_channels=home_channels,
        openai_api_key=_llm_api_key(env, base),
        openai_base_url=base,
        model=(env.get("OPENAI_MODEL") or os.environ.get("OPENAI_MODEL") or "").strip() or "auto",
    )
