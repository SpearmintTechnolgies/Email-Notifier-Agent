"""Server preset resolution for bulk mailbox onboarding.

A row in the CSV may name a ``provider`` (``hostinger``, ``siteground`` or
any admin-saved preset from ``mb_quick.presets_load``). The preset fills in
IMAP/SMTP host and port; any explicit column on the row still wins. If no
preset is named, the row MUST carry its own host/port columns.

This module is pure and side-effect free - the bulk UI and tests can import
it freely.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..bot import mb_quick

# Built-in presets. Keys match the CSV ``provider`` column after lower-casing.
# The CEO's two active vendors are first-class so even a totally empty row
# (except email+password) can onboard a mailbox with provider=hostinger.
BUILTIN_PRESETS: dict[str, dict[str, Any]] = {
    "hostinger": {
        "imap_host": "imap.hostinger.com",
        "imap_port": 993,
        "smtp_host": "smtp.hostinger.com",
        "smtp_port": 465,
    },
    # SiteGround server names vary between tenants (gcam1108, gcam1092, ...).
    # We intentionally DO NOT hard-code a specific hostname: the CEO's spec
    # explicitly says "Do not assume every SiteGround account uses the same
    # server. Allow per-row overrides." So the SiteGround built-in only
    # fills in the common ports; the host must come from the row (or from
    # a named custom preset like 'siteground-gcam1108' the admin saved via
    # Mailboxes → Quick Add → Save server as preset).
    "siteground": {
        "imap_port": 993,
        "smtp_port": 465,
    },
}


@dataclass
class ResolvedSpec:
    """Everything the validator needs to call ``webmail_onboarding.add_account``."""

    row_no: int
    account_id: str
    email: str
    display_name: str
    imap_host: str
    imap_port: int
    imap_user: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    provider: str
    # Only valid in-memory; validator never persists this before validation
    # succeeds (the vault seals the final storage).
    imap_password: str
    smtp_password: str
    problems: list[str]


def resolve(row, taken_ids: set[str]) -> ResolvedSpec:
    """Combine a parsed CSV row with its preset (if any) and derive defaults.

    Raises ``ValueError`` only through downstream validation; shape problems
    land in ``ResolvedSpec.problems`` so the caller can route the row to
    ``failed_validation`` without aborting the whole job.
    """
    from .csv_parse import ParsedRow
    from ..bot.mb_quick import derive_account_id

    assert isinstance(row, ParsedRow), "resolve() needs a ParsedRow"
    problems: list[str] = []

    provider = (row.provider or "").lower().strip()
    builtin = BUILTIN_PRESETS.get(provider) if provider else None
    custom = mb_quick.preset_get(provider) if provider else None
    preset: dict[str, Any] = {}
    if builtin:
        preset.update(builtin)
    if custom:
        preset.update(custom)

    def _pick(host_key: str, port_key: str, user_key: str, csv_host: str, csv_port: int, csv_user: str) -> tuple[str, int, str]:
        # Row columns always take precedence over the preset.
        host = csv_host or str(preset.get(host_key) or "")
        port = csv_port or int(preset.get(port_key) or 0)
        user = csv_user or str(preset.get(user_key) or "") or row.email
        return host, port, user

    imap_host, imap_port, imap_user = _pick(
        "imap_host", "imap_port", "imap_user",
        row.imap_host, row.imap_port, row.imap_username,
    )
    smtp_host, smtp_port, smtp_user = _pick(
        "smtp_host", "smtp_port", "smtp_user",
        row.smtp_host, row.smtp_port, row.smtp_username,
    )

    # Fall back to the implicit-TLS defaults the single-account wizard uses.
    if not imap_port:
        imap_port = 993
    if not smtp_port:
        smtp_port = 465

    if not imap_host:
        problems.append(
            "IMAP host missing. Either set 'imap_host' on the row or use a "
            "provider preset that includes it."
        )
    if not smtp_host:
        problems.append(
            "SMTP host missing. Either set 'smtp_host' on the row or use a "
            "provider preset that includes it."
        )
    if provider and not builtin and not custom:
        problems.append(
            f"Unknown provider '{provider}'. Saved presets: "
            + (", ".join(sorted(mb_quick.presets_load())) or "(none)")
            + ". Built-in: hostinger, siteground."
        )

    account_id = row.account_id or derive_account_id(row.email, taken_ids)
    display_name = row.display_name or row.email.split("@", 1)[0]

    return ResolvedSpec(
        row_no=row.row_no,
        account_id=account_id,
        email=row.email,
        display_name=display_name,
        imap_host=imap_host,
        imap_port=imap_port,
        imap_user=imap_user,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        provider=provider or "custom",
        imap_password=row.password,
        smtp_password=row.password,  # single-password CSV; validator may override
        problems=problems,
    )
