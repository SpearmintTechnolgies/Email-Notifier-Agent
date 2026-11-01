"""Navigation state bound to the message that displays it.

Why this module exists (October 2026 live-group incident, phase 2)
------------------------------------------------------------------
Email lists were rendered from ``context.user_data["page_refs"]`` — a
per-USER dict — while the inline keyboard lives on a per-CHAT message. The
``gr:n:<N>`` / ``n:<N>`` Open buttons carry only a position, so "Open 1"
meant "the first ref in whatever that user's ``user_data`` holds right now".
Three real failures follow from that:

1. **Write-before-render race.** ``show_bucket_page`` stored the new refs
   *before* editing the message. Two taps in quick succession (the live log
   shows ``ab:coinography2|p1`` and ``ab:memecoinist|p1`` one second apart)
   interleave: the message can end up showing one mailbox's list while
   ``page_refs`` holds the other's. "Open 1" then resolves to an email from
   a mailbox the user is not looking at.

2. **Two owners, one keyboard.** In a group, owner B replacing the shared
   dashboard does not touch owner A's ``user_data``. Owner A taps the list
   B rendered and gets positions resolved against A's older list — or
   "That Open button is empty on this page" when A has no list at all.

3. **Stale keyboards.** A keyboard cached in a Telegram client outlives the
   list it was rendered for, and a bare position cannot be recognised as
   stale.

The fix: the refs for a list belong to the *message* that shows it, keyed by
``(chat_id, message_id)``, and are stored only AFTER the render succeeds, so
the stored refs always describe what is actually on screen. Each snapshot also
carries a short generation ``token`` which new keyboards embed
(``gr:n:<token>:<N>``), so a tap from a superseded keyboard is detected
instead of silently resolving to the wrong email.

Legacy positional callbacks (``gr:n:1``) stay supported: keyboards already
sitting in the live group must keep working across the restart that deploys
this. They resolve against the current snapshot for their own message, which
is still strictly better than per-user state, and the identity check in
``mailops.identity`` remains the final guarantee that the email shown is the
email that was listed.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("mailops.bot.nav")

#: Keep the most recent N (chat, message) snapshots. A busy group with a few
#: owners uses a handful; the bound just stops unbounded growth in a
#: long-lived poller.
_MAX_SNAPSHOTS = 512
#: Snapshots older than this are treated as stale (the list is re-fetched).
_TTL_SECONDS = 24 * 3600

_lock = threading.RLock()
_snapshots: "dict[tuple[int, int], Snapshot]" = {}


@dataclass
class Snapshot:
    """What a particular Telegram message is currently displaying."""

    chat_id: int
    message_id: int
    #: Short generation id embedded in this render's keyboard.
    token: str
    #: ``{"priority": int, "account_id": str|None, "page": int, ...}``
    view: dict[str, Any] = field(default_factory=dict)
    #: Refs for the rows shown on THIS page, in display order.
    page_refs: list[str] = field(default_factory=list)
    #: Refs for the whole result set (drives Previous/Next email).
    browse_refs: list[str] = field(default_factory=list)
    #: Full item dicts for the whole result set (avoids an IMAP re-fetch).
    items: list[dict[str, str]] = field(default_factory=list)
    #: Who rendered it (diagnostics only — never an authorization input).
    rendered_by: int | None = None
    created_at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    @property
    def expired(self) -> bool:
        return self.age_seconds > _TTL_SECONDS


def new_token() -> str:
    """A short, URL-safe generation id (callback data is capped at 64 bytes)."""
    return secrets.token_urlsafe(4)[:6]


def save(
    chat_id: int | None,
    message_id: int | None,
    *,
    token: str,
    view: dict[str, Any] | None = None,
    page_refs: list[str] | None = None,
    browse_refs: list[str] | None = None,
    items: list[dict[str, str]] | None = None,
    rendered_by: int | None = None,
) -> Snapshot | None:
    """Record what ``message_id`` now displays.

    Call this AFTER the edit/send succeeded, so stored refs can never
    describe a render that did not happen.
    """
    if chat_id is None or message_id is None:
        return None
    snap = Snapshot(
        chat_id=int(chat_id),
        message_id=int(message_id),
        token=token,
        view=dict(view or {}),
        page_refs=list(page_refs or []),
        browse_refs=list(browse_refs or []),
        items=list(items or []),
        rendered_by=rendered_by,
    )
    with _lock:
        _snapshots[(snap.chat_id, snap.message_id)] = snap
        if len(_snapshots) > _MAX_SNAPSHOTS:
            for key, _ in sorted(
                _snapshots.items(), key=lambda kv: kv[1].created_at
            )[: len(_snapshots) - _MAX_SNAPSHOTS]:
                _snapshots.pop(key, None)
    return snap


def load(chat_id: int | None, message_id: int | None) -> Snapshot | None:
    """The snapshot for this message, or None when unknown/expired."""
    if chat_id is None or message_id is None:
        return None
    with _lock:
        snap = _snapshots.get((int(chat_id), int(message_id)))
        if snap is not None and snap.expired:
            _snapshots.pop((int(chat_id), int(message_id)), None)
            return None
        return snap


def clear(chat_id: int | None = None, message_id: int | None = None) -> None:
    """Forget one snapshot, or all of them (tests / shutdown)."""
    with _lock:
        if chat_id is None or message_id is None:
            _snapshots.clear()
            return
        _snapshots.pop((int(chat_id), int(message_id)), None)


# ------------------------------------------------------------ callback parsing
@dataclass(frozen=True)
class Position:
    """A parsed positional callback (``gr:n:<token>:<N>`` or ``gr:n:<N>``)."""

    index: int
    token: str = ""

    @property
    def tokenless(self) -> bool:
        return not self.token


def parse_position(payload: str) -> Position | None:
    """Parse the payload after a positional prefix.

    ``"ab12cd:3"`` -> token ``ab12cd``, index 3 (new keyboards).
    ``"3"``        -> index 3, no token (keyboards rendered before this fix).
    """
    raw = str(payload or "").strip()
    if not raw:
        return None
    if ":" in raw:
        token, _, tail = raw.rpartition(":")
        if tail.isdigit():
            return Position(index=int(tail), token=token)
        return None
    if raw.isdigit():
        return Position(index=int(raw))
    return None


def position_data(prefix: str, token: str, index: int) -> str:
    """Build callback data for row ``index`` of the render named by ``token``."""
    if token:
        return f"{prefix}{token}:{index}"
    return f"{prefix}{index}"


# ------------------------------------------------------------------ resolution
#: Why a positional tap could not be resolved.
EMPTY = "empty"
STALE = "stale"
UNKNOWN = "unknown"

STALE_LIST_TEXT = (
    "That list was replaced by a newer one, so button {index} no longer "
    "points at the email you tapped. Nothing was opened. The current list "
    "is shown above — tap Refresh list and try again."
)
UNKNOWN_LIST_TEXT = (
    "This list is from an older session and its buttons can no longer be "
    "matched to emails. Nothing was opened. Open the dashboard again for a "
    "fresh list."
)
EMPTY_ROW_TEXT = "That Open button is empty on this page."


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving a positional tap to a message ref."""

    ref: str | None = None
    problem: str | None = None
    snapshot: Snapshot | None = None

    @property
    def ok(self) -> bool:
        return self.ref is not None

    def message(self, index: int) -> str:
        if self.problem == STALE:
            return STALE_LIST_TEXT.format(index=index)
        if self.problem == UNKNOWN:
            return UNKNOWN_LIST_TEXT
        return EMPTY_ROW_TEXT


def resolve(
    chat_id: int | None,
    message_id: int | None,
    payload: str,
    *,
    fallback_refs: list[str] | None = None,
) -> Resolution:
    """Turn a positional callback into the ref it was rendered for.

    Resolution order:

    1. the snapshot for THIS message (authoritative — it is what the user is
       looking at, no matter which owner rendered it);
    2. ``fallback_refs`` (the tapper's own ``user_data``) only for a legacy
       tokenless callback whose message predates this store, so keyboards
       already live in the group survive the deploy.

    A token that does not match the current snapshot is reported as ``STALE``
    rather than resolved positionally — that is the case where the old code
    could hand back an email from a different mailbox.
    """
    pos = parse_position(payload)
    if pos is None:
        return Resolution(problem=EMPTY)
    snap = load(chat_id, message_id)
    if snap is not None:
        if pos.token and pos.token != snap.token:
            log.info(
                "stale list tap chat=%s message=%s index=%s (keyboard token "
                "does not match current render)",
                chat_id,
                message_id,
                pos.index,
            )
            return Resolution(problem=STALE, snapshot=snap)
        refs = snap.page_refs
        if 1 <= pos.index <= len(refs):
            return Resolution(ref=refs[pos.index - 1], snapshot=snap)
        return Resolution(problem=EMPTY, snapshot=snap)
    if pos.token:
        # Keyboard names a render we no longer know about.
        return Resolution(problem=UNKNOWN)
    refs = list(fallback_refs or [])
    if 1 <= pos.index <= len(refs):
        log.info(
            "legacy positional tap chat=%s message=%s index=%s resolved from "
            "per-user state (keyboard predates message-scoped nav)",
            chat_id,
            message_id,
            pos.index,
        )
        return Resolution(ref=refs[pos.index - 1])
    return Resolution(problem=UNKNOWN if not refs else EMPTY)
