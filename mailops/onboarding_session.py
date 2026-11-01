"""Short-lived, single-use mailbox-onboarding sessions.

Design rules (why this module exists at all)
--------------------------------------------
* The CEO must be able to start onboarding a new business mailbox from
  Telegram without using Cursor, PowerShell or editing config files.
* Mailops is a local Windows app: there is NO web server, NO TLS termination
  and NO browser-session infrastructure. Standing up a self-signed HTTPS
  server on 127.0.0.1 to "secure" credential entry would be theatre.
* Passwords therefore never travel through Telegram messages, URLs,
  callback data, bot logs, browser storage or git. They are supplied either
  on the server's own interactive terminal (default, Mode CLI) or - when
  the operator explicitly opts in via ``MAILOPS_ALLOW_DM_CREDENTIALS=1`` -
  through a one-shot Telegram DM that this module *holds in RAM* for the
  duration of a single `webmail_onboarding.add_account` call and then wipes.
* This module is a thin ledger: it tracks who started which onboarding, with
  which non-secret fields (account id, email, hostnames, ports, usernames),
  for how long, and whether the session has been consumed. It never persists
  a password.

Threat model
------------
* An attacker with the Telegram bot token could post messages but would
  still need to be a seeded admin to open a session (``_require_admin``).
* An attacker with read access to ``data/ops.db`` sees non-secret fields
  only; even the DM-mode password is kept in process memory, not on disk.
* An attacker who steals a session token cannot use it: tokens are bound to
  a specific Telegram user id and spent in a single atomic SQLite UPDATE.
* Replay: every state transition rotates ``nonce``; the admin screens
  reject taps that carry a stale nonce.
* Rate limiting: an admin cannot have more than ``MAX_OPEN_PER_USER``
  draft sessions at once, and cannot open more than ``MAX_PER_HOUR`` new
  ones per rolling hour.

See ``tests/test_mailbox_onboarding.py`` for the authoritative behaviours.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from .db import db, init_db

log = logging.getLogger("mailops.onboarding")

SESSION_TTL_S = 20 * 60      # 20 minutes - a human filling a form
MAX_OPEN_PER_USER = 5
MAX_PER_HOUR = 10
MAX_FIELDS_LEN = 4000        # defence against oversize payloads

STATE_DRAFT = "draft"
STATE_AWAITING_CREDS = "awaiting_credentials"
STATE_VALIDATING = "validating"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_EXPIRED = "expired"

MODE_CLI = "cli"
MODE_DM = "dm"

_TERMINAL_STATES = frozenset({STATE_DONE, STATE_FAILED, STATE_CANCELLED, STATE_EXPIRED})


class SessionError(ValueError):
    """A session action was refused (expired, wrong user, replay, rate limit)."""


@dataclass
class OnboardingSession:
    token: str
    user_id: int
    mode: str
    state: str
    nonce: str
    fields: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0
    finished_at: float | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at


def _now() -> float:
    return time.time()


def _ensure() -> None:
    init_db()


def _row_to_session(row: Any) -> OnboardingSession:
    try:
        fields_data = json.loads(row["fields_json"] or "{}")
    except ValueError:
        fields_data = {}
    if not isinstance(fields_data, dict):
        fields_data = {}
    return OnboardingSession(
        token=str(row["token"]),
        user_id=int(row["user_id"]),
        mode=str(row["mode"]),
        state=str(row["state"]),
        nonce=str(row["nonce"]),
        fields=fields_data,
        last_error=str(row["last_error"] or ""),
        created_at=float(row["created_at"] or 0),
        updated_at=float(row["updated_at"] or 0),
        expires_at=float(row["expires_at"] or 0),
        finished_at=None if row["finished_at"] is None else float(row["finished_at"]),
    )


def _mark_stale_expired(conn, now: float) -> None:
    """Any non-terminal row past its TTL becomes 'expired' (one place does it)."""
    conn.execute(
        "UPDATE onboarding_sessions SET state=?, finished_at=? "
        "WHERE expires_at < ? AND state NOT IN (?,?,?,?)",
        (
            STATE_EXPIRED,
            now,
            now,
            STATE_DONE,
            STATE_FAILED,
            STATE_CANCELLED,
            STATE_EXPIRED,
        ),
    )


def _rate_limit(conn, user_id: int, now: float) -> None:
    open_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM onboarding_sessions "
        "WHERE user_id=? AND state IN (?,?,?)",
        (user_id, STATE_DRAFT, STATE_AWAITING_CREDS, STATE_VALIDATING),
    ).fetchone()
    if int(open_rows["n"]) >= MAX_OPEN_PER_USER:
        raise SessionError(
            f"You already have {open_rows['n']} open onboarding sessions. "
            "Finish or cancel one before starting another."
        )
    recent = conn.execute(
        "SELECT COUNT(*) AS n FROM onboarding_sessions "
        "WHERE user_id=? AND created_at > ?",
        (user_id, now - 3600),
    ).fetchone()
    if int(recent["n"]) >= MAX_PER_HOUR:
        raise SessionError(
            "Too many onboarding attempts in the last hour. Wait a bit and try again."
        )


def _bind_user(session: OnboardingSession, user_id: int) -> None:
    if int(session.user_id) != int(user_id):
        raise SessionError("That onboarding session belongs to a different administrator.")


def _bind_nonce(session: OnboardingSession, nonce: str) -> None:
    if not nonce or nonce != session.nonce:
        raise SessionError("This onboarding step expired. Reopen the Mailboxes menu.")


# --------------------------------------------------------------------------- public API


def start_session(user_id: int, *, mode: str = MODE_CLI) -> OnboardingSession:
    """Open a fresh onboarding draft for ``user_id``.

    Only authorized admins should reach this (callers must guard with
    ``access.can(uid, access.P_ADMIN)``). The token is per-user and
    unguessable; the nonce rotates on every subsequent write.
    """
    if int(user_id) <= 0:
        raise SessionError("Onboarding requires a Telegram user id.")
    if mode not in (MODE_CLI, MODE_DM):
        raise SessionError("Unknown onboarding mode.")
    _ensure()
    token = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(12)
    now = _now()
    with db() as conn:
        _mark_stale_expired(conn, now)
        _rate_limit(conn, int(user_id), now)
        conn.execute(
            "INSERT INTO onboarding_sessions"
            "(token,user_id,mode,state,fields_json,nonce,last_error,"
            "created_at,updated_at,expires_at,finished_at) "
            "VALUES(?,?,?,?,?,?,'',?,?,?,NULL)",
            (
                token,
                int(user_id),
                mode,
                STATE_DRAFT,
                "{}",
                nonce,
                now,
                now,
                now + SESSION_TTL_S,
            ),
        )
    log.info(
        "onboarding start user=%s mode=%s token=%s", user_id, mode, token[:8]
    )
    return get_session(token, user_id)


def get_session(token: str, user_id: int) -> OnboardingSession:
    """Fetch an active session or raise ``SessionError``.

    Lazily transitions over-TTL rows to ``expired`` so stale drafts cannot
    be used after the user looked away for too long.
    """
    if not token or len(token) > 64:
        raise SessionError("Unknown onboarding session.")
    _ensure()
    now = _now()
    with db() as conn:
        _mark_stale_expired(conn, now)
        row = conn.execute(
            "SELECT * FROM onboarding_sessions WHERE token=?",
            (token,),
        ).fetchone()
        if row is None:
            raise SessionError("Unknown or expired onboarding session.")
        session = _row_to_session(row)
    _bind_user(session, user_id)
    if session.state == STATE_EXPIRED:
        raise SessionError("This onboarding session expired.")
    return session


def _write(
    token: str,
    user_id: int,
    expected_state: tuple[str, ...],
    *,
    nonce: str | None,
    new_state: str | None = None,
    patch: dict[str, Any] | None = None,
    last_error: str | None = None,
    finish: bool = False,
) -> OnboardingSession:
    """Atomic state transition. One place: no caller touches the table.

    ``nonce`` is the nonce the caller believes is current. The DB row is
    only updated when it matches; the new row carries a fresh nonce so a
    stale callback button cannot be replayed.
    """
    _ensure()
    now = _now()
    with db() as conn:
        _mark_stale_expired(conn, now)
        row = conn.execute(
            "SELECT * FROM onboarding_sessions WHERE token=?", (token,)
        ).fetchone()
        if row is None:
            raise SessionError("Unknown or expired onboarding session.")
        session = _row_to_session(row)
        _bind_user(session, user_id)
        if session.is_terminal:
            raise SessionError("This onboarding session is closed.")
        if session.expired:
            conn.execute(
                "UPDATE onboarding_sessions SET state=?, finished_at=? WHERE token=?",
                (STATE_EXPIRED, now, token),
            )
            raise SessionError("This onboarding session expired.")
        if expected_state and session.state not in expected_state:
            raise SessionError(
                f"Session is in state '{session.state}', not {list(expected_state)}."
            )
        if nonce is not None:
            _bind_nonce(session, nonce)

        merged = dict(session.fields)
        if patch:
            merged.update({k: v for k, v in patch.items() if v is not None})
        fields_blob = json.dumps(merged, ensure_ascii=False)
        if len(fields_blob) > MAX_FIELDS_LEN:
            raise SessionError("Too much data for this onboarding session.")

        next_state = new_state or session.state
        next_nonce = secrets.token_urlsafe(12)
        finished_at = now if finish else session.finished_at
        last_err = session.last_error if last_error is None else last_error[:400]

        # Atomic single-step update: tokens cannot be double-spent even if
        # two callbacks arrive at the same time, because the WHERE binds
        # the OLD nonce.
        cur = conn.execute(
            "UPDATE onboarding_sessions SET state=?, fields_json=?, nonce=?, "
            "last_error=?, updated_at=?, finished_at=? WHERE token=? AND nonce=?",
            (
                next_state,
                fields_blob,
                next_nonce,
                last_err,
                now,
                finished_at,
                token,
                nonce if nonce is not None else session.nonce,
            ),
        )
        if cur.rowcount != 1:
            raise SessionError("This onboarding step was already used. Reopen Mailboxes.")
        row = conn.execute(
            "SELECT * FROM onboarding_sessions WHERE token=?", (token,)
        ).fetchone()
    return _row_to_session(row)


def set_fields(
    token: str, user_id: int, *, nonce: str, patch: dict[str, Any]
) -> OnboardingSession:
    """Store non-secret draft fields (account id, email, hosts, ports, user)."""
    if not isinstance(patch, dict):
        raise SessionError("Bad onboarding payload.")
    # Hard refusal: this field is never persisted.
    forbidden = {"password", "imap_password", "smtp_password", "pass", "token"}
    if any(k.lower() in forbidden for k in patch):
        raise SessionError("Passwords are never stored on an onboarding session.")
    return _write(
        token,
        user_id,
        (STATE_DRAFT,),
        nonce=nonce,
        patch=patch,
    )


def arm_for_credentials(
    token: str, user_id: int, *, nonce: str
) -> OnboardingSession:
    """Draft fields are complete: unlock the credential-capture step."""
    return _write(
        token,
        user_id,
        (STATE_DRAFT,),
        nonce=nonce,
        new_state=STATE_AWAITING_CREDS,
    )


def begin_validation(token: str, user_id: int) -> OnboardingSession:
    """Mark 'validating' at the moment add_account starts.

    No nonce is required here because this is only called by the server-side
    worker that owns the session; callers (CLI command, DM handler) have
    already proven admin membership through the Telegram ingress.
    """
    return _write(
        token,
        user_id,
        (STATE_AWAITING_CREDS,),
        nonce=None,
        new_state=STATE_VALIDATING,
    )


def finish_success(token: str, user_id: int) -> OnboardingSession:
    """IMAP+SMTP handshake passed and the account was registered."""
    return _write(
        token,
        user_id,
        (STATE_VALIDATING, STATE_AWAITING_CREDS),
        nonce=None,
        new_state=STATE_DONE,
        last_error="",
        finish=True,
    )


def finish_failure(
    token: str, user_id: int, reason: str
) -> OnboardingSession:
    """Validation failed or exception was raised. Everything is already
    rolled back by ``webmail_onboarding.add_account``; this just records why."""
    return _write(
        token,
        user_id,
        (STATE_VALIDATING, STATE_AWAITING_CREDS, STATE_DRAFT),
        nonce=None,
        new_state=STATE_FAILED,
        last_error=reason,
        finish=True,
    )


def cancel(token: str, user_id: int, *, nonce: str | None = None) -> OnboardingSession:
    """Admin aborted. No credentials are stored, so nothing else to wipe."""
    return _write(
        token,
        user_id,
        (STATE_DRAFT, STATE_AWAITING_CREDS),
        nonce=nonce,
        new_state=STATE_CANCELLED,
        finish=True,
    )


def list_sessions(user_id: int) -> list[OnboardingSession]:
    _ensure()
    now = _now()
    with db() as conn:
        _mark_stale_expired(conn, now)
        rows = conn.execute(
            "SELECT * FROM onboarding_sessions WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
            (int(user_id),),
        ).fetchall()
    return [_row_to_session(r) for r in rows]


def list_open_sessions(user_id: int) -> list[OnboardingSession]:
    """Open (non-terminal) sessions for ``user_id``, newest first."""
    return [s for s in list_sessions(user_id) if not s.is_terminal]


def pending_cli_session(user_id: int) -> OnboardingSession | None:
    """The DM / CLI worker only acts on one session at a time per admin."""
    for s in list_open_sessions(user_id):
        if s.state == STATE_AWAITING_CREDS:
            return s
    return None


def purge_terminal(older_than_s: float = 7 * 24 * 3600) -> int:
    """House-keeping: drop closed sessions older than a week."""
    _ensure()
    cutoff = _now() - older_than_s
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM onboarding_sessions WHERE finished_at IS NOT NULL AND finished_at < ?",
            (cutoff,),
        )
        return int(cur.rowcount or 0)
