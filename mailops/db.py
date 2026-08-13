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
            CREATE TABLE IF NOT EXISTS sync_cursors (
              account_id TEXT PRIMARY KEY,
              last_uid INTEGER NOT NULL DEFAULT 0,
              updated_at REAL NOT NULL
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


def get_cursor(account_id: str) -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT last_uid FROM sync_cursors WHERE account_id=?", (account_id,)
        ).fetchone()
        return int(row["last_uid"]) if row else 0


def set_cursor(account_id: str, last_uid: int) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO sync_cursors(account_id, last_uid, updated_at)
            VALUES(?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET last_uid=excluded.last_uid, updated_at=excluded.updated_at
            """,
            (account_id, last_uid, time.time()),
        )


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


def pop_batch(older_than_seconds: float = 0) -> list[dict[str, Any]]:
    cutoff = time.time() - older_than_seconds
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_batch WHERE created_at <= ? ORDER BY created_at ASC",
            (cutoff,),
        ).fetchall()
        keys = [r["key"] for r in rows]
        if keys:
            conn.executemany("DELETE FROM pending_batch WHERE key=?", [(k,) for k in keys])
        return [dict(r) for r in rows]
