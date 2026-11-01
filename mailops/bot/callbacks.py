"""Prompt, exactly-once Telegram callback acknowledgment and error triage.

Why this module exists (October 2026 live-group incident)
--------------------------------------------------------
The live log showed taps that looked "inconsistent" to users::

    20:05:51 callback:ab:coinography2|p1 received
    20:05:52 Telegram error: Query is too old and response timeout expired
             or query id is invalid
    20:06:03 callback:gr:n:1 received
    20:06:25 refused open of memecoinist INBOX/61   <-- 22s later
    20:06:29 another gr:n:1 -> callback query expired/invalid

Three distinct defects produced that output, and all three are addressed
here:

1. ``answerCallbackQuery`` was attempted more than once for the same query.
   ``mailops.bot.app._on_callback`` answers every authorized callback up
   front, and refusal helpers (``_notice``) then answered the *same* query
   again to show an alert. Telegram rejects the second call with
   "Query is too old and response timeout expired or query id is invalid",
   which is indistinguishable from a genuine expiry. The second alert was
   also never displayed, so the user saw a button that "did nothing".
   ``Ack`` makes the answer exactly-once *process-wide* (keyed by query id,
   so the app/mailboxes/admin layers cannot double-answer each other) and
   routes any later refusal text to a chat message instead.

2. ``telegram.error.BadRequest`` is a SUBCLASS of ``telegram.error.NetworkError``
   in python-telegram-bot (confirmed on 22.6). The error handler tested
   ``isinstance(err, NetworkError)`` first, so every BadRequest — expired
   query, missing reply target, un-editable message — was logged as
   "Telegram network error". That hid a pure application bug behind an
   apparent connectivity problem. ``classify`` separates them.

3. A callback that is acknowledged only *after* IMAP/rendering work expires
   before it is ever answered. ``Ack.ok`` is therefore called before any
   blocking work, and the latency of every callback is logged so a slow
   route is visible without reading email content.

Nothing here logs subjects, senders, bodies, tokens or credentials: only
the callback *route family*, timings, and the error category.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("mailops.bot.callbacks")

# --------------------------------------------------------------------- outcome
#: Telegram refused the answer because the query id is expired or already used.
EXPIRED = "callback_expired"
#: The edit was a no-op (same text + same keyboard re-tapped).
NOT_MODIFIED = "message_not_modified"
#: ``reply_to_message_id`` pointed at a message that no longer exists.
REPLY_TARGET_MISSING = "reply_target_missing"
#: The message we tried to edit is gone or too old to edit.
MESSAGE_MISSING = "message_missing"
#: Telegram asked us to slow down (flood control).
RATE_LIMITED = "rate_limited"
#: Bot was blocked / kicked / lacks rights in the chat.
FORBIDDEN = "forbidden"
#: Request timed out.
TIMED_OUT = "timed_out"
#: Genuine transport failure.
NETWORK = "network"
#: Some other malformed request.
BAD_REQUEST = "bad_request"
#: Any other Telegram error.
TELEGRAM = "telegram"
#: Not a Telegram error at all.
INTERNAL = "internal"
#: No error.
OK = "ok"

#: Categories that mean "the callback query is unusable" — the user's tap can
#: still be served by editing or posting a message, but no alert will show.
UNUSABLE_QUERY = frozenset({EXPIRED, TIMED_OUT, NETWORK})

#: Categories that are expected background noise rather than a defect. These
#: are logged at INFO/DEBUG so a real problem stands out in the log.
BENIGN = frozenset({NOT_MODIFIED, OK})


def classify(exc: BaseException | None) -> str:
    """Map an exception to one of the categories above.

    Ordering matters: ``BadRequest``, ``TimedOut``, ``RetryAfter`` and
    ``Forbidden`` all inherit from ``NetworkError`` or ``TelegramError`` in
    python-telegram-bot, so the most specific class is tested first and a
    bare ``NetworkError`` is only reported as ``NETWORK`` when it really is
    one.
    """
    if exc is None:
        return OK
    try:
        from telegram.error import (
            BadRequest,
            Forbidden,
            NetworkError,
            RetryAfter,
            TelegramError,
            TimedOut,
        )
    except Exception:  # noqa: BLE001 - telegram not importable (unit tests)
        return INTERNAL

    text = str(exc).lower()
    if isinstance(exc, RetryAfter):
        return RATE_LIMITED
    if isinstance(exc, Forbidden):
        return FORBIDDEN
    if isinstance(exc, BadRequest):
        # Telegram returns one opaque string for "expired" and "already
        # answered"; both mean the query id can no longer be used.
        if "query is too old" in text or "query id is invalid" in text:
            return EXPIRED
        if "not modified" in text:
            return NOT_MODIFIED
        if "message to be replied not found" in text or "reply message not found" in text:
            return REPLY_TARGET_MISSING
        if (
            "message to edit not found" in text
            or "message can't be edited" in text
            or "message to delete not found" in text
            or "message identifier is not specified" in text
        ):
            return MESSAGE_MISSING
        if "too many requests" in text or "flood" in text:
            return RATE_LIMITED
        return BAD_REQUEST
    if isinstance(exc, TimedOut):
        return TIMED_OUT
    if isinstance(exc, NetworkError):
        # Real transport problem: BadRequest/TimedOut were excluded above.
        return NETWORK
    if isinstance(exc, TelegramError):
        if "query is too old" in text or "query id is invalid" in text:
            return EXPIRED
        return TELEGRAM
    return INTERNAL


def is_expired(exc: BaseException | None) -> bool:
    """True when Telegram says the callback query can no longer be answered."""
    return classify(exc) == EXPIRED


# ------------------------------------------------------- route family (no PII)
#: Longest-prefix-first so ``gr:n:`` is reported instead of ``gr:``.
_ROUTES = (
    "gr:aidraft:", "gr:writeme:", "gr:restricted:", "gr:reply:", "gr:send:",
    "gr:back", "gr:search", "gr:bp:", "gr:go:", "gr:nogo:", "gr:b:", "gr:n:",
    "gr:o:", "gr:",
    "mb:arm:", "mb:dm_prompt:", "mb:cancel:", "mb:view:", "mb:audit:",
    "mb:add:", "mb:", "adm:", "bulk:",
    "set:kb:note:", "set:kb:view:", "set:purpose:", "set:rule:", "set:",
    "health:", "dg:open:", "dg:pick:", "dg:", "gdl:", "nav:", "ab:", "b:p",
    "pj:", "pg:", "page:", "dash", "backpage", "n:", "g:all", "noop",
)


def route_of(data: str | None) -> str:
    """The callback's route family, with its payload stripped.

    ``"ab:memecoinist|p1"`` -> ``"ab:"``. Used for logging: it identifies the
    code path without recording which mailbox or message a user looked at.
    """
    raw = str(data or "")
    for prefix in _ROUTES:
        if raw.startswith(prefix):
            return prefix
    return raw.split(":", 1)[0][:16] or "(empty)"


# --------------------------------------------------- exactly-once answer state
_MAX_TRACKED = 8192
_answered: "OrderedDict[str, float]" = OrderedDict()
_state_lock = threading.Lock()


def _claim(query_id: str) -> bool:
    """Atomically claim the single allowed answer for ``query_id``.

    True for the first caller, False for every later one. Bounded LRU so a
    long-running poller cannot grow this without limit.
    """
    if not query_id:
        return True
    with _state_lock:
        if query_id in _answered:
            return False
        _answered[query_id] = time.monotonic()
        while len(_answered) > _MAX_TRACKED:
            _answered.popitem(last=False)
        return True


def _already_claimed(query_id: str) -> bool:
    if not query_id:
        return False
    with _state_lock:
        return query_id in _answered


def reset_state() -> None:
    """Clear the answered-query ledger (tests only)."""
    with _state_lock:
        _answered.clear()


@dataclass
class AckResult:
    """Outcome of one acknowledgment attempt."""

    #: True when Telegram accepted an answer for this query (now or earlier).
    acknowledged: bool
    #: True when *this* call performed the network answer.
    sent: bool
    #: Error category, or ``OK``.
    category: str = OK
    #: True when an alert popup was requested and actually delivered.
    alert_shown: bool = False

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.acknowledged


#: How long a handler may run before the query is acknowledged on its behalf.
#:
#: Telegram only accepts ONE ``answerCallbackQuery`` per query, and only an
#: answer that carries text can show a popup. So "ack immediately" and "show
#: the user an alert explaining a refusal" are mutually exclusive — and the
#: incident needed both: taps must never expire unacknowledged, yet a denied
#: tap must still say why.
#:
#: The resolution is this grace window. A handler that refuses quickly (a
#: permission check, a missing draft, a bad payload — all in-memory or a
#: single SQLite read) answers WITH its alert well inside the window. A
#: handler that reaches IMAP, the cache or the network cannot finish in time,
#: so the watchdog answers for it and the button stops spinning long before
#: Telegram would invalidate the query; its later refusal text is posted as a
#: chat message instead (which is also more durable than a popup).
WATCHDOG_SECONDS = 0.6


class Ack:
    """Exactly-once acknowledgment for a single callback query.

    Usage inside a handler::

        ack = Ack(update.callback_query, route="gr:n:")
        ack.arm()                                  # watchdog starts
        if denied:
            await ack.alert("Not allowed here.")   # fast: popup still shows
            return
        ...slow IMAP work...                       # watchdog acks meanwhile
        await ack.notice("That email moved.")      # posts a message instead

    The first ``ok``/``alert``/watchdog answer wins; every later call degrades
    to posting a chat message, so the user always gets feedback and Telegram
    never sees a duplicate ``answerCallbackQuery``.
    """

    __slots__ = (
        "_q", "route", "_started", "_acked_at", "_outcome", "_logged",
        "_watchdog", "_watchdog_fired",
    )

    def __init__(self, q: Any, route: str = "") -> None:
        self._q = q
        self.route = route or route_of(getattr(q, "data", None))
        self._started = time.monotonic()
        self._acked_at: float | None = None
        self._outcome: str = OK
        self._logged = False
        self._watchdog: Any = None
        self._watchdog_fired = False

    # ------------------------------------------------------------- properties
    @property
    def query_id(self) -> str:
        return str(getattr(self._q, "id", "") or "")

    @property
    def answered(self) -> bool:
        """True once any layer has claimed the answer for this query."""
        return self._acked_at is not None or _already_claimed(self.query_id)

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    # -------------------------------------------------------------- watchdog
    def arm(self, delay: float = WATCHDOG_SECONDS) -> None:
        """Guarantee this query is answered even if the handler runs long.

        Without this, a handler that blocks on IMAP for 20+ seconds leaves the
        query unanswered until Telegram rejects it — the user sees a button
        that spins and then does nothing (the ``gr:n:1`` / ``ab:…|p1`` reports).
        """
        if self._q is None or self._watchdog is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync unit test): nothing to schedule
        self._watchdog = loop.create_task(self._watchdog_run(delay))

    async def _watchdog_run(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self.answered:
            return
        self._watchdog_fired = True
        log.debug(
            "callback route=%s still unanswered after %.0fms; acknowledging "
            "on the handler's behalf",
            self.route,
            delay * 1000,
        )
        await self._answer(None, show_alert=False)

    def disarm(self) -> None:
        """Stop the watchdog (handler finished)."""
        task = self._watchdog
        self._watchdog = None
        if task is not None and not task.done():
            task.cancel()

    # ---------------------------------------------------------------- answers
    async def ok(self) -> AckResult:
        """Acknowledge with no popup."""
        return await self._answer(None, show_alert=False)

    async def alert(self, text: str) -> AckResult:
        """Acknowledge with a popup alert.

        If the query was already answered (watchdog, or an outer layer) the
        popup cannot be shown, so the text is posted into the chat instead —
        the user still finds out why the tap was refused.
        """
        result = await self._answer(text, show_alert=True)
        if not result.alert_shown:
            await self.post(text)
        return result

    async def notice(self, text: str) -> AckResult:
        """Deliver refusal/status text, alerting only if still possible.

        Never issues a second ``answerCallbackQuery``.
        """
        if self.answered:
            await self.post(text)
            return AckResult(acknowledged=True, sent=False, category=self._outcome)
        return await self.alert(text)

    async def _answer(self, text: str | None, *, show_alert: bool) -> AckResult:
        if self._q is None:
            return AckResult(acknowledged=False, sent=False, category=INTERNAL)
        qid = self.query_id
        if not _claim(qid):
            # Another layer already answered this exact query. Answering again
            # is what produced the misleading "Query is too old" warnings.
            log.debug(
                "callback route=%s already acknowledged; skipping duplicate answer",
                self.route,
            )
            return AckResult(acknowledged=True, sent=False, category=self._outcome)
        try:
            if text:
                await self._q.answer(text[:200], show_alert=show_alert)
            else:
                await self._q.answer()
        except Exception as exc:  # noqa: BLE001 - never let the ack kill a tap
            category = classify(exc)
            self._outcome = category
            # A failed answer must not block a retry by a later layer that may
            # legitimately want to try again (e.g. transient timeout).
            if category in UNUSABLE_QUERY:
                # The query is dead; keep it claimed so nobody retries it.
                self._acked_at = time.monotonic()
            else:
                with _state_lock:
                    _answered.pop(qid, None)
            log.log(
                logging.INFO if category in BENIGN else logging.WARNING,
                "callback ack failed route=%s category=%s after=%sms",
                self.route,
                category,
                self.elapsed_ms,
            )
            return AckResult(acknowledged=False, sent=True, category=category)
        self._acked_at = time.monotonic()
        return AckResult(
            acknowledged=True,
            sent=True,
            category=OK,
            alert_shown=bool(text) and show_alert,
        )

    # ----------------------------------------------------------------- output
    async def post(self, text: str) -> bool:
        """Post ``text`` as a message in the callback's chat.

        Falls back to a standalone (non-reply) message when the original
        message was deleted — see ``safe_reply``.
        """
        msg = getattr(self._q, "message", None)
        if msg is None:
            return False
        return await safe_reply(msg, text) is not None

    # ------------------------------------------------------------------- logs
    def done(self, outcome: str = "handled", *, category: str | None = None) -> None:
        """Record one line per callback: route, timings, outcome, category.

        ``ack_ms`` is how long the user waited for the button to stop
        spinning; ``total_ms`` is the full handler cost. A large gap between
        them is exactly the "slow handler" signature from the incident.
        """
        if self._logged:
            return
        self._logged = True
        cat = category or self._outcome
        ack_ms = (
            int((self._acked_at - self._started) * 1000)
            if self._acked_at is not None
            else -1
        )
        level = logging.WARNING if cat not in BENIGN and cat != OK else logging.INFO
        log.log(
            level,
            "callback route=%s outcome=%s category=%s ack_ms=%s total_ms=%s",
            self.route,
            outcome,
            cat,
            ack_ms,
            self.elapsed_ms,
        )


# ------------------------------------------------------- message-level helpers
async def safe_reply(message: Any, text: str, markup: Any = None) -> int | None:
    """Reply to ``message``, falling back to a standalone chat message.

    Returns the id of the message that was posted, or None if nothing could
    be delivered.

    Phase 4 of the incident: "Message to be replied not found". The user had
    deleted (or Telegram had expired) the message a handler was replying to,
    so the whole response was lost. The fallback keeps the SAME chat — never
    a different one — so authorization and content-exposure decisions already
    made for this chat still hold, and it is only attempted once so the user
    cannot receive the text twice.
    """
    if message is None:
        return None
    try:
        sent = await message.reply_text(text, reply_markup=markup)
        return getattr(sent, "message_id", None)
    except Exception as exc:  # noqa: BLE001
        category = classify(exc)
        if category not in (REPLY_TARGET_MISSING, MESSAGE_MISSING):
            log.warning("reply failed category=%s", category)
            return None
    # The quoted message is gone. Post into the same chat without quoting.
    chat = getattr(message, "chat", None)
    bot = getattr(message, "get_bot", None)
    chat_id = getattr(chat, "id", None)
    if chat_id is None or bot is None:
        log.warning("reply target missing and no chat to fall back to")
        return None
    try:
        sent = await bot().send_message(
            chat_id=chat_id, text=text, reply_markup=markup
        )
        log.info("reply target missing; posted standalone message instead")
        return getattr(sent, "message_id", None)
    except Exception as exc:  # noqa: BLE001
        log.warning("standalone fallback failed category=%s", classify(exc))
        return None


async def safe_edit(q: Any, text: str, markup: Any = None) -> str:
    """Edit the callback's message; report the outcome category.

    Returns ``OK`` on success, ``NOT_MODIFIED`` when the content was
    identical (a shared group dashboard re-tapped — not an error), or the
    category of whatever went wrong. Callers use ``MESSAGE_MISSING`` to
    decide to post a fresh message instead of silently losing the response.
    """
    if q is None:
        return INTERNAL
    try:
        await q.edit_message_text(text, reply_markup=markup)
        return OK
    except Exception as exc:  # noqa: BLE001
        category = classify(exc)
        if category != NOT_MODIFIED:
            log.info("callback edit failed category=%s", category)
        return category
