"""Verify Telegram Mini App ``initData`` server-side.

Telegram's Mini App sends an ``initData`` query string whose signature we
can verify with HMAC-SHA256 keyed by a hash of the bot token. If a sender
is not the Telegram client (or is tampering), the signature will not match.

This is the ONLY mechanism we trust to establish the Telegram user id of
whoever is loading the onboarding form. A raw user id POSTed by the browser
is NEVER used.

Algorithm (per https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)
----------------------------------------------------------------------------
1. Parse the URL-encoded ``initData`` string into (key, value) pairs.
2. Remove the ``hash`` field and remember its value.
3. Sort the remaining fields alphabetically by key.
4. Build ``data_check_string`` = "\n".join(f"{k}={v}" for k, v in sorted_items).
5. secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token).digest()
6. expected = HMAC_SHA256(key=secret_key, msg=data_check_string).hexdigest()
7. Compare expected to the ``hash`` field in constant time.

In addition to signature verification, this module enforces a freshness
window (default 10 minutes) based on the ``auth_date`` field, so a
leaked initData string cannot be replayed hours later.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

log = logging.getLogger("mailops.web.tgauth")

# Maximum age of an initData blob. Telegram officially suggests 24h for
# normal Mini Apps; we tighten to 10 minutes for a credential-handling flow.
INITDATA_MAX_AGE_S = 10 * 60


class TelegramAuthError(ValueError):
    """initData failed verification. Message is user-safe (no secrets)."""


@dataclass(frozen=True)
class TelegramInitData:
    user_id: int
    username: str
    first_name: str
    last_name: str
    auth_date: int
    query_id: str
    raw: str  # the exact initData string we verified (for defence-in-depth logging)


def _hmac_secret_key(bot_token: str) -> bytes:
    """``secret_key = HMAC_SHA256("WebAppData", bot_token).digest()``"""
    if not bot_token or ":" not in bot_token:
        raise TelegramAuthError("bot token not configured")
    return hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()


def _parse_initdata(init_data: str) -> tuple[dict[str, str], str]:
    """Return the field dict (with ``hash`` removed) and the hash value."""
    if not init_data or len(init_data) > 8 * 1024:
        raise TelegramAuthError("initData is empty or too large")
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    fields: dict[str, str] = {}
    for k, v in pairs:
        # First occurrence wins; a duplicated key is suspicious.
        if k in fields:
            raise TelegramAuthError("duplicate field in initData")
        fields[k] = v
    hash_value = fields.pop("hash", "")
    if not hash_value or len(hash_value) != 64:
        raise TelegramAuthError("initData hash is missing or malformed")
    return fields, hash_value


def _data_check_string(fields: dict[str, str]) -> str:
    return "\n".join(f"{k}={fields[k]}" for k in sorted(fields.keys()))


def verify_initdata(
    init_data: str,
    bot_token: str,
    *,
    now: float | None = None,
    max_age_s: int = INITDATA_MAX_AGE_S,
) -> TelegramInitData:
    """Verify ``init_data`` and return the authenticated user record.

    Raises ``TelegramAuthError`` for any failure. The error messages are
    deliberately generic (never say "hash was X, expected Y") so an
    attacker probing the endpoint learns nothing beyond "no".
    """
    now = time.time() if now is None else float(now)
    fields, provided_hash = _parse_initdata(init_data)
    data_check = _data_check_string(fields)
    secret_key = _hmac_secret_key(bot_token)
    expected = hmac.new(secret_key, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided_hash):
        log.info("initData signature mismatch (len=%d)", len(init_data))
        raise TelegramAuthError("initData could not be verified")

    # Freshness
    try:
        auth_date = int(fields.get("auth_date") or "0")
    except (TypeError, ValueError):
        raise TelegramAuthError("initData is missing a valid auth_date")
    if auth_date <= 0:
        raise TelegramAuthError("initData is missing a valid auth_date")
    age = now - auth_date
    if age < -60:
        # Clock skew tolerance: don't accept data minted far in the future.
        raise TelegramAuthError("initData auth_date is in the future")
    if age > max_age_s:
        raise TelegramAuthError("initData has expired; reopen the form from Telegram")

    user_blob = fields.get("user") or ""
    if not user_blob:
        raise TelegramAuthError("initData is missing the user block")
    try:
        user = json.loads(user_blob)
    except (TypeError, ValueError):
        raise TelegramAuthError("initData user block is not valid JSON")
    if not isinstance(user, dict):
        raise TelegramAuthError("initData user block is not an object")

    try:
        user_id = int(user.get("id"))
    except (TypeError, ValueError):
        raise TelegramAuthError("initData user id is missing or non-numeric")
    if user_id <= 0:
        raise TelegramAuthError("initData user id must be positive")
    # Mini Apps also include is_bot; refuse bot-origin initData just in case.
    if bool(user.get("is_bot")):
        raise TelegramAuthError("initData originated from a bot account")

    return TelegramInitData(
        user_id=user_id,
        username=str(user.get("username") or "")[:80],
        first_name=str(user.get("first_name") or "")[:80],
        last_name=str(user.get("last_name") or "")[:80],
        auth_date=auth_date,
        query_id=str(fields.get("query_id") or "")[:128],
        raw=init_data,
    )


def build_test_initdata(bot_token: str, *, user_id: int, auth_date: int | None = None,
                        username: str = "", extra: dict[str, Any] | None = None) -> str:
    """Produce a correctly-signed initData string for tests.

    Not for production code paths — only called from the test suite.
    """
    from urllib.parse import quote

    user = {"id": int(user_id), "is_bot": False, "first_name": "", "username": username}
    fields: dict[str, str] = {
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
        "query_id": "AAG_test_query",
        "user": json.dumps(user, separators=(",", ":"), sort_keys=True),
    }
    if extra:
        for k, v in extra.items():
            fields[k] = str(v)
    data_check = _data_check_string(fields)
    secret_key = _hmac_secret_key(bot_token)
    hash_value = hmac.new(secret_key, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    parts = [f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in fields.items()]
    parts.append(f"hash={hash_value}")
    return "&".join(parts)
