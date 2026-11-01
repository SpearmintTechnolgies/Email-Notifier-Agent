from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .paths import DATA_DIR, OPS_DB


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(OPS_DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
              account_id TEXT NOT NULL,
              msg_id TEXT NOT NULL,
              processed_at REAL NOT NULL,
              PRIMARY KEY (account_id, msg_id)
            );
            CREATE TABLE IF NOT EXISTS notified (
              key TEXT PRIMARY KEY,
              created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drafts (
              user_id INTEGER PRIMARY KEY,
              account_id TEXT,
              mailbox TEXT,
              msg_id TEXT,
              body TEXT,
              updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ui_sessions (
              user_id INTEGER PRIMARY KEY,
              state_json TEXT NOT NULL,
              updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS message_locations (
              account_id TEXT NOT NULL,
              msg_id TEXT NOT NULL,
              mailbox TEXT NOT NULL,
              subject TEXT,
              sender TEXT,
              priority INTEGER,
              updated_at REAL NOT NULL,
              PRIMARY KEY (account_id, msg_id)
            );
            CREATE TABLE IF NOT EXISTS message_identity (
              account_id TEXT NOT NULL,
              mailbox TEXT NOT NULL,
              msg_id TEXT NOT NULL,
              message_id TEXT NOT NULL DEFAULT '',
              subject TEXT NOT NULL DEFAULT '',
              sender TEXT NOT NULL DEFAULT '',
              date TEXT NOT NULL DEFAULT '',
              recorded_at REAL NOT NULL,
              PRIMARY KEY (account_id, mailbox, msg_id)
            );
            CREATE TABLE IF NOT EXISTS pending_notifications (
              key TEXT PRIMARY KEY,
              account_id TEXT NOT NULL,
              mailbox TEXT NOT NULL,
              msg_id TEXT NOT NULL,
              payload TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              created_at REAL NOT NULL,
              last_attempt_at REAL
            );
            CREATE TABLE IF NOT EXISTS pending_batch (
              key TEXT PRIMARY KEY,
              account_id TEXT NOT NULL,
              mailbox TEXT NOT NULL,
              msg_id TEXT NOT NULL,
              subject TEXT,
              sender TEXT,
              priority INTEGER NOT NULL,
              created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS send_approvals (
              token TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              account_id TEXT NOT NULL,
              mailbox TEXT NOT NULL,
              msg_id TEXT NOT NULL,
              message_id TEXT NOT NULL,
              recipient TEXT NOT NULL,
              subject TEXT NOT NULL,
              from_addr TEXT NOT NULL,
              body_sha256 TEXT NOT NULL,
              in_reply_to TEXT NOT NULL,
              references_header TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at REAL NOT NULL,
              claimed_at REAL,
              finished_at REAL,
              smtp_code INTEGER,
              smtp_note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS access_users (
              user_id INTEGER PRIMARY KEY,
              role TEXT NOT NULL CHECK(role IN ('owner','operator','viewer')),
              can_send INTEGER NOT NULL DEFAULT 0,
              is_bootstrap INTEGER NOT NULL DEFAULT 0,
              name TEXT NOT NULL DEFAULT '',
              username TEXT NOT NULL DEFAULT '',
              added_by INTEGER,
              added_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS access_groups (
              chat_id INTEGER PRIMARY KEY,
              title TEXT NOT NULL DEFAULT '',
              added_by INTEGER,
              added_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS access_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              actor_id INTEGER NOT NULL,
              action TEXT NOT NULL,
              target_type TEXT NOT NULL DEFAULT '',
              target_id INTEGER,
              detail TEXT NOT NULL DEFAULT '',
              created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS access_actions (
              token TEXT PRIMARY KEY,
              actor_id INTEGER NOT NULL,
              action TEXT NOT NULL,
              payload TEXT NOT NULL,
              created_at REAL NOT NULL,
              used_at REAL
            );
            CREATE TABLE IF NOT EXISTS access_awaiting (
              user_id INTEGER PRIMARY KEY,
              mode TEXT NOT NULL,
              payload TEXT NOT NULL DEFAULT '{}',
              created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS access_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS group_action_tokens (
              token TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              chat_id INTEGER NOT NULL,
              action TEXT NOT NULL,
              payload TEXT NOT NULL DEFAULT '{}',
              created_at REAL NOT NULL,
              expires_at REAL NOT NULL,
              used_at REAL
            );
            CREATE TABLE IF NOT EXISTS onboarding_sessions (
              token TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              mode TEXT NOT NULL,         -- 'cli' or 'dm'
              state TEXT NOT NULL,        -- 'draft'|'awaiting_credentials'|'validating'|'done'|'failed'|'cancelled'|'expired'
              fields_json TEXT NOT NULL DEFAULT '{}',   -- encrypted blob of non-secret onboarding fields
              nonce TEXT NOT NULL,        -- CSRF-style nonce rotated on each transition
              last_error TEXT NOT NULL DEFAULT '',
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              expires_at REAL NOT NULL,
              finished_at REAL
            );
            CREATE INDEX IF NOT EXISTS onboarding_sessions_user
              ON onboarding_sessions(user_id, state);
            CREATE TABLE IF NOT EXISTS mailbox_audit (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              actor_id INTEGER NOT NULL,
              action TEXT NOT NULL,       -- 'onboard_start','onboard_validated','onboard_rolled_back','enable','disable','health','duplicate_refused',...
              account_id TEXT NOT NULL DEFAULT '',
              detail TEXT NOT NULL DEFAULT '', -- NEVER contains passwords (redacted on write)
              created_at REAL NOT NULL
            );
            -- Bulk CSV mailbox onboarding (see mailops/bulk/*).
            -- A row in bulk_jobs owns N rows in bulk_rows. Credentials for each
            -- row are DPAPI-sealed in the ``cred_blob`` column; nothing in these
            -- tables ever stores a plaintext password, URL, token or raw CSV.
            CREATE TABLE IF NOT EXISTS bulk_jobs (
              job_id TEXT PRIMARY KEY,
              actor_id INTEGER NOT NULL,         -- Telegram user id that owns the job
              chat_id INTEGER NOT NULL,          -- chat the job is controlled from
              origin TEXT NOT NULL,              -- 'group' | 'private'
              state TEXT NOT NULL,               -- 'uploaded'|'previewed'|'confirmed'|'running'|'paused'|'cancelled'|'done'|'failed'
              activation TEXT NOT NULL DEFAULT 'inactive',   -- 'inactive'|'active'
              total_rows INTEGER NOT NULL DEFAULT 0,
              valid_rows INTEGER NOT NULL DEFAULT 0,
              invalid_rows INTEGER NOT NULL DEFAULT 0,
              duplicate_rows INTEGER NOT NULL DEFAULT 0,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              finished_at REAL,
              last_error TEXT NOT NULL DEFAULT '',
              preview_json TEXT NOT NULL DEFAULT '{}',   -- redacted preview (counts, providers, no secrets)
              document_file_id TEXT NOT NULL DEFAULT '', -- Telegram file id (NOT a URL), kept only until ingest finishes
              document_message_id INTEGER,       -- Telegram message id of the upload (so we can delete it)
              document_deleted INTEGER NOT NULL DEFAULT 0 -- 0=no attempt, 1=deleted, 2=delete failed
            );
            CREATE INDEX IF NOT EXISTS bulk_jobs_actor ON bulk_jobs(actor_id, state);
            CREATE TABLE IF NOT EXISTS bulk_rows (
              job_id TEXT NOT NULL,
              row_no INTEGER NOT NULL,                   -- 1-based row number inside the uploaded CSV
              account_id TEXT NOT NULL DEFAULT '',       -- derived or supplied
              email TEXT NOT NULL DEFAULT '',
              display_name TEXT NOT NULL DEFAULT '',
              provider TEXT NOT NULL DEFAULT '',
              imap_host TEXT NOT NULL DEFAULT '',
              imap_port INTEGER NOT NULL DEFAULT 993,
              imap_user TEXT NOT NULL DEFAULT '',
              smtp_host TEXT NOT NULL DEFAULT '',
              smtp_port INTEGER NOT NULL DEFAULT 465,
              smtp_user TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL,                      -- 'pending'|'running'|'ok'|'failed_auth'|'failed_network'|'failed_duplicate'|'failed_validation'|'skipped'|'rolled_back'
              failure_kind TEXT NOT NULL DEFAULT '',     -- short category for retry decisions
              failure_detail TEXT NOT NULL DEFAULT '',   -- redacted human-safe message
              attempts INTEGER NOT NULL DEFAULT 0,
              cred_blob BLOB,                            -- DPAPI/Fernet-sealed JSON {imap_pw, smtp_pw}
              started_at REAL,
              finished_at REAL,
              PRIMARY KEY (job_id, row_no)
            );
            CREATE INDEX IF NOT EXISTS bulk_rows_status ON bulk_rows(job_id, status);
            CREATE INDEX IF NOT EXISTS bulk_rows_account ON bulk_rows(account_id);
            -- Consolidated 15-minute Telegram digest.
            --
            -- ``digest_pending`` rows are newly-discovered actionable emails
            -- (P0 / P1 by default; configurable) that have NOT yet been
            -- counted in a successfully-delivered digest. Rows are created
            -- by the sync worker and deleted when a digest covering their
            -- window is confirmed delivered by Telegram, so an inbox is
            -- never counted twice and a crash between discovery and send
            -- can replay from this table (point 6 + 10 of the design).
            CREATE TABLE IF NOT EXISTS digest_pending (
              key TEXT PRIMARY KEY,          -- "<account_id>:<mailbox>:<msg_id>"
              account_id TEXT NOT NULL,
              mailbox TEXT NOT NULL,
              msg_id TEXT NOT NULL,
              priority INTEGER NOT NULL,
              subject TEXT NOT NULL DEFAULT '',
              sender TEXT NOT NULL DEFAULT '',
              discovered_at REAL NOT NULL,
              seeded INTEGER NOT NULL DEFAULT 0,  -- 1 = first-sync backlog, never alert
              delivered_at REAL                   -- set only when a digest succeeded
            );
            CREATE INDEX IF NOT EXISTS digest_pending_undelivered
              ON digest_pending(account_id, priority, delivered_at);
            CREATE INDEX IF NOT EXISTS digest_pending_window
              ON digest_pending(discovered_at);
            -- One-row-per-key digest run state. ``last_run_at`` is the start
            -- of the window just delivered. ``last_window_count`` is the
            -- number of actionable rows in that delivery (for 'no mail =>
            -- no message' decisions). ``last_run_token`` prevents two
            -- concurrent builds (sync_once overlap, Manual refresh, restart
            -- race) from double-sending the same window.
            CREATE TABLE IF NOT EXISTS digest_state (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL DEFAULT '',
              updated_at REAL NOT NULL
            );
            -- Per-mailbox 'first sync completed' marker. Rows discovered
            -- BEFORE this timestamp are seeded (silent) so that onboarding
            -- an inbox with hundreds of emails does not spam the digest.
            CREATE TABLE IF NOT EXISTS mailbox_sync_state (
              account_id TEXT PRIMARY KEY,
              first_sync_at REAL,            -- NULL until the first pass completes
              initial_seeded_count INTEGER NOT NULL DEFAULT 0
            );
            -- Operational sync-health incidents (October 2026 alert-flood fix).
            --
            -- One row per account. A sync failure OPENS (or refreshes) an
            -- incident here instead of firing a Telegram message directly.
            -- ``pending_alert`` is the only thing the consolidated ops-alert
            -- builder looks at: it is set to 1 on first detection of a new
            -- incident, on a MATERIAL category change while already open,
            -- and on recovery (status flips back to 'resolved'). Repeated
            -- identical failures every poll cycle do NOT set it again, so
            -- the same unresolved error never re-floods Telegram. The row
            -- (and ``pending_alert``) survive a process restart because
            -- this lives in ``data/ops.db``, which is how restart dedupe
            -- is guaranteed without any extra state file.
            CREATE TABLE IF NOT EXISTS sync_incidents (
              account_id TEXT PRIMARY KEY,
              status TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'resolved'
              category TEXT NOT NULL DEFAULT '',      -- sanitized category, see mailops.health
              detail TEXT NOT NULL DEFAULT '',        -- redacted human-readable detail, no secrets
              first_failure_at REAL NOT NULL,
              last_failure_at REAL NOT NULL,
              consecutive_failures INTEGER NOT NULL DEFAULT 0,
              last_check_at REAL,
              last_manual_check_at REAL,              -- rate-limits the manual Retry button
              resolved_at REAL,
              pending_alert INTEGER NOT NULL DEFAULT 0,
              last_alert_sent_at REAL
            );
            """
        )


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def already_notified(key: str) -> bool:
    with db() as conn:
        row = conn.execute("SELECT 1 FROM notified WHERE key=?", (key,)).fetchone()
        return row is not None


def mark_notified(key: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO notified(key, created_at) VALUES(?,?)",
            (key, time.time()),
        )


def save_draft(user_id: int, account_id: str, mailbox: str, msg_id: str, body: str) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO drafts(user_id, account_id, mailbox, msg_id, body, updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              account_id=excluded.account_id,
              mailbox=excluded.mailbox,
              msg_id=excluded.msg_id,
              body=excluded.body,
              updated_at=excluded.updated_at
            """,
            (user_id, account_id, mailbox, msg_id, body, time.time()),
        )


def get_draft(user_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM drafts WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None


def clear_draft(user_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM drafts WHERE user_id=?", (user_id,))


def set_message_location(
    account_id: str,
    msg_id: str,
    mailbox: str,
    subject: str = "",
    sender: str = "",
    priority: int | None = None,
) -> None:
    """Record the logical mailbox a message was classified into.

    In test mode the physical IMAP move is skipped, so this row is the source
    of truth the dashboard cache uses to render priority folders virtually.
    In production it mirrors the real move and is harmless.
    """
    with db() as conn:
        conn.execute(
            """
            INSERT INTO message_locations
            (account_id, msg_id, mailbox, subject, sender, priority, updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(account_id, msg_id) DO UPDATE SET
              mailbox=excluded.mailbox,
              subject=excluded.subject,
              sender=excluded.sender,
              priority=excluded.priority,
              updated_at=excluded.updated_at
            """,
            (account_id, msg_id, mailbox, subject, sender, priority, time.time()),
        )


def get_message_locations(account_id: str) -> dict[str, dict[str, Any]]:
    """{msg_id: row} for every classified message of an account."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM message_locations WHERE account_id=?",
            (account_id,),
        ).fetchall()
        return {str(r["msg_id"]): dict(r) for r in rows}


def locations_by_priority(account_id: str, priority: int) -> list[dict[str, Any]]:
    """Recorded classifications for one account at one priority.

    Enough to render a bucket list screen (subject, sender, recorded folder)
    without touching IMAP — the fallback used when the mail server is
    unreachable. Newest first, matching the live listing order.
    """
    with db() as conn:
        rows = conn.execute(
            """
            SELECT msg_id, mailbox, subject, sender, priority, updated_at
              FROM message_locations
             WHERE account_id=? AND priority=?
             ORDER BY updated_at DESC
            """,
            (account_id, int(priority)),
        ).fetchall()
    return [dict(r) for r in rows]


def count_locations_by_priority() -> dict[str, dict[int, int]]:
    """``{account_id: {priority: count}}`` over every classified message.

    One grouped query, so the dashboard can be populated from local state in
    milliseconds instead of waiting on a full-fleet IMAP pass (see
    ``mailops.cache.seed_from_db``). Rows with a NULL priority are skipped:
    they were recorded before classification stored one.
    """
    out: dict[str, dict[int, int]] = {}
    with db() as conn:
        rows = conn.execute(
            """
            SELECT account_id, priority, COUNT(*) AS n
              FROM message_locations
             WHERE priority IS NOT NULL
             GROUP BY account_id, priority
            """
        ).fetchall()
    for r in rows:
        try:
            account = str(r["account_id"])
            priority = int(r["priority"])
        except Exception:  # noqa: BLE001 - ignore a malformed row
            continue
        out.setdefault(account, {})[priority] = int(r["n"])
    return out


def queue_notification(
    key: str,
    account_id: str,
    mailbox: str,
    msg_id: str,
    payload: dict[str, Any],
) -> bool:
    """Persist a pending P0/P1 alert. Returns True if newly queued.

    INSERT OR IGNORE makes queueing idempotent: reprocessing the same message
    (e.g. after a crash between move and notify) never creates a duplicate row.
    """
    import json

    with db() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO pending_notifications
            (key, account_id, mailbox, msg_id, payload, attempts, created_at)
            VALUES(?,?,?,?,?,0,?)
            """,
            (key, account_id, mailbox, msg_id, json.dumps(payload), time.time()),
        )
        return cur.rowcount > 0


def list_pending_notifications(
    account_ids: "set[str] | list[str] | None" = None,
) -> list[dict[str, Any]]:
    """Queued alerts, oldest first. ``account_ids`` limits the result to those
    accounts (rows of other accounts stay in the table, untouched)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_notifications ORDER BY created_at ASC"
        ).fetchall()
        out = [dict(r) for r in rows]
    if account_ids is not None:
        allowed = set(account_ids)
        out = [r for r in out if r["account_id"] in allowed]
    return out


def bump_notification_attempt(key: str) -> None:
    with db() as conn:
        conn.execute(
            """
            UPDATE pending_notifications
            SET attempts = attempts + 1, last_attempt_at = ?
            WHERE key = ?
            """,
            (time.time(), key),
        )


def mark_notification_delivered(key: str) -> None:
    """Telegram confirmed the alert: drop the pending row and record the dedupe key."""
    with db() as conn:
        conn.execute("DELETE FROM pending_notifications WHERE key=?", (key,))
    mark_notified(key)


def queue_batch(
    key: str,
    account_id: str,
    mailbox: str,
    msg_id: str,
    subject: str,
    sender: str,
    priority: int,
) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO pending_batch(key, account_id, mailbox, msg_id, subject, sender, priority, created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (key, account_id, mailbox, msg_id, subject, sender, priority, time.time()),
        )


def pop_batch(
    older_than_seconds: float = 0,
    account_ids: "set[str] | list[str] | None" = None,
) -> list[dict[str, Any]]:
    """Remove and return due P2 batch rows. With ``account_ids`` only those
    accounts' rows are popped; rows of other accounts are left in place."""
    cutoff = time.time() - older_than_seconds
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_batch WHERE created_at <= ? ORDER BY created_at ASC",
            (cutoff,),
        ).fetchall()
        if account_ids is not None:
            allowed = set(account_ids)
            rows = [r for r in rows if r["account_id"] in allowed]
        keys = [r["key"] for r in rows]
        if keys:
            conn.executemany("DELETE FROM pending_batch WHERE key=?", [(k,) for k in keys])
        return [dict(r) for r in rows]


def is_message_processed(account_id: str, msg_id: str) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_messages WHERE account_id=? AND msg_id=?",
            (account_id, msg_id),
        ).fetchone()
        return row is not None


def mark_message_processed(account_id: str, msg_id: str) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO processed_messages
            (account_id, msg_id, processed_at)
            VALUES (?, ?, ?)
            """,
            (account_id, msg_id, time.time()),
        )


# ---------------------------------------------------------------------------
# Consolidated 15-minute digest (point 1-10 of the Mailops notification redesign).
#
# A single digest message per 15-minute window replaces per-mailbox / per-email
# alerts. These helpers persist discovery + delivery state so:
#   - the same email is never counted in two digests (point 6);
#   - a crash between discovery and send does not drop or duplicate alerts
#     (point 10 - restart recovery);
#   - a mailbox's first-ever sync does NOT fire an alert for its historical
#     backlog, which is what caused the Alien FI 12-13 message flood today
#     and would scale to hundreds of messages across 90-100 inboxes.
# ---------------------------------------------------------------------------


def _digest_key(account_id: str, mailbox: str, msg_id: str) -> str:
    return f"{account_id}:{mailbox}:{msg_id}"


def enqueue_digest_pending(
    account_id: str,
    mailbox: str,
    msg_id: str,
    priority: int,
    subject: str = "",
    sender: str = "",
    *,
    seeded: bool = False,
    discovered_at: float | None = None,
) -> bool:
    """Record a newly-classified email for the next digest window.

    Idempotent on ``(account_id, mailbox, msg_id)``: re-syncing the same
    message (e.g. after a crash, or because the IMAP envelope was returned
    twice during a restart) never creates a duplicate pending row.

    ``seeded=True`` marks rows discovered on a mailbox's first sync so the
    digest counter can ignore them - the mailbox is already covered by the
    priority dashboard and must not fire a 500-email catch-up alert.
    """
    key = _digest_key(account_id, mailbox, msg_id)
    ts = time.time() if discovered_at is None else float(discovered_at)
    with db() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO digest_pending
            (key, account_id, mailbox, msg_id, priority, subject, sender,
             discovered_at, seeded, delivered_at)
            VALUES (?,?,?,?,?,?,?,?,?,NULL)
            """,
            (
                key,
                account_id,
                mailbox,
                msg_id,
                int(priority),
                (subject or "")[:400],
                (sender or "")[:200],
                ts,
                1 if seeded else 0,
            ),
        )
        return cur.rowcount > 0


def list_undelivered_digest_rows(
    account_ids: "set[str] | list[str] | None" = None,
    *,
    max_priority: int = 1,
    include_seeded: bool = False,
    limit: int = 10000,
) -> list[dict[str, Any]]:
    """Return undelivered digest rows, oldest first.

    By default returns actionable (P0 / P1) rows that are NOT first-sync
    seeded. ``max_priority`` can be relaxed to pull in P2 when the digest
    policy includes Normal.
    """
    with db() as conn:
        rows = conn.execute(
            """
            SELECT key, account_id, mailbox, msg_id, priority, subject, sender,
                   discovered_at, seeded
            FROM digest_pending
            WHERE delivered_at IS NULL
              AND priority <= ?
              AND (seeded = 0 OR ? = 1)
            ORDER BY discovered_at ASC
            LIMIT ?
            """,
            (int(max_priority), 1 if include_seeded else 0, int(limit)),
        ).fetchall()
    out = [dict(r) for r in rows]
    if account_ids is not None:
        allowed = {str(a) for a in account_ids}
        out = [r for r in out if r["account_id"] in allowed]
    return out


def mark_digest_delivered(keys: "list[str] | tuple[str, ...]") -> int:
    """Confirm a digest window landed on Telegram. Returns rows updated.

    Rows stay in the table (set ``delivered_at`` instead of deleting) so
    the audit trail / 'why did this inbox appear in that digest' question
    remains answerable for a short retention window; a sweeper later
    prunes delivered rows older than N days.
    """
    if not keys:
        return 0
    ts = time.time()
    with db() as conn:
        cur = conn.executemany(
            "UPDATE digest_pending SET delivered_at = ? WHERE key = ? AND delivered_at IS NULL",
            [(ts, k) for k in keys],
        )
        return cur.rowcount or 0


def purge_delivered_digest_rows(older_than_seconds: float) -> int:
    """Housekeeping: drop delivered rows older than ``older_than_seconds``.

    Called from the sync loop on each tick so the table never grows
    unbounded on a long-running 100-mailbox deployment.
    """
    cutoff = time.time() - float(older_than_seconds)
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM digest_pending WHERE delivered_at IS NOT NULL AND delivered_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0


def digest_state_get(key: str, default: str = "") -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM digest_state WHERE key=?", (key,)
        ).fetchone()
    return default if row is None else str(row["value"])


def digest_state_set(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO digest_state(key, value, updated_at)
            VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """,
            (key, str(value), time.time()),
        )


def mailbox_first_sync_at(account_id: str) -> float | None:
    """Timestamp of ``account_id``'s first completed sync, or None.

    None means the next sync is the first one; its discoveries are
    seeded silently.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT first_sync_at FROM mailbox_sync_state WHERE account_id=?",
            (account_id,),
        ).fetchone()
    if row is None or row["first_sync_at"] is None:
        return None
    return float(row["first_sync_at"])


def record_first_sync(account_id: str, seeded_count: int) -> None:
    """Called once per account after its first successful classification pass.

    Idempotent: later syncs do NOT overwrite ``first_sync_at`` so the seeding
    behaviour is a one-shot thing per mailbox. ``initial_seeded_count`` is
    kept for operational visibility (and for the 'during initial sync' audit
    line in the first digest).
    """
    with db() as conn:
        conn.execute(
            """
            INSERT INTO mailbox_sync_state(account_id, first_sync_at, initial_seeded_count)
            VALUES(?,?,?)
            ON CONFLICT(account_id) DO NOTHING
            """,
            (account_id, time.time(), int(seeded_count)),
        )


# ---------------------------------------------------------------------------
# Sync-health incidents (October 2026 alert-flood fix).
#
# These helpers are the ONLY place that decides whether an operational
# sync failure is "new" / "materially changed" / "recovered" and therefore
# needs to be surfaced in the next consolidated Telegram ops alert. The
# sync worker calls ``record_health_failure`` / ``record_health_recovery``
# on every poll; most calls are no-ops from an alerting point of view
# (same account, same category, still failing) because ``pending_alert``
# is only set on a genuine state transition. See ``mailops.health``.
# ---------------------------------------------------------------------------


def get_incident(account_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM sync_incidents WHERE account_id=?", (account_id,)
        ).fetchone()
        return dict(row) if row else None


def record_health_failure(account_id: str, category: str, detail: str) -> dict[str, Any]:
    """Upsert the incident row for a failed sync. Returns a diagnostic dict:

        {"is_new": bool, "category_changed": bool, "consecutive_failures": int}

    ``is_new`` covers both a brand-new incident and the reopening of a
    previously-resolved one. ``category_changed`` is true only when the
    account was ALREADY in an open incident and the sanitized failure
    category changed (e.g. network_timeout -> auth) — this is the only
    case where an already-notified incident is flagged for another alert,
    satisfying 'if errors change materially, update incident status
    without flooding'.
    """
    now = time.time()
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM sync_incidents WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None or str(row["status"]) == "resolved":
            conn.execute(
                """
                INSERT INTO sync_incidents
                (account_id, status, category, detail, first_failure_at,
                 last_failure_at, consecutive_failures, last_check_at,
                 resolved_at, pending_alert, last_alert_sent_at)
                VALUES (?, 'open', ?, ?, ?, ?, 1, ?, NULL, 1, NULL)
                ON CONFLICT(account_id) DO UPDATE SET
                  status='open', category=excluded.category,
                  detail=excluded.detail, first_failure_at=excluded.first_failure_at,
                  last_failure_at=excluded.last_failure_at, consecutive_failures=1,
                  last_check_at=excluded.last_check_at, resolved_at=NULL,
                  pending_alert=1
                """,
                (account_id, category, detail[:500], now, now, now),
            )
            return {"is_new": True, "category_changed": True, "consecutive_failures": 1}
        changed = str(row["category"]) != category
        consecutive = int(row["consecutive_failures"] or 0) + 1
        pending = 1 if (changed or int(row["pending_alert"] or 0)) else 0
        conn.execute(
            """
            UPDATE sync_incidents
            SET category=?, detail=?, last_failure_at=?, consecutive_failures=?,
                last_check_at=?, pending_alert=?
            WHERE account_id=?
            """,
            (category, detail[:500], now, consecutive, now, pending, account_id),
        )
        return {
            "is_new": False,
            "category_changed": changed,
            "consecutive_failures": consecutive,
        }


def record_health_recovery(account_id: str) -> bool:
    """Mark an open incident resolved. Returns True iff it WAS open (i.e. a
    recovery actually happened and the consolidated alert should mention it).
    A healthy account with no incident row is a silent no-op."""
    now = time.time()
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM sync_incidents WHERE account_id=?", (account_id,)
        ).fetchone()
        if row is None or str(row["status"]) != "open":
            return False
        conn.execute(
            """
            UPDATE sync_incidents
            SET status='resolved', resolved_at=?, pending_alert=1, last_check_at=?
            WHERE account_id=?
            """,
            (now, now, account_id),
        )
        return True


def touch_incident_check(account_id: str, *, manual: bool = False) -> None:
    """Record that a health check ran for ``account_id`` (used for the
    manual Retry button's rate limit), without changing status. Upserts a
    placeholder row for a never-failed, healthy account so the rate limit
    still applies even when there is no open incident to track."""
    now = time.time()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO sync_incidents
            (account_id, status, category, detail, first_failure_at,
             last_failure_at, consecutive_failures, last_check_at,
             last_manual_check_at, resolved_at, pending_alert, last_alert_sent_at)
            VALUES (?, 'resolved', '', '', ?, ?, 0, ?, NULL, ?, 0, NULL)
            ON CONFLICT(account_id) DO NOTHING
            """,
            (account_id, now, now, now, now),
        )
        if manual:
            conn.execute(
                "UPDATE sync_incidents SET last_manual_check_at=? WHERE account_id=?",
                (now, account_id),
            )
        else:
            conn.execute(
                "UPDATE sync_incidents SET last_check_at=? WHERE account_id=?",
                (now, account_id),
            )


def list_open_incidents() -> list[dict[str, Any]]:
    """Currently-unhealthy accounts, oldest failure first (for the Telegram
    'unhealthy accounts' dashboard view)."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_incidents WHERE status='open' ORDER BY first_failure_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_pending_health_alerts() -> list[dict[str, Any]]:
    """Incidents (open or just-resolved) awaiting inclusion in the next
    consolidated ops alert."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM sync_incidents WHERE pending_alert=1 ORDER BY first_failure_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def clear_pending_health_alerts(account_ids: "list[str] | tuple[str, ...]") -> int:
    """Confirm a consolidated ops alert landed on Telegram: clear the pending
    flag for the covered accounts and drop fully-resolved rows (nothing left
    to show on the unhealthy-accounts dashboard for them)."""
    if not account_ids:
        return 0
    now = time.time()
    with db() as conn:
        conn.executemany(
            "UPDATE sync_incidents SET pending_alert=0, last_alert_sent_at=? WHERE account_id=?",
            [(now, a) for a in account_ids],
        )
        cur = conn.executemany(
            "DELETE FROM sync_incidents WHERE account_id=? AND status='resolved' AND pending_alert=0",
            [(a,) for a in account_ids],
        )
        return cur.rowcount or 0


def purge_stale_resolved_incidents(older_than_seconds: float) -> int:
    """Housekeeping: drop long-resolved, already-notified rows."""
    cutoff = time.time() - float(older_than_seconds)
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM sync_incidents WHERE status='resolved' AND pending_alert=0 "
            "AND resolved_at IS NOT NULL AND resolved_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0


def count_digest_pending(account_ids: "set[str] | list[str] | None" = None) -> dict[str, int]:
    """{account_id: undelivered-actionable-count}. For UI counters."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT account_id, COUNT(*) AS n
            FROM digest_pending
            WHERE delivered_at IS NULL AND seeded = 0
            GROUP BY account_id
            """
        ).fetchall()
    out = {str(r["account_id"]): int(r["n"]) for r in rows}
    if account_ids is not None:
        allowed = {str(a) for a in account_ids}
        out = {k: v for k, v in out.items() if k in allowed}
    return out
