"""One-time approvals for human-sent replies.

A Telegram "Confirm send" tap does not submit SMTP. It records a pending
approval of the exact draft the user was shown. Only the matching
"Send this reply" tap can claim that approval, and the claim is a single
SQLite update. A second tap, a Telegram retry, a restart, or a concurrent
handler finds the row already taken and does not send again.

An attempt whose SMTP result is not a clear acceptance or a clear refusal
stays unresolved. The same reply cannot be approved again.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from . import db as dbmod
from .identity import find_envelope, identity_from_envelope, recall, verify

_EMAIL = re.compile(r"[\w.+'-]+@[\w.-]+\.[A-Za-z]{2,}")
_MSGID = re.compile(r"<[^>\s]+>")
_BLOCKING = ("claimed", "accepted", "ambiguous")
_TOKEN = re.compile(r"^[0-9a-f]{32}$")

_SCHEMA = """
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
)
"""


@dataclass(frozen=True)
class ReplyPlan:
    account_id: str
    mailbox: str
    msg_id: str
    message_id: str
    recipient: str
    subject: str
    from_addr: str
    body: str
    body_sha256: str
    in_reply_to: str
    references: str


@dataclass(frozen=True)
class Claim:
    ok: bool
    row: dict[str, Any] | None
    reason: str


def body_sha256(body: str) -> str:
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


def reply_subject(subject: str) -> str:
    text = " ".join(str(subject or "").split())
    if text.lower().startswith("re:"):
        return text
    return f"Re: {text}" if text else "Re:"


def _addresses(value: Any) -> list[str]:
    found: list[str] = []
    if value is None:
        return found
    if isinstance(value, str):
        found.extend(m.group(0).lower() for m in _EMAIL.finditer(value))
        return found
    if isinstance(value, dict):
        for key in ("email", "addr", "address"):
            raw = value.get(key)
            if raw:
                found.extend(_addresses(raw))
                return found
        found.extend(m.group(0).lower() for m in _EMAIL.finditer(str(value)))
        return found
    if isinstance(value, list):
        for item in value:
            found.extend(_addresses(item))
    return found


def _unique(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def reply_recipient(env: dict[str, Any]) -> tuple[str, str]:
    """The single address a reply must use, or ('', reason)."""
    reply_to = _unique(_addresses(env.get("reply-to") if env.get("reply-to") is not None else env.get("reply_to")))
    if len(reply_to) > 1:
        return "", "the original message has more than one Reply-To address"
    if len(reply_to) == 1:
        return reply_to[0], ""
    senders = _unique(_addresses(env.get("from")))
    if len(senders) != 1:
        return "", "the original message has no single sender to reply to"
    return senders[0], ""


def threading_headers(env: dict[str, Any], message_id: str) -> tuple[str, str]:
    """In-Reply-To and References that a reply to this message must carry."""
    raw = str(env.get("references") or env.get("References") or "")
    ids = _MSGID.findall(raw)
    if message_id and message_id not in ids:
        ids.append(message_id)
    return message_id, " ".join(ids)


def plan_reply(
    him: Any,
    account_id: str,
    mailbox: str,
    msg_id: str,
    body: str,
    from_addr: str,
) -> tuple[ReplyPlan | None, str]:
    """Read-only check that this draft is still a reply to the recorded message."""
    verdict = verify(him, account_id, mailbox, msg_id)
    if not verdict.ok:
        return None, verdict.reason
    try:
        env = find_envelope(him, account_id, mailbox, msg_id)
    except Exception:  # noqa: BLE001 - cannot verify, do not send
        return None, "the mailbox could not be read to verify the reply"
    if env is None:
        return None, "the original message is no longer in that folder"
    ident = identity_from_envelope(account_id, mailbox, env)
    if ident.account_id != account_id:
        return None, "the message belongs to a different account"
    if not ident.message_id:
        return None, "the original message has no Message-ID, so threading cannot be verified"
    recorded = recall(account_id, mailbox, msg_id)
    if recorded is None:
        return None, "this message was never recorded, so it cannot be verified"
    if recorded.message_id and recorded.message_id != ident.message_id:
        return None, "the original Message-ID no longer matches"
    if recorded.sender and ident.sender and recorded.sender != ident.sender:
        return None, "the original sender no longer matches"
    recipient, why = reply_recipient(env)
    if why:
        return None, why
    reply_to = _unique(
        _addresses(env.get("reply-to") if env.get("reply-to") is not None else env.get("reply_to"))
    )
    if not reply_to and recorded.sender and recipient != recorded.sender:
        return None, "the reply recipient does not match the original sender"
    sender = (from_addr or "").strip()
    if "@" not in sender:
        return None, "the sending account has no From address"
    in_reply_to, references = threading_headers(env, ident.message_id)
    if not in_reply_to or in_reply_to not in references:
        return None, "threading headers could not be built from the original message"
    text = body if isinstance(body, str) else ""
    if not text.strip():
        return None, "the draft is empty"
    return (
        ReplyPlan(
            account_id=account_id,
            mailbox=mailbox,
            msg_id=str(msg_id),
            message_id=ident.message_id,
            recipient=recipient,
            subject=reply_subject(ident.subject),
            from_addr=sender,
            body=text,
            body_sha256=body_sha256(text),
            in_reply_to=in_reply_to,
            references=references,
        ),
        "",
    )


def format_confirmation(plan: ReplyPlan) -> str:
    return (
        "Final confirmation — nothing has been sent.\n\n"
        f"From: {plan.from_addr}\n"
        f"To: {plan.recipient}\n"
        f"Subject: {plan.subject}\n"
        f"Account: {plan.account_id}\n"
        f"In-Reply-To: {plan.in_reply_to}\n"
        f"References: {plan.references}\n\n"
        f"{plan.body}\n\n"
        "Send this reply once, or cancel. A second press will not send another copy."
    )


def plans_match(plan: ReplyPlan, row: dict[str, Any]) -> bool:
    return (
        plan.account_id == row["account_id"]
        and plan.mailbox == row["mailbox"]
        and plan.msg_id == str(row["msg_id"])
        and plan.message_id == row["message_id"]
        and plan.recipient == row["recipient"]
        and plan.subject == row["subject"]
        and plan.from_addr == row["from_addr"]
        and plan.body_sha256 == row["body_sha256"]
        and plan.in_reply_to == row["in_reply_to"]
        and plan.references == row["references_header"]
    )


def classify_smtp(code: int, stdout: str, stderr: str) -> str:
    """accepted, rejected (nothing was accepted), or ambiguous (do not retry)."""
    text = f"{stdout or ''}\n{stderr or ''}".lower()
    local_block = "test-mode" in text or "not sent" in text
    success = "successfully sent" in text or "successfully saved and sent" in text
    if local_block:
        return "rejected"
    if code == 0 and success:
        # A success phrase mixed with a refusal is not a clear acceptance.
        if any(bad in text for bad in ("550", "552", "553", "554", "535")):
            return "ambiguous"
        return "accepted"
    if code == 0 or code == 124 or not text.strip():
        return "ambiguous"
    if success:
        return "ambiguous"
    clear_fail = (
        "policy:" in text
        or "auth failed" in text
        or "authentication failed" in text
        or "535" in text
        or "connection refused" in text
        or "name or service not known" in text
        or "no such host" in text
        or "himalaya not found" in text
        or "certificate" in text
        or "550" in text
        or "552" in text
        or "553" in text
        or "554" in text
    )
    if clear_fail:
        return "rejected"
    return "ambiguous"


def ensure_schema() -> None:
    with dbmod.db() as conn:
        conn.execute(_SCHEMA)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(dbmod.OPS_DB), isolation_level=None, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _blocking(conn: sqlite3.Connection, plan: ReplyPlan, *, except_token: str = "") -> str:
    inflight = conn.execute(
        """
        SELECT token FROM send_approvals
        WHERE account_id=? AND message_id=? AND status='claimed' AND token!=?
        """,
        (plan.account_id, plan.message_id, except_token),
    ).fetchone()
    if inflight:
        return "in_flight"
    same = conn.execute(
        """
        SELECT token FROM send_approvals
        WHERE account_id=? AND message_id=? AND recipient=? AND body_sha256=?
          AND status IN ('claimed', 'accepted', 'ambiguous') AND token!=?
        """,
        (plan.account_id, plan.message_id, plan.recipient, plan.body_sha256, except_token),
    ).fetchone()
    if same:
        return "already_used"
    return ""


def create_pending(user_id: int, plan: ReplyPlan) -> tuple[str | None, str]:
    """Insert one pending approval, or reuse the identical unused one.

    Returns (token, '') or (None, reason). reason is 'blocked' when this exact
    reply was already claimed, accepted, or left ambiguous.
    """
    ensure_schema()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        why = _blocking(conn, plan)
        if why:
            conn.execute("COMMIT")
            return None, "blocked"
        existing = conn.execute(
            """
            SELECT token FROM send_approvals
            WHERE user_id=? AND account_id=? AND mailbox=? AND msg_id=?
              AND message_id=? AND recipient=? AND subject=? AND from_addr=?
              AND body_sha256=? AND in_reply_to=? AND references_header=?
              AND status='pending'
            ORDER BY created_at DESC LIMIT 1
            """,
            (
                user_id,
                plan.account_id,
                plan.mailbox,
                plan.msg_id,
                plan.message_id,
                plan.recipient,
                plan.subject,
                plan.from_addr,
                plan.body_sha256,
                plan.in_reply_to,
                plan.references,
            ),
        ).fetchone()
        if existing:
            conn.execute("COMMIT")
            return str(existing["token"]), ""
        token = secrets.token_hex(16)
        conn.execute(
            """
            INSERT INTO send_approvals (
              token, user_id, account_id, mailbox, msg_id, message_id, recipient,
              subject, from_addr, body_sha256, in_reply_to, references_header,
              status, created_at, smtp_note
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?, '')
            """,
            (
                token,
                user_id,
                plan.account_id,
                plan.mailbox,
                plan.msg_id,
                plan.message_id,
                plan.recipient,
                plan.subject,
                plan.from_addr,
                plan.body_sha256,
                plan.in_reply_to,
                plan.references,
                time.time(),
            ),
        )
        conn.execute("COMMIT")
        return token, ""
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def claim_approval(token: str, user_id: int) -> Claim:
    """Atomically take a pending approval. Only one caller can succeed."""
    if not _TOKEN.fullmatch(token or ""):
        return Claim(False, None, "invalid")
    ensure_schema()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM send_approvals WHERE token=?", (token,)).fetchone()
        if row is None or int(row["user_id"]) != int(user_id) or row["status"] != "pending":
            conn.execute("COMMIT")
            return Claim(False, None, "used")
        plan = ReplyPlan(
            account_id=row["account_id"],
            mailbox=row["mailbox"],
            msg_id=str(row["msg_id"]),
            message_id=row["message_id"],
            recipient=row["recipient"],
            subject=row["subject"],
            from_addr=row["from_addr"],
            body="",
            body_sha256=row["body_sha256"],
            in_reply_to=row["in_reply_to"],
            references=row["references_header"],
        )
        why = _blocking(conn, plan, except_token=token)
        if why:
            conn.execute(
                """
                UPDATE send_approvals
                SET status='rejected', finished_at=?, smtp_note='duplicate_blocked'
                WHERE token=? AND status='pending'
                """,
                (time.time(), token),
            )
            conn.execute("COMMIT")
            return Claim(False, None, "blocked")
        cur = conn.execute(
            """
            UPDATE send_approvals
            SET status='claimed', claimed_at=?
            WHERE token=? AND user_id=? AND status='pending'
            """,
            (time.time(), token, int(user_id)),
        )
        if cur.rowcount != 1:
            conn.execute("COMMIT")
            return Claim(False, None, "used")
        taken = conn.execute("SELECT * FROM send_approvals WHERE token=?", (token,)).fetchone()
        conn.execute("COMMIT")
        return Claim(True, dict(taken), "claimed")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def finish_approval(token: str, status: str, code: int | None, note: str) -> bool:
    """Record the outcome of a claimed attempt. Never stores message text."""
    if status not in {"accepted", "rejected", "ambiguous"}:
        raise ValueError("unsupported send status")
    safe_note = (note or "")[:80]
    ensure_schema()
    with dbmod.db() as conn:
        cur = conn.execute(
            """
            UPDATE send_approvals
            SET status=?, finished_at=?, smtp_code=?, smtp_note=?
            WHERE token=? AND status='claimed'
            """,
            (status, time.time(), code, safe_note, token),
        )
        return cur.rowcount == 1


def cancel_approval(token: str, user_id: int) -> bool:
    if not _TOKEN.fullmatch(token or ""):
        return False
    ensure_schema()
    with dbmod.db() as conn:
        cur = conn.execute(
            """
            UPDATE send_approvals
            SET status='cancelled', finished_at=?, smtp_note='cancelled'
            WHERE token=? AND user_id=? AND status='pending'
            """,
            (time.time(), token, int(user_id)),
        )
        return cur.rowcount == 1
