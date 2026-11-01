"""Short-lived single-use deep links from the group dashboard to a private
bot session.

The group is the main navigation hub, but anything that would leak email
bodies, drafts, credentials, or admin internals must happen in a private
chat. A deep link is a token tied to:

* a specific authorized user (whoever tapped the group button),
* a specific group chat_id (where the tap happened),
* a specific action name (``reply_pick``, ``admin_users`` …),
* an optional small JSON payload,
* a 5-minute expiry.

The token is embedded in a ``t.me/<bot>?start=mdl_<token>`` URL button.
When the user opens that private chat and sends ``/start mdl_<token>``,
the bot verifies the user id matches, consumes the token in a single
SQLite update (so a replay or shared link cannot be reused), and runs
the requested private screen.

A token is single-use. Expiry, user mismatch, and wrong-action attempts
raise ``LinkError``. Permission is also checked on every tap -
whitelisting a group never grants its members access to the private
flows these links lead to.
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .db import db, init_db

log = logging.getLogger("mailops.group_links")

TOKEN_TTL_S = 5 * 60
TOKEN_PREFIX = "mdl_"
MAX_PAYLOAD_LEN = 2000


class LinkError(ValueError):
    """A deep-link token could not be consumed (expired, used, wrong user)."""


@dataclass(frozen=True)
class LinkPayload:
    user_id: int
    chat_id: int
    action: str
    payload: dict[str, Any]
    created_at: float


def _ensure() -> None:
    init_db()


def issue(
    user_id: int,
    chat_id: int,
    action: str,
    payload: dict[str, Any] | None = None,
) -> str:
    """Return a fresh deep-link token.

    ``user_id`` must be positive (a Telegram user id). ``chat_id`` is the
    group where the user tapped - the private handler checks the deep link
    is being consumed by the same person who created it, not that it's
    opened from the same chat. ``action`` is a short identifier the
    private handler switches on.
    """
    if not isinstance(user_id, int) or user_id <= 0:
        raise LinkError("user_id must be a positive Telegram user id")
    if not action or len(action) > 64:
        raise LinkError("action name is empty or too long")
    body = json.dumps(payload or {}, separators=(",", ":"))
    if len(body) > MAX_PAYLOAD_LEN:
        raise LinkError("payload is too large for a deep link")
    _ensure()
    token = secrets.token_urlsafe(12)
    now = time.time()
    with db() as conn:
        conn.execute(
            "DELETE FROM group_action_tokens WHERE expires_at < ? OR used_at IS NOT NULL",
            (now - 60,),
        )
        conn.execute(
            "INSERT INTO group_action_tokens(token, user_id, chat_id, action, payload, "
            "created_at, expires_at) VALUES(?,?,?,?,?,?,?)",
            (token, int(user_id), int(chat_id), action, body, now, now + TOKEN_TTL_S),
        )
    log.info(
        "group deep-link issued user=%s chat=%s action=%s", user_id, chat_id, action
    )
    return token


def deep_link(bot_username: str, token: str) -> str:
    """``https://t.me/<bot>?start=mdl_<token>`` for the inline URL button."""
    name = (bot_username or "").lstrip("@")
    if not name:
        raise LinkError("bot username is required for deep links")
    return f"https://t.me/{name}?start={TOKEN_PREFIX}{token}"


def start_token(raw: str) -> str | None:
    """Extract the raw token from a ``/start`` payload, if it looks like ours."""
    raw = (raw or "").strip()
    if raw.startswith(TOKEN_PREFIX):
        return raw[len(TOKEN_PREFIX):]
    return None


def consume(user_id: int, token: str) -> LinkPayload:
    """Spend a deep-link token or raise ``LinkError``.

    Verifies the user id matches, the token has not been used, and has
    not expired. The ``UPDATE ... WHERE used_at IS NULL`` is atomic in
    SQLite, so two concurrent taps cannot both succeed.
    """
    if not token or len(token) > 64:
        raise LinkError("Unknown link")
    _ensure()
    now = time.time()
    with db() as conn:
        row = conn.execute(
            "SELECT token, user_id, chat_id, action, payload, created_at, expires_at, used_at "
            "FROM group_action_tokens WHERE token=?",
            (token,),
        ).fetchone()
        if row is None:
            raise LinkError("This link is unknown or has already expired.")
        if int(row["user_id"]) != int(user_id):
            raise LinkError("This link belongs to a different administrator.")
        if row["used_at"] is not None:
            raise LinkError("This link was already used.")
        if float(row["expires_at"]) < now:
            raise LinkError("This link expired. Open the group dashboard again.")
        cur = conn.execute(
            "UPDATE group_action_tokens SET used_at=? WHERE token=? AND used_at IS NULL",
            (now, token),
        )
        if cur.rowcount != 1:
            raise LinkError("This link was already used.")
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return LinkPayload(
            user_id=int(row["user_id"]),
            chat_id=int(row["chat_id"]),
            action=str(row["action"]),
            payload=payload,
            created_at=float(row["created_at"]),
        )


def revoke_user(user_id: int) -> int:
    """Invalidate every pending token for a user (used on role revocation)."""
    _ensure()
    with db() as conn:
        cur = conn.execute(
            "UPDATE group_action_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL",
            (time.time(), int(user_id)),
        )
        return int(cur.rowcount or 0)
