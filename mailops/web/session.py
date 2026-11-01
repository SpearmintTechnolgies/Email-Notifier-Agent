"""Short-lived server-side web sessions for the mobile onboarding form.

Why not reuse ``onboarding_session``?
-------------------------------------
``onboarding_session`` is the mailbox-level draft/validate/activate state
machine. It's the right place to persist the non-secret draft fields and
flip the account live. But it does not know anything about the web layer:
cookies, CSRF, bearer tokens, HTTP replay. This module is the thin HTTP
layer that:

* Opens a server-side web session only AFTER Telegram WebApp ``initData``
  has been verified AND the user re-passes the ``access.P_ADMIN`` check.
* Binds a random session id to that Telegram user id, with a short TTL
  (default 15 minutes) and a per-session CSRF token.
* Issues a corresponding ``onboarding_session`` row so the existing
  draft/validate code is reused unchanged.
* Marks the session single-use on the final activate step: a second POST
  to /api/activate with the same session is refused with 409.
* Scrubs itself on explicit cancel or TTL expiry.

Threat model
------------
* Replay: web sessions have server-side random ids, so a stolen cookie is
  only valid until TTL. CSRF-protected mutations require a header the
  attacker cannot read cross-origin (SameSite=Strict + a double-submit
  CSRF token).
* Session fixation: the session id is minted server-side after auth,
  never derived from a client-provided value.
* User swap: every write re-checks that the stored Telegram user id still
  has P_ADMIN; a demoted admin loses access immediately.
* Concurrent activation: the ``onboarding_session`` state machine already
  enforces single-step transitions on the DB layer.
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .. import access
from ..db import db, init_db

log = logging.getLogger("mailops.web.session")

SESSION_TTL_S = 15 * 60  # web session (cookie) lifetime
MAX_OPEN_PER_USER = 3     # concurrent web sessions per admin
MAX_LOGINS_PER_HOUR = 20  # rate-limit new web sessions

STATE_OPEN = "open"
STATE_ACTIVATED = "activated"
STATE_CANCELLED = "cancelled"
STATE_EXPIRED = "expired"
_TERMINAL = frozenset({STATE_ACTIVATED, STATE_CANCELLED, STATE_EXPIRED})


class WebSessionError(ValueError):
    """A web session action was refused (expired, replay, forged CSRF, ...)."""


@dataclass(frozen=True)
class WebSession:
    sid: str
    user_id: int
    csrf: str
    state: str
    onboarding_token: str
    created_at: float
    expires_at: float
    activated_at: float | None

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at


# --------------------------------------------------------------------------- storage


def _ensure_schema() -> None:
    init_db()
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS web_sessions (
              sid TEXT PRIMARY KEY,
              user_id INTEGER NOT NULL,
              csrf TEXT NOT NULL,
              state TEXT NOT NULL,
              onboarding_token TEXT NOT NULL,
              created_at REAL NOT NULL,
              expires_at REAL NOT NULL,
              activated_at REAL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS web_sessions_user ON web_sessions(user_id, state)")


def _row_to_session(row: sqlite3.Row | None) -> WebSession | None:
    if row is None:
        return None
    return WebSession(
        sid=str(row["sid"]),
        user_id=int(row["user_id"]),
        csrf=str(row["csrf"]),
        state=str(row["state"]),
        onboarding_token=str(row["onboarding_token"]),
        created_at=float(row["created_at"]),
        expires_at=float(row["expires_at"]),
        activated_at=None if row["activated_at"] is None else float(row["activated_at"]),
    )


def _mark_expired(conn, now: float) -> None:
    conn.execute(
        "UPDATE web_sessions SET state=? WHERE expires_at < ? AND state NOT IN (?,?,?)",
        (STATE_EXPIRED, now, STATE_ACTIVATED, STATE_CANCELLED, STATE_EXPIRED),
    )


def _rate_limit(conn, user_id: int, now: float) -> None:
    open_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM web_sessions WHERE user_id=? AND state=?",
        (user_id, STATE_OPEN),
    ).fetchone()
    if int(open_rows["n"]) >= MAX_OPEN_PER_USER:
        raise WebSessionError("too many open onboarding sessions; finish or cancel one")
    recent = conn.execute(
        "SELECT COUNT(*) AS n FROM web_sessions WHERE user_id=? AND created_at > ?",
        (user_id, now - 3600),
    ).fetchone()
    if int(recent["n"]) >= MAX_LOGINS_PER_HOUR:
        raise WebSessionError("too many onboarding attempts in the last hour")


def _require_admin(user_id: int) -> None:
    if not access.can(user_id, access.P_ADMIN):
        raise WebSessionError("user is not an authorized administrator")


# --------------------------------------------------------------------------- public API


def start_session(user_id: int) -> WebSession:
    """Open a new web session for an authenticated admin.

    Caller MUST have already verified Telegram initData for ``user_id``.
    """
    _ensure_schema()
    _require_admin(user_id)
    from .. import onboarding_session as os_mod

    onboarding = os_mod.start_session(user_id, mode=os_mod.MODE_DM)

    sid = secrets.token_urlsafe(24)
    csrf = secrets.token_urlsafe(24)
    now = time.time()
    with db() as conn:
        _mark_expired(conn, now)
        _rate_limit(conn, int(user_id), now)
        conn.execute(
            "INSERT INTO web_sessions(sid,user_id,csrf,state,onboarding_token,created_at,expires_at,activated_at) "
            "VALUES(?,?,?,?,?,?,?,NULL)",
            (sid, int(user_id), csrf, STATE_OPEN, onboarding.token, now, now + SESSION_TTL_S),
        )
    log.info("web session opened user=%s sid=%s", user_id, sid[:8])
    return WebSession(
        sid=sid, user_id=int(user_id), csrf=csrf, state=STATE_OPEN,
        onboarding_token=onboarding.token, created_at=now,
        expires_at=now + SESSION_TTL_S, activated_at=None,
    )


def load_session(sid: str) -> WebSession:
    """Return the web session for ``sid`` or raise ``WebSessionError``.

    Also re-verifies the stored user still has P_ADMIN; a demoted admin
    can therefore never advance a session they opened earlier.
    """
    if not sid or not isinstance(sid, str) or len(sid) > 128:
        raise WebSessionError("unknown session")
    _ensure_schema()
    now = time.time()
    with db() as conn:
        _mark_expired(conn, now)
        row = conn.execute("SELECT * FROM web_sessions WHERE sid=?", (sid,)).fetchone()
    session = _row_to_session(row)
    if session is None:
        raise WebSessionError("unknown session")
    if session.expired:
        raise WebSessionError("session expired")
    if session.is_terminal:
        raise WebSessionError("session is closed")
    try:
        _require_admin(session.user_id)
    except WebSessionError:
        # Admin revoked while the form was open.
        cancel_session(session.sid, reason="revoked")
        raise WebSessionError("your administrator role was revoked")
    return session


def require_csrf(session: WebSession, header_value: str | None) -> None:
    """Compare the header-provided CSRF token to the session's."""
    if not header_value or not isinstance(header_value, str):
        raise WebSessionError("missing CSRF token")
    import hmac

    if not hmac.compare_digest(session.csrf, header_value):
        raise WebSessionError("CSRF token mismatch")


def mark_activated(sid: str) -> None:
    """Flip a session to terminal 'activated'. Called after a successful
    final /api/activate so the same session cannot be re-used."""
    _ensure_schema()
    now = time.time()
    with db() as conn:
        cur = conn.execute(
            "UPDATE web_sessions SET state=?, activated_at=? WHERE sid=? AND state=?",
            (STATE_ACTIVATED, now, sid, STATE_OPEN),
        )
        if cur.rowcount != 1:
            raise WebSessionError("session is not open for activation")
    log.info("web session activated sid=%s", sid[:8])


def cancel_session(sid: str, *, reason: str = "cancel") -> None:
    _ensure_schema()
    with db() as conn:
        conn.execute(
            "UPDATE web_sessions SET state=? WHERE sid=? AND state=?",
            (STATE_CANCELLED, sid, STATE_OPEN),
        )
    log.info("web session cancelled sid=%s reason=%s", sid[:8], reason)


def purge_old(older_than_s: float = 24 * 3600) -> int:
    _ensure_schema()
    cutoff = time.time() - older_than_s
    with db() as conn:
        cur = conn.execute("DELETE FROM web_sessions WHERE created_at < ?", (cutoff,))
        return int(cur.rowcount or 0)
