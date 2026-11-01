"""SQLite-backed job queue for bulk mailbox onboarding.

Tables (defined in ``mailops.db.init_db``):

* ``bulk_jobs``  - one row per uploaded CSV; owns the state machine.
* ``bulk_rows``  - one row per CSV data row; carries DPAPI-sealed credentials
                   and the per-row progress.

Credential handling
-------------------
Each row's IMAP (and optional separate SMTP) password is sealed with
``mailops.secrets_vault.seal`` (DPAPI on Windows, Fernet on CI) BEFORE
being written to SQLite. The plaintext never lands on disk. The validator
unseals in-process just long enough to call ``add_account`` and then drops
the reference. On successful registration, ``add_account`` stores the
long-term secret in the standard ``.enc`` vault file (same scheme the
single-account wizard uses) and we scrub the sealed job-row blob.

State machine
-------------
job.state ::=  uploaded -> previewed -> confirmed -> running -> done
                                       |-> cancelled
                                        -> paused -> running
                                        -> failed (fatal: e.g. CSV rejected after confirm)

row.status ::= pending -> running -> ok
                                  -> failed_auth        (do NOT retry automatically)
                                  -> failed_network     (eligible for backoff retry)
                                  -> failed_duplicate   (collides with existing registry)
                                  -> failed_validation  (shape error that slipped past csv_parse)
                                  -> rolled_back        (add_account rollback)
                                  -> skipped            (dupe within job)

Admins can PAUSE a running job (worker finishes its in-flight row then stops),
RESUME a paused one, CANCEL any non-terminal one, and RETRY only
``failed_network`` + explicitly re-queued rows. Auth failures are never
retried automatically - that would risk the provider locking the mailbox.

Everything here is importable without side effects; the ``init_db`` call is
cheap and idempotent.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .. import secrets_vault
from ..db import db, init_db
from ..redact import redact_text

log = logging.getLogger("mailops.bulk.jobs")

# Final job states (no further transitions).
TERMINAL_JOB_STATES = frozenset({"done", "cancelled", "failed"})
# Row statuses that are "finished" from the validator's perspective. The
# ``_retry_candidates`` helper decides which of those are actually retryable
# WITHOUT risking a mailbox lockout.
FINISHED_ROW_STATUSES = frozenset(
    {
        "ok",
        "failed_auth",
        "failed_network",
        "failed_duplicate",
        "failed_validation",
        "rolled_back",
        "skipped",
    }
)
RETRYABLE_ROW_STATUSES = frozenset({"failed_network", "failed_validation"})
# Statuses eligible for an explicit admin-triggered retry (the admin takes
# responsibility for a password that previously failed auth).
MANUALLY_RETRYABLE_STATUSES = RETRYABLE_ROW_STATUSES | {"failed_auth"}


class BulkJobError(ValueError):
    """A bulk-job action was refused. Message is safe to show to the admin."""


@dataclass
class BulkJob:
    job_id: str
    actor_id: int
    chat_id: int
    origin: str
    state: str
    activation: str
    total_rows: int
    valid_rows: int
    invalid_rows: int
    duplicate_rows: int
    created_at: float
    updated_at: float
    finished_at: float | None
    last_error: str
    preview: dict[str, Any] = field(default_factory=dict)
    document_file_id: str = ""
    document_message_id: int | None = None
    document_deleted: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_JOB_STATES


@dataclass
class BulkRow:
    job_id: str
    row_no: int
    account_id: str
    email: str
    display_name: str
    provider: str
    imap_host: str
    imap_port: int
    imap_user: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    status: str
    failure_kind: str
    failure_detail: str
    attempts: int
    started_at: float | None
    finished_at: float | None
    has_credentials: bool  # True iff cred_blob is populated

    def safe_summary(self) -> dict[str, Any]:
        """Password-free view used for progress and final reports."""
        return {
            "row_no": self.row_no,
            "account_id": self.account_id,
            "email": self.email,
            "display_name": self.display_name,
            "provider": self.provider,
            "imap_host": self.imap_host,
            "imap_port": self.imap_port,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "status": self.status,
            "failure_kind": self.failure_kind,
            "failure_detail": self.failure_detail,
            "attempts": self.attempts,
        }


# ------------------------------------------------------------------ CRUD


def create_job(
    actor_id: int,
    chat_id: int,
    origin: str,
    *,
    document_file_id: str = "",
    document_message_id: int | None = None,
) -> BulkJob:
    """Open a fresh job in state 'uploaded'. No rows yet."""
    if origin not in ("group", "private"):
        raise BulkJobError("origin must be 'group' or 'private'")
    init_db()
    job_id = secrets.token_urlsafe(10)
    now = _now()
    with db() as conn:
        conn.execute(
            "INSERT INTO bulk_jobs(job_id,actor_id,chat_id,origin,state,activation,"
            "total_rows,valid_rows,invalid_rows,duplicate_rows,"
            "created_at,updated_at,finished_at,last_error,preview_json,"
            "document_file_id,document_message_id,document_deleted) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,'','{}',?,?,0)",
            (
                job_id,
                int(actor_id),
                int(chat_id),
                origin,
                "uploaded",
                "inactive",
                0,
                0,
                0,
                0,
                now,
                now,
                document_file_id,
                document_message_id,
            ),
        )
    return get_job(job_id)


def get_job(job_id: str) -> BulkJob:
    init_db()
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM bulk_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
    if row is None:
        raise BulkJobError("Unknown bulk job.")
    return _row_to_job(row)


def list_jobs_for(actor_id: int, limit: int = 10) -> list[BulkJob]:
    init_db()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM bulk_jobs WHERE actor_id=? ORDER BY created_at DESC LIMIT ?",
            (int(actor_id), int(limit)),
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def active_job_for(actor_id: int) -> BulkJob | None:
    """Non-terminal job of this admin, if any (used to prevent overlap)."""
    for job in list_jobs_for(actor_id, limit=5):
        if not job.is_terminal:
            return job
    return None


def _guard_actor(job: BulkJob, actor_id: int) -> None:
    if int(job.actor_id) != int(actor_id):
        raise BulkJobError("This bulk job belongs to a different administrator.")


def _set_state(job_id: str, state: str, *, finish: bool = False, last_error: str = "") -> None:
    now = _now()
    with db() as conn:
        if finish:
            conn.execute(
                "UPDATE bulk_jobs SET state=?, updated_at=?, finished_at=?, last_error=? "
                "WHERE job_id=?",
                (state, now, now, redact_text(last_error)[:400], job_id),
            )
        else:
            conn.execute(
                "UPDATE bulk_jobs SET state=?, updated_at=?, last_error=? "
                "WHERE job_id=?",
                (state, now, redact_text(last_error)[:400], job_id),
            )


def record_preview(
    job_id: str,
    actor_id: int,
    *,
    rows_specs: Iterable[Any],
    invalid_count: int,
    duplicate_count: int,
    preview: dict[str, Any],
) -> BulkJob:
    """Attach parsed+resolved rows to the job and move to 'previewed'.

    ``rows_specs`` is an iterable of ``mailops.bulk.presets.ResolvedSpec``;
    they are inserted as 'pending' rows with sealed credentials. The caller
    MUST have already filtered out rows the admin should not try to onboard
    (duplicates, validation failures) - those stay purely in the summary.
    """
    job = get_job(job_id)
    _guard_actor(job, actor_id)
    if job.state not in ("uploaded",):
        raise BulkJobError(f"Cannot preview a job in state '{job.state}'.")
    specs = list(rows_specs)
    total = len(specs) + int(invalid_count)
    now = _now()
    with db() as conn:
        # Fresh preview wipes any previously stored rows (admin may re-upload).
        conn.execute("DELETE FROM bulk_rows WHERE job_id=?", (job_id,))
        for spec in specs:
            payload = {
                "imap_pw": spec.imap_password,
                "smtp_pw": spec.smtp_password or spec.imap_password,
            }
            blob = secrets_vault.seal(json.dumps(payload).encode("utf-8"))
            conn.execute(
                "INSERT INTO bulk_rows(job_id,row_no,account_id,email,display_name,"
                "provider,imap_host,imap_port,imap_user,smtp_host,smtp_port,smtp_user,"
                "status,failure_kind,failure_detail,attempts,cred_blob,started_at,finished_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,NULL,NULL)",
                (
                    job_id,
                    int(spec.row_no),
                    spec.account_id,
                    spec.email,
                    spec.display_name,
                    spec.provider,
                    spec.imap_host,
                    int(spec.imap_port),
                    spec.imap_user,
                    spec.smtp_host,
                    int(spec.smtp_port),
                    spec.smtp_user,
                    "pending" if not spec.problems else "failed_validation",
                    "" if not spec.problems else "preset_or_shape",
                    "; ".join(spec.problems)[:400],
                    blob,
                ),
            )
        conn.execute(
            "UPDATE bulk_jobs SET state=?, total_rows=?, valid_rows=?, "
            "invalid_rows=?, duplicate_rows=?, preview_json=?, updated_at=? "
            "WHERE job_id=?",
            (
                "previewed",
                total,
                sum(1 for s in specs if not s.problems),
                int(invalid_count) + sum(1 for s in specs if s.problems),
                int(duplicate_count),
                json.dumps(preview, ensure_ascii=False)[:60000],
                now,
                job_id,
            ),
        )
    return get_job(job_id)


def confirm(job_id: str, actor_id: int, *, activation: str = "inactive") -> BulkJob:
    """Admin confirmed - job becomes runnable."""
    if activation not in ("inactive", "active"):
        raise BulkJobError("activation must be 'inactive' or 'active'")
    job = get_job(job_id)
    _guard_actor(job, actor_id)
    if job.state != "previewed":
        raise BulkJobError(f"Cannot confirm a job in state '{job.state}'.")
    with db() as conn:
        conn.execute(
            "UPDATE bulk_jobs SET state=?, activation=?, updated_at=? WHERE job_id=?",
            ("confirmed", activation, _now(), job_id),
        )
    return get_job(job_id)


def mark_running(job_id: str) -> None:
    """Worker is about to start pulling rows."""
    _set_state(job_id, "running")


def pause(job_id: str, actor_id: int) -> BulkJob:
    job = get_job(job_id)
    _guard_actor(job, actor_id)
    if job.state not in ("running", "confirmed"):
        raise BulkJobError(f"Cannot pause a job in state '{job.state}'.")
    _set_state(job_id, "paused")
    return get_job(job_id)


def resume(job_id: str, actor_id: int) -> BulkJob:
    job = get_job(job_id)
    _guard_actor(job, actor_id)
    if job.state != "paused":
        raise BulkJobError(f"Cannot resume a job in state '{job.state}'.")
    # Transition back to 'confirmed' so the worker picks it up on its next scan.
    _set_state(job_id, "confirmed")
    return get_job(job_id)


def cancel(job_id: str, actor_id: int, *, reason: str = "cancelled") -> BulkJob:
    job = get_job(job_id)
    _guard_actor(job, actor_id)
    if job.is_terminal:
        return job
    # Mark the job terminal first so a racing worker will not pick a new row.
    _set_state(job_id, "cancelled", finish=True, last_error=reason)
    # Downgrade still-pending rows to 'skipped' - rows that already failed /
    # succeeded keep their outcome.
    with db() as conn:
        conn.execute(
            "UPDATE bulk_rows SET status='skipped', failure_detail=?, "
            "finished_at=? WHERE job_id=? AND status IN ('pending','running')",
            (redact_text(reason)[:200], _now(), job_id),
        )
    return get_job(job_id)


def finalise_if_done(job_id: str) -> BulkJob:
    """Flip to 'done' when every row has reached a terminal status."""
    job = get_job(job_id)
    if job.is_terminal:
        return job
    rows = list_rows(job_id)
    if not rows:
        return job
    if all(r.status in FINISHED_ROW_STATUSES for r in rows):
        _set_state(job_id, "done", finish=True)
    return get_job(job_id)


def mark_document_deleted(job_id: str, *, success: bool) -> None:
    """Record best-effort deletion of the uploaded Telegram message."""
    with db() as conn:
        conn.execute(
            "UPDATE bulk_jobs SET document_deleted=?, updated_at=? WHERE job_id=?",
            (1 if success else 2, _now(), job_id),
        )


def clear_document_handle(job_id: str) -> None:
    """Remove the Telegram file id as soon as ingest is complete.

    We only kept it so the worker could re-fetch the document if the parse
    failed and the admin asked to retry. Once the sealed credential blobs
    are in bulk_rows, the file id is useless to us and must not persist.
    """
    with db() as conn:
        conn.execute(
            "UPDATE bulk_jobs SET document_file_id='', updated_at=? WHERE job_id=?",
            (_now(), job_id),
        )


# ------------------------------------------------------------------ row ops


def list_rows(job_id: str) -> list[BulkRow]:
    init_db()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM bulk_rows WHERE job_id=? ORDER BY row_no ASC",
            (job_id,),
        ).fetchall()
    return [_row_to_bulkrow(r) for r in rows]


def pending_rows(job_id: str, limit: int = 32) -> list[BulkRow]:
    init_db()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM bulk_rows WHERE job_id=? AND status='pending' "
            "ORDER BY row_no ASC LIMIT ?",
            (job_id, int(limit)),
        ).fetchall()
    return [_row_to_bulkrow(r) for r in rows]


def claim_row(job_id: str, row_no: int) -> BulkRow | None:
    """Atomically flip 'pending' -> 'running' for one row.

    Returns the claimed row or ``None`` if someone else got there first
    (or the row is no longer pending). Called by the worker before any
    network activity so a pause/cancel race cannot start a second attempt.
    """
    now = _now()
    with db() as conn:
        cur = conn.execute(
            "UPDATE bulk_rows SET status='running', started_at=?, attempts=attempts+1 "
            "WHERE job_id=? AND row_no=? AND status='pending'",
            (now, job_id, int(row_no)),
        )
        if cur.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM bulk_rows WHERE job_id=? AND row_no=?",
            (job_id, int(row_no)),
        ).fetchone()
    return _row_to_bulkrow(row) if row else None


def finish_row(
    job_id: str,
    row_no: int,
    status: str,
    *,
    failure_kind: str = "",
    failure_detail: str = "",
    clear_credentials: bool = False,
) -> None:
    """Record the outcome of a row and (optionally) wipe its sealed blob."""
    if status not in FINISHED_ROW_STATUSES:
        raise BulkJobError(f"Unknown row status: {status}")
    now = _now()
    detail = redact_text(failure_detail or "")[:400]
    with db() as conn:
        if clear_credentials:
            conn.execute(
                "UPDATE bulk_rows SET status=?, failure_kind=?, failure_detail=?, "
                "finished_at=?, cred_blob=NULL WHERE job_id=? AND row_no=?",
                (status, failure_kind, detail, now, job_id, int(row_no)),
            )
        else:
            conn.execute(
                "UPDATE bulk_rows SET status=?, failure_kind=?, failure_detail=?, "
                "finished_at=? WHERE job_id=? AND row_no=?",
                (status, failure_kind, detail, now, job_id, int(row_no)),
            )


def requeue_row(job_id: str, row_no: int, actor_id: int) -> None:
    """Admin-triggered retry - flip a failed row back to 'pending' if it is
    eligible under the retry policy.

    Auth failures ARE retryable through this path (the admin explicitly took
    responsibility by tapping Retry); auto-retry inside the validator never
    walks this path.
    """
    job = get_job(job_id)
    _guard_actor(job, actor_id)
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM bulk_rows WHERE job_id=? AND row_no=?",
            (job_id, int(row_no)),
        ).fetchone()
        if row is None:
            raise BulkJobError("Unknown row.")
        if row["status"] not in MANUALLY_RETRYABLE_STATUSES:
            raise BulkJobError(
                f"Row {row_no} status '{row['status']}' is not retryable."
            )
        conn.execute(
            "UPDATE bulk_rows SET status='pending', failure_detail='', "
            "started_at=NULL, finished_at=NULL WHERE job_id=? AND row_no=?",
            (job_id, int(row_no)),
        )
    # If the job already finished, nudge it back to runnable.
    if job.is_terminal:
        _set_state(job_id, "confirmed")


def retry_failed(job_id: str, actor_id: int) -> int:
    """Requeue every retryable row. Returns the number re-armed."""
    job = get_job(job_id)
    _guard_actor(job, actor_id)
    now = _now()
    with db() as conn:
        cur = conn.execute(
            "UPDATE bulk_rows SET status='pending', failure_detail='', "
            "started_at=NULL, finished_at=NULL "
            "WHERE job_id=? AND status IN ('failed_network','failed_validation')",
            (job_id,),
        )
        count = int(cur.rowcount or 0)
    if count and job.is_terminal:
        _set_state(job_id, "confirmed")
    return count


def unseal_credentials(job_id: str, row_no: int) -> tuple[str, str]:
    """Return (imap_pw, smtp_pw) for one row.

    Called only by the validator worker. The decrypted plaintext is held in
    the caller's locals; this function does NOT log, cache, or echo it.
    """
    init_db()
    with db() as conn:
        row = conn.execute(
            "SELECT cred_blob FROM bulk_rows WHERE job_id=? AND row_no=?",
            (job_id, int(row_no)),
        ).fetchone()
    if row is None or row["cred_blob"] is None:
        raise BulkJobError("No sealed credentials on file for this row.")
    plain = secrets_vault.open_envelope(bytes(row["cred_blob"]))
    try:
        payload = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BulkJobError(f"Credential blob is unreadable: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise BulkJobError("Credential blob is unreadable.")
    imap_pw = str(payload.get("imap_pw") or "")
    smtp_pw = str(payload.get("smtp_pw") or imap_pw)
    if not imap_pw:
        raise BulkJobError("Row has no stored IMAP password.")
    return imap_pw, smtp_pw


def purge_credentials(job_id: str) -> int:
    """Wipe every sealed credential blob for a job (e.g. after done/cancel)."""
    init_db()
    with db() as conn:
        cur = conn.execute(
            "UPDATE bulk_rows SET cred_blob=NULL WHERE job_id=? AND cred_blob IS NOT NULL",
            (job_id,),
        )
    return int(cur.rowcount or 0)


def runnable_jobs() -> list[BulkJob]:
    """Jobs the background worker should wake up for."""
    init_db()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM bulk_jobs WHERE state IN ('confirmed','running') "
            "ORDER BY created_at ASC"
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def purge_terminal(older_than_s: float = 30 * 24 * 3600) -> int:
    """Drop closed jobs older than ``older_than_s`` seconds (default 30d).

    Keeps the DB bounded while leaving fresh audit history available.
    """
    init_db()
    cutoff = _now() - older_than_s
    with db() as conn:
        rows = conn.execute(
            "SELECT job_id FROM bulk_jobs WHERE finished_at IS NOT NULL "
            "AND finished_at < ?",
            (cutoff,),
        ).fetchall()
        for r in rows:
            conn.execute("DELETE FROM bulk_rows WHERE job_id=?", (r["job_id"],))
            conn.execute("DELETE FROM bulk_jobs WHERE job_id=?", (r["job_id"],))
        return len(rows)


# ------------------------------------------------------------------ helpers


def _now() -> float:
    return time.time()


def _row_to_job(row: Any) -> BulkJob:
    try:
        preview = json.loads(row["preview_json"] or "{}")
    except ValueError:
        preview = {}
    if not isinstance(preview, dict):
        preview = {}
    return BulkJob(
        job_id=str(row["job_id"]),
        actor_id=int(row["actor_id"]),
        chat_id=int(row["chat_id"]),
        origin=str(row["origin"]),
        state=str(row["state"]),
        activation=str(row["activation"] or "inactive"),
        total_rows=int(row["total_rows"] or 0),
        valid_rows=int(row["valid_rows"] or 0),
        invalid_rows=int(row["invalid_rows"] or 0),
        duplicate_rows=int(row["duplicate_rows"] or 0),
        created_at=float(row["created_at"] or 0),
        updated_at=float(row["updated_at"] or 0),
        finished_at=None if row["finished_at"] is None else float(row["finished_at"]),
        last_error=str(row["last_error"] or ""),
        preview=preview,
        document_file_id=str(row["document_file_id"] or ""),
        document_message_id=(
            None if row["document_message_id"] is None else int(row["document_message_id"])
        ),
        document_deleted=int(row["document_deleted"] or 0),
    )


def _row_to_bulkrow(row: Any) -> BulkRow:
    return BulkRow(
        job_id=str(row["job_id"]),
        row_no=int(row["row_no"]),
        account_id=str(row["account_id"] or ""),
        email=str(row["email"] or ""),
        display_name=str(row["display_name"] or ""),
        provider=str(row["provider"] or ""),
        imap_host=str(row["imap_host"] or ""),
        imap_port=int(row["imap_port"] or 0),
        imap_user=str(row["imap_user"] or ""),
        smtp_host=str(row["smtp_host"] or ""),
        smtp_port=int(row["smtp_port"] or 0),
        smtp_user=str(row["smtp_user"] or ""),
        status=str(row["status"] or ""),
        failure_kind=str(row["failure_kind"] or ""),
        failure_detail=str(row["failure_detail"] or ""),
        attempts=int(row["attempts"] or 0),
        started_at=None if row["started_at"] is None else float(row["started_at"]),
        finished_at=None if row["finished_at"] is None else float(row["finished_at"]),
        has_credentials=row["cred_blob"] is not None,
    )
