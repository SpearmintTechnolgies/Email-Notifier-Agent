"""Regression tests: Telegram group support + multi-target notifications.

Root causes covered:
  * TELEGRAM_HOME_CHANNEL="user_id,-group_id" was parsed as None (isdigit on
    the whole string), so the group never received anything.
  * Group chats had no authorization / routing model: replies used the
    persistent reply keyboard (invisible in groups), and strangers got
    "Unauthorized." replies.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup

import mailops.bot.app as app_mod
from mailops.bot import keyboards as kb
from mailops.config import load_runtime, parse_id_list
from mailops.db import init_db

USER_A = 8925736576
USER_B = 5337250772
GROUP = -5576242098
OTHER_GROUP = -1009999999999
STRANGER = 424242


def _run(coro):
    return asyncio.run(coro)


def set_env(profile, *, allowed=f"{USER_A},{USER_B}", home=f"{USER_A},{GROUP}"):
    (profile / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=test-token\n"
        f"TELEGRAM_ALLOWED_USERS={allowed}\n"
        f"TELEGRAM_HOME_CHANNEL={home}\n",
        encoding="utf-8",
    )


# --- minimal Telegram doubles -------------------------------------------------
_fake_message_id_seq = iter(range(900000, 999999))


class FakeMessage:
    def __init__(self):
        self.sent: list[tuple] = []

    async def reply_text(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return SimpleNamespace(message_id=next(_fake_message_id_seq))


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.edits: list[tuple] = []
        self.answers: list[tuple] = []
        self.message = FakeMessage()

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edits.append(("", reply_markup))


def chat(chat_id, kind):
    return SimpleNamespace(id=chat_id, type=kind)


def command_update(user_id, chat_id, kind):
    msg = FakeMessage()
    upd = SimpleNamespace(
        callback_query=None,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=chat(chat_id, kind),
        effective_message=msg,
        message=msg,
    )
    return upd, msg


def callback_update(data, user_id, chat_id, kind):
    q = FakeQuery(data)
    upd = SimpleNamespace(
        callback_query=q,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=chat(chat_id, kind),
        effective_message=q.message,
    )
    return upd, q


def ctx():
    return SimpleNamespace(user_data={})


def callback_datas(markup: InlineKeyboardMarkup) -> list[str]:
    """Button callback data, with nav generation tokens normalised away.

    Positional Open buttons now carry the id of the render they belong to
    (``gr:n:<token>:1`` — see ``mailops.bot.nav``) so a tap from a superseded
    keyboard is detected instead of resolving to whatever is at that position
    now. These tests assert routing intent ("row 1 opens in-group"), not the
    wire encoding, so the token is stripped here. ``raw_callback_datas``
    keeps the verbatim values for the tests that check tokens specifically.
    """
    return [_strip_nav_token(d) for d in raw_callback_datas(markup)]


def raw_callback_datas(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


_POSITIONAL_PREFIXES = ("gr:restricted:", "gr:n:", "n:")


def _strip_nav_token(data: str) -> str:
    for prefix in _POSITIONAL_PREFIXES:
        if not data.startswith(prefix):
            continue
        tail = data[len(prefix):]
        if ":" in tail:
            _, _, index = tail.rpartition(":")
            if index.isdigit():
                return f"{prefix}{index}"
        return data
    return data


def button_labels(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


class ExplodingHimalaya:
    """Any mailbox access from a group-originated callback is a bug."""

    def __init__(self, *a, **k):
        raise AssertionError("group callback must not touch the mailbox client")


# --- config parsing -----------------------------------------------------------
def test_parse_id_list_handles_real_world_value():
    # The exact shape that loaded as None in production.
    ids, bad = parse_id_list("8925736576,-5576242098")
    assert ids == [8925736576, -5576242098]
    assert bad == []


def test_parse_id_list_handles_configured_mailops_group():
    ids, bad = parse_id_list("8925736576,5337250772,-5531260744")
    assert ids == [8925736576, 5337250772, -5531260744]
    assert bad == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        (" 8925736576,5337250772", [8925736576, 5337250772]),
        ("'8925736576' , \"-100123\"", [8925736576, -100123]),
        ("1;2  3", [1, 2, 3]),
        ("5, 5,5", [5]),
        ("7 # my group", [7]),
        ("", []),
        (None, []),
    ],
)
def test_parse_id_list_variants(raw, expected):
    assert parse_id_list(raw)[0] == expected


def test_parse_id_list_reports_invalid_tokens():
    ids, bad = parse_id_list("123,abc,-,--5,4-")
    assert ids == [123]
    assert set(bad) == {"abc", "-", "--5", "4-"}


def test_load_runtime_two_users_and_group(tmp_profile):
    set_env(tmp_profile)
    rt = load_runtime()
    assert rt.allowed_users == [USER_A, USER_B]
    assert rt.home_channels == [USER_A, GROUP]
    assert rt.home_channel == USER_A  # backward-compatible first target
    assert rt.home_groups == [GROUP]
    assert rt.notify_targets == [USER_A, GROUP]


def test_notify_targets_fall_back_to_first_user(tmp_profile):
    set_env(tmp_profile, home="")
    rt = load_runtime()
    assert rt.home_channel is None and rt.home_groups == []
    assert rt.notify_targets == [USER_A]


def test_negative_allowed_user_entries_are_ignored(tmp_profile):
    set_env(tmp_profile, allowed=f"{USER_A},{GROUP}")
    assert load_runtime().allowed_users == [USER_A]


# --- group authorization & command routing ------------------------------------
def test_group_start_authorized_opens_inline_dashboard(tmp_profile, fake_him):
    init_db()
    set_env(tmp_profile)
    # Operator (USER_B) sees non-admin controls; owner (USER_A) sees everything.
    # Mailboxes is split for GROUP-FIRST onboarding into two buttons:
    #   "Mailboxes (in group)" (callback mb:m — runs in group)
    #   "Mailboxes (private)"  (callback gdl:mailboxes — deep-link to DM)
    expected_by_user = {
        USER_A: (
            "Dashboard", "All Emails", "Reply", "AI Drafts", "Send / Approvals",
            "Manage Users", "Whitelist Groups",
            "Mailboxes (in group)", "Mailboxes (private)",
            "Settings", "Audit Logs",
            "Manual Refresh",
        ),
        USER_B: (
            "Dashboard", "All Emails", "Reply", "AI Drafts", "Send / Approvals",
            "Settings", "Manual Refresh",
        ),
    }
    for user, required_labels in expected_by_user.items():
        upd, msg = command_update(user, GROUP, "group")
        _run(app_mod.cmd_start(upd, ctx()))
        assert len(msg.sent) == 1, f"user {user} got no dashboard"
        text, markup = msg.sent[0]
        assert "Priority dashboard" in text
        assert isinstance(markup, InlineKeyboardMarkup)
        assert not isinstance(markup, ReplyKeyboardMarkup)
        labels = button_labels(markup)
        for required in required_labels:
            assert required in labels, f"user {user} missing {required}"
        datas = callback_datas(markup)
        # Sensitive controls use the gdl: deep-link minter (never act in group).
        assert "gdl:reply_pick" in datas
        assert "gdl:ai_drafts" in datas
        assert "gdl:approvals" in datas
        assert "gdl:settings" in datas
        # Operators without admin perm do not see admin / audit callbacks.
        if user == USER_A:
            assert "gdl:admin_users" in datas
            assert "gdl:admin_groups" in datas
            assert "gdl:audit" in datas
            assert "gdl:mailboxes" in datas
        else:
            assert "gdl:admin_users" not in datas
            assert "gdl:admin_groups" not in datas
            assert "gdl:audit" not in datas
            assert "gdl:mailboxes" not in datas
        # No settings / purposes / rules / KB callbacks act in a group.
        assert not any(d.startswith("set:") or d == "nav:settings" for d in datas)


def test_group_start_supergroup_also_works(tmp_profile, fake_him):
    init_db()
    set_env(tmp_profile, home=f"{USER_A},{OTHER_GROUP}")
    upd, msg = command_update(USER_A, OTHER_GROUP, "supergroup")
    _run(app_mod.cmd_start(upd, ctx()))
    assert msg.sent and "Priority dashboard" in msg.sent[0][0]


def test_group_start_stranger_is_ignored_silently(tmp_profile, fake_him):
    init_db()
    set_env(tmp_profile)
    upd, msg = command_update(STRANGER, GROUP, "group")
    _run(app_mod.cmd_start(upd, ctx()))
    assert msg.sent == []  # no reply, nothing leaked


def test_group_dashboard_command_stranger_ignored(tmp_profile, fake_him):
    init_db()
    set_env(tmp_profile)
    upd, msg = command_update(STRANGER, GROUP, "group")
    _run(app_mod.cmd_dashboard(upd, ctx()))
    assert msg.sent == []


def test_unconfigured_group_gives_guidance_not_data(tmp_profile, fake_him):
    init_db()
    set_env(tmp_profile)
    upd, msg = command_update(USER_A, OTHER_GROUP, "supergroup")
    _run(app_mod.cmd_start(upd, ctx()))
    text = msg.sent[0][0]
    assert str(OTHER_GROUP) in text
    assert "not configured" in text.lower() or "whitelist" in text.lower()
    assert "Priority dashboard" not in text
    # /dashboard in the same unconfigured group is refused silently.
    upd2, msg2 = command_update(USER_A, OTHER_GROUP, "supergroup")
    _run(app_mod.cmd_dashboard(upd2, ctx()))
    assert msg2.sent == []


def test_unconfigured_group_stranger_gets_nothing_even_for_chatid(tmp_profile):
    set_env(tmp_profile)
    upd, msg = command_update(STRANGER, OTHER_GROUP, "group")
    _run(app_mod.cmd_chatid(upd, ctx()))
    _run(app_mod.cmd_start(upd, ctx()))
    assert msg.sent == []


def test_chatid_reports_id_for_authorized_user(tmp_profile):
    set_env(tmp_profile)
    upd, msg = command_update(USER_B, OTHER_GROUP, "supergroup")
    _run(app_mod.cmd_chatid(upd, ctx()))
    text = msg.sent[0][0]
    assert str(OTHER_GROUP) in text
    assert "whitelist" in text.lower() or "Admin" in text
    upd2, msg2 = command_update(USER_A, GROUP, "group")
    _run(app_mod.cmd_chatid(upd2, ctx()))
    assert "Configured as a notification target" in msg2.sent[0][0]


def test_group_fails_closed_when_allowlist_empty(tmp_profile, fake_him):
    init_db()
    set_env(tmp_profile, allowed="", home=f"{GROUP}")
    upd, msg = command_update(STRANGER, GROUP, "group")
    _run(app_mod.cmd_start(upd, ctx()))
    assert msg.sent == []
    # Private chats also fail closed: empty store is not "allow all".
    upd2, msg2 = command_update(STRANGER, STRANGER, "private")
    _run(app_mod.cmd_start(upd2, ctx()))
    assert any("Unauthorized" in t for t, _ in msg2.sent)


def test_group_text_messages_are_ignored(tmp_profile, fake_him):
    init_db()
    set_env(tmp_profile)
    upd, msg = command_update(USER_A, GROUP, "group")
    upd.effective_message.text = "Dashboard"
    upd.message.text = "Dashboard"
    _run(app_mod.on_text(upd, ctx()))
    assert msg.sent == []


def test_waiting_text_prompt_expires_after_ttl(tmp_profile, fake_him, monkeypatch):
    """A WAITING_TEXT prompt (e.g. 'Search') armed long ago must not silently
    consume an unrelated later message. Regression for the missing-TTL gap
    found during the pre-deployment audit: WAITING_TEXT entries never
    expired, so a stale 'Search' or 'Type my reply' prompt from hours/days
    earlier could capture an unrelated message as if it were live input."""
    init_db()
    set_env(tmp_profile)
    app_mod.WAITING_TEXT.clear()
    upd, msg = command_update(USER_A, USER_A, "private")
    upd.effective_message.text = "Search"
    upd.message.text = "Search"
    _run(app_mod.on_text(upd, ctx()))
    assert app_mod.WAITING_TEXT[USER_A]["mode"] == "search"

    # Simulate the prompt having been armed long before its TTL.
    app_mod.WAITING_TEXT[USER_A]["expires_at"] = __import__("time").time() - 1

    upd2, msg2 = command_update(USER_A, USER_A, "private")
    upd2.effective_message.text = "unrelated later message"
    upd2.message.text = "unrelated later message"
    _run(app_mod.on_text(upd2, ctx()))

    # The stale prompt must be gone, and the message must NOT have been
    # treated as a search query (do_search renders "Search: <query>" as the
    # page title — that title must never appear for this unrelated text).
    assert app_mod.WAITING_TEXT.get(USER_A) is None
    assert not any("Search: unrelated later message" in t for t, _ in msg2.sent)


def test_waiting_text_and_awaiting_slots_never_coexist(tmp_profile, fake_him, monkeypatch):
    """Regression for the cross-dict starvation gap found during the
    pre-deployment audit: WAITING_TEXT (app.py) and AWAITING
    (mailboxes.py) never used to touch each other, so a user who had an
    AWAITING slot armed (e.g. 'Edit purpose') and then separately armed a
    WAITING_TEXT slot (e.g. 'Search') would have one of the two prompts
    silently swallow their next unrelated message instead of either being
    cleanly cancelled. Arming either slot must now always clear the other
    non-secret slot, so at most one prompt is ever live for a user."""
    from mailops.bot import mailboxes as mb_bot

    init_db()
    set_env(tmp_profile)
    app_mod.WAITING_TEXT.clear()
    mb_bot.AWAITING.clear()

    # Arm an AWAITING slot directly (mirrors "Edit purpose" / "Search
    # mailboxes" — any non-secret mailboxes.py prompt).
    installed = mb_bot._set_awaiting(
        USER_A, {"token": "", "field": mb_bot._PURPOSE_FIELD, "account_id": "sales", "chat_id": USER_A}
    )
    assert installed
    assert USER_A in mb_bot.AWAITING

    # Now arm a WAITING_TEXT slot for the same user directly (mirrors
    # tapping "Search"). Going through the real text dispatcher here would
    # also exercise the (separate, pre-existing) fact that mailboxes.py's
    # free-text slot is checked before WAITING_TEXT's button-label check —
    # this test isolates just the cross-dict-clearing behavior of
    # ``_set_waiting`` itself.
    installed2 = app_mod._set_waiting(USER_A, {"mode": "search"})
    assert installed2

    # The WAITING_TEXT slot won: the AWAITING slot must have been cleared,
    # so the admin's next message cannot be silently absorbed as a purpose
    # edit they are no longer looking at.
    assert app_mod.WAITING_TEXT[USER_A]["mode"] == "search"
    assert USER_A not in mb_bot.AWAITING

    # And the reverse direction: re-arm AWAITING and confirm it clears the
    # now-live WAITING_TEXT slot in turn.
    mb_bot._set_awaiting(
        USER_A, {"token": "", "field": mb_bot._MB_SEARCH_FIELD, "chat_id": USER_A}
    )
    assert USER_A in mb_bot.AWAITING
    assert app_mod.WAITING_TEXT.get(USER_A) is None


# --- group callbacks -----------------------------------------------------------
def test_group_callback_dashboard_navigation_allowed(tmp_profile, fake_him, monkeypatch):
    """Group-first reading fix (Oct 2026): opening an email's body now
    works INSIDE the group via the ``gr:n:`` namespace (unrestricted
    mailboxes are group-shareable by default - mailops.policy.
    group_shareable). The legacy private-only ``n:`` namespace must never
    appear on a group-rendered keyboard - that would bypass the group
    allow-list / sharing-policy gate entirely."""
    init_db()
    set_env(tmp_profile)
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    fake_him.add_message("sales", "INBOX.P0-Critical", "1", subject="Security breach", sender="a@b.co")
    fake_him.add_message("sales", "INBOX.P0-Critical", "2", subject="Payment failed", sender="c@d.co")
    from mailops.cache import refresh_all

    refresh_all()
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)

    upd, q = callback_update("b:p0", USER_B, GROUP, "group")
    shared_ctx = ctx()
    _run(app_mod.on_callback(upd, shared_ctx))
    assert q.answers == [(None, False)]
    text, markup = q.edits[-1]
    assert "Security breach" in text  # subjects visible to authorized users
    datas = callback_datas(markup)
    assert not any(d.startswith("n:") for d in datas)  # legacy private-only namespace absent
    assert "gr:n:1" in datas and "gr:n:2" in datas  # group-first reading IS available
    assert all(app_mod.group_callback_allowed(d) for d in datas)
    assert "Open 1" in button_labels(markup)
    assert "dash" in datas  # can go back to the group dashboard

    # Tapping it opens the body IN THE GROUP (no DM redirect). Reuses the
    # same context as the list screen above: page_refs (which "gr:n:1"
    # resolves against) lives in context.user_data, exactly like a real
    # Telegram session's per-user state persists across their taps.
    upd2, q2 = callback_update("gr:n:1", USER_B, GROUP, "group")
    _run(app_mod.on_callback(upd2, shared_ctx))
    denied = [t for t, show in q2.answers if show and t == app_mod.GROUP_DENIED_ALERT]
    assert not denied
    assert q2.edits, f"gr:n:1 produced no screen: answers={q2.answers!r}"
    body_text, body_markup = q2.edits[-1]
    assert "Security breach" in body_text
    assert "Subject:" in body_text and "From:" in body_text and "Inbox:" in body_text
    body_datas = callback_datas(body_markup)
    assert "gr:reply:sales|INBOX.P0-Critical|1" in body_datas
    assert "gr:back" in body_datas
    assert "dash" in body_datas

    upd2, q2 = callback_update("dash", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd2, ctx()))
    assert "Priority dashboard" in q2.edits[-1][0]
    assert not any(d.startswith("set:") for d in callback_datas(q2.edits[-1][1]))


@pytest.mark.parametrize(
    "data",
    [
        "n:1",
        "open:sales|INBOX.P0-Critical|1",
        "reply:sales|INBOX.P0-Critical|1",
        "aidraft:sales|INBOX.P0-Critical|1",
        "writeme:sales|INBOX.P0-Critical|1",
        "send:sales|INBOX.P0-Critical|1",
        "done:sales|INBOX.P0-Critical|1",
        "mute:sales|INBOX.P0-Critical|1",
        "setprio:sales|INBOX.P0-Critical|1|3",
        "settag:sales|INBOX.P0-Critical|1|legal",
        "browse:sales|INBOX.P0-Critical|1|n",
        "nav:settings",
        "nav:accounts",
        "nav:flagged",
        "set:rules",
        "set:purposes",
        "set:kb",
        "set:context",
        "set:vip",
        "set:alertcycle",
        "set:synccycle",
        "acct:sales",
        "list:sales|INBOX.P0-Critical",
        "backpage",
        # GROUP-FIRST: mb:m / mb:add / mb:add:* are now allowed in group
        # for admins. mb:view/enable/disable/health/purpose/audit/dm_prompt
        # remain private-only (covered by test_mailboxes_group_callback_safe_mb_
        # is_allowed_for_admin in test_mailbox_onboarding.py).
    ],
)
def test_group_callback_private_only_actions_blocked(tmp_profile, monkeypatch, data):
    init_db()
    set_env(tmp_profile)
    monkeypatch.setattr(app_mod, "Himalaya", ExplodingHimalaya)
    app_mod.WAITING_TEXT.clear()
    upd, q = callback_update(data, USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, ctx()))
    assert q.answers == [(app_mod.GROUP_DENIED_ALERT, True)]
    assert q.edits == [] and q.message.sent == []
    assert app_mod.WAITING_TEXT == {}  # no pending private text-capture state


def test_group_callback_stranger_gets_private_alert_only(tmp_profile, fake_him):
    init_db()
    set_env(tmp_profile)
    upd, q = callback_update("b:p0", STRANGER, GROUP, "group")
    _run(app_mod.on_callback(upd, ctx()))
    assert q.answers == [("Unauthorized.", True)]
    assert q.edits == [] and q.message.sent == []  # nothing posted in the group


def test_group_callback_from_unconfigured_group_denied(tmp_profile, fake_him):
    init_db()
    set_env(tmp_profile)
    upd, q = callback_update("dash", USER_A, OTHER_GROUP, "supergroup")
    _run(app_mod.on_callback(upd, ctx()))
    assert q.answers == [("Unauthorized.", True)]
    assert q.edits == []


def test_group_callback_allowlist_matches_group_keyboards(tmp_profile, fake_him):
    """Every callback a group keyboard can emit must be group-allowed."""
    from mailops.cache import AccountCounts

    counts = [AccountCounts("sales", "sales@example.com", 1, 2, 3, 4)]
    markups = [
        kb.group_dashboard_keyboard(counts),
        kb.group_alert_keyboard("ourmailagent_bot"),
        kb.page_keyboard(1, 3, 10, group=True),
    ]
    for m in markups:
        for d in callback_datas(m):
            assert app_mod.group_callback_allowed(d), d


def test_group_callback_body_pagination_reply_aidraft_send_journey(
    tmp_profile, fake_him, monkeypatch
):
    """Full group-first reading+reply+send journey (Phase 1/3), unrestricted
    mailbox: open (with body pagination) -> Reply -> AI draft -> Send
    preview -> Send confirm. Nothing here should ever require a DM."""
    init_db()
    set_env(tmp_profile)
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    monkeypatch.setenv("MAILOPS_TEST_MODE", "0")
    long_body = "A" * 7000  # forces >1 body page at BODY_PAGE_SIZE=3000
    fake_him.add_message(
        "sales", "INBOX.P0-Critical", "1", subject="Security breach", sender="a@b.co"
    )
    fake_him.store["sales"]["INBOX.P0-Critical"]["1"]["body"] = long_body

    def fake_read_card(self, account, msg_id, mailbox="INBOX"):
        from mailops.mail_format import MailCard

        env = self.store.get(account, {}).get(mailbox, {}).get(str(msg_id))
        return MailCard("", env["from"][0]["email"], env["subject"], "", long_body)

    monkeypatch.setattr(type(fake_him), "read_card", fake_read_card, raising=False)
    from mailops.cache import refresh_all

    refresh_all()
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    from tests.conftest import remember_all

    remember_all(fake_him)

    shared_ctx = ctx()
    upd, q = callback_update("b:p0", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, shared_ctx))
    assert "gr:n:1" in callback_datas(q.edits[-1][1])

    # Open: body is paginated (BODY_PAGE_SIZE=3000, body is 7000 chars).
    upd2, q2 = callback_update("gr:n:1", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd2, shared_ctx))
    body_text, body_markup = q2.edits[-1]
    body_datas = callback_datas(body_markup)
    assert any(d.startswith("gr:bp:") for d in body_datas)
    assert "Body 1/3" in button_labels(body_markup)

    # Page forward through the body.
    next_page_cb = next(d for d in body_datas if d.startswith("gr:bp:"))
    upd3, q3 = callback_update(next_page_cb, USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd3, shared_ctx))
    assert "Body 2/3" in button_labels(q3.edits[-1][1])

    # Reply -> AI draft (mocked HTTP) -> Send preview -> confirm send.
    upd4, q4 = callback_update("gr:reply:sales|INBOX.P0-Critical|1", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd4, shared_ctx))
    reply_datas = callback_datas(q4.edits[-1][1])
    assert "gr:aidraft:sales|INBOX.P0-Critical|1" in reply_datas
    assert "gr:writeme:sales|INBOX.P0-Critical|1" in reply_datas

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "Thanks for flagging this."}}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            return FakeResp()

    env = tmp_profile / ".env"
    env.write_text(
        env.read_text(encoding="utf-8") + "OPENAI_API_KEY=sk-test\nOPENAI_MODEL=gpt-4o-mini\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(app_mod.httpx, "AsyncClient", FakeClient)
    upd5, q5 = callback_update("gr:aidraft:sales|INBOX.P0-Critical|1", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd5, shared_ctx))
    draft_text, draft_markup = q5.edits[-1]
    assert "Thanks for flagging this." in draft_text
    draft_datas = callback_datas(draft_markup)
    assert "gr:send:sales|INBOX.P0-Critical|1" in draft_datas

    from mailops.policy import policy_for

    assert policy_for("sales").send  # tmp_profile fixture enables send for 'sales'
    upd6, q6 = callback_update("gr:send:sales|INBOX.P0-Critical|1", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd6, shared_ctx))
    confirm_text, confirm_markup = q6.edits[-1]
    confirm_datas = callback_datas(confirm_markup)
    go_cb = next(d for d in confirm_datas if d.startswith("gr:go:"))
    assert any(d.startswith("gr:nogo:") for d in confirm_datas)

    upd7, q7 = callback_update(go_cb, USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd7, shared_ctx))
    assert len(fake_him.sent) == 1
    assert fake_him.sent[0]["body"] == "Thanks for flagging this."


def test_group_search_uses_forcereply_and_binds_to_prompt(tmp_profile, fake_him, monkeypatch):
    """gr:search arms a ForceReply prompt (Telegram privacy mode safe) bound
    to (user, chat, prompt message id) - exactly like gr:writeme:."""
    init_db()
    set_env(tmp_profile)
    fake_him.add_message("sales", "INBOX.P1-Important", "9", subject="ad rates?", sender="z@z.co")
    from mailops.cache import refresh_all

    refresh_all()
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    app_mod.WAITING_TEXT.clear()

    upd, q = callback_update("gr:search", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, ctx()))
    assert q.message.sent, "gr:search must send a ForceReply prompt"
    prompt_text, prompt_markup = q.message.sent[-1]
    from telegram import ForceReply

    assert isinstance(prompt_markup, ForceReply)
    pending = app_mod.WAITING_TEXT[USER_A]
    assert pending["mode"] == "search" and pending["group"] is True
    prompt_id = pending["prompt_message_id"]

    # A reply to a DIFFERENT message id must NOT be consumed as the query.
    reply_msg = FakeMessage()
    reply_msg.text = "rates"
    reply_msg.reply_to_message = SimpleNamespace(message_id=prompt_id + 1)
    upd_bad = SimpleNamespace(
        callback_query=None,
        effective_user=SimpleNamespace(id=USER_A),
        effective_chat=chat(GROUP, "group"),
        effective_message=reply_msg,
    )
    handled = _run(app_mod._handle_group_waiting_text(upd_bad, ctx()))
    assert handled is False
    assert app_mod.WAITING_TEXT.get(USER_A) is not None  # slot still armed

    # A genuine reply to the prompt message IS consumed, runs the search,
    # and the search renders using the group-first ("gr:n:") list keyboard.
    reply_msg_ok = FakeMessage()
    reply_msg_ok.text = "ad rates"
    reply_msg_ok.reply_to_message = SimpleNamespace(message_id=prompt_id)
    upd_ok = SimpleNamespace(
        callback_query=None,
        effective_user=SimpleNamespace(id=USER_A),
        effective_chat=chat(GROUP, "group"),
        effective_message=reply_msg_ok,
    )
    shared_ctx = ctx()
    handled2 = _run(app_mod._handle_group_waiting_text(upd_ok, shared_ctx))
    assert handled2 is True
    assert app_mod.WAITING_TEXT.get(USER_A) is None  # consumed, slot cleared
    assert reply_msg_ok.sent, "search results must be posted back in the group"
    result_text, result_markup = reply_msg_ok.sent[-1]
    assert "Search: ad rates" in result_text
    assert "gr:n:1" in callback_datas(result_markup)


def test_group_restricted_mailbox_never_renders_body_or_reply_or_send(
    tmp_profile, fake_him, monkeypatch
):
    """A mailbox opted into ``group_restricted_accounts`` must never have its
    subject-list row open inline, nor its Reply/AI-draft/Send buttons act
    in the group - every one of those taps must mint a private deep link
    instead (Phase 1 'restricted or sensitive' requirement)."""
    from mailops.policy import set_group_restricted

    init_db()
    set_env(tmp_profile)
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    set_group_restricted("sales", True)
    fake_him.add_message(
        "sales", "INBOX.P0-Critical", "1", subject="Confidential HR matter", sender="a@b.co"
    )
    from mailops.cache import refresh_all

    refresh_all()
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    from tests.conftest import remember_all

    remember_all(fake_him)

    shared_ctx = ctx()
    upd, q = callback_update("b:p0", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, shared_ctx))
    text, markup = q.edits[-1]
    datas = callback_datas(markup)
    # Restricted row shows the locked private-only button, never gr:n:.
    assert "gr:n:1" not in datas
    assert "gr:restricted:1" in datas
    assert "🔒 1 (private)" in button_labels(markup)
    # Subjects are already group-visible via the dashboard/digest (policy.py
    # "Group-read sharing policy": restriction gates BODY/reply/send content
    # exposure, not the subject line already shown in lists elsewhere) - so
    # only the full body/reply/send surface is asserted restricted below.

    for data in (
        "gr:restricted:1",
        "gr:o:sales|INBOX.P0-Critical|1",
        "gr:reply:sales|INBOX.P0-Critical|1",
        "gr:aidraft:sales|INBOX.P0-Critical|1",
        "gr:send:sales|INBOX.P0-Critical|1",
    ):
        upd_r, q_r = callback_update(data, USER_A, GROUP, "group")
        _run(app_mod.on_callback(upd_r, shared_ctx))
        edit_text, edit_markup = q_r.edits[-1]
        assert "Confidential HR matter" not in edit_text
        # Every restricted tap must offer the one-shot private link, never
        # an inline body/reply/send surface.
        urls = [b.url for row in edit_markup.inline_keyboard for b in row if b.url]
        assert urls, f"{data} did not mint a private deep link"


def test_group_waiting_text_isolated_per_user(tmp_profile, fake_him, monkeypatch):
    """Two authorized users in the same group arming gr:writeme: for
    DIFFERENT emails must never have their typed replies cross-contaminate
    - each WAITING_TEXT slot is keyed by uid and bound to its own prompt
    message id."""
    init_db()
    set_env(tmp_profile)
    fake_him.add_message("sales", "INBOX.P1-Important", "1", subject="A", sender="a@a.co")
    fake_him.add_message("info", "INBOX.P1-Important", "2", subject="B", sender="b@b.co")
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    app_mod.WAITING_TEXT.clear()

    upd_a, q_a = callback_update("gr:writeme:sales|INBOX.P1-Important|1", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd_a, ctx()))
    upd_b, q_b = callback_update("gr:writeme:info|INBOX.P1-Important|2", USER_B, GROUP, "group")
    _run(app_mod.on_callback(upd_b, ctx()))

    pending_a = app_mod.WAITING_TEXT[USER_A]
    pending_b = app_mod.WAITING_TEXT[USER_B]
    assert pending_a["account_id"] == "sales" and pending_b["account_id"] == "info"
    assert pending_a["prompt_message_id"] != pending_b["prompt_message_id"]

    # USER_B replying to USER_A's prompt must not be consumed as USER_A's draft
    # (bound to uid AND exact prompt id) - and must not touch USER_A's slot.
    reply_msg = FakeMessage()
    reply_msg.text = "I'll handle this"
    reply_msg.reply_to_message = SimpleNamespace(message_id=pending_a["prompt_message_id"])
    upd_cross = SimpleNamespace(
        callback_query=None,
        effective_user=SimpleNamespace(id=USER_B),
        effective_chat=chat(GROUP, "group"),
        effective_message=reply_msg,
    )
    handled = _run(app_mod._handle_group_waiting_text(upd_cross, ctx()))
    assert handled is False  # USER_B has no pending slot bound to that prompt id
    assert app_mod.WAITING_TEXT.get(USER_A) == pending_a  # untouched
    assert app_mod.WAITING_TEXT.get(USER_B) == pending_b  # untouched

    # USER_A correctly replying to their own prompt saves their own draft only.
    from mailops.db import get_draft

    reply_msg_a = FakeMessage()
    reply_msg_a.text = "On it, will respond today."
    reply_msg_a.reply_to_message = SimpleNamespace(message_id=pending_a["prompt_message_id"])
    upd_a2 = SimpleNamespace(
        callback_query=None,
        effective_user=SimpleNamespace(id=USER_A),
        effective_chat=chat(GROUP, "group"),
        effective_message=reply_msg_a,
    )
    handled2 = _run(app_mod._handle_group_waiting_text(upd_a2, ctx()))
    assert handled2 is True
    assert app_mod.WAITING_TEXT.get(USER_A) is None
    assert app_mod.WAITING_TEXT.get(USER_B) == pending_b  # USER_B's slot unaffected
    saved = get_draft(USER_A)
    assert saved and saved["body"] == "On it, will respond today." and saved["account_id"] == "sales"
    assert get_draft(USER_B) is None  # USER_B never got a draft from USER_A's reply


# --- LIVE UAT REGRESSION: every group email-list screen must be readable ------
# Root cause of the Oct 25, 2026 failed user-acceptance test
# ("Dashboard -> Important -> coinography2" showed 8 emails, the text
# "Open an email in a private chat with the bot", and ONLY Page/Back/
# Refresh buttons): two separate defects.
#   1. The LIVE process was started Oct 24 15:24 and therefore ran the
#      pre-fix on-disk code (Python does not hot-reload) - no restart had
#      happened after the group-first work landed.
#   2. A real code defect that a restart alone would NOT have fully fixed:
#      ``format_page`` took ONE ``group`` flag that callers passed as
#      ``in_group or not can_read``, so a group list that really did carry
#      working gr:n:<N> buttons still printed the "open it in a private
#      chat" instruction under them; and ``show_flagged`` /
#      ``show_list_clean`` had NO group branch at all, emitting private
#      ``n:<N>`` buttons that the group dispatcher refuses outright.
# These tests assert TEXT and BUTTONS TOGETHER for every list renderer,
# which is what the previous (passing) tests failed to do.
def _seed_two_accounts(fake_him, monkeypatch, *, account="coinography2"):
    accounts = [
        {"id": account, "email": f"{account}@example.com", "display_name": "Coinography2", "default": True},
        {"id": "info", "email": "info@example.com", "display_name": "Info"},
    ]
    import json

    from mailops import paths

    paths.ACCOUNTS_JSON.write_text(json.dumps(accounts), encoding="utf-8")
    import mailops.config as config_mod

    config_mod.load_accounts.cache_clear() if hasattr(
        config_mod.load_accounts, "cache_clear"
    ) else None
    for n in range(1, 9):  # the live screen showed 8 emails
        fake_him.add_message(
            account, "INBOX.P1-Important", str(n), subject=f"Important {n}", sender=f"s{n}@x.co"
        )
    # Seed every priority folder too, so the parametrized All Critical /
    # Normal / Noise and digest screens below are non-empty (an empty
    # bucket legitimately renders no Open buttons and would prove nothing).
    for folder, prefix, base in (
        ("INBOX.P0-Critical", "Critical", 100),
        ("INBOX.P2-Normal", "Normal", 200),
        ("INBOX.P3-Noise", "Noise", 300),
    ):
        for n in range(1, 4):
            fake_him.add_message(
                account, folder, str(base + n), subject=f"{prefix} {n}", sender=f"s{n}@x.co"
            )
    from mailops.cache import refresh_all

    refresh_all()
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    from tests.conftest import remember_all

    remember_all(fake_him)


@pytest.mark.parametrize(
    "data,label",
    [
        ("b:p0", "All Critical"),
        ("b:p1", "All Important"),
        ("b:p2", "All Normal"),
        ("b:p3", "All Noise"),
        ("ab:coinography2|p1", "coinography2 Important (the exact failing screen)"),
        ("g:all", "All Emails"),
        ("dg:crit", "digest View Critical"),
        ("dg:imp", "digest View Important"),
        ("dg:norm", "digest View Normal"),
        ("dg:noise", "digest View Noise"),
        ("dg:open:coinography2", "digest Open Inbox"),
    ],
)
def test_every_group_email_list_screen_has_open_buttons(
    tmp_profile, fake_him, monkeypatch, data, label
):
    """LIVE UAT regression: EVERY group-reachable email-list screen must
    render tappable Open buttons AND must not tell the user to go to a
    private chat. This is the assertion whose absence let the live failure
    ship green."""
    init_db()
    set_env(tmp_profile)
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    _seed_two_accounts(fake_him, monkeypatch)

    shared_ctx = ctx()
    upd, q = callback_update(data, USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, shared_ctx))
    assert q.answers and q.answers[0] == (None, False), f"{label}: callback was denied"
    assert q.edits, f"{label}: rendered nothing"
    text, markup = q.edits[-1]
    datas = callback_datas(markup)

    # 1. The stale DM instruction must be gone for group-readable mail.
    assert "private chat with the bot" not in text, f"{label}: still redirects to DM"
    # 2. Tappable per-email Open buttons must exist.
    assert "gr:n:1" in datas, f"{label}: no Open button (datas={datas})"
    assert "Open 1" in button_labels(markup), f"{label}: no 'Open N' label"
    # 3. Never the private-only namespace, which the group dispatcher denies.
    assert not any(d.startswith("n:") for d in datas), f"{label}: leaked private n: buttons"
    # 4. Everything on the keyboard must be group-allowed.
    for d in datas:
        assert app_mod.group_callback_allowed(d), f"{label}: {d} not group-allowed"

    # 5. And the Open button really opens the body IN THIS GROUP.
    upd2, q2 = callback_update("gr:n:1", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd2, shared_ctx))
    assert q2.edits, f"{label}: gr:n:1 opened nothing"
    body_text, body_markup = q2.edits[-1]
    assert "Subject:" in body_text and "From:" in body_text
    body_datas = callback_datas(body_markup)
    assert any(d.startswith("gr:reply:") for d in body_datas), f"{label}: no Reply button"
    assert "gr:back" in body_datas, f"{label}: no Back button"
    assert "dash" in body_datas, f"{label}: no Home button"

    # 6. Back returns to a list that still has Open buttons (no dead end).
    upd3, q3 = callback_update("gr:back", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd3, shared_ctx))
    back_text, back_markup = q3.edits[-1]
    assert "gr:n:1" in callback_datas(back_markup), f"{label}: Back lost the Open buttons"
    assert "private chat with the bot" not in back_text


def test_group_needs_reply_and_folder_list_are_group_readable(
    tmp_profile, fake_him, monkeypatch
):
    """``show_flagged`` ("Needs reply") and ``show_list_clean`` (a non-priority
    folder listing) had NO group branch at all before this fix, so in a group
    they emitted private ``n:<N>`` buttons. Both now route through
    ``_list_render`` like every other list screen."""
    init_db()
    set_env(tmp_profile)
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    _seed_two_accounts(fake_him, monkeypatch)
    # Flag one message so "Needs reply" is non-empty.
    env = fake_him.store["coinography2"]["INBOX.P1-Important"]["1"]
    env["flags"] = [{"iana": "flagged"}]

    upd, _ = callback_update("noop", USER_A, GROUP, "group")
    shared_ctx = ctx()
    _run(app_mod.show_flagged(upd, shared_ctx))
    text, markup = upd.callback_query.edits[-1]
    datas = callback_datas(markup)
    assert "gr:n:1" in datas and not any(d.startswith("n:") for d in datas)
    assert "private chat with the bot" not in text
    for d in datas:
        assert app_mod.group_callback_allowed(d), d

    # Non-priority folder listing in a group.
    fake_him.add_message("coinography2", "INBOX.Archive", "77", subject="old", sender="o@o.co")
    from tests.conftest import remember_all

    remember_all(fake_him)
    upd2, _ = callback_update("noop", USER_A, GROUP, "group")
    _run(app_mod.show_list_clean(upd2, ctx(), "coinography2", "INBOX.Archive"))
    text2, markup2 = upd2.callback_query.edits[-1]
    datas2 = callback_datas(markup2)
    assert "gr:n:1" in datas2 and not any(d.startswith("n:") for d in datas2)
    assert "private chat with the bot" not in text2


def test_viewer_role_group_list_has_no_open_buttons_and_says_why(
    tmp_profile, fake_him, monkeypatch
):
    """The ``can_open`` half of the split flag: a viewer-role user (no
    P_READ) must still get NO Open buttons and summary-only copy - in a
    group or in private. Splitting group/can_open must not have widened
    access for low-privilege roles."""
    from mailops import access

    init_db()
    set_env(tmp_profile)
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    _seed_two_accounts(fake_him, monkeypatch)
    access.set_role(USER_A, USER_B, "viewer")  # owner demotes the operator
    assert not access.can(USER_B, access.P_READ)

    upd, q = callback_update("b:p1", USER_B, GROUP, "group")
    _run(app_mod.on_callback(upd, ctx()))
    text, markup = q.edits[-1]
    datas = callback_datas(markup)
    assert not any(d.startswith(("gr:n:", "n:", "gr:restricted:")) for d in datas)
    assert "Summary view" in text
    assert "higher role" in text


def test_group_restricted_account_list_masks_but_others_stay_open(
    tmp_profile, fake_him, monkeypatch
):
    """Mixed page: a restricted mailbox's row is locked to a private deep
    link while every permitted row stays inline-readable in the group."""
    from mailops.policy import set_group_restricted

    init_db()
    set_env(tmp_profile)
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    _seed_two_accounts(fake_him, monkeypatch)
    fake_him.add_message("info", "INBOX.P1-Important", "50", subject="HR private", sender="hr@x.co")
    from mailops.cache import refresh_all

    refresh_all()
    from tests.conftest import remember_all

    remember_all(fake_him)
    set_group_restricted("info", True)

    upd, q = callback_update("b:p1", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, ctx()))
    _text, markup = q.edits[-1]
    datas = callback_datas(markup)
    labels = button_labels(markup)
    # The restricted row is locked...
    assert any(d.startswith("gr:restricted:") for d in datas)
    assert any("(private)" in lab for lab in labels)
    # ...while permitted rows remain directly readable in the group.
    assert "gr:n:1" in datas


def test_group_alert_keyboard_has_private_deeplink():
    m = kb.group_alert_keyboard("ourmailagent_bot")
    urls = [b.url for row in m.inline_keyboard for b in row if b.url]
    assert urls == ["https://t.me/ourmailagent_bot"]
    assert kb.group_alert_keyboard(None).inline_keyboard[0][0].callback_data == "dash"


# --- private chats keep working -------------------------------------------------
def test_private_start_for_both_users_has_full_dashboard(tmp_profile, fake_him):
    init_db()
    set_env(tmp_profile)
    for user in (USER_A, USER_B):
        upd, msg = command_update(user, user, "private")
        _run(app_mod.cmd_start(upd, ctx()))
        assert len(msg.sent) == 2  # menu message + dashboard
        assert isinstance(msg.sent[0][1], ReplyKeyboardMarkup)
        dash_text, dash_markup = msg.sent[1]
        assert "Priority dashboard" in dash_text
        datas = callback_datas(dash_markup)
        assert "set:rules" in datas and "nav:settings" in datas  # full controls


def test_private_stranger_still_unauthorized(tmp_profile):
    set_env(tmp_profile)
    upd, msg = command_update(STRANGER, STRANGER, "private")
    _run(app_mod.cmd_start(upd, ctx()))
    assert msg.sent and msg.sent[0][0].startswith("Unauthorized.")
    assert str(STRANGER) in msg.sent[0][0]


def test_private_callback_open_still_routes_to_mailbox(tmp_profile, fake_him, monkeypatch):
    init_db()
    set_env(tmp_profile)
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    fake_him.add_message("sales", "INBOX.P1-Important", "5", subject="rates?", sender="b@b.co")
    for user in (USER_A, USER_B):
        upd, q = callback_update("open:sales|INBOX.P1-Important|5", user, user, "private")
        _run(app_mod.on_callback(upd, ctx()))
        assert q.edits, f"user {user} could not open a message in private chat"
        assert "rates?" in q.edits[-1][0]


def test_private_bucket_page_keeps_open_buttons(tmp_profile, fake_him, monkeypatch):
    init_db()
    set_env(tmp_profile)
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    fake_him.add_message("sales", "INBOX.P0-Critical", "1", subject="Security breach", sender="a@b.co")
    from mailops.cache import refresh_all

    refresh_all()
    upd, q = callback_update("b:p0", USER_A, USER_A, "private")
    _run(app_mod.on_callback(upd, ctx()))
    assert "n:1" in callback_datas(q.edits[-1][1])


# --- notifications --------------------------------------------------------------
class FakeBot:
    username = "ourmailagent_bot"

    def __init__(self, fail_for=()):
        self.fail_for = set(fail_for)
        self.sent: list[tuple] = []

    async def send_message(self, chat_id=None, text=None, reply_markup=None):
        if chat_id in self.fail_for:
            raise RuntimeError(f"chat {chat_id} not found")
        self.sent.append((chat_id, text, reply_markup))


NEW_MAIL = {
    "type": "new_mail",
    "key": "k-group-1",
    "account_id": "sales",
    "email": "sales@example.com",
    "msg_id": "1",
    "mailbox": "INBOX.P1-Important",
    "subject": "Ad rates",
    "sender": "a@b.co",
    "priority": 1,
    "reason": "rule",
}


def test_new_mail_goes_to_user_and_group_with_separate_keyboards(tmp_profile):
    from mailops.notify import handle_event

    init_db()
    set_env(tmp_profile)
    bot = FakeBot()
    _run(handle_event(bot, dict(NEW_MAIL)))
    by_chat = {c: (t, m) for c, t, m in bot.sent}
    assert set(by_chat) == {USER_A, GROUP}
    # Private chat gets full actions.
    private_datas = callback_datas(by_chat[USER_A][1])
    assert any(d.startswith("open:") for d in private_datas)
    assert any(d.startswith("reply:") for d in private_datas)
    # Group gets read-only keyboard: no open/reply/done/priority buttons.
    group_markup = by_chat[GROUP][1]
    group_datas = callback_datas(group_markup)
    assert group_datas == ["dash"]
    assert all(app_mod.group_callback_allowed(d) for d in group_datas)
    assert any(b.url for row in group_markup.inline_keyboard for b in row)
    # Group alert carries metadata only.
    assert "Ad rates" in by_chat[GROUP][0] and "body" not in by_chat[GROUP][0].lower()


def test_unreachable_group_does_not_block_private_delivery(tmp_profile):
    """Bot not (yet) in the group => 'chat not found' for that target only."""
    from mailops.db import already_notified
    from mailops.notify import handle_event

    init_db()
    set_env(tmp_profile)
    bot = FakeBot(fail_for={GROUP})
    _run(handle_event(bot, dict(NEW_MAIL)))
    assert [c for c, _, _ in bot.sent] == [USER_A]
    assert already_notified("k-group-1")  # counted as delivered


def test_all_targets_failing_raises_so_alert_stays_queued(tmp_profile):
    from mailops.db import already_notified
    from mailops.notify import handle_event

    init_db()
    set_env(tmp_profile)
    bot = FakeBot(fail_for={USER_A, GROUP})
    with pytest.raises(RuntimeError):
        _run(handle_event(bot, dict(NEW_MAIL)))
    assert not already_notified("k-group-1")


def test_no_duplicate_alert_after_success(tmp_profile):
    from mailops.notify import handle_event

    init_db()
    set_env(tmp_profile)
    bot = FakeBot()
    _run(handle_event(bot, dict(NEW_MAIL)))
    _run(handle_event(bot, dict(NEW_MAIL)))
    assert len(bot.sent) == 2  # one per target, exactly once


def test_group_only_home_channel_notifies_group(tmp_profile):
    from mailops.notify import send_to_home

    set_env(tmp_profile, home=f"{GROUP}")
    bot = FakeBot()
    _run(send_to_home(bot, "hello"))
    assert [c for c, _, _ in bot.sent] == [GROUP]


def test_home_channel_unset_falls_back_to_first_user(tmp_profile):
    from mailops.notify import send_to_home

    set_env(tmp_profile, home="")
    bot = FakeBot()
    _run(send_to_home(bot, "hello"))
    assert [c for c, _, _ in bot.sent] == [USER_A]


def test_digest_uses_group_variant_for_group(tmp_profile, fake_him):
    from mailops.notify import send_dashboard_digest

    init_db()
    set_env(tmp_profile)
    bot = FakeBot()
    _run(send_dashboard_digest(bot, note="Auto-check complete"))
    by_chat = {c: (t, m) for c, t, m in bot.sent}
    private_text, private_markup = by_chat[USER_A]
    group_text, group_markup = by_chat[GROUP]
    assert "set:rules" in callback_datas(private_markup)
    assert not any(d.startswith("set:") for d in callback_datas(group_markup))
    assert "Auto-check complete" in group_text
    assert "Set inbox purposes" not in group_text and "purposes" not in group_text.lower().split("group view")[0]
    assert "private chat" in group_text.lower()


# --- handler registration (command routing incl. @botname) ---------------------
def _app_with_bot_username(username="ourmailagent_bot"):
    from telegram import User

    application = app_mod.build_application()
    application.bot._bot_user = User(
        id=7653116966, is_bot=True, first_name="mailagentbot", username=username
    )
    return application


def _text_update(application, text, chat_id, chat_type="supergroup", user_id=USER_A):
    from telegram import Update

    data = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 1700000000,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": False, "first_name": "z"},
            "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}],
        },
    }
    return Update.de_json(data, application.bot)


def _matching_commands(application, update):
    from telegram.ext import CommandHandler

    hits = []
    for group in application.handlers.values():
        for h in group:
            if isinstance(h, CommandHandler) and h.check_update(update):
                hits.append(sorted(h.commands)[0])
    return hits


def test_start_with_correct_bot_suffix_routes_to_start(tmp_profile):
    application = _app_with_bot_username()
    upd = _text_update(application, "/start@ourmailagent_bot", GROUP)
    assert _matching_commands(application, upd) == ["start"]


def test_start_addressed_to_other_bot_is_not_handled(tmp_profile):
    application = _app_with_bot_username()
    upd = _text_update(application, "/start@mailagentbot", GROUP)
    assert _matching_commands(application, upd) == []


def test_dashboard_and_chatid_commands_registered(tmp_profile):
    application = _app_with_bot_username()
    for cmd in ("/dashboard@ourmailagent_bot", "/chatid@ourmailagent_bot", "/dashboard", "/chatid"):
        upd = _text_update(application, cmd, GROUP)
        assert _matching_commands(application, upd) == [cmd.split("@")[0].lstrip("/")]


def test_my_chat_member_and_error_handlers_registered(tmp_profile):
    from telegram.ext import ChatMemberHandler

    application = app_mod.build_application()
    kinds = [type(h) for group in application.handlers.values() for h in group]
    assert ChatMemberHandler in kinds
    assert app_mod.on_error in application.error_handlers


def test_error_handler_swallows_conflict_quietly(caplog):
    from telegram.error import Conflict

    with caplog.at_level("WARNING", logger="mailops.bot"):
        _run(app_mod.on_error(None, SimpleNamespace(error=Conflict("terminated by other getUpdates"))))
    assert any("another getUpdates consumer" in r.message for r in caplog.records)
    assert not any(r.levelname == "ERROR" for r in caplog.records)
