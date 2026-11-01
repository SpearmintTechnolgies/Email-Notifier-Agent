"""Bounded-concurrency validator for bulk mailbox onboarding.

Reads rows from ``mailops.bulk.jobs``, unseals credentials one at a time,
and drives each row through the existing ``webmail_onboarding.add_account``
flow. ``add_account`` provides atomic registration + rollback and performs
IMAP + SMTP handshakes with TLS/cert verification; it does NOT send mail.

Policy
------
* Overall concurrency, per-host concurrency, and a per-host request-rate are
  enforced via async semaphores + a small token bucket.
* Rejected passwords (``failed_auth``) are NEVER retried automatically.
  Returning to the mailbox too soon risks a provider lockout; the admin
  must explicitly re-queue them from Telegram.
* Network / transient errors (``failed_network``) are backed off and
  (optionally) retried once per worker pass, up to ``MAX_AUTO_ATTEMPTS``.
* The validator respects job-level pause/cancel: it finishes its in-flight
  row then stops; it never leaves a 'running' row behind on exit.
* Newly registered accounts land with ``organize=false`` and ``send=false``
  (the existing ``webmail_onboarding._activate_account`` default). Monitoring
  activation is a separate opt-in step owned by ``mailops.bulk.service``.

Imports
-------
This module imports Himalaya + webmail_onboarding lazily so test harnesses
can inject a FakeHimalaya through ``run_job(..., him=fake)``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .. import mailbox_admin
from ..redact import redact_text
from . import jobs

log = logging.getLogger("mailops.bulk.validator")

MAX_AUTO_ATTEMPTS = 2         # one initial + one network retry
DEFAULT_WORKERS = 6
DEFAULT_PER_HOST = 3
DEFAULT_HOST_RATE = 2.0       # requests per second per host
BACKOFF_INITIAL_S = 2.0
BACKOFF_MAX_S = 30.0

# Hard upper bounds. The admin cannot push past these from .env because a
# laptop simply can't carry it; we clamp silently.
HARD_WORKERS = 16
HARD_PER_HOST = 6
HARD_HOST_RATE = 5.0


# Progress callback shape: ``await progress(job_id, state_dict)``.
ProgressFn = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class ValidatorConfig:
    workers: int = DEFAULT_WORKERS
    per_host: int = DEFAULT_PER_HOST
    host_rate: float = DEFAULT_HOST_RATE

    @classmethod
    def from_env(cls) -> "ValidatorConfig":
        def _int(key: str, default: int, hard: int) -> int:
            raw = (os.environ.get(key) or "").strip()
            if not raw:
                return default
            try:
                v = int(raw)
            except ValueError:
                return default
            return max(1, min(hard, v))

        def _float(key: str, default: float, hard: float) -> float:
            raw = (os.environ.get(key) or "").strip()
            if not raw:
                return default
            try:
                v = float(raw)
            except ValueError:
                return default
            return max(0.1, min(hard, v))

        return cls(
            workers=_int("MAILOPS_BULK_WORKERS", DEFAULT_WORKERS, HARD_WORKERS),
            per_host=_int("MAILOPS_BULK_PER_HOST", DEFAULT_PER_HOST, HARD_PER_HOST),
            host_rate=_float("MAILOPS_BULK_HOST_RATE_PER_SEC", DEFAULT_HOST_RATE, HARD_HOST_RATE),
        )


class _HostBudget:
    """Semaphore + token-bucket per IMAP/SMTP host.

    Enforces 'at most ``per_host`` concurrent IMAP sessions touching this
    host AND at most ``rate`` login attempts per second'.
    """

    def __init__(self, per_host: int, rate: float):
        self._per_host = max(1, per_host)
        self._rate = max(0.1, rate)
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._next_slot: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, host: str) -> None:
        host = (host or "").lower()
        async with self._lock:
            sem = self._sems.get(host)
            if sem is None:
                sem = asyncio.Semaphore(self._per_host)
                self._sems[host] = sem
        await sem.acquire()
        # Token-bucket smoothing: refuse to send another request to this host
        # faster than once every ``1 / rate`` seconds.
        async with self._lock:
            now = time.monotonic()
            earliest = self._next_slot.get(host, 0.0)
            wait = max(0.0, earliest - now)
            self._next_slot[host] = max(now, earliest) + (1.0 / self._rate)
        if wait > 0:
            await asyncio.sleep(wait)

    def release(self, host: str) -> None:
        host = (host or "").lower()
        sem = self._sems.get(host)
        if sem is not None:
            sem.release()


# ------------------------------------------------------------------ classification


_AUTH_HINTS = (
    "authenticationfailed",
    "auth failed",
    "authentication failed",
    "invalid credentials",
    "login failed",
    "535",            # SMTP auth reject
    "incorrect password",
    "bad credentials",
    "logon failed",
)
_NETWORK_HINTS = (
    "connection refused",
    "connection reset",
    "connection timed out",
    "timed out",
    "temporary failure",
    "name or service not known",
    "could not resolve",
    "name resolution",
    "network is unreachable",
    "ssl",
    "tls",
    "unexpectedeof",
    "socket",
    "econn",
    "handshake",
)


def _classify_failure(detail: str) -> str:
    """Return 'failed_auth' / 'failed_network' / 'failed_validation'."""
    text = (detail or "").lower()
    if any(h in text for h in _AUTH_HINTS):
        return "failed_auth"
    if any(h in text for h in _NETWORK_HINTS):
        return "failed_network"
    return "failed_validation"


# ------------------------------------------------------------------ main loop


async def _process_row(
    row: jobs.BulkRow,
    budget: _HostBudget,
    overall: asyncio.Semaphore,
    *,
    him: Any = None,
    now_fn: Callable[[], float] = time.time,
) -> tuple[str, str, str]:
    """Validate one row.

    Returns a tuple ``(final_status, failure_kind, failure_detail)``.
    """
    from .. import webmail_onboarding as wm

    try:
        imap_pw, smtp_pw = jobs.unseal_credentials(row.job_id, row.row_no)
    except jobs.BulkJobError as exc:
        return "failed_validation", "credential_blob", str(exc)

    spec = wm.WebmailSpec(
        account_id=row.account_id,
        email=row.email,
        imap_host=row.imap_host,
        smtp_host=row.smtp_host,
        imap_port=row.imap_port or wm.DEFAULT_IMAP_PORT,
        smtp_port=row.smtp_port or wm.DEFAULT_SMTP_PORT,
        imap_user=row.imap_user or row.email,
        smtp_user=row.smtp_user or row.imap_user or row.email,
        display_name=row.display_name or row.email.split("@", 1)[0],
    )
    problems = wm.validate_spec(spec)
    if problems:
        return "failed_validation", "shape", "; ".join(problems)

    # Pre-flight duplicate check: ``add_account`` also enforces this but we
    # want a clean 'failed_duplicate' status and no vault touch.
    try:
        existing_ids, existing_emails = wm._existing_ids_and_emails()
    except Exception as exc:  # noqa: BLE001
        return "failed_validation", "registry_read", f"{type(exc).__name__}"
    if spec.account_id in existing_ids:
        return "failed_duplicate", "account_id", f"id '{spec.account_id}' already registered"
    if spec.email.lower() in {e.lower() for e in existing_emails}:
        return "failed_duplicate", "email", f"{spec.email} already registered"

    hosts = {row.imap_host.lower(), row.smtp_host.lower()}
    acquired: list[str] = []
    attempt = 0
    last_detail = ""
    while attempt < MAX_AUTO_ATTEMPTS:
        attempt += 1
        try:
            await overall.acquire()
            for h in hosts:
                await budget.acquire(h)
                acquired.append(h)
            # Jitter so a burst of rows against the same host does not
            # synchronise retries.
            await asyncio.sleep(random.uniform(0, 0.1))
            try:
                result = await asyncio.to_thread(
                    wm.add_account, spec, imap_pw, smtp_pw, him=him
                )
            finally:
                for h in acquired:
                    budget.release(h)
                acquired.clear()
                overall.release()
        except wm.OnboardingError as exc:
            last_detail = redact_text(str(exc))[:300]
            kind = _classify_failure(last_detail)
            if kind != "failed_network":
                # Scrub the plaintext passwords from locals.
                imap_pw = ""
                smtp_pw = ""
                return kind, kind.replace("failed_", ""), last_detail
            # transient error -> back off and retry once
        except Exception as exc:  # noqa: BLE001 - defence in depth
            last_detail = f"{type(exc).__name__}"
            # Treat unknown exceptions as network-ish; next pass may succeed
            # or the admin can retry explicitly.
        else:
            imap_pw = ""
            smtp_pw = ""
            if result.ok:
                return "ok", "", ""
            detail = (
                result.raw_error
                or ", ".join(f"{k}:{v}" for k, v in result.detail.items() if v)
                or "validation failed"
            )
            detail = redact_text(detail)[:300]
            kind = _classify_failure(detail)
            if kind != "failed_network":
                return kind, kind.replace("failed_", ""), detail
            last_detail = detail
        # Backoff before next attempt.
        backoff = min(BACKOFF_MAX_S, BACKOFF_INITIAL_S * (2 ** (attempt - 1)))
        backoff *= 1.0 + random.uniform(-0.2, 0.2)
        await asyncio.sleep(backoff)
    # Exhausted auto-attempts on a network error.
    imap_pw = ""
    smtp_pw = ""
    return "failed_network", "network", last_detail or "network error"


async def run_job(
    job_id: str,
    *,
    config: ValidatorConfig | None = None,
    him: Any = None,
    progress: ProgressFn | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> jobs.BulkJob:
    """Drive one job to completion (or until paused / cancelled).

    ``stop_check`` is polled between rows; return True to abort. Normally
    the job's own ``state`` is the source of truth and the function stops
    on its own.
    """
    cfg = config or ValidatorConfig.from_env()
    budget = _HostBudget(per_host=cfg.per_host, rate=cfg.host_rate)
    overall = asyncio.Semaphore(cfg.workers)

    job = jobs.get_job(job_id)
    if job.state == "confirmed":
        jobs.mark_running(job_id)
    elif job.state != "running":
        return job

    async def _work_row(row: jobs.BulkRow) -> None:
        claimed = jobs.claim_row(row.job_id, row.row_no)
        if claimed is None:
            return
        status, kind, detail = await _process_row(
            claimed, budget, overall, him=him
        )
        jobs.finish_row(
            row.job_id,
            row.row_no,
            status,
            failure_kind=kind,
            failure_detail=detail,
            clear_credentials=(status == "ok"),
        )
        if status == "ok":
            try:
                mailbox_admin.record(
                    job.actor_id,
                    "bulk_onboarded",
                    claimed.account_id,
                    f"job={job_id} email={claimed.email}",
                )
            except Exception:  # noqa: BLE001 - audit is best-effort
                log.debug("audit record failed", exc_info=True)
        if progress is not None:
            try:
                await progress(job_id, _snapshot(job_id))
            except Exception:  # noqa: BLE001
                log.debug("progress callback raised", exc_info=True)

    while True:
        if stop_check and stop_check():
            break
        current = jobs.get_job(job_id)
        if current.state in ("paused", "cancelled", "done", "failed"):
            break
        batch = jobs.pending_rows(job_id, limit=max(cfg.workers * 2, 8))
        if not batch:
            jobs.finalise_if_done(job_id)
            break
        await asyncio.gather(*(_work_row(r) for r in batch))

    # Final finalise + credential scrub (the per-row path clears credentials
    # on success; this is a safety net).
    jobs.finalise_if_done(job_id)
    final = jobs.get_job(job_id)
    if final.is_terminal:
        jobs.purge_credentials(job_id)
    return final


def _snapshot(job_id: str) -> dict[str, Any]:
    """Password-free progress snapshot. Safe to post into Telegram."""
    job = jobs.get_job(job_id)
    rows = jobs.list_rows(job_id)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    return {
        "job_id": job_id,
        "state": job.state,
        "activation": job.activation,
        "total": len(rows),
        "counts": counts,
        "updated_at": job.updated_at,
    }


def snapshot(job_id: str) -> dict[str, Any]:
    """Public helper used by the Telegram UI."""
    return _snapshot(job_id)
