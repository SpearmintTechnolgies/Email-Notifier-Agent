"""Regression tests for the October 2026 live-group callback incident.

Reproduces, from the real log evidence, the conditions under which authorized
group mailbox buttons appeared unresponsive or refused to open an email::

    20:05:51 callback:ab:coinography2|p1 received
    20:05:52 Telegram error: Query is too old and response timeout expired
             or query id is invalid
    20:05:52 callback:ab:memecoinist|p1 received
    20:06:03 callback:gr:n:1 received
    20:06:25 refused open of memecoinist INBOX/61: the message is no longer
             at INBOX (moved, deleted or renumbered)
    20:06:29 another gr:n:1 -> callback query expired/invalid
    (earlier)  Message to be replied not found.

Scenario coverage (the ten required cases):

1.  slow IMAP operations                      -> test_slow_imap_*
2.  expired callback queries                  -> test_expired_query_*
3.  repeated rapid button taps                -> test_rapid_repeated_taps_*
4.  two owners navigating simultaneously      -> test_two_owners_*
5.  Telegram message deletion                 -> test_deleted_message_*
6.  old inline keyboards                      -> test_stale_keyboard_*
7.  email moved/deleted between list and open -> test_message_moved_*,
                                                 test_message_deleted_*
8.  UID / UIDVALIDITY changes                 -> test_uidvalidity_*
9.  group open + return to list                -> test_group_open_then_back_*
10. correct mailbox across all accounts        -> test_every_account_*

Every test asserts on behaviour that reaches the user (was the query answered
exactly once? was an email opened, and WHICH one?), never on internal call
order, so they keep their value if the implementation is refactored again.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError, TimedOut

import mailops.bot.app as app_mod
from mailops.bot import callbacks as cbq
from mailops.bot import nav
from mailops.db import init_db
from mailops.identity import remember, recall, MessageIdentity

from tests.conftest import record_identity, remember_all

USER_A = 8925736576          # owner (bootstrap)
USER_B = 5337250772          # second owner
GROUP = -5576242098
ACC = "memecoinist"          # the mailbox from the incident log
ACC2 = "coinography2"        # the other mailbox tapped one second later


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- doubles
_message_ids = iter(range(500000, 599999))
_query_ids = iter(range(1, 999999))

#: Telegram's exact wording when a query id is expired OR already answered.
EXPIRED_TEXT = (
    "Query is too old and response timeout expired or query id is invalid"
)


class FakeMessage:
    """A Telegram message that can be 'deleted' mid-flight.

    ``deleted=True`` makes every edit/reply raise exactly what Telegram
    raises for a message that no longer exists, which is how the live
    "Message to be replied not found" warning was produced.
    """

    def __init__(self, chat_id=GROUP, chat_type="group", message_id=None):
        self.message_id = message_id if message_id is not None else next(_message_ids)
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.sent: list[tuple] = []
        self.deleted = False
        self.standalone: list[tuple] = []

    async def reply_text(self, text, reply_markup=None):
        if self.deleted:
            raise BadRequest("Message to be replied not found")
        self.sent.append((text, reply_markup))
        return SimpleNamespace(message_id=next(_message_ids))

    def get_bot(self):
        return self._bot()

    def _bot(self):
        outer = self

        class _Bot:
            async def send_message(self, chat_id, text, reply_markup=None):
                # The fallback must stay in the SAME chat.
                assert chat_id == outer.chat.id, (
                    f"standalone fallback went to chat {chat_id}, "
                    f"expected {outer.chat.id}"
                )
                outer.standalone.append((text, reply_markup))
                return SimpleNamespace(message_id=next(_message_ids))

        return _Bot()


class FakeQuery:
    """Callback query that records answers and can simulate expiry.

    ``answers`` keeps EVERY attempt (including ones Telegram rejected) so a
    test can prove the bot answered exactly once.
    """

    def __init__(self, data, message=None, expired=False, answer_error=None):
        self.id = f"q{next(_query_ids)}"
        self.data = data
        self.message = message if message is not None else FakeMessage()
        self.edits: list[tuple] = []
        self.answers: list[tuple] = []
        self.answer_attempts = 0
        self.expired = expired
        self.answer_error = answer_error

    async def answer(self, text=None, show_alert=False):
        self.answer_attempts += 1
        if self.answer_error is not None:
            raise self.answer_error
        if self.expired:
            raise BadRequest(EXPIRED_TEXT)
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None):
        if self.message.deleted:
            raise BadRequest("Message to edit not found")
        self.edits.append((text, reply_markup))
        return SimpleNamespace(message_id=self.message.message_id)

    async def edit_message_reply_markup(self, reply_markup=None):
        if self.message.deleted:
            raise BadRequest("Message to edit not found")
        self.edits.append(("", reply_markup))

    # -- assertions used by the tests ------------------------------------
    @property
    def alert_texts(self) -> list[str]:
        return [t for t, _ in self.answers if t]

    @property
    def shown_text(self) -> str:
        """Everything the user could actually read as a result of the tap."""
        parts = [t for t, _ in self.edits]
        parts += [t for t, _ in self.answers if t]
        parts += [t for t, _ in self.message.sent]
        parts += [t for t, _ in self.message.standalone]
        return "\n".join(parts)


def callback_update(data, user_id, chat_id=GROUP, kind="group", *, message=None,
                    expired=False, answer_error=None):
    q = FakeQuery(
        data,
        message=message or FakeMessage(chat_id=chat_id, chat_type=kind),
        expired=expired,
        answer_error=answer_error,
    )
    upd = SimpleNamespace(
        callback_query=q,
        effective_user=SimpleNamespace(id=user_id, username="u", full_name="U"),
        effective_chat=SimpleNamespace(id=chat_id, type=kind),
        effective_message=q.message,
        message=None,
        my_chat_member=None,
    )
    return upd, q


class Ctx:
    """Per-user context, exactly like PTB's per-user ``user_data``."""

    def __init__(self, store=None):
        self.user_data = store if store is not None else {}
        self.bot = SimpleNamespace(username="ourmailagent_bot")
        self.args: list[str] = []


def ctx():
    return Ctx()


def datas(markup: InlineKeyboardMarkup) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row
            if b.callback_data]


def opens(markup: InlineKeyboardMarkup) -> list[str]:
    return [d for d in datas(markup)
            if d.startswith(("gr:n:", "n:")) and not d.endswith(":force")]


def mbx(account: str) -> str:
    """The unambiguous marker that identifies an account's mail in a screen.

    Delimited so ``news`` never matches ``100xnews``.
    """
    return f"[mbx:{account}]"


# ------------------------------------------------------------- environment
def _seed(profile, him, *, accounts=(ACC, ACC2), messages=3):
    """Authorize both owners + the group, and fill the mailboxes."""
    import json

    (profile / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=test-token\n"
        f"TELEGRAM_ALLOWED_USERS={USER_A},{USER_B}\n"
        f"TELEGRAM_HOME_CHANNEL={USER_A},{GROUP}\n"
        f"TELEGRAM_OWNER_ID={USER_A}\n",
        encoding="utf-8",
    )
    (profile / "himalaya" / "accounts.json").write_text(
        json.dumps(
            [
                {"id": a, "email": f"sales@{a}.com", "display_name": a}
                for a in accounts
            ]
        ),
        encoding="utf-8",
    )
    # Reading is what these tests exercise; sending stays off everywhere.
    (profile.parent / "data" / "settings.json").write_text(
        json.dumps(
            {
                "account_policies": {
                    a: {"organize": False, "send": False} for a in accounts
                }
            }
        ),
        encoding="utf-8",
    )
    init_db()
    from mailops import access

    # Bootstraps owners from TELEGRAM_ALLOWED_USERS / TELEGRAM_OWNER_ID and
    # the group from TELEGRAM_HOME_CHANNEL (written above).
    access.ensure()
    # Mail lands directly in the priority folders, which is what the group
    # dashboard's per-account Important button (``ab:<account>|p1``) lists.
    # Subjects carry a delimited marker (``[mbx:<id>]``) so a test can assert
    # which mailbox a list came from without substring collisions between
    # ids like ``news`` and ``100xnews``.
    for a in accounts:
        for n in range(1, messages + 1):
            him.add_message(
                a, "INBOX.P1-Important", str(n),
                subject=f"[mbx:{a}] subject {n}",
                sender=f"s{n}@sender.test",
                message_id=f"<{a}-p1-{n}@mailops.test>",
            )
        # Plain INBOX copies back the incident's ``INBOX/61`` reference shape.
        for n in range(1, messages + 1):
            him.add_message(
                a, "INBOX", str(60 + n),
                subject=f"[mbx:{a}] inbox subject {n}",
                sender=f"i{n}@sender.test",
                message_id=f"<{a}-inbox-{60 + n}@mailops.test>",
            )
    remember_all(him)
    from mailops.cache import refresh_all

    refresh_all()


@pytest.fixture()
def group(tmp_profile, fake_him, monkeypatch):
    """An authorized group with two owners and two populated mailboxes."""
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    _seed(tmp_profile, fake_him)
    return fake_him


# =====================================================================
# Harness sanity: the exact callback from the incident must work at all.
# =====================================================================
def test_harness_group_list_renders_open_buttons(group):
    upd, q = callback_update(f"ab:{ACC}|p1", USER_A)
    _run(app_mod.on_callback(upd, ctx()))
    assert q.edits, f"no screen rendered: {q.shown_text!r}"
    assert opens(q.edits[-1][1]), f"no Open buttons: {datas(q.edits[-1][1])}"


# =====================================================================
# 1. SLOW IMAP OPERATIONS
#
# Root cause of "Query is too old" on a one-second-old query: the handler
# did its IMAP work BEFORE answering, on the event loop. Telegram
# invalidates an unanswered query, so the tap died even though the bot was
# healthy. The query must now be acknowledged while the mailbox is slow.
# =====================================================================
def test_slow_imap_still_acknowledges_callback_promptly(group):
    """A slow mailbox must not delay the acknowledgment by its full duration.

    ``page:force`` is the live "Manual refresh" callback: it bypasses the
    cache and re-lists every folder over IMAP, so it is the real slow path.
    """
    group.latency = 0.4  # per folder listing; a forced refresh does many

    async def scenario():
        upd, q = callback_update("page:force", USER_A)
        task = asyncio.create_task(app_mod.on_callback(upd, ctx()))
        # Well inside Telegram's tolerance, and far less than the refresh
        # takes: the button must already have stopped spinning.
        await asyncio.sleep(cbq.WATCHDOG_SECONDS + 0.25)
        acked_early = q.answer_attempts >= 1
        still_working = not task.done()
        await task
        return q, acked_early, still_working

    q, acked_early, still_working = _run(scenario())
    assert still_working, (
        "the forced refresh finished too fast to prove anything — raise the "
        "simulated latency"
    )
    assert acked_early, (
        "callback was not acknowledged while IMAP was still running — this is "
        "exactly what made Telegram reject the answer as 'too old'"
    )
    assert q.answer_attempts == 1, (
        f"expected exactly one answerCallbackQuery, got {q.answer_attempts}"
    )


def test_slow_imap_does_not_block_other_users_callbacks(group):
    """A slow refresh for owner A must not stall owner B's tap.

    The synchronous Himalaya client used to run ON the event loop, so one
    user's IMAP wait froze every other user's callbacks (and their
    acknowledgments) for its full duration.
    """
    group.latency = 0.3

    async def scenario():
        upd_a, q_a = callback_update("page:force", USER_A)
        slow = asyncio.create_task(app_mod.on_callback(upd_a, ctx()))
        await asyncio.sleep(0.05)  # let A's handler reach the mailbox
        upd_b, q_b = callback_update("dash", USER_B)
        started = time.monotonic()
        await app_mod.on_callback(upd_b, ctx())
        elapsed = time.monotonic() - started
        a_done_at = slow.done()
        await slow
        return q_b, elapsed, a_done_at

    q_b, elapsed, a_done_early = _run(scenario())
    assert not a_done_early, (
        "owner A's slow refresh had already finished — the test proves nothing"
    )
    assert q_b.answer_attempts == 1, "second owner's tap was never answered"
    assert elapsed < 1.0, (
        f"second owner waited {elapsed:.2f}s behind another user's IMAP work; "
        "blocking calls must run off the event loop"
    )


# =====================================================================
# 2. EXPIRED CALLBACK QUERIES
#
# When Telegram really has invalidated the query, the tap must still be
# served (the message is edited) and the failure must be classified as an
# expiry rather than logged as a network fault. BadRequest subclasses
# NetworkError in PTB, which is why the live log said "network error".
# =====================================================================
def test_expired_query_still_renders_the_screen(group):
    """An expired query must not cost the user their screen."""
    upd, q = callback_update(f"ab:{ACC}|p1", USER_A, expired=True)
    _run(app_mod.on_callback(upd, ctx()))
    assert q.edits, (
        "an expired callback query must still render its result — only the "
        "popup is lost, not the whole response"
    )
    assert opens(q.edits[-1][1]), "the rendered list must still be usable"


def test_expired_query_is_not_retried(group):
    """Answering an expired query twice is pointless and hides real errors."""
    upd, q = callback_update(f"ab:{ACC}|p1", USER_A, expired=True)
    _run(app_mod.on_callback(upd, ctx()))
    assert q.answer_attempts == 1, (
        f"expired query was answered {q.answer_attempts} times; a dead query "
        "must be abandoned, not retried"
    )


def test_expired_error_is_classified_as_expiry_not_network():
    """The live log's misleading 'Telegram network error' line.

    ``BadRequest`` inherits from ``NetworkError``, so an isinstance check on
    NetworkError matched expired-query errors first and reported an
    application bug as connectivity.
    """
    expired = BadRequest(EXPIRED_TEXT)
    assert isinstance(expired, NetworkError), (
        "guard assumption: PTB really does subclass NetworkError here"
    )
    assert cbq.classify(expired) == cbq.EXPIRED
    assert cbq.classify(NetworkError("connection reset")) == cbq.NETWORK
    assert cbq.classify(TimedOut()) == cbq.TIMED_OUT
    assert cbq.classify(BadRequest("Message to be replied not found")) == (
        cbq.REPLY_TARGET_MISSING
    )
    assert cbq.classify(BadRequest("Message is not modified")) == cbq.NOT_MODIFIED


def test_error_handler_separates_expiry_from_network(group, caplog):
    """on_error must not label an expired query a network error."""
    import logging

    caplog.set_level(logging.DEBUG, logger="mailops.bot")
    context = SimpleNamespace(error=BadRequest(EXPIRED_TEXT))
    _run(app_mod.on_error(object(), context))
    joined = " ".join(r.getMessage() for r in caplog.records).lower()
    assert "network" not in joined, (
        f"expired query still reported as a network error: {joined!r}"
    )
    assert "expired" in joined or "already answered" in joined


# =====================================================================
# 3. REPEATED RAPID BUTTON TAPS
#
# The live log shows the same button tapped twice within seconds
# (gr:n:1 at 20:06:03 and 20:06:29). Each tap is a DIFFERENT query id and
# must be answered exactly once; no tap may be dropped, and no query may
# be answered twice (which Telegram reports as "Query is too old").
# =====================================================================
def test_rapid_repeated_taps_each_answered_exactly_once(group):
    """Five fast taps on the same button: five queries, five single answers."""
    async def scenario():
        queries = []
        for _ in range(5):
            upd, q = callback_update(f"ab:{ACC}|p1", USER_A)
            queries.append(q)
            await app_mod.on_callback(upd, ctx())
        return queries

    queries = _run(scenario())
    for i, q in enumerate(queries, start=1):
        assert q.answer_attempts == 1, (
            f"tap {i} issued {q.answer_attempts} answerCallbackQuery calls"
        )
        assert q.edits, f"tap {i} produced no screen"


def test_rapid_concurrent_taps_do_not_double_answer(group):
    """Concurrent taps must not be answered twice even when interleaved."""
    group.latency = 0.2

    async def scenario():
        pairs = [callback_update(f"ab:{ACC}|p1", USER_A) for _ in range(4)]
        await asyncio.gather(
            *(app_mod.on_callback(upd, ctx()) for upd, _ in pairs)
        )
        return [q for _, q in pairs]

    for i, q in enumerate(_run(scenario()), start=1):
        assert q.answer_attempts == 1, (
            f"concurrent tap {i} was answered {q.answer_attempts} times"
        )


def test_same_query_is_never_answered_twice_by_two_layers(group):
    """A refusal after an acknowledgment must post a message, not re-answer.

    This is the precise defect behind the incident's sub-second
    "Query is too old": the dispatcher answered the query up front and a
    refusal helper then answered the SAME query again to show an alert.
    """
    async def scenario():
        _, q = callback_update("dash", USER_A)
        ack = cbq.Ack(q)
        await ack.ok()              # dispatcher acknowledges
        await ack.notice("Refused for a reason the user must see.")
        return q

    q = _run(scenario())
    assert q.answer_attempts == 1, (
        f"the same query was answered {q.answer_attempts} times"
    )
    assert "Refused for a reason" in q.shown_text, (
        "the refusal reason was swallowed instead of being shown"
    )


# =====================================================================
# 4. TWO OWNERS NAVIGATING SIMULTANEOUSLY
#
# Lists were resolved from per-USER state while the keyboard lives on a
# per-CHAT message. Owner B replacing the shared group list did not touch
# owner A's state, so A's tap on the list B rendered resolved against A's
# own (different, or empty) list. That is how coinography2 and memecoinist
# produced inconsistent results seconds apart.
# =====================================================================
def test_two_owners_sharing_one_message_open_the_same_email(group):
    """Both owners tap row 1 of ONE shared message: same email for both."""
    shared = FakeMessage(chat_id=GROUP, chat_type="group")

    def tap(data, user, ctx_store):
        upd, q = callback_update(data, user, message=shared)
        _run(app_mod.on_callback(upd, Ctx(ctx_store)))
        return q

    store_a: dict = {}
    store_b: dict = {}
    # Owner A renders memecoinist's list into the shared message.
    list_q = tap(f"ab:{ACC}|p1", USER_A, store_a)
    row1 = opens(list_q.edits[-1][1])[0]

    a_open = tap(row1, USER_A, store_a)
    b_open = tap(row1, USER_B, store_b)

    a_text = a_open.edits[-1][0] if a_open.edits else a_open.shown_text
    b_text = b_open.edits[-1][0] if b_open.edits else b_open.shown_text
    assert mbx(ACC) in a_text, f"owner A did not open a {ACC} email: {a_text[:200]!r}"
    assert mbx(ACC) in b_text, (
        "owner B tapped the SAME shared button and got something else "
        f"(per-user navigation state): {b_text[:200]!r}"
    )
    assert a_text == b_text, (
        "the same button opened different emails for two owners:\n"
        f"A: {a_text[:160]!r}\nB: {b_text[:160]!r}"
    )


def test_two_owners_different_mailboxes_do_not_cross_over(group):
    """A's list of ACC and B's list of ACC2 live on separate messages.

    Each owner's Open button must resolve against the message it is on, so
    neither can be served an email from the other's mailbox.
    """
    msg_a = FakeMessage(chat_id=GROUP, chat_type="group")
    msg_b = FakeMessage(chat_id=GROUP, chat_type="group")
    store_a: dict = {}
    store_b: dict = {}

    upd, q_a = callback_update(f"ab:{ACC}|p1", USER_A, message=msg_a)
    _run(app_mod.on_callback(upd, Ctx(store_a)))
    row_a = opens(q_a.edits[-1][1])[0]

    upd, q_b = callback_update(f"ab:{ACC2}|p1", USER_B, message=msg_b)
    _run(app_mod.on_callback(upd, Ctx(store_b)))
    row_b = opens(q_b.edits[-1][1])[0]

    # A taps their own list AFTER B rendered a different mailbox elsewhere.
    upd, open_a = callback_update(row_a, USER_A, message=msg_a)
    _run(app_mod.on_callback(upd, Ctx(store_a)))
    upd, open_b = callback_update(row_b, USER_B, message=msg_b)
    _run(app_mod.on_callback(upd, Ctx(store_b)))

    text_a = open_a.edits[-1][0]
    text_b = open_b.edits[-1][0]
    assert mbx(ACC) in text_a and mbx(ACC2) not in text_a, (
        f"owner A got the wrong mailbox: {text_a[:200]!r}"
    )
    assert mbx(ACC2) in text_b and mbx(ACC) not in text_b, (
        f"owner B got the wrong mailbox: {text_b[:200]!r}"
    )


def test_two_owners_interleaved_rapid_taps_each_get_their_own_mailbox(group):
    """The incident's timing: two mailboxes tapped ~1s apart in one group."""
    group.latency = 0.1
    msg_a = FakeMessage(chat_id=GROUP, chat_type="group")
    msg_b = FakeMessage(chat_id=GROUP, chat_type="group")

    async def scenario():
        store_a: dict = {}
        store_b: dict = {}
        upd_a, q_a = callback_update(f"ab:{ACC2}|p1", USER_A, message=msg_a)
        upd_b, q_b = callback_update(f"ab:{ACC}|p1", USER_B, message=msg_b)
        await asyncio.gather(
            app_mod.on_callback(upd_a, Ctx(store_a)),
            app_mod.on_callback(upd_b, Ctx(store_b)),
        )
        row_a = opens(q_a.edits[-1][1])[0]
        row_b = opens(q_b.edits[-1][1])[0]
        upd_a2, open_a = callback_update(row_a, USER_A, message=msg_a)
        upd_b2, open_b = callback_update(row_b, USER_B, message=msg_b)
        await asyncio.gather(
            app_mod.on_callback(upd_a2, Ctx(store_a)),
            app_mod.on_callback(upd_b2, Ctx(store_b)),
        )
        return open_a, open_b

    open_a, open_b = _run(scenario())
    assert mbx(ACC2) in open_a.edits[-1][0], open_a.edits[-1][0][:200]
    assert mbx(ACC) in open_b.edits[-1][0], open_b.edits[-1][0][:200]


# =====================================================================
# 5. TELEGRAM MESSAGE DELETION  (live: "Message to be replied not found")
#
# If the message a handler is replying to was deleted, the whole response
# used to be lost. It must fall back to a standalone message in the SAME
# chat, exactly once.
# =====================================================================
def test_deleted_message_reply_falls_back_to_standalone(group):
    msg = FakeMessage(chat_id=GROUP, chat_type="group")
    msg.deleted = True

    async def scenario():
        delivered = await cbq.safe_reply(msg, "Something the user must read.")
        return delivered

    delivered = _run(scenario())
    assert delivered is not None, "response was lost when the message was gone"
    assert len(msg.standalone) == 1, (
        f"expected exactly one standalone fallback, got {len(msg.standalone)}"
    )
    assert "must read" in msg.standalone[0][0]


def test_deleted_message_fallback_is_not_duplicated(group):
    """The user must never receive the same text twice."""
    msg = FakeMessage(chat_id=GROUP, chat_type="group")
    msg.deleted = True
    _run(cbq.safe_reply(msg, "Only once."))
    assert len(msg.sent) == 0
    assert len(msg.standalone) == 1, msg.standalone


def test_deleted_message_callback_still_informs_the_user(group):
    """A tap on a keyboard whose message was deleted is not silently dropped."""
    msg = FakeMessage(chat_id=GROUP, chat_type="group")
    upd, q = callback_update(f"ab:{ACC}|p1", USER_A, message=msg)
    msg.deleted = True  # deleted between rendering and tapping
    _run(app_mod.on_callback(upd, ctx()))
    assert q.answer_attempts == 1, "the tap was never acknowledged"
    assert msg.standalone, (
        "the edit failed and nothing was posted instead — the user's tap "
        "vanished without a trace"
    )


def test_deleted_message_fallback_keeps_the_same_chat(group):
    """Authorization is per chat: the fallback must never change chats.

    ``FakeMessage``'s bot double asserts the chat id, so a cross-chat post
    fails loudly here rather than leaking content into another chat.
    """
    msg = FakeMessage(chat_id=GROUP, chat_type="group")
    msg.deleted = True
    _run(cbq.safe_reply(msg, "Stays in this group."))
    assert msg.standalone, "nothing was delivered"


# =====================================================================
# 6. OLD INLINE KEYBOARDS
#
# A keyboard cached in a Telegram client outlives the list it was built
# for. A bare position ("open row 1") cannot be told apart from a current
# one, so it used to resolve against whatever list was stored — possibly a
# different mailbox. Tokenised buttons make a superseded keyboard
# detectable, and the user gets a refreshed, working list.
# =====================================================================
def test_stale_keyboard_tap_does_not_open_a_different_mailbox(group):
    """Re-render the same message with ANOTHER mailbox, then tap the old row."""
    shared = FakeMessage(chat_id=GROUP, chat_type="group")
    store: dict = {}

    upd, q_old = callback_update(f"ab:{ACC}|p1", USER_A, message=shared)
    _run(app_mod.on_callback(upd, Ctx(store)))
    stale_button = opens(q_old.edits[-1][1])[0]  # row 1 of ACC's list

    # The same message now shows the OTHER mailbox.
    upd, _ = callback_update(f"ab:{ACC2}|p1", USER_A, message=shared)
    _run(app_mod.on_callback(upd, Ctx(store)))

    # Tapping the stale keyboard must not open ACC2's row 1 as if it were
    # the ACC email the button was built for.
    upd, q_tap = callback_update(stale_button, USER_A, message=shared)
    _run(app_mod.on_callback(upd, Ctx(store)))
    shown = q_tap.shown_text
    assert "subject" not in shown.lower() or "replaced by a newer" in shown, (
        "a stale keyboard silently opened an email from the list that "
        f"replaced it: {shown[:300]!r}"
    )
    assert q_tap.answer_attempts == 1


def test_stale_keyboard_tap_leaves_a_working_list(group):
    """The user must be able to recover without hunting for a fresh screen."""
    shared = FakeMessage(chat_id=GROUP, chat_type="group")
    store: dict = {}
    upd, q_old = callback_update(f"ab:{ACC}|p1", USER_A, message=shared)
    _run(app_mod.on_callback(upd, Ctx(store)))
    stale_button = opens(q_old.edits[-1][1])[0]
    upd, _ = callback_update(f"ab:{ACC2}|p1", USER_A, message=shared)
    _run(app_mod.on_callback(upd, Ctx(store)))

    upd, q_tap = callback_update(stale_button, USER_A, message=shared)
    _run(app_mod.on_callback(upd, Ctx(store)))
    assert q_tap.edits, "no refreshed screen was offered after a stale tap"
    assert opens(q_tap.edits[-1][1]), (
        "the recovery screen has no working Open buttons: "
        f"{datas(q_tap.edits[-1][1])}"
    )


def test_stale_keyboard_from_an_unknown_render_is_reported(group):
    """A keyboard naming a render the bot no longer knows about."""
    shared = FakeMessage(chat_id=GROUP, chat_type="group")
    upd, q = callback_update("gr:n:deadbe:1", USER_A, message=shared)
    _run(app_mod.on_callback(upd, ctx()))
    assert q.answer_attempts == 1
    assert "older session" in q.shown_text or "refreshed" in q.shown_text, (
        f"unknown render was not explained: {q.shown_text[:300]!r}"
    )


def test_legacy_positional_keyboard_still_works_across_the_deploy(group):
    """Keyboards already live in the group must not break on restart.

    Buttons rendered before this fix carry a bare position (``gr:n:1``).
    They have no token, so they resolve against the tapper's own state —
    which is what they were built against.
    """
    upd, q_list = callback_update(f"ab:{ACC}|p1", USER_A)
    store: dict = {}
    _run(app_mod.on_callback(upd, Ctx(store)))
    assert store.get("page_refs"), "legacy fallback state was not populated"

    upd, q = callback_update("gr:n:1", USER_A)  # pre-deploy keyboard shape
    _run(app_mod.on_callback(upd, Ctx(store)))
    assert q.edits, f"a legacy keyboard stopped working: {q.shown_text[:300]!r}"
    assert mbx(ACC) in q.edits[-1][0]


# =====================================================================
# 7. EMAILS MOVED OR DELETED BETWEEN LISTING AND OPENING
#
# The live refusal: "refused open of memecoinist INBOX/61: the message is
# no longer at INBOX (moved, deleted or renumbered)". The guard was right
# to refuse, but it dead-ended. The bot must relocate the SAME message by
# its stable identity when it can, and never open the message that took
# its place.
# =====================================================================
def _open_inbox_61(him, store, *, user=USER_A, message=None):
    """Render a list that contains INBOX/61, then tap it."""
    ref = f"{ACC}|INBOX|61"
    msg = message or FakeMessage(chat_id=GROUP, chat_type="group")
    # Put the INBOX message into the list the user is looking at.
    nav.save(
        GROUP,
        msg.message_id,
        token="tok61",
        view={"priority": 1, "account_id": ACC, "page": 0},
        page_refs=[ref],
        browse_refs=[ref],
        items=[{
            "ref": ref, "subject": f"{mbx(ACC)} inbox subject 1",
            "from": "i1@sender.test", "account_id": ACC,
            "mailbox": "INBOX", "msg_id": "61",
        }],
    )
    upd, q = callback_update("gr:n:tok61:1", user, message=msg)
    _run(app_mod.on_callback(upd, Ctx(store)))
    return q


def test_message_moved_between_listing_and_opening_is_relocated(group):
    """The email moved to another folder: open THE SAME email at its new id."""
    env = group.store[ACC]["INBOX"].pop("61")
    # The destination folder assigns a different id - the whole point.
    env = dict(env, id="904", uid="904")
    group.store[ACC].setdefault("INBOX.P1-Important", {})["904"] = env

    q = _open_inbox_61(group, {})
    shown = q.shown_text
    assert "inbox subject 1" in shown, (
        f"the relocated email was not opened: {shown[:400]!r}"
    )
    assert q.answer_attempts == 1


def test_message_moved_never_opens_the_replacement_message(group):
    """A DIFFERENT email now sits at INBOX/61. It must never be shown.

    This is the "did the bot ever risk opening the wrong email?" case: the
    reference is a sequence number, and after an expunge a different
    message occupies it.
    """
    group.store[ACC]["INBOX"].pop("61")
    group.add_message(
        ACC, "INBOX", "61",
        subject="SOMEONE ELSES EMAIL",
        sender="intruder@elsewhere.test",
        message_id="<intruder@mailops.test>",
    )
    q = _open_inbox_61(group, {})
    assert "SOMEONE ELSES EMAIL" not in q.shown_text, (
        "the bot opened the message that took the old sequence number — "
        f"this is the wrong-email risk: {q.shown_text[:400]!r}"
    )


def test_message_deleted_between_listing_and_opening_is_explained(group):
    """Deleted for good: say so plainly and refresh the list."""
    group.store[ACC]["INBOX"].pop("61")
    q = _open_inbox_61(group, {})
    shown = q.shown_text.lower()
    assert "moved or deleted" in shown or "no longer" in shown, (
        f"the user was not told what happened: {q.shown_text[:400]!r}"
    )
    assert q.answer_attempts == 1, "the tap was not acknowledged"


def test_message_deleted_leaves_a_working_retry_list(group):
    """After the failure the screen must still offer a usable list."""
    group.store[ACC]["INBOX"].pop("61")
    q = _open_inbox_61(group, {})
    assert q.edits, "no refreshed list after a stale reference"
    assert opens(q.edits[-1][1]), (
        f"recovery screen has no Open buttons: {datas(q.edits[-1][1])}"
    )


def test_message_identity_check_is_not_bypassed(group):
    """Relocation must be identity-based, never positional.

    A message whose stored identity matches nothing anywhere must NOT be
    opened, even though some message exists at the same id elsewhere.
    """
    group.store[ACC]["INBOX"].pop("61")
    remember(
        MessageIdentity(
            account_id=ACC, mailbox="INBOX", msg_id="61",
            message_id="<nowhere@mailops.test>",
            subject="vanished", sender="gone@sender.test",
            date="Mon, 01 Jan 2024 00:00:00 +0000",
        )
    )
    group.add_message(
        ACC, "INBOX.P1-Important", "61",
        subject="decoy at the same id", sender="decoy@sender.test",
        message_id="<decoy@mailops.test>",
    )
    q = _open_inbox_61(group, {})
    assert "decoy" not in q.shown_text.lower(), (
        "identity check was bypassed and a decoy at the same id was opened: "
        f"{q.shown_text[:400]!r}"
    )


# =====================================================================
# 8. UID / UIDVALIDITY CHANGES
#
# An IMAP UID is only meaningful inside one UIDVALIDITY epoch. If the
# server renumbers a folder, every stored reference for it is void - even
# if some message happens to sit at that number now.
# =====================================================================
def test_uidvalidity_change_invalidates_stored_references(group):
    """Recorded under epoch 1, folder is now epoch 2: refuse to trust the id."""
    group.validity[(ACC, "INBOX")] = "1"
    record_identity(group, ACC, "INBOX", "61")      # stamped with epoch 1
    group.validity[(ACC, "INBOX")] = "2"            # server renumbered

    from mailops.identity import verify

    verdict = verify(group, ACC, "INBOX", "61")
    assert not verdict.ok, (
        "a reference from a previous UIDVALIDITY epoch was accepted"
    )
    assert "renumber" in verdict.reason.lower() or "uidvalidity" in verdict.reason.lower(), (
        f"the reason does not mention renumbering: {verdict.reason!r}"
    )


def test_uidvalidity_change_does_not_open_the_wrong_email(group):
    """End to end: a renumbered folder must not serve whatever is at the id."""
    group.validity[(ACC, "INBOX")] = "1"
    record_identity(group, ACC, "INBOX", "61")
    group.validity[(ACC, "INBOX")] = "7"
    # After renumbering, a completely different email occupies id 61.
    group.store[ACC]["INBOX"]["61"] = dict(
        group.store[ACC]["INBOX"]["61"],
        subject="POST RENUMBER STRANGER",
        **{"message-id": "<stranger@mailops.test>"},
    )
    q = _open_inbox_61(group, {})
    assert "POST RENUMBER STRANGER" not in q.shown_text, (
        "a UIDVALIDITY change was ignored and the wrong email was opened: "
        f"{q.shown_text[:400]!r}"
    )


def test_unknown_uidvalidity_keeps_the_previous_behaviour(group):
    """Himalaya does not expose UIDVALIDITY today: '' must not break reads.

    When the epoch is unknown the Message-ID / subject / sender comparison
    remains the guarantee, exactly as before this change.
    """
    assert group.uid_validity(ACC, "INBOX") == ""
    record_identity(group, ACC, "INBOX", "61")
    from mailops.identity import verify

    verdict = verify(group, ACC, "INBOX", "61")
    assert verdict.ok, (
        f"an unchanged message failed to verify without UIDVALIDITY: {verdict.reason!r}"
    )


def test_uid_change_for_same_message_is_detected_by_message_id(group):
    """The id moved but the message is the same: identity, not position, wins."""
    record_identity(group, ACC, "INBOX", "61")
    identity = recall(ACC, "INBOX", "61")
    assert identity is not None and identity.message_id

    env = group.store[ACC]["INBOX"].pop("61")
    group.store[ACC]["INBOX"]["7788"] = dict(env, id="7788", uid="7788")

    from mailops.identity import locate

    found = locate(group, identity, "INBOX")
    assert found is not None, "the same message could not be relocated by id"
    assert found.msg_id == "7788", f"relocated to the wrong id: {found.msg_id}"
    assert found.message_id == identity.message_id


def test_database_written_before_uid_validity_existed_is_migrated_in_place(group):
    """The live database is exactly this shape, so prove the upgrade is safe.

    Every ``message_identity`` row in production was written before the
    ``uid_validity`` column existed. Opening such a database must add the
    column without dropping rows, and the pre-existing rows must stay
    openable - a migration that lost them would make every already-notified
    email dead on the first tap after deploy.
    """
    import sqlite3

    from mailops.db import db
    from mailops.identity import _ensure_schema, verify

    record_identity(group, ACC, "INBOX", "61")

    # Recreate the pre-migration schema with the row still in it.
    with db() as conn:
        row = conn.execute(
            "SELECT account_id, mailbox, msg_id, message_id, subject, sender,"
            " date FROM message_identity"
        ).fetchall()
        assert row, "fixture wrote no identity row"
        conn.execute("DROP TABLE message_identity")
        conn.execute(
            "CREATE TABLE message_identity ("
            " account_id TEXT NOT NULL, mailbox TEXT NOT NULL,"
            " msg_id TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '',"
            " subject TEXT NOT NULL DEFAULT '', sender TEXT NOT NULL DEFAULT '',"
            " date TEXT NOT NULL DEFAULT '', recorded_at TEXT NOT NULL DEFAULT '',"
            " PRIMARY KEY (account_id, mailbox, msg_id))"
        )
        conn.executemany(
            "INSERT INTO message_identity (account_id, mailbox, msg_id,"
            " message_id, subject, sender, date) VALUES (?,?,?,?,?,?,?)",
            [tuple(r) for r in row],
        )
        cols = {r[1] for r in conn.execute("pragma table_info(message_identity)")}
        assert "uid_validity" not in cols, "failed to build the legacy schema"

        before = conn.execute("SELECT COUNT(*) FROM message_identity").fetchone()[0]
        _ensure_schema(conn)
        cols = {r[1] for r in conn.execute("pragma table_info(message_identity)")}
        after = conn.execute("SELECT COUNT(*) FROM message_identity").fetchone()[0]

    assert "uid_validity" in cols, "the live database would not gain uid_validity"
    assert after == before, f"migration lost rows: {before} -> {after}"

    # The legacy row (unknown epoch) must still verify and still open.
    identity = recall(ACC, "INBOX", "61")
    assert identity is not None, "the migrated row became unreadable"
    assert identity.uid_validity == "", "a legacy row invented an epoch"
    assert verify(group, ACC, "INBOX", "61").ok, (
        "a row written before the migration stopped verifying after it"
    )
    q = _open_inbox_61(group, {})
    assert f"{mbx(ACC)} inbox subject 1" in q.shown_text, (
        f"a pre-migration email could not be opened: {q.shown_text[:300]!r}"
    )


# =====================================================================
# 9. GROUP EMAIL OPENING AND RETURNING TO THE LIST
# =====================================================================
def test_group_open_then_back_restores_a_usable_list(group):
    """Open an email in the group, then Back: the list must work again."""
    store: dict = {}
    shared = FakeMessage(chat_id=GROUP, chat_type="group")

    upd, q_list = callback_update(f"ab:{ACC}|p1", USER_A, message=shared)
    _run(app_mod.on_callback(upd, Ctx(store)))
    row1 = opens(q_list.edits[-1][1])[0]

    upd, q_open = callback_update(row1, USER_A, message=shared)
    _run(app_mod.on_callback(upd, Ctx(store)))
    assert mbx(ACC) in q_open.edits[-1][0], "the email did not open in the group"

    upd, q_back = callback_update("gr:back", USER_A, message=shared)
    _run(app_mod.on_callback(upd, Ctx(store)))
    assert q_back.edits, "Back produced no screen"
    back_opens = opens(q_back.edits[-1][1])
    assert back_opens, (
        f"Back lost the Open buttons: {datas(q_back.edits[-1][1])}"
    )

    # And the restored buttons must still open the right mailbox.
    upd, q_again = callback_update(back_opens[0], USER_A, message=shared)
    _run(app_mod.on_callback(upd, Ctx(store)))
    assert mbx(ACC) in q_again.edits[-1][0], (
        f"after Back, row 1 opened the wrong thing: {q_again.edits[-1][0][:200]!r}"
    )


def test_group_open_then_back_is_acknowledged_each_step(group):
    """Every step of the journey answers its own query exactly once."""
    store: dict = {}
    shared = FakeMessage(chat_id=GROUP, chat_type="group")
    upd, q_list = callback_update(f"ab:{ACC}|p1", USER_A, message=shared)
    _run(app_mod.on_callback(upd, Ctx(store)))
    row1 = opens(q_list.edits[-1][1])[0]
    steps = [row1, "gr:back", row1, "dash"]
    for data in steps:
        upd, q = callback_update(data, USER_A, message=shared)
        _run(app_mod.on_callback(upd, Ctx(store)))
        assert q.answer_attempts == 1, (
            f"step {data!r} issued {q.answer_attempts} answers"
        )


def test_group_page_force_refreshes_without_losing_buttons(group):
    """'page:force' is one of the incident's callbacks."""
    store: dict = {}
    shared = FakeMessage(chat_id=GROUP, chat_type="group")
    upd, _ = callback_update(f"ab:{ACC}|p1", USER_A, message=shared)
    _run(app_mod.on_callback(upd, Ctx(store)))

    upd, q = callback_update("page:force", USER_A, message=shared)
    _run(app_mod.on_callback(upd, Ctx(store)))
    assert q.answer_attempts == 1
    assert q.edits, "page:force rendered nothing"
    assert opens(q.edits[-1][1]), (
        f"page:force lost the Open buttons: {datas(q.edits[-1][1])}"
    )


def test_group_dash_callback_is_answered_once(group):
    """'dash' produced sub-second 'Query is too old' twice in the live log."""
    upd, q = callback_update("dash", USER_A)
    _run(app_mod.on_callback(upd, ctx()))
    assert q.answer_attempts == 1, (
        f"dash issued {q.answer_attempts} answerCallbackQuery calls"
    )
    assert q.edits, "dash rendered no dashboard"


def test_application_processes_updates_concurrently(tmp_profile, monkeypatch):
    """PTB must not be left on its sequential default.

    ``concurrent_updates=1`` (the library default) means a handler waiting on
    IMAP stops PTB from STARTING any other update, so another owner's tap
    expires in the queue before it is ever seen. Offloading blocking work to
    threads does not help while updates are serialised, so this is asserted
    on the built Application rather than on a handler.
    """
    (tmp_profile / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=123456:test-token\n"
        f"TELEGRAM_ALLOWED_USERS={USER_A}\n",
        encoding="utf-8",
    )
    app = app_mod.build_application()
    assert app.concurrent_updates > 1, (
        "updates are still processed one at a time; one slow mailbox will "
        "keep stalling every other owner's buttons"
    )


# =====================================================================
# 10. CORRECT MAILBOX SELECTION ACROSS ALL REGISTERED ACCOUNTS
#
# With 39 accounts, ``ab:<account>|p1`` must select exactly that account.
# A routing collision (e.g. a prefix match) would silently list another
# mailbox - which is how "coinography2 vs memecoinist" looked random.
# =====================================================================
#: Names chosen to expose prefix/substring routing collisions: several share
#: a prefix, one is a strict prefix of another, and ids contain the
#: separator-adjacent characters used in callback payloads.
_FLEET = (
    "memecoinist", "memecoinist2", "coinography", "coinography2",
    "altcoininformer", "alien", "marketing", "marketing2", "sales",
    "news", "media", "seo", "100xnews", "aivideo", "aivideoagency",
    "bitcoinnewsbulletin", "ethereumnewsbulletin", "coinnetwork",
    "oracleworks", "blockwire", "shekel", "nftinsidernews",
)


@pytest.fixture()
def fleet(tmp_profile, fake_him, monkeypatch):
    """A realistic multi-account fleet with colliding-looking ids."""
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    _seed(tmp_profile, fake_him, accounts=_FLEET, messages=2)
    return fake_him


@pytest.mark.parametrize("account", _FLEET)
def test_every_account_button_lists_only_that_mailbox(fleet, account):
    """``ab:<account>|p1`` must never show another account's mail."""
    upd, q = callback_update(f"ab:{account}|p1", USER_A)
    _run(app_mod.on_callback(upd, ctx()))
    assert q.answer_attempts == 1, f"{account}: tap not acknowledged once"
    assert q.edits, f"{account}: no list rendered ({q.shown_text[:200]!r})"
    text = q.edits[-1][0]
    others = [
        a for a in _FLEET
        if a != account and mbx(a) in text
    ]
    assert not others, (
        f"{account}'s list also contained mail from {others}"
    )
    assert mbx(account) in text, (
        f"{account}: its own mail is missing from its list: {text[:300]!r}"
    )


@pytest.mark.parametrize("account", ["memecoinist", "coinography2", "marketing"])
def test_every_account_open_button_opens_that_mailbox(fleet, account):
    """Row 1 of an account's list opens an email from THAT account."""
    shared = FakeMessage(chat_id=GROUP, chat_type="group")
    store: dict = {}
    upd, q_list = callback_update(f"ab:{account}|p1", USER_A, message=shared)
    _run(app_mod.on_callback(upd, Ctx(store)))
    row1 = opens(q_list.edits[-1][1])[0]

    upd, q_open = callback_update(row1, USER_A, message=shared)
    _run(app_mod.on_callback(upd, Ctx(store)))
    text = q_open.edits[-1][0]
    assert mbx(account) in text, (
        f"{account}: row 1 opened the wrong email: {text[:300]!r}"
    )


def test_switching_between_all_accounts_never_leaks_the_previous_one(fleet):
    """Tap every account in turn on ONE shared message (the group pattern)."""
    shared = FakeMessage(chat_id=GROUP, chat_type="group")
    store: dict = {}
    for account in _FLEET:
        upd, q = callback_update(f"ab:{account}|p1", USER_A, message=shared)
        _run(app_mod.on_callback(upd, Ctx(store)))
        text = q.edits[-1][0]
        leaked = [
            a for a in _FLEET if a != account and mbx(a) in text
        ]
        assert not leaked, (
            f"after switching to {account}, the list still showed {leaked}"
        )
