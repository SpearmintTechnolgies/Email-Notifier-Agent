"""Smart IMAP/SMTP server resolution for mailbox onboarding.

CEO-scale goal: 90-100+ business mailboxes, onboarded with as few typed
fields as practical, WITHOUT ever guessing a hosting-tenant-specific server
(SiteGround above all) from the email domain or an MX lookup.

Resolution order (see ``resolve_for_email``), cheapest/most-trustworthy
first:

  1. VERIFIED - an existing mailbox on the SAME email domain was already
     onboarded successfully. Same registered domain is treated as the same
     hosting tenant (two mailboxes @coinography.com are, in practice, on the
     same hosting account) - this is the "hosting tenant relationship
     confirmed" case from the spec, not a domain-name guess.
  2. VERIFIED - a named saved preset explicitly tags this domain (admin
     opted in to that association when saving/editing the preset).
  3. VERIFIED - the admin explicitly identified the provider (e.g. tapped
     "Hostinger") AND that provider's server is universal/reliable for
     every tenant (Hostinger only - SiteGround is deliberately excluded
     here because its server varies per tenant).
  4. SUGGESTED - a small, curated table of third-party mailbox providers
     whose MX records are distinctive and whose IMAP/SMTP server is the
     same for every customer (Zoho Mail, Yahoo Business, Namecheap Private
     Email, GoDaddy, Fastmail, ...). Only used when
     ``MAILOPS_AUTOCONFIG_MX=1`` is set (opt-in) and MX lookup succeeds;
     ALWAYS requires explicit admin confirmation and is never auto-applied.
  5. UNKNOWN - ask. For SiteGround this is always the terminal result for
     the hostname unless tiers 1/2 already matched; only the standard
     implicit-TLS ports are prefilled.

Nothing here ever uses a mailbox password, and nothing here silently
registers a mailbox - every tier is surfaced to the UI layer labelled
VERIFIED / SUGGESTED / UNKNOWN, and the UI requires an explicit admin
confirmation before any mailbox is registered (see
``mailops.bot.mailboxes`` Smart Add).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from . import config

log = logging.getLogger("mailops.server_resolution")

STATUS_VERIFIED = "verified"
STATUS_SUGGESTED = "suggested"
STATUS_UNKNOWN = "unknown"

DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_PORT = 465

# Hostinger's IMAP/SMTP endpoints are the SAME for every Hostinger
# customer (shared mail infrastructure) - safe to treat as VERIFIED the
# moment the admin explicitly says "this is a Hostinger mailbox".
HOSTINGER_SERVER = {
    "imap_host": "imap.hostinger.com",
    "imap_port": DEFAULT_IMAP_PORT,
    "smtp_host": "smtp.hostinger.com",
    "smtp_port": DEFAULT_SMTP_PORT,
}

# SiteGround intentionally has NO universal hostname here - see module
# docstring and the CEO's explicit instruction. Ports only.
SITEGROUND_PORTS = {
    "imap_port": DEFAULT_IMAP_PORT,
    "smtp_port": DEFAULT_SMTP_PORT,
}

# Curated table for tier 4 (opt-in autoconfiguration suggestions). Each
# entry maps a *distinctive MX answer suffix* to a universal server for
# that provider. Only providers that support IMPLICIT TLS on 993/465 are
# listed (this deployment cannot verify STARTTLS-only providers, e.g.
# Microsoft 365's submission port 587, so those are deliberately excluded
# rather than offered as a suggestion that can never pass validation).
KNOWN_MX_PROVIDERS: tuple[tuple[str, dict[str, Any], str], ...] = (
    ("zoho.com", {"imap_host": "imap.zoho.com", "imap_port": 993,
                   "smtp_host": "smtp.zoho.com", "smtp_port": 465}, "Zoho Mail"),
    ("yahoodns.net", {"imap_host": "imap.mail.yahoo.com", "imap_port": 993,
                       "smtp_host": "smtp.mail.yahoo.com", "smtp_port": 465}, "Yahoo"),
    ("privateemail.com", {"imap_host": "mail.privateemail.com", "imap_port": 993,
                           "smtp_host": "mail.privateemail.com", "smtp_port": 465}, "Namecheap Private Email"),
    ("secureserver.net", {"imap_host": "imap.secureserver.net", "imap_port": 993,
                           "smtp_host": "smtpout.secureserver.net", "smtp_port": 465}, "GoDaddy"),
    ("messagingengine.com", {"imap_host": "imap.fastmail.com", "imap_port": 993,
                              "smtp_host": "smtp.fastmail.com", "smtp_port": 465}, "Fastmail"),
)

_DOMAIN_KEY_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


@dataclass
class ServerResolution:
    status: str  # STATUS_VERIFIED / STATUS_SUGGESTED / STATUS_UNKNOWN
    imap_host: str = ""
    imap_port: int = DEFAULT_IMAP_PORT
    smtp_host: str = ""
    smtp_port: int = DEFAULT_SMTP_PORT
    source: str = ""          # human-readable "why" shown to the admin
    preset_name: str = ""     # set when a saved preset supplied the match
    needs_hostname: bool = False  # True when host is still missing (e.g. SiteGround)

    @property
    def complete(self) -> bool:
        return bool(self.imap_host and self.smtp_host)


def email_domain(email: str) -> str:
    return (email or "").strip().lower().rsplit("@", 1)[-1]


# --------------------------------------------------------------------------- the verified-domain ledger


def _settings_domain_map(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = data.get("verified_mail_servers") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for domain, blob in raw.items():
        if not isinstance(domain, str) or not _DOMAIN_KEY_RE.match(domain.lower()):
            continue
        if not isinstance(blob, dict):
            continue
        try:
            imap_port = int(blob.get("imap_port") or DEFAULT_IMAP_PORT)
            smtp_port = int(blob.get("smtp_port") or DEFAULT_SMTP_PORT)
        except (TypeError, ValueError):
            continue
        imap_host = str(blob.get("imap_host") or "").strip().lower()
        smtp_host = str(blob.get("smtp_host") or "").strip().lower()
        if not imap_host or not smtp_host:
            continue
        out[domain.lower()] = {
            "imap_host": imap_host,
            "imap_port": imap_port,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "provider": str(blob.get("provider") or "custom"),
            "verified_at": float(blob.get("verified_at") or 0),
            "source_account_id": str(blob.get("source_account_id") or ""),
        }
    return out


def record_verified_server(spec: Any) -> None:
    """Remember a just-registered mailbox's server, keyed by email domain.

    Best-effort / additive only: called from ``webmail_onboarding.add_account``
    right after a mailbox is successfully validated and registered. Never
    raises to the caller (wrapped there too, but defensive here as well).
    A later mailbox on the SAME domain can then be offered this exact
    server as a VERIFIED match without the admin retyping it - this is the
    "existing registered mailbox configuration, hosting tenant relationship
    confirmed" tier from the spec.
    """
    domain = email_domain(getattr(spec, "email", ""))
    if not domain or not _DOMAIN_KEY_RE.match(domain):
        return
    imap_host = str(getattr(spec, "imap_host", "") or "").strip().lower()
    smtp_host = str(getattr(spec, "smtp_host", "") or "").strip().lower()
    if not imap_host or not smtp_host:
        return
    settings = config.load_settings()
    domain_map = _settings_domain_map(settings)
    domain_map[domain] = {
        "imap_host": imap_host,
        "imap_port": int(getattr(spec, "imap_port", DEFAULT_IMAP_PORT) or DEFAULT_IMAP_PORT),
        "smtp_host": smtp_host,
        "smtp_port": int(getattr(spec, "smtp_port", DEFAULT_SMTP_PORT) or DEFAULT_SMTP_PORT),
        "provider": _guess_provider_label(imap_host),
        "verified_at": time.time(),
        "source_account_id": str(getattr(spec, "account_id", "") or ""),
    }
    settings["verified_mail_servers"] = domain_map
    config.save_settings(settings)


def _guess_provider_label(imap_host: str) -> str:
    h = (imap_host or "").lower()
    if h == "imap.hostinger.com":
        return "hostinger"
    if "siteground" in h:
        return "siteground"
    return "custom"


def lookup_verified_domain(domain: str) -> dict[str, Any] | None:
    domain = (domain or "").strip().lower()
    if not domain:
        return None
    return _settings_domain_map(config.load_settings()).get(domain)


def list_verified_domains() -> dict[str, dict[str, Any]]:
    return _settings_domain_map(config.load_settings())


# --------------------------------------------------------------------------- existing-mailbox fallback (tier 2)


def _find_existing_mailbox_host(domain: str) -> dict[str, Any] | None:
    """Scan currently-registered mailboxes for one on ``domain``.

    Reads the ground-truth ``himalaya/config.toml`` block for that account
    (not just our own settings.json cache) so this stays correct even if a
    mailbox was onboarded before this module existed. Returns ``None`` on
    any error - this is an advisory fallback, never a hard dependency.
    """
    try:
        from . import mailbox_admin, paths

        if not paths.CONFIG_TOML.exists():
            return None
        text = paths.CONFIG_TOML.read_text(encoding="utf-8-sig")
        for m in mailbox_admin.list_mailboxes():
            if email_domain(m.email) != domain:
                continue
            block_re = re.compile(
                r"\[accounts\." + re.escape(m.account_id) + r"\.imap\]\s*server\s*=\s*\"([^\"]+)\""
            )
            smtp_re = re.compile(
                r"\[accounts\." + re.escape(m.account_id) + r"\.smtp\]\s*server\s*=\s*\"([^\"]+)\""
            )
            imap_match = block_re.search(text)
            smtp_match = smtp_re.search(text)
            if not imap_match or not smtp_match:
                continue
            imap_host, _, imap_port = imap_match.group(1).rpartition(":")
            smtp_host, _, smtp_port = smtp_match.group(1).rpartition(":")
            if not imap_host or not smtp_host:
                continue
            return {
                "imap_host": imap_host,
                "imap_port": int(imap_port) if imap_port.isdigit() else DEFAULT_IMAP_PORT,
                "smtp_host": smtp_host,
                "smtp_port": int(smtp_port) if smtp_port.isdigit() else DEFAULT_SMTP_PORT,
                "source_account_id": m.account_id,
            }
    except Exception:  # noqa: BLE001 - advisory only
        log.debug("existing-mailbox server lookup failed", exc_info=True)
    return None


# --------------------------------------------------------------------------- preset domain tags (tier 1b)


def _preset_domain_match(domain: str) -> tuple[str, dict[str, Any]] | None:
    try:
        from .bot import mb_quick
    except Exception:  # noqa: BLE001
        return None
    for name, blob in mb_quick.presets_list():
        domains = blob.get("domains") if isinstance(blob, dict) else None
        if isinstance(domains, list) and domain in [str(d).strip().lower() for d in domains]:
            return name, blob
    return None


# --------------------------------------------------------------------------- MX-based suggestion (tier 4, opt-in)


def _mx_hint(domain: str, resolver=None) -> tuple[dict[str, Any], str] | None:
    import os as _os

    if (_os.environ.get("MAILOPS_AUTOCONFIG_MX") or "").strip() != "1":
        return None
    try:
        if resolver is not None:
            answers = resolver(domain)
        else:
            import dns.resolver  # type: ignore

            answers = [str(r.exchange).rstrip(".").lower() for r in dns.resolver.resolve(domain, "MX")]
    except Exception:  # noqa: BLE001 - no dnspython / lookup failed / offline
        return None
    for suffix, server, label in KNOWN_MX_PROVIDERS:
        if any(a.endswith(suffix) for a in answers):
            return server, label
    return None


# --------------------------------------------------------------------------- public entry point


def resolve_for_email(
    email: str,
    *,
    provider_hint: str = "",
    allow_mx_autoconfig: bool = True,
    mx_resolver=None,
) -> ServerResolution:
    """Resolve the best-known server for ``email`` without ever guessing a
    SiteGround-style per-tenant hostname from the domain.

    ``provider_hint`` is only trusted when it came from an explicit admin
    choice (e.g. tapping "Hostinger" in the UI) - this function does not
    infer a provider from the domain itself.
    """
    domain = email_domain(email)
    provider_hint = (provider_hint or "").strip().lower()

    # Tier 1: domain already has a verified server from a prior mailbox.
    hit = lookup_verified_domain(domain) if domain else None
    if hit:
        return ServerResolution(
            status=STATUS_VERIFIED,
            imap_host=hit["imap_host"], imap_port=hit["imap_port"],
            smtp_host=hit["smtp_host"], smtp_port=hit["smtp_port"],
            source=(
                f"Reusing the verified server from mailbox "
                f"'{hit.get('source_account_id') or '?'}' on the same domain "
                f"({domain})."
            ),
        )

    # Tier 1b: a saved preset explicitly tags this domain.
    preset_hit = _preset_domain_match(domain) if domain else None
    if preset_hit:
        name, blob = preset_hit
        return ServerResolution(
            status=STATUS_VERIFIED,
            imap_host=str(blob.get("imap_host", "")), imap_port=int(blob.get("imap_port", DEFAULT_IMAP_PORT)),
            smtp_host=str(blob.get("smtp_host", "")), smtp_port=int(blob.get("smtp_port", DEFAULT_SMTP_PORT)),
            source=f"Saved preset '{name}' is tagged for domain {domain}.",
            preset_name=name,
        )

    # Tier 2: another registered mailbox already lives on this domain.
    existing = _find_existing_mailbox_host(domain) if domain else None
    if existing:
        return ServerResolution(
            status=STATUS_VERIFIED,
            imap_host=existing["imap_host"], imap_port=existing["imap_port"],
            smtp_host=existing["smtp_host"], smtp_port=existing["smtp_port"],
            source=(
                f"Existing mailbox '{existing['source_account_id']}' is already "
                f"registered on {domain} with this server."
            ),
        )

    # Tier 3: explicit, reliable provider identification.
    if provider_hint == "hostinger":
        return ServerResolution(
            status=STATUS_VERIFIED,
            imap_host=HOSTINGER_SERVER["imap_host"], imap_port=HOSTINGER_SERVER["imap_port"],
            smtp_host=HOSTINGER_SERVER["smtp_host"], smtp_port=HOSTINGER_SERVER["smtp_port"],
            source="Hostinger uses the same IMAP/SMTP server for every tenant.",
        )
    if provider_hint == "siteground":
        # Ports are safe to prefill; the hostname is NEVER guessed.
        return ServerResolution(
            status=STATUS_UNKNOWN,
            imap_port=SITEGROUND_PORTS["imap_port"], smtp_port=SITEGROUND_PORTS["smtp_port"],
            source=(
                "SiteGround assigns a different server per tenant - the "
                "hostname cannot be inferred from the email domain or MX "
                "record. Enter it (e.g. gcam1108.siteground.biz) or pick a "
                "saved SiteGround preset."
            ),
            needs_hostname=True,
        )

    # Tier 4: optional, curated MX-based suggestion. Opt-in via env var,
    # NEVER auto-applied - the caller must still show it as SUGGESTED and
    # get an explicit confirmation.
    if allow_mx_autoconfig and domain:
        mx = _mx_hint(domain, resolver=mx_resolver)
        if mx:
            server, label = mx
            return ServerResolution(
                status=STATUS_SUGGESTED,
                imap_host=server["imap_host"], imap_port=server["imap_port"],
                smtp_host=server["smtp_host"], smtp_port=server["smtp_port"],
                source=(
                    f"Unverified suggestion: MX records for {domain} look like "
                    f"{label}, which uses a universal server. Confirm this "
                    "connects before trusting it."
                ),
            )

    # Tier 5: ask.
    return ServerResolution(
        status=STATUS_UNKNOWN,
        source="No verified or reliable server match found - enter IMAP/SMTP details.",
        needs_hostname=True,
    )
