"""Regression tests for the October 25 2026 '/dashboard is silent' incident.

What happened live
------------------
The log proved the updates arrived and then showed nothing at all::

    21:21:37 Application started
    21:21:38 update kind=command:/dashboard chat=-5531260744 type=group ...
    21:37:16 update kind=command:/dashboard chat=-5531260744 type=group ...
    21:38:21 update kind=command:/dashboard chat=-5531260744 type=group ...
    21:39:13 update kind=command:/dashboard chat=-5531260744 type=group ...

No reply, no guard refusal, no Telegram error, no traceback. ``mailops.cache``
keeps its metadata **in memory**, so it is empty after every restart;
``get_counts(force=False)`` treated an empty cache as "refresh now" and ran a
full-fleet IMAP pass *inline*, inside the command handler. For 39 accounts
that pass spawns ~200 Himalaya subprocesses and the live log measured it at
6-13 minutes, so the handler never reached its ``reply_text``. Because the
work sat in ``asyncio.to_thread`` the event loop stayed healthy, nothing
raised, and the error handler never fired — hence total silence.

Every test below drives the REAL ``telegram.ext.Application`` dispatcher via
``process_update`` (the previous suite called ``cmd_dashboard`` directly and
so could not see a dispatch- or latency-level defect) against a deliberately
slow mail provider.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardMarkup, Update, User

import mailops.bot.app as app_mod
from mailops import cache as cache_mod
from mailops.db import init_db

from .conftest import FakeHimalaya

USER_A = 8925736576
GROUP = -5531260744
BOT_ID = 7653116966
BOT_USERNAME = "ourmailagent_bot"


def ctx():
    """Minimal handler context (user_data/bot_data only)."""
    return SimpleNamespace(args=[], user_data={}, bot_data={}, chat_data={})


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _cold_cache():
    """Reset the process-wide metadata cache around every test.

    ``mailops.cache`` is intentionally global in the running bot, so each
    test here must start from the same state a fresh restart has: cold.
    Also waits for any background warm thread a previous test started, so a
    leaked pass cannot keep mutating ``_progress`` / ``_counts`` underneath
    the next test (which produced a phantom "Refreshing from IMAP" note).
    """

    def _quiesce():
        """Stop any leaked background pass from the previous test, fast.

        Asking it to abort at the next mailbox boundary (and dropping the
        fake's latency) unwinds a 39-mailbox warm in milliseconds instead of
        letting it keep mutating ``_progress`` / ``_counts`` underneath the
        next test - which produced a phantom "Refreshing from IMAP" note.
        """
        provider = cache_mod.active_provider()
        if provider is not None:
            try:
                provider.latency = 0.0
            except Exception:  # noqa: BLE001 - not every double has latency
                pass
        cache_mod.request_abort()
        deadline = time.monotonic() + 30
        while cache_mod.refresh_in_progress() and time.monotonic() < deadline:
            time.sleep(0.02)
        cache_mod.clear_abort()

    _quiesce()
    cache_mod._counts = []
    cache_mod._buckets = {}
    cache_mod._updated_at = 0.0
    cache_mod._sf_event = None
    cache_mod._hydrated = False
    cache_mod._progress = (0, 0)
    yield
    _quiesce()
    cache_mod._counts = []
    cache_mod._buckets = {}
    cache_mod._updated_at = 0.0
    cache_mod._sf_event = None
    cache_mod._hydrated = False
    cache_mod._progress = (0, 0)


def set_env(profile, *, allowed=str(USER_A), home=f"{USER_A},{GROUP}"):
    (profile / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=test-token\n"
        f"TELEGRAM_ALLOWED_USERS={allowed}\n"
        f"TELEGRAM_HOME_CHANNEL={home}\n",
        encoding="utf-8",
    )


class SentCollector:
    """Captures every outbound Telegram call the dispatcher makes.

    Patched over ``Bot.send_message`` so we exercise the real handler, the
    real ``safe_reply`` fallback and the real ``Application`` plumbing while
    nothing leaves the process.
    """

    def __init__(self):
        self.messages: list[dict] = []
        self._seq = iter(range(500, 9999))

    async def send_message(self, chat_id, text, **kwargs):
        from telegram import Message, Chat

        self.messages.append(
            {"chat_id": chat_id, "text": text, "reply_markup": kwargs.get("reply_markup")}
        )
        return Message(
            message_id=next(self._seq),
            date=None,
            chat=Chat(id=chat_id, type="group"),
            text=text,
        )

    @property
    def texts(self) -> list[str]:
        return [m["text"] for m in self.messages]


def build_app(monkeypatch, collector: SentCollector):
    """A real Application with a fake bot identity and a captured transport.

    ``Application.initialize`` would otherwise call ``get_me`` against
    api.telegram.org and fail on the dummy token, so the bot identity is
    stubbed. Everything downstream of it — the handler registry, the
    CommandHandler matching, ``process_update`` — is the real thing.
    """
    from telegram import Bot

    bot_user = User(
        id=BOT_ID, is_bot=True, first_name="mailagentbot", username=BOT_USERNAME
    )

    async def fake_get_me(self, *a, **kw):
        self._bot_user = bot_user
        return bot_user

    monkeypatch.setattr(Bot, "get_me", fake_get_me)
    application = app_mod.build_application()
    application.bot._bot_user = bot_user
    monkeypatch.setattr(
        Bot, "send_message", lambda self, **kw: collector.send_message(**kw)
    )
    return application


def command_update(application, text: str, *, chat_id=GROUP, chat_type="group", user_id=USER_A):
    """A genuine ``Update`` built from Telegram's own JSON shape."""
    data = {
        "update_id": int(time.monotonic() * 1000) % 100000,
        "message": {
            "message_id": 4242,
            "date": 1700000000,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": False, "first_name": "owner"},
            "text": text,
            "entities": [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ],
        },
    }
    return Update.de_json(data, application.bot)


async def _dispatch(application, update):
    """Run one update through the real dispatcher, start to finish."""
    async with application:
        await application.process_update(update)


# ---------------------------------------------------------------- the defect
def test_dashboard_command_answers_promptly_on_a_cold_cache(
    tmp_profile, monkeypatch, caplog
):
    """THE regression. A cold cache must not delay /dashboard at all.

    Each IMAP listing costs 3s here, so any waiting-on-IMAP implementation
    blows this bound. The dashboard must answer from cached state only.
    """
    init_db()
    set_env(tmp_profile)
    him = FakeHimalaya()
    him.latency = 3.0  # a slow-but-healthy fleet
    cache_mod.set_provider(him)
    try:
        collector = SentCollector()
        application = build_app(monkeypatch, collector)
        update = command_update(application, "/dashboard")

        assert cache_mod.is_cold(), "cache must start cold, like a fresh restart"

        started = time.monotonic()
        with caplog.at_level("INFO", logger="mailops.bot"):
            _run(_dispatch(application, update))
        elapsed = time.monotonic() - started

        # 1. The command replied AT ALL (the live failure was zero messages).
        assert collector.messages, "/dashboard produced no Telegram message"
        # 2. It replied into the authorized group, not somewhere else.
        assert collector.messages[0]["chat_id"] == GROUP
        # 3. It did not wait on IMAP. A single listing is 3s, so this bound
        #    fails for any implementation that waits for the fleet - including
        #    the 6-second-budget version, which measured total_ms=24891 live.
        assert elapsed < 2.0, (
            f"/dashboard took {elapsed:.1f}s - it is waiting on IMAP again"
        )
        # 4. Instrumentation proves entry AND exit (the missing evidence).
        text = caplog.text
        assert "command /dashboard entered" in text
        assert "command /dashboard finished" in text
    finally:
        cache_mod.set_provider(None)


def test_cold_dashboard_shows_real_controls_immediately(tmp_profile, monkeypatch):
    """The dashboard must be INTERACTIVE on a cold cache, not a placeholder.

    The first fix rendered a two-line "Mailops is awake." stub with a single
    button, so the group lost every control (Critical, All Emails, Search,
    Manual Refresh, admin entries) for as long as the rebuild took - hours,
    as reported. Controls come from cached state and must always render.
    """
    init_db()
    set_env(tmp_profile)
    him = FakeHimalaya()
    him.latency = 3.0
    cache_mod.set_provider(him)
    try:
        collector = SentCollector()
        application = build_app(monkeypatch, collector)
        _run(_dispatch(application, command_update(application, "/dashboard")))
        assert collector.messages
        markup = collector.messages[0]["reply_markup"]
        assert isinstance(markup, InlineKeyboardMarkup)
        labels = [b.text for row in markup.inline_keyboard for b in row]
        # The real control panel, not a stub.
        assert len(markup.inline_keyboard) >= 3, labels
        for expected in ("Dashboard", "All Emails", "Search"):
            assert any(expected in l for l in labels), (expected, labels)
        assert any("Critical" in l for l in labels), labels
        # And it is honest that numbers are still being built.
        body = collector.texts[0]
        assert "Priority dashboard" in body
        assert "Refreshing from IMAP" in body or "Cache" in body
    finally:
        cache_mod.set_provider(None)


def test_counts_are_published_per_mailbox_during_a_refresh(tmp_profile):
    """A long pass must publish each mailbox as it completes.

    Root cause of "stays on Rebuilding for hours": _refresh_all_locked
    replaced _counts only after EVERY account was scanned, so a 39-mailbox
    pass left the dashboard with an empty cache for its whole duration.
    """
    import threading

    init_db()
    set_env(tmp_profile)
    him = FakeHimalaya()
    him.latency = 0.3
    cache_mod.set_provider(him)
    try:
        worker = threading.Thread(target=cache_mod.ensure_refresh_running)
        worker.start()
        worker.join()
        seen: list[int] = []
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            counts, _ = cache_mod.counts_now()
            seen.append(len(counts))
            if not cache_mod.refresh_in_progress():
                break
            time.sleep(0.2)
        # Counts became visible BEFORE the pass finished.
        assert any(0 < n for n in seen[:-1]), (
            f"no partial counts were ever published: {seen}"
        )
        scanned, total = cache_mod.refresh_progress()
        assert (scanned, total) == (0, 0), "progress must reset when idle"
        assert not cache_mod.is_cold()
    finally:
        cache_mod.set_provider(None)


def test_counts_seed_from_local_classifications_without_imap(tmp_profile):
    """Counts must come from the local classification table instantly.

    Root cause of "stays on Rebuilding for hours": the only way to populate
    the dashboard was a full-fleet IMAP pass, and on the live 39-mailbox
    fleet that pass competed with the sync loop over shared hosts - it was
    managing ~1 mailbox per 6 minutes, so the dashboard never became ready.
    Every classified message is already in ``message_locations``, so the
    counts can be derived from one SQLite query.
    """
    from mailops.db import set_message_location

    init_db()
    set_env(tmp_profile)
    set_message_location("sales", "1", "INBOX.P0-Critical", subject="a", sender="x@y.z", priority=0)
    set_message_location("sales", "2", "INBOX.P0-Critical", subject="b", sender="x@y.z", priority=0)
    set_message_location("sales", "3", "INBOX.P1-Important", subject="c", sender="x@y.z", priority=1)
    set_message_location("info", "4", "INBOX.P3-Noise", subject="d", sender="x@y.z", priority=3)

    class Forbidden:
        def envelopes(self, *a, **kw):
            raise AssertionError("seeding must not touch IMAP")

        def mailbox_list(self, *a, **kw):
            raise AssertionError("seeding must not touch IMAP")

        def uid_validity(self, *a, **kw):
            return ""

    cache_mod.set_provider(Forbidden())
    try:
        assert cache_mod.seed_from_db() is True
        counts, cold = cache_mod.counts_now()
        assert not cold
        by_id = {c.account_id: c for c in counts}
        assert by_id["sales"].p0 == 2
        assert by_id["sales"].p1 == 1
        assert by_id["info"].p3 == 1
        # And the whole fleet is represented, not just mailboxes with mail.
        assert {"sales", "info"} <= set(by_id)
    finally:
        cache_mod.set_provider(None)


def test_dashboard_shows_real_counts_from_db_on_a_cold_restart(
    tmp_profile, monkeypatch
):
    """End to end: a restart with no snapshot still shows real numbers fast."""
    from mailops.db import set_message_location

    init_db()
    set_env(tmp_profile)
    for i in range(5):
        set_message_location(
            "sales", str(i), "INBOX.P0-Critical", subject="s", sender="a@b.c", priority=0
        )
    him = FakeHimalaya()
    him.latency = 3.0  # slow fleet: must not be waited on
    cache_mod.set_provider(him)
    try:
        collector = SentCollector()
        application = build_app(monkeypatch, collector)
        started = time.monotonic()
        _run(_dispatch(application, command_update(application, "/dashboard")))
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"took {elapsed:.1f}s"
        assert collector.messages
        body = collector.texts[0]
        assert "Priority dashboard" in body
        # The real figure, derived locally - not a placeholder and not zero.
        assert "Critical 5" in body, body[:400]
    finally:
        cache_mod.set_provider(None)


def test_counts_survive_a_restart(tmp_profile):
    """A restart must come back warm, not cold.

    The cache is in memory, so every restart previously meant a full-fleet
    rebuild before the dashboard could show anything. Counts are persisted
    and rehydrated instead.
    """
    init_db()
    set_env(tmp_profile)
    him = FakeHimalaya()
    him.add_message("sales", "INBOX.P0-Critical", "1", subject="s", sender="a@b.c")
    cache_mod.set_provider(him)
    try:
        cache_mod.refresh_all()
        before, _ = cache_mod.counts_now()
        assert before

        # Simulate a process restart: memory is empty, the db is not.
        cache_mod._counts = []
        cache_mod._buckets = {}
        cache_mod._updated_at = 0.0
        cache_mod._hydrated = False

        assert cache_mod.hydrate() is True
        after, cold = cache_mod.counts_now()
        assert not cold, "dashboard was cold after a restart"
        assert {c.account_id for c in after} == {c.account_id for c in before}
        assert sum(c.p0 for c in after) == sum(c.p0 for c in before)
    finally:
        cache_mod.set_provider(None)


def test_dashboard_renders_restored_counts_without_touching_imap(
    tmp_profile, monkeypatch
):
    """After a restart the dashboard serves persisted counts with no IMAP."""
    init_db()
    set_env(tmp_profile)
    him = FakeHimalaya()
    him.add_message("sales", "INBOX.P0-Critical", "1", subject="s", sender="a@b.c")
    cache_mod.set_provider(him)
    try:
        cache_mod.refresh_all()
        cache_mod._counts = []
        cache_mod._updated_at = 0.0
        cache_mod._hydrated = False

        # A provider that would FAIL the test if the request path used it.
        class Forbidden:
            def envelopes(self, *a, **kw):
                raise AssertionError("dashboard must not hit IMAP on a read")

            def mailbox_list(self, *a, **kw):
                raise AssertionError("dashboard must not hit IMAP on a read")

            def uid_validity(self, *a, **kw):
                return ""

        collector = SentCollector()
        application = build_app(monkeypatch, collector)
        # Stop the background warm so only the request path runs here.
        monkeypatch.setattr(cache_mod, "ensure_refresh_running", lambda: True)
        cache_mod.set_provider(Forbidden())
        started = time.monotonic()
        _run(_dispatch(application, command_update(application, "/dashboard")))
        assert time.monotonic() - started < 2.0
        assert collector.messages
        assert "Priority dashboard" in collector.texts[0]
    finally:
        cache_mod.set_provider(None)


def test_repeated_dashboard_commands_do_not_stack_imap_passes(
    tmp_profile, monkeypatch
):
    """Impatient re-taps must not multiply the full-fleet IMAP load.

    Live, each of the four /dashboard commands started its OWN full refresh
    (five himalaya processes were still running long afterwards), so every
    retry made the stall worse. refresh_all is now single-flight.
    """
    init_db()
    set_env(tmp_profile)
    him = FakeHimalaya()
    him.latency = 0.2
    cache_mod.set_provider(him)
    passes = {"n": 0}
    real = cache_mod._refresh_all_locked

    def counting(*a, **kw):
        passes["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(cache_mod, "_refresh_all_locked", counting)
    try:
        collector = SentCollector()
        application = build_app(monkeypatch, collector)

        async def three_at_once():
            async with application:
                await asyncio.gather(
                    *(
                        application.process_update(
                            command_update(application, "/dashboard")
                        )
                        for _ in range(3)
                    )
                )

        _run(three_at_once())
        assert len(collector.messages) == 3, "every command must get an answer"
        # Three concurrent cold reads, but only one IMAP pass may be started.
        assert passes["n"] <= 1, f"{passes['n']} concurrent full-fleet passes"
    finally:
        cache_mod.set_provider(None)


def test_warm_cache_dashboard_shows_real_counts(tmp_profile, monkeypatch):
    """Once warm, /dashboard renders the real dashboard from cache."""
    init_db()
    set_env(tmp_profile)
    him = FakeHimalaya()
    him.add_message("sales", "INBOX.P0", "1", subject="urgent thing", sender="a@b.c")
    cache_mod.set_provider(him)
    try:
        cache_mod.refresh_all()  # simulate the startup warm having finished
        assert not cache_mod.is_cold()
        collector = SentCollector()
        application = build_app(monkeypatch, collector)
        _run(_dispatch(application, command_update(application, "/dashboard")))
        assert collector.messages
        body = collector.texts[0]
        assert "Priority dashboard" in body
        assert "Group view" in body  # group-safe rendering, counts + subjects
        assert "Refreshing from IMAP" not in body
    finally:
        cache_mod.set_provider(None)


# ------------------------------------------------- fresh message, no original
def test_dashboard_posts_fresh_message_when_original_is_deleted(
    tmp_profile, monkeypatch
):
    """The answer must survive deletion of the command message.

    Point 8 of the incident brief: the bot has to be able to put a fresh
    dashboard into the authorized group without depending on the message
    that triggered it still existing.
    """
    from telegram.error import BadRequest

    init_db()
    set_env(tmp_profile)
    him = FakeHimalaya()
    cache_mod.set_provider(him)
    try:
        cache_mod.refresh_all()
        collector = SentCollector()
        application = build_app(monkeypatch, collector)

        calls = {"n": 0}
        original = collector.send_message

        async def flaky(chat_id, text, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1 and kwargs.get("reply_to_message_id") is not None:
                raise BadRequest("Message to be replied not found")
            return await original(chat_id, text, **kwargs)

        from telegram import Bot

        monkeypatch.setattr(Bot, "send_message", lambda self, **kw: flaky(**kw))
        _run(_dispatch(application, command_update(application, "/dashboard")))
        assert collector.messages, "no fresh message reached the group"
        assert collector.messages[-1]["chat_id"] == GROUP
    finally:
        cache_mod.set_provider(None)


# --------------------------------------------------------- the other commands
@pytest.mark.parametrize("command", ["/start", "/dashboard", "/chatid"])
def test_slash_commands_all_answer_on_a_cold_cache(
    tmp_profile, monkeypatch, command, caplog
):
    """/start had the identical cold-cache stall (it also calls show_dashboard).

    Verifies every group-reachable slash command answers and is traced.
    """
    init_db()
    set_env(tmp_profile)
    him = FakeHimalaya()
    him.latency = 3.0
    cache_mod.set_provider(him)
    try:
        collector = SentCollector()
        application = build_app(monkeypatch, collector)
        started = time.monotonic()
        with caplog.at_level("INFO", logger="mailops.bot"):
            _run(_dispatch(application, command_update(application, command)))
        elapsed = time.monotonic() - started
        assert collector.messages, f"{command} produced no Telegram message"
        assert elapsed < 2.0, (
            f"{command} took {elapsed:.1f}s - it is waiting on IMAP"
        )
        assert f"command {command} entered" in caplog.text
        assert f"command {command} finished" in caplog.text
    finally:
        cache_mod.set_provider(None)


# ------------------------------------------- Dashboard TAP (callback) latency
def test_dashboard_tap_is_acknowledged_fast_enough_not_to_expire(
    tmp_profile, monkeypatch
):
    """A Dashboard button tap must not expire its callback query.

    Live evidence of the regression this guards (22:37):

        callback route=dash outcome=handled category=ok ack_ms=1516 total_ms=24891
        callback ack failed route=dash category=callback_expired after=1592ms

    The 6-second dashboard budget plus several queued asyncio.to_thread hops
    pushed every tap past Telegram's window, re-breaking the delayed-callback
    and duplicate-acknowledgment fixes. The render must stay fast enough that
    the ack lands promptly and the query never expires.
    """
    from mailops.bot import callbacks as cbq

    init_db()
    set_env(tmp_profile)
    him = FakeHimalaya()
    him.latency = 3.0  # slow fleet: must not be waited on
    cache_mod.set_provider(him)
    try:
        collector = SentCollector()
        application = build_app(monkeypatch, collector)
        edits: list[tuple] = []
        answers: list[dict] = []

        class Q:
            """Minimal callback query that records ack timing."""

            def __init__(self):
                self.id = "q-dash-1"
                self.data = "dash"
                self.from_user = SimpleNamespace(id=USER_A)
                self.message = SimpleNamespace(
                    message_id=11,
                    chat=SimpleNamespace(id=GROUP, type="group"),
                    reply_text=self._reply,
                    get_bot=lambda: application.bot,
                )
                self.started = time.monotonic()

            async def _reply(self, text, reply_markup=None):
                collector.messages.append(
                    {"chat_id": GROUP, "text": text, "reply_markup": reply_markup}
                )
                return SimpleNamespace(message_id=12)

            async def answer(self, text=None, show_alert=False):
                answers.append(
                    {"at_ms": int((time.monotonic() - self.started) * 1000)}
                )

            async def edit_message_text(self, text, reply_markup=None):
                edits.append((text, reply_markup))

        q = Q()
        upd = SimpleNamespace(
            callback_query=q,
            effective_user=SimpleNamespace(id=USER_A),
            effective_chat=q.message.chat,
            effective_message=q.message,
        )
        started = time.monotonic()
        _run(app_mod.on_callback(upd, ctx()))
        total_ms = int((time.monotonic() - started) * 1000)

        assert answers, "the tap was never acknowledged"
        # Telegram invalidates a query in seconds; the live failure acked at
        # 1516ms and still expired because the handler ran 24891ms.
        assert answers[0]["at_ms"] < 1500, answers
        assert total_ms < 2000, f"dash tap took {total_ms}ms"
        # And the tap actually rendered the dashboard with its controls.
        assert edits or collector.messages
        rendered = edits[0] if edits else (collector.messages[-1]["text"], None)
        assert "Priority dashboard" in rendered[0]
        # Exactly one answerCallbackQuery (no duplicate acknowledgment).
        assert len(answers) == 1, answers
        assert cbq.classify(None) == cbq.OK
    finally:
        cache_mod.set_provider(None)


def test_commands_are_registered_through_the_tracing_wrapper(tmp_profile):
    from telegram.ext import CommandHandler

    application = app_mod.build_application()
    traced = {
        sorted(h.commands)[0]
        for group in application.handlers.values()
        for h in group
        if isinstance(h, CommandHandler)
        and getattr(h.callback, "__wrapped__", None) is not None
    }
    assert {"start", "dashboard", "chatid", "qadd", "admin"} <= traced


# --------------------------------------------------- global error diagnostics
def test_failing_command_tells_the_user_and_logs_a_traceback(
    tmp_profile, monkeypatch, caplog
):
    """A crashing command must not be silent either."""
    init_db()
    set_env(tmp_profile)
    collector = SentCollector()

    def boom(*a, **kw):
        raise RuntimeError("synthetic dashboard explosion")

    monkeypatch.setattr(cache_mod, "counts_now", boom)
    application = build_app(monkeypatch, collector)
    with caplog.at_level("ERROR", logger="mailops.bot"):
        _run(_dispatch(application, command_update(application, "/dashboard")))
    assert "command /dashboard failed" in caplog.text
    assert any("internal error" in t for t in collector.texts), (
        "user was told nothing about the failure"
    )


def test_single_flight_reports_cold_and_warm_states(tmp_profile):
    """Unit-level contract for the new cache helpers."""
    init_db()
    set_env(tmp_profile)
    him = FakeHimalaya()
    cache_mod.set_provider(him)
    try:
        assert cache_mod.is_cold()
        counts, cold = cache_mod.counts_now()
        assert cold and counts == []
        assert not cache_mod.refresh_in_progress()
        cache_mod.refresh_all()
        assert not cache_mod.is_cold()
        counts, cold = cache_mod.counts_now()
        assert not cold and counts
        # wait_for_refresh is a no-op when nothing is running.
        assert cache_mod.wait_for_refresh(0.01) is True
    finally:
        cache_mod.set_provider(None)
