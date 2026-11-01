from __future__ import annotations

import asyncio
from types import SimpleNamespace

import mailops.bot.app as app_mod
from mailops.db import clear_draft, get_draft, init_db, save_draft
from tests.conftest import remember_all


class FakeMessage:
    def __init__(self):
        self.sent: list[tuple] = []

    async def reply_text(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))


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


def make_update(data, user_id=111):
    q = FakeQuery(data)
    update = SimpleNamespace(
        callback_query=q,
        effective_user=SimpleNamespace(id=user_id),
        effective_message=q.message,
    )
    return update, q


def make_context():
    return SimpleNamespace(user_data={})


def _run(coro):
    return asyncio.run(coro)


def _press(data, user_id=111):
    update, q = make_update(data, user_id)
    _run(app_mod.on_callback(update, make_context()))
    return q


def _go(q):
    for _text, markup in reversed(q.edits):
        if markup is None:
            continue
        for row in markup.inline_keyboard:
            for btn in row:
                data = btn.callback_data or ""
                if data.startswith("go:"):
                    return data
    raise AssertionError("final send button was not shown")


def test_unauthorized_user_rejected(tmp_profile):
    update, q = make_update("dash", user_id=999)
    _run(app_mod.on_callback(update, make_context()))
    assert any("Unauthorized" in t for t, _ in q.message.sent)


def test_send_without_draft_blocked(tmp_profile, fake_him, monkeypatch):
    init_db()
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    clear_draft(111)
    q = _press("send:sales|INBOX|1")
    assert any("No draft" in (t or "") for t, _ in q.answers)
    assert fake_him.sent == []


def test_send_with_draft_delivers_and_files(tmp_profile, fake_him, monkeypatch):
    init_db()
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    monkeypatch.setenv("MAILOPS_TEST_MODE", "0")
    fake_him.add_message("sales", "INBOX.P1-Important", "5", subject="rates?", sender="b@b.co")
    remember_all(fake_him)
    save_draft(111, "sales", "INBOX.P1-Important", "5", "Thanks, our rates are...")
    preview = _press("send:sales|INBOX.P1-Important|5")
    shown = preview.edits[-1][0]
    assert "Final confirmation" in shown
    assert "To: b@b.co" in shown
    assert "From: sales@example.com" in shown
    assert "Subject: Re: rates?" in shown
    assert "Thanks, our rates are..." in shown
    assert fake_him.sent == []  # preview never submits SMTP
    sent = _press(_go(preview))
    assert len(fake_him.sent) == 1
    assert fake_him.sent[0]["body"] == "Thanks, our rates are..."
    # original moved to Waiting, draft cleared
    assert len(fake_him.envelopes("sales", "INBOX.Waiting")) == 1
    assert get_draft(111) is None
    assert any("Sent via" in t for t, _ in sent.edits)
    again = _press(_go(preview))
    assert len(fake_him.sent) == 1


def test_send_blocked_in_test_mode(tmp_profile, fake_him, monkeypatch):
    init_db()
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    monkeypatch.setenv("MAILOPS_TEST_MODE", "1")
    fake_him.add_message("sales", "INBOX.P1-Important", "5", subject="rates?", sender="b@b.co")
    remember_all(fake_him)
    save_draft(111, "sales", "INBOX.P1-Important", "5", "body")
    q = _press("send:sales|INBOX.P1-Important|5")
    assert fake_him.sent == []
    assert any("TEST MODE" in t for t, _ in q.edits)
    # message untouched
    assert len(fake_him.envelopes("sales", "INBOX.P1-Important")) == 1


def test_send_failure_keeps_draft(tmp_profile, fake_him, monkeypatch):
    init_db()
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    monkeypatch.setenv("MAILOPS_TEST_MODE", "0")

    from mailops.himalaya_client import RunResult

    calls = []

    def fail_send(*a, **k):
        calls.append(1)
        return RunResult(1, "", "smtp auth failed")

    monkeypatch.setattr(fake_him, "reply_send", fail_send)
    fake_him.add_message("sales", "INBOX.P1-Important", "5", subject="x", sender="b@b.co")
    remember_all(fake_him)
    save_draft(111, "sales", "INBOX.P1-Important", "5", "body")
    preview = _press("send:sales|INBOX.P1-Important|5")
    assert calls == []
    q = _press(_go(preview))
    assert calls == [1]
    assert get_draft(111) is not None  # clear failure keeps the draft
    assert any("Send failed" in t for t, _ in q.edits)
    _press(_go(preview))
    assert calls == [1]  # the same approval cannot submit again


def test_done_moves_to_handled(tmp_profile, fake_him, monkeypatch):
    init_db()
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    fake_him.add_message("sales", "INBOX.P1-Important", "9", subject="x", sender="b@b.co")
    remember_all(fake_him)
    update, q = make_update("done:sales|INBOX.P1-Important|9")
    ctx = make_context()
    _run(app_mod.on_callback(update, ctx))
    assert len(fake_him.envelopes("sales", "INBOX.Handled")) == 1
    assert fake_him.envelopes("sales", "INBOX.P1-Important") == []


def test_snooze_acknowledges_without_mailbox_change(tmp_profile, fake_him, monkeypatch):
    init_db()
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    fake_him.add_message("sales", "INBOX", "1", subject="hello", sender="a@b.co")
    remember_all(fake_him)
    update, q = make_update("snooze:sales|INBOX|1|1h")
    _run(app_mod.on_callback(update, make_context()))
    assert fake_him.envelopes("sales", "INBOX")
    assert fake_him.mutations == []
    joined = " ".join(t for t, _ in q.edits)
    assert "Snooze noted" in joined
    assert "mailbox was changed" in joined.lower()


def test_setprio_moves_message(tmp_profile, fake_him, monkeypatch):
    init_db()
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    fake_him.add_message("sales", "INBOX.P2-Normal", "3", subject="x", sender="b@b.co")
    remember_all(fake_him)
    update, q = make_update("setprio:sales|INBOX.P2-Normal|3|0")
    _run(app_mod.on_callback(update, make_context()))
    assert len(fake_him.envelopes("sales", "INBOX.P0-Critical")) == 1
