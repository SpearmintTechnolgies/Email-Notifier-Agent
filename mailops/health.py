"""Operational sync-health tracking + consolidated ops alerts.

Incident fixed: October 22, 2026, 18:28-20:16 IST. All three registered
mailboxes (altcoininformer, alien, marketing) produced the generic
"Sync issue on [account]. Check credentials / IMAP." alert every ~15-minute
poll, 24 Telegram messages across 8 cycles. The ROOT CAUSE was never IMAP,
DNS, TLS or credentials: every account shares exactly one local dependency,
the Himalaya CLI binary, and the OS (Windows Smart App Control / Application
Control) was blocking that single unsigned executable (``OSError [WinError
4551] An Application Control policy has blocked this file``), so every
account failed identically on every poll. See docs / incident report.

Two separate problems, two separate fixes:
  1. The binary's location (fixed in ``paths``/``.env``: moved off the
     Downloads tree, Mark-of-the-Web stripped).
  2. The alerting itself re-sent the SAME unresolved error every poll
     cycle forever, with zero deduplication across restarts and no way
     to distinguish "the OS blocked our tool" from "your password is
     wrong" (both produced the same generic sentence). THIS MODULE fixes
     that: it is the digest.py pattern (see that module's docstring)
     applied to operational errors instead of new mail.

Design
------
* ``record_sync_failure`` / ``record_sync_success`` are called once per
  account per poll from ``sync_worker.sync_once``. They NEVER touch
  Telegram directly - they only update the persisted ``sync_incidents``
  table (``mailops.db``), which is what makes this durable across a
  process restart: a crash-and-restart mid-incident does not re-alert
  because the row (and its ``pending_alert`` flag) is already set from
  before the crash, not re-derived from nothing.
* ``build_and_send_health_alert`` is the once-per-poll consolidated
  sender (mirrors ``digest.build_and_send_digest``): ONE Telegram message
  covering every account whose health STATE CHANGED (new incident,
  materially different error category, or recovery) since the last
  alert. An account stuck on the exact same error for 50 straight polls
  produces ZERO additional Telegram traffic.
* This is a completely separate message/table from the 15-minute mail
  digest (``mailops.digest``) - operational errors and new-mail counts
  are never combined into the same alert, per the incident requirements.
* ``classify_error`` turns a raw Python exception / Himalaya stderr into
  one of a small set of sanitized categories so the alert can finally
  distinguish "the OS blocked the sync tool" from "bad password" from
  "DNS/network" from "the mail provider rejected us" - instead of the
  one-size-fits-all "Check credentials / IMAP" sentence that caused the
  original misdiagnosis risk.
* ``run_manual_health_check`` backs the Telegram "Refresh / Retry Health
  Check" button. It is rate-limited per account (``MIN_MANUAL_RECHECK_S``)
  specifically so an impatient operator cannot hammer a real IMAP login
  with repeated bad-password attempts by mashing the button.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from .access import effective_notify_targets
from .config import active_account_ids, load_accounts
from .db import (
    clear_pending_health_alerts,
    get_incident,
    list_open_incidents,
    list_pending_health_alerts,
    purge_stale_resolved_incidents,
    record_health_failure,
    record_health_recovery,
    touch_incident_check,
)
from .redact import redact_text

log = logging.getLogger("mailops.health")

# Minimum seconds between two MANUAL health-check attempts for the same
# account. Stops an admin mashing "Retry" from turning into a repeated
# bad-password hammer against a real mail provider.
MIN_MANUAL_RECHECK_S = 60

# How long a fully-resolved, already-notified incident row lingers before
# housekeeping drops it (keeps a short "recently recovered" audit trail).
RESOLVED_RETENTION_S = 24 * 3600

# ---------------------------------------------------------------------------
# Error classification — sanitized categories only. Never includes a
# password, token, or raw provider response verbatim (redact_text() runs on
# every detail string before it is persisted or sent to Telegram).
# ---------------------------------------------------------------------------

CATEGORY_LABELS = {
    "local_dependency_blocked": "Local tool blocked (Application Control)",
    "binary_missing": "Sync tool not found",
    "auth_failed": "Authentication failed",
    "dns_failure": "DNS resolution failed",
    "tls_failure": "TLS/certificate error",
    "network_timeout": "Network timeout / unreachable",
    "provider_rejected": "Mail provider rejected the request",
    "internal_error": "Internal application error",
    "unknown": "Unknown sync error",
}

_AUTH_PAT = re.compile(
    r"authentic|invalid credential|login failed|AUTHENTICATIONFAILED|"
    r"NO \[AUTHENTICATIONFAILED\]|bad username|incorrect password",
    re.IGNORECASE,
)
_DNS_PAT = re.compile(
    r"getaddrinfo failed|name or service not known|nodename nor servname|"
    r"dns.{0,10}(fail|resolv)|11001", re.IGNORECASE
)
_TLS_PAT = re.compile(
    r"ssl|tls|certificate verify|CERTIFICATE_VERIFY_FAILED|handshake", re.IGNORECASE
)
_TIMEOUT_PAT = re.compile(
    r"timed? ?out|timeout|connection refused|unreachable|WSAETIMEDOUT|10060|10061",
    re.IGNORECASE,
)
_APP_CONTROL_PAT = re.compile(
    r"application control|smartscreen|4551|not digitally signed|"
    r"operation did not complete successfully because the file contains",
    re.IGNORECASE,
)
_PROVIDER_PAT = re.compile(
    r"quota|rate limit|too many connections|mailbox (unavailable|disabled)|"
    r"OVERQUOTA|try again later", re.IGNORECASE
)


def classify_error(exc: BaseException | str) -> tuple[str, str]:
    """Map a raw exception (or Himalaya stderr string) to
    ``(sanitized_category, sanitized_detail)``. ``detail`` is short,
    human-safe, and has every known secret pattern scrubbed."""
    text = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    text = redact_text(text)[:400]
    winerror = getattr(exc, "winerror", None) if not isinstance(exc, str) else None
    code = getattr(exc, "code", None) if not isinstance(exc, str) else None

    if winerror == 4551 or _APP_CONTROL_PAT.search(text):
        return "local_dependency_blocked", (
            "The Himalaya sync tool was blocked by an OS Application Control "
            "/ Smart App Control policy (unsigned or untrusted executable). "
            "This is a LOCAL machine policy issue, not a mailbox credential "
            "or network problem, and affects every account that shares this "
            "tool."
        )
    if code == 127 or "himalaya not found" in text.lower() or isinstance(exc, FileNotFoundError):
        return "binary_missing", "The Himalaya sync tool executable could not be located."
    if _AUTH_PAT.search(text):
        return "auth_failed", "The mailbox rejected the stored credentials (authentication failure)."
    if _DNS_PAT.search(text):
        return "dns_failure", "DNS lookup failed for the configured mail server."
    if _TLS_PAT.search(text):
        return "tls_failure", "TLS/SSL handshake or certificate verification failed."
    if code == 124 or _TIMEOUT_PAT.search(text):
        return "network_timeout", "Connection to the mail server timed out or was refused."
    if _PROVIDER_PAT.search(text):
        return "provider_rejected", "The mail provider rejected the request (quota/rate limit/unavailable)."
    if text.strip():
        return "internal_error", text
    return "unknown", "Unspecified sync error."


# ---------------------------------------------------------------------------
# Per-poll recorders (called from sync_worker; never touch Telegram)
# ---------------------------------------------------------------------------


def record_sync_failure(account_id: str, exc: BaseException) -> dict[str, Any]:
    category, detail = classify_error(exc)
    result = record_health_failure(account_id, category, detail)
    log.warning(
        "sync health: %s category=%s consecutive=%d new=%s changed=%s detail=%s",
        account_id,
        category,
        result["consecutive_failures"],
        result["is_new"],
        result["category_changed"],
        detail,
    )
    return result


def record_sync_success(account_id: str) -> bool:
    """Returns True iff this call represents a RECOVERY (the account was
    previously in an open incident)."""
    recovered = record_health_recovery(account_id)
    if recovered:
        log.info("sync health: %s recovered", account_id)
    return recovered


# ---------------------------------------------------------------------------
# Consolidated ops alert (digest.py pattern, applied to operational errors)
# ---------------------------------------------------------------------------


def _display_for(account_id: str) -> tuple[str, str]:
    for acc in load_accounts():
        if acc.id == account_id:
            return acc.display_name or acc.id, acc.email
    return account_id, ""


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "unknown"
    import datetime

    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _fmt_ago(ts: float | None) -> str:
    if not ts:
        return "unknown"
    secs = max(0, int(time.time() - ts))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60}m ago"
    return f"{secs // 86400}d ago"


def format_health_alert(rows: list[dict[str, Any]]) -> str:
    """ONE consolidated operational alert covering every account whose
    health state changed since the last alert. Distinct from, and never
    merged with, the new-mail digest."""
    opened = [r for r in rows if r["status"] == "open"]
    recovered = [r for r in rows if r["status"] == "resolved"]
    lines = ["Mailops operational alert"]
    if opened:
        lines.append(f"{len(opened)} mailbox(es) unhealthy:")
        for r in opened:
            name, email = _display_for(r["account_id"])
            label = CATEGORY_LABELS.get(r["category"], r["category"] or "unknown")
            lines.append(f"- {name} <{email}>")
            lines.append(
                f"    {label} · first failed {_fmt_ts(r['first_failure_at'])} "
                f"({_fmt_ago(r['first_failure_at'])}) · {r['consecutive_failures']}x in a row"
            )
            if r.get("detail"):
                lines.append(f"    {str(r['detail'])[:160]}")
    if recovered:
        if opened:
            lines.append("")
        lines.append(f"{len(recovered)} mailbox(es) recovered:")
        for r in recovered:
            name, email = _display_for(r["account_id"])
            lines.append(f"- {name} <{email}>  (healthy again as of {_fmt_ts(r['resolved_at'])})")
    lines.append("")
    lines.append("Tap Unhealthy Accounts to view current status or Retry Health Check to re-test now.")
    return "\n".join(lines)


def health_alert_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Unhealthy Accounts", callback_data="health:view"),
                InlineKeyboardButton("Retry Health Check", callback_data="health:retry:_all"),
            ],
        ]
    )


async def build_and_send_health_alert(
    bot: Bot | None,
    *,
    force: bool = False,
    reason: str = "scheduled",
) -> dict[str, Any]:
    """Compute the pending-change set and (if non-empty) send ONE
    consolidated Telegram message. Mirrors ``digest.build_and_send_digest``
    but for operational incidents instead of new mail; the two are never
    combined into a single message.
    """
    diag: dict[str, Any] = {"sent": False, "reason": reason, "changed": 0, "skipped": ""}
    actives = active_account_ids()
    rows = [r for r in list_pending_health_alerts() if r["account_id"] in actives]
    diag["changed"] = len(rows)
    if not rows:
        diag["skipped"] = "empty"
        return diag
    if bot is None:
        diag["skipped"] = "no_bot"
        return diag
    text = format_health_alert(rows)
    markup = health_alert_keyboard()
    from .notify import send_to_home

    try:
        await send_to_home(bot, text, markup, group_markup=markup, group_text=text)
    except Exception:  # noqa: BLE001
        # Delivery failed on every target: leave pending_alert set so the
        # next tick retries the SAME consolidated alert (never dropped,
        # never duplicated once it does land).
        log.exception("ops health alert delivery failed; incidents remain pending")
        raise
    cleared = clear_pending_health_alerts([r["account_id"] for r in rows])
    diag["sent"] = True
    try:
        purge_stale_resolved_incidents(RESOLVED_RETENTION_S)
    except Exception:  # noqa: BLE001
        log.debug("resolved-incident purge failed", exc_info=True)
    log.info("ops health alert sent: %d account(s) changed, %d row(s) cleaned up", len(rows), cleared)
    return diag


# ---------------------------------------------------------------------------
# Telegram "unhealthy accounts" dashboard view
# ---------------------------------------------------------------------------


def format_unhealthy_dashboard() -> str:
    rows = list_open_incidents()
    total = len(load_accounts())
    if not rows:
        return f"Account Health\n\nAll {total} mailbox(es) are healthy."
    lines = [f"Account Health\n\n{len(rows)} of {total} mailbox(es) unhealthy:"]
    for r in rows:
        name, email = _display_for(r["account_id"])
        label = CATEGORY_LABELS.get(r["category"], r["category"] or "unknown")
        lines.append("")
        lines.append(f"- {name} <{email}>")
        lines.append(
            f"    {label} · since {_fmt_ts(r['first_failure_at'])} "
            f"({_fmt_ago(r['first_failure_at'])}) · {r['consecutive_failures']} consecutive failure(s)"
        )
        if r.get("detail"):
            lines.append(f"    {str(r['detail'])[:160]}")
    return "\n".join(lines)


def unhealthy_dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Retry Health Check", callback_data="health:retry:_all")],
            [InlineKeyboardButton("Back to dashboard", callback_data="dash")],
        ]
    )


# ---------------------------------------------------------------------------
# Manual, rate-limited health check (backs the Telegram Retry button)
# ---------------------------------------------------------------------------


@dataclass
class HealthCheckResult:
    account_id: str
    ok: bool
    category: str = ""
    detail: str = ""
    rate_limited: bool = False


def _cooldown_remaining(account_id: str) -> float:
    row = get_incident(account_id)
    if not row or not row.get("last_manual_check_at"):
        return 0.0
    elapsed = time.time() - float(row["last_manual_check_at"])
    return max(0.0, MIN_MANUAL_RECHECK_S - elapsed)


def _probe_account(him, account_id: str, timeout: int = 30) -> None:
    """Minimal read-only IMAP round-trip. Raises on ANY failure (subprocess
    OSError such as the Application Control block, a non-zero Himalaya exit,
    a timeout, etc.) so the caller can classify it precisely. Returning
    normally means the probe succeeded.

    Himalaya v2 reports errors as JSON on STDOUT (not stderr) even with a
    non-zero exit code, e.g. ``{"error":"IMAP AUTHENTICATE PLAIN failed: ...
    "}``. Earlier versions of this probe only looked at stderr, which made a
    real authentication failure misclassify as a generic internal error."""
    r = him.run(
        ["-a", account_id, "envelope", "list", "-m", "INBOX", "-s", "1", "-p", "1", "--json"],
        timeout=timeout,
    )
    if not r.ok:
        msg = (r.stderr or "").strip() or (r.stdout or "").strip() or f"himalaya exited with code {r.code}"
        raise RuntimeError(msg)


def run_manual_health_check(account_ids: list[str] | None = None) -> list[HealthCheckResult]:
    """Read-only, rate-limited probe: ONE lightweight IMAP round-trip
    (envelope list, page size 1) per account via the already-configured
    Himalaya account, exactly like the automatic sync path, so it never
    retries a bad password more aggressively than a normal poll would -
    it is rate-limited per account to ``MIN_MANUAL_RECHECK_S`` on top of
    that so an impatient operator cannot mash the button into a
    credential-stuffing-shaped retry loop.
    """
    from .cache import _get_him

    targets = account_ids or [a.id for a in load_accounts()]
    him = _get_him()
    out: list[HealthCheckResult] = []
    for account_id in targets:
        remaining = _cooldown_remaining(account_id)
        if remaining > 0:
            out.append(
                HealthCheckResult(
                    account_id=account_id,
                    ok=False,
                    rate_limited=True,
                    detail=f"Retried too recently; wait {int(remaining)}s",
                )
            )
            continue
        touch_incident_check(account_id, manual=True)
        try:
            _probe_account(him, account_id)
            record_sync_success(account_id)
            out.append(HealthCheckResult(account_id=account_id, ok=True))
        except Exception as exc:  # noqa: BLE001
            record_sync_failure(account_id, exc)
            category, detail = classify_error(exc)
            out.append(
                HealthCheckResult(account_id=account_id, ok=False, category=category, detail=detail)
            )
    return out


__all__ = [
    "CATEGORY_LABELS",
    "classify_error",
    "record_sync_failure",
    "record_sync_success",
    "format_health_alert",
    "health_alert_keyboard",
    "build_and_send_health_alert",
    "format_unhealthy_dashboard",
    "unhealthy_dashboard_keyboard",
    "run_manual_health_check",
    "HealthCheckResult",
    "MIN_MANUAL_RECHECK_S",
]
