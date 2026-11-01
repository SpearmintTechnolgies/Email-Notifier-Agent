"""Secret redaction for logs, exception text and diagnostics.

Telegram puts the bot token in every API URL (``/bot<id>:<secret>/method``),
so request logs and exception messages leak it unless scrubbed. This module is
the single place that knows what a secret looks like.
"""
from __future__ import annotations

import logging
import os
import re

# 123456789:AA... (bot id, colon, 30+ url-safe chars). Also matches inside URLs.
_TELEGRAM_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}")
_BOT_URL = re.compile(r"(/bot)\d{6,12}:[A-Za-z0-9_-]+")
_GOOGLE_ACCESS = re.compile(r"\bya29\.[A-Za-z0-9_\-.]+")
_GOOGLE_REFRESH = re.compile(r"\b1//[A-Za-z0-9_\-.]{20,}")
_GOOGLE_CLIENT_SECRET = re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]+")
_KV = re.compile(
    r"(?i)((?:client_secret|refresh_token|access_token|password|passwd|token)[\"']?\s*[=:]\s*[\"']?)[^&\s,\"'}]+"
)

# Env/.env keys whose literal values must never appear in output.
_SECRET_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "GOOGLE_CLIENT_SECRET",
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
)

REDACTED = "<redacted>"


def _literal_secrets() -> list[str]:
    values: list[str] = []
    for key in _SECRET_KEYS:
        v = os.environ.get(key, "").strip()
        if len(v) >= 8:
            values.append(v)
    return values


def redact_text(text: str, extra: tuple[str, ...] = ()) -> str:
    """Return ``text`` with every recognisable secret replaced."""
    if not text:
        return text
    for literal in (*_literal_secrets(), *extra):
        if literal and len(literal) >= 8:
            text = text.replace(literal, REDACTED)
    text = _BOT_URL.sub(r"\1" + REDACTED, text)
    text = _TELEGRAM_TOKEN.sub(REDACTED, text)
    text = _GOOGLE_ACCESS.sub(REDACTED, text)
    text = _GOOGLE_REFRESH.sub(REDACTED, text)
    text = _GOOGLE_CLIENT_SECRET.sub(REDACTED, text)
    text = _KV.sub(r"\1" + REDACTED, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formats the whole record (message, args AND traceback) then scrubs it,
    so secrets leaking via exception chains or %-args are caught too."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


class RedactingFilter(logging.Filter):
    """Belt and braces for handlers that bypass our formatter: scrub the
    message/args in place."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_text(record.getMessage())
            record.args = ()
        except Exception:  # noqa: BLE001 - never break logging
            pass
        return True


def install(root: logging.Logger | None = None) -> None:
    """Make every current handler of ``root`` redact, and keep the noisy
    HTTP client loggers (which print request URLs) at WARNING."""
    root = root or logging.getLogger()
    for handler in root.handlers:
        fmt = handler.formatter
        fmt_str = getattr(fmt, "_fmt", None) or "%(asctime)s %(levelname)s %(name)s: %(message)s"
        handler.setFormatter(RedactingFormatter(fmt_str))
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
