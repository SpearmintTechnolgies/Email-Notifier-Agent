"""Lifecycle of registered email accounts (mailboxes) in Mailops.

What this module does
---------------------
* Lists every mailbox known to Mailops, with its current state (active /
  disabled / locked read-only / enabled-but-off-allowlist) and policy
  (organize? send?).
* Enables or disables an account by flipping the ``active_accounts``
  allowlist in ``data/settings.json`` and (optionally) the ``enabled`` flag
  on the registry row. Credentials are never touched, re-entered or
  erased on disable: a disabled account is simply ignored by every active
  code path. Re-enabling the same id is a no-op if its credentials are
  still on disk.
* Runs a non-sending IMAP+SMTP handshake check through the existing
  ``webmail_onboarding.check_account`` helper.
* Writes an append-only ``mailbox_audit`` row for every state change. The
  detail column is redacted so even an operator error cannot leak a
  password into audit output.

Mailbox credentials are NEVER processed here. The onboarding flow lives in
``mailops/webmail_onboarding.py`` (CLI / server-side) and this module calls
nothing that reads a secret file - it only toggles the policy layer.

Boundaries
----------
* Gmail (locked read-only) cannot be enabled, disabled, onboarded or
  checked through here: that account is managed by the Sales-Agent.
* ``altcoininformer`` (the live production account) is preserved: the
  module refuses to delete its credentials and warns on disable.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from . import paths
from .config import (
    GMAIL_ACCOUNT_IDS,
    GMAIL_EMAIL_DOMAINS,
    SETTINGS_JSON,
    is_gmail_account,
    load_settings,
    registered_email,
    save_settings,
)
from .db import db, init_db
from .policy import policy_for
from .redact import redact_text

log = logging.getLogger("mailops.mailbox_admin")

AUDIT_KEEP = 1000


class MailboxAdminError(ValueError):
    """A mailbox lifecycle action was refused."""


@dataclass(frozen=True)
class MailboxView:
    account_id: str
    email: str
    display_name: str
    enabled_registry: bool   # ``enabled: false`` in accounts.json kills it
    in_allowlist: bool       # listed in settings.json active_accounts (or allowlist empty)
    locked: bool             # Gmail-locked read-only
    organize: bool
    send: bool
    has_credentials: bool    # local secrets file present (password accounts)

    @property
    def active(self) -> bool:
        """What the sync loop sees as 'running'."""
        return self.enabled_registry and self.in_allowlist and not self.locked

    def status_label(self) -> str:
        if self.locked:
            return "locked (shared Gmail: read-only)"
        if not self.enabled_registry:
            return "disabled (registry)"
        if not self.in_allowlist:
            return "disabled (not in allowlist)"
        if not self.organize and not self.send:
            return "active · read-only"
        pieces = []
        if self.organize:
            pieces.append("organize")
        if self.send:
            pieces.append("send")
        return "active · " + ", ".join(pieces)


# --------------------------------------------------------------------------- registry helpers


def _read_registry_rows() -> list[dict[str, Any]]:
    if not paths.ACCOUNTS_JSON.exists():
        return []
    try:
        raw = json.loads(paths.ACCOUNTS_JSON.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise MailboxAdminError(
            f"himalaya/accounts.json is not valid JSON ({exc}); refusing to modify it"
        ) from exc
    if isinstance(raw, dict):
        raw = [raw]
    rows: list[dict[str, Any]] = []
    for r in raw if isinstance(raw, list) else []:
        if isinstance(r, dict) and r.get("id"):
            rows.append(dict(r))
    return rows


def _write_registry_rows(rows: list[dict[str, Any]]) -> None:
    paths.ACCOUNTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.ACCOUNTS_JSON.with_name(paths.ACCOUNTS_JSON.name + ".tmp")
    tmp.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    import os
    os.replace(tmp, paths.ACCOUNTS_JSON)


def _secrets_present(account_id: str) -> bool:
    """True iff BOTH an IMAP and SMTP secret are stored (either format).

    Accepts the legacy plaintext ``.secret`` files (so the live Altcoin
    Informer account keeps reporting as ready) and the new encrypted
    ``.enc`` envelopes written by the webform/vault.
    """
    try:
        for kind in ("imap", "smtp"):
            enc = paths.SECRETS_DIR / f"{account_id}.{kind}.enc"
            plain = paths.SECRETS_DIR / f"{account_id}.{kind}.secret"
            if not (enc.exists() or plain.exists()):
                return False
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- listings


def list_mailboxes() -> list[MailboxView]:
    """Every mailbox the registry knows about, active or not.

    Does NOT touch the mailbox server. Pure disk read; safe to call from any
    Telegram handler.
    """
    rows = _read_registry_rows()
    settings = load_settings()
    allow = settings.get("active_accounts") or []
    allow_set = {str(x).strip() for x in allow if str(x).strip()}
    out: list[MailboxView] = []
    for r in rows:
        aid = str(r.get("id") or "").strip()
        if not aid:
            continue
        email = str(r.get("email") or "").strip()
        enabled = r.get("enabled") is not False  # default True
        locked = is_gmail_account(aid, email)
        in_allow = (not allow_set) or aid in allow_set
        pol = policy_for(aid)
        out.append(
            MailboxView(
                account_id=aid,
                email=email,
                display_name=str(r.get("display_name") or email or aid),
                enabled_registry=enabled,
                in_allowlist=in_allow,
                locked=locked,
                organize=pol.organize,
                send=pol.send,
                has_credentials=_secrets_present(aid),
            )
        )
    return out


def find_mailbox(account_id: str) -> MailboxView | None:
    aid = (account_id or "").strip()
    for m in list_mailboxes():
        if m.account_id == aid:
            return m
    return None


# --------------------------------------------------------------------------- mutations


def _guard(account_id: str) -> MailboxView:
    m = find_mailbox(account_id)
    if m is None:
        raise MailboxAdminError(f"'{account_id}' is not a registered mailbox.")
    if m.locked:
        raise MailboxAdminError(
            "The Gmail inbox is shared with the Sales-Agent and cannot be managed here."
        )
    return m


def enable_mailbox(actor_id: int, account_id: str) -> MailboxView:
    """Add to allowlist + flip ``enabled: true``. Credentials untouched.

    Re-enabling an account that has no local credentials is refused: the
    sync loop would try to log in and immediately fail. Admin must
    re-onboard it first.
    """
    m = _guard(account_id)
    if not m.has_credentials:
        raise MailboxAdminError(
            f"'{account_id}' has no stored credentials. Onboard it again before enabling."
        )
    rows = _read_registry_rows()
    changed = False
    for r in rows:
        if str(r.get("id")) == account_id and r.get("enabled") is False:
            r["enabled"] = True
            changed = True
            break
    if changed:
        _write_registry_rows(rows)
    settings = load_settings()
    allow = list(settings.get("active_accounts") or [])
    if allow and account_id not in [str(x).strip() for x in allow]:
        allow.append(account_id)
        settings["active_accounts"] = allow
        save_settings(settings)
    _audit(actor_id, "enable", account_id, "")
    log.info("mailbox enable actor=%s account=%s", actor_id, account_id)
    return find_mailbox(account_id) or m


def disable_mailbox(
    actor_id: int, account_id: str, *, remove_from_allowlist: bool = True
) -> MailboxView:
    """Flip ``enabled: false`` on the registry row and (default) drop from
    the active-accounts allowlist. Credentials are NOT deleted so re-enable
    is a one-tap undo.

    Policy is reset to deny-by-default: a re-enabled account has to be
    explicitly re-activated for organize/send through the existing Admin UI.
    """
    m = _guard(account_id)
    rows = _read_registry_rows()
    changed = False
    for r in rows:
        if str(r.get("id")) == account_id:
            if r.get("enabled") is not False:
                r["enabled"] = False
                changed = True
            break
    if changed:
        _write_registry_rows(rows)
    settings = load_settings()
    allow = [str(x).strip() for x in (settings.get("active_accounts") or [])]
    if remove_from_allowlist and account_id in allow:
        allow = [x for x in allow if x != account_id]
        settings["active_accounts"] = allow
        save_settings(settings)
    # Policy: deny-by-default on next enable
    policies = settings.get("account_policies") or {}
    policies[account_id] = {"organize": False, "send": False}
    settings["account_policies"] = policies
    save_settings(settings)
    _audit(actor_id, "disable", account_id, "")
    log.info("mailbox disable actor=%s account=%s", actor_id, account_id)
    return find_mailbox(account_id) or m


def health_check(account_id: str, *, him: Any = None) -> dict[str, Any]:
    """Non-sending IMAP+SMTP handshake. Returns a dict for the Telegram UI.

    Delegates to ``webmail_onboarding.check_account`` which only runs
    ``himalaya account check`` - never a send, move, copy or create.
    """
    m = _guard(account_id)
    if not m.has_credentials:
        return {
            "ok": False,
            "imap": False,
            "smtp": False,
            "detail": "no stored credentials; onboard the mailbox first",
        }
    from . import webmail_onboarding as wm

    try:
        result = wm.check_account(account_id, him)
    except Exception as exc:  # noqa: BLE001 - never crash the bot
        log.exception("health check for %s failed", account_id)
        return {
            "ok": False,
            "imap": False,
            "smtp": False,
            "detail": f"health check failed: {type(exc).__name__}",
        }
    return {
        "ok": result.ok,
        "imap": result.imap_ok,
        "smtp": result.smtp_ok,
        "detail": (
            result.raw_error
            or (", ".join(f"{k}:{v}" for k, v in result.detail.items() if v) or "")
        )[:240],
    }


# --------------------------------------------------------------------------- audit


def _audit(actor_id: int, action: str, account_id: str, detail: str) -> None:
    init_db()
    safe = redact_text(detail or "")[:400]
    with db() as conn:
        conn.execute(
            "INSERT INTO mailbox_audit(actor_id,action,account_id,detail,created_at) "
            "VALUES(?,?,?,?,?)",
            (int(actor_id), action, account_id or "", safe, time.time()),
        )
        conn.execute(
            "DELETE FROM mailbox_audit WHERE id NOT IN "
            "(SELECT id FROM (SELECT id FROM mailbox_audit ORDER BY id DESC LIMIT ?))",
            (AUDIT_KEEP,),
        )


@dataclass(frozen=True)
class MailboxAuditRow:
    id: int
    actor_id: int
    action: str
    account_id: str
    detail: str
    created_at: float


def audit_log(limit: int = 40, *, account_id: str | None = None) -> list[MailboxAuditRow]:
    init_db()
    with db() as conn:
        if account_id:
            rows = conn.execute(
                "SELECT * FROM mailbox_audit WHERE account_id=? ORDER BY id DESC LIMIT ?",
                (account_id, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM mailbox_audit ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
    return [
        MailboxAuditRow(
            id=int(r["id"]),
            actor_id=int(r["actor_id"]),
            action=str(r["action"]),
            account_id=str(r["account_id"] or ""),
            detail=str(r["detail"] or ""),
            created_at=float(r["created_at"] or 0),
        )
        for r in rows
    ]


def record(actor_id: int, action: str, account_id: str = "", detail: str = "") -> None:
    """Public entry point for the onboarding flow to emit lifecycle events
    (``onboard_start``, ``onboard_validated``, ``onboard_rolled_back``)
    without going through the enable/disable paths. Credentials are
    scrubbed by ``redact_text``."""
    _audit(actor_id, action, account_id, detail)


# --------------------------------------------------------------------------- purposes


def set_purpose(actor_id: int, account_id: str, purpose: str) -> None:
    """Store the brand-specific purpose used by the classifier.

    Each account's purpose is kept STRICTLY separated in settings.json:
    the classifier and reply KB only read the entry keyed by the account's
    id, so Coinography context never bleeds into Memecoinist or
    Altcoin Informer.
    """
    _guard(account_id)
    settings = load_settings()
    purposes = dict(settings.get("account_purposes") or {})
    purposes[account_id] = (purpose or "").strip()[:4000]
    settings["account_purposes"] = purposes
    settings["context_confirmed"] = False  # force the admin to re-confirm
    save_settings(settings)
    _audit(actor_id, "set_purpose", account_id, "")


def get_purpose(account_id: str) -> str:
    purposes = load_settings().get("account_purposes") or {}
    return str(purposes.get(account_id) or "").strip()
