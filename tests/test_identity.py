"""Stale message identity: Himalaya ids are not a stable UIDVALIDITY handle.

A Telegram button or saved draft that outlives an expunge/move/renumber must
not act on whatever now sits at that (account, folder, id).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import mailops.bot.app as app_mod
from mailops.db import init_db, save_draft
from mailops.identity import (
    STALE_TEXT,
    identity_from_envelope,
    locate,
    recall,
    remember,
    verify,
)
from tests.conftest import record_identity, remember_all


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def ident_db(tmp_profile):
    init_db()
    return tmp_profile


def test_remember_and_recall_roundtrip(ident_db, fake_him):
    fake_him.add_message("altcoininformer", "INBOX", "12", subject="Rates?", sender="buyer@ads.example")
    env = record_identity(fake_him, "altcoininformer", "INBOX", "12")
    got = recall("altcoininformer", "INBOX", "12")
    assert got is not None
    assert got.msg_id == "12"
    assert got.account_id == "altcoininformer"
    assert got.mailbox == "INBOX"
    assert got.message_id == env["message-id"]
    assert got.sender == "buyer@ads.example"
    assert verify(fake_him, "altcoininformer", "INBOX", "12").ok


def test_verify_rejects_never_recorded_id(ident_db, fake_him):
    fake_him.add_message("altcoininformer", "INBOX", "1", subject="hello", sender="a@b.co")
    v = verify(fake_him, "altcoininformer", "INBOX", "1")
    assert not v.ok
    assert "never recorded" in v.reason


def test_verify_rejects_different_account(ident_db, fake_him):
    fake_him.add_message("altcoininformer", "INBOX", "1", subject="hello", sender="a@b.co")
    record_identity(fake_him, "altcoininformer", "INBOX", "1")
    fake_him.add_message("coinography", "INBOX", "1", subject="hello", sender="a@b.co")
    v = verify(fake_him, "coinography", "INBOX", "1")
    assert not v.ok
    assert "never recorded" in v.reason or "different account" in v.reason


def test_verify_rejects_replaced_message_at_same_id(ident_db, fake_him):
    fake_him.add_message(
        "altcoininformer", "INBOX", "7", subject="Ad rates", sender="buyer@ads.example",
        message_id="<orig@x>",
    )
    record_identity(fake_him, "altcoininformer", "INBOX", "7")
    fake_him.store["altcoininformer"]["INBOX"]["7"]["subject"] = "Unrelated spam"
    fake_him.store["altcoininformer"]["INBOX"]["7"]["from"] = [{"email": "spam@bulk.invalid"}]
    fake_him.store["altcoininformer"]["INBOX"]["7"]["message-id"] = "<other@x>"
    v = verify(fake_him, "altcoininformer", "INBOX", "7")
    assert not v.ok
    assert "different message" in v.reason


def test_verify_rejects_missing_message(ident_db, fake_him):
    fake_him.add_message("altcoininformer", "INBOX", "4", subject="gone", sender="a@b.co")
    record_identity(fake_him, "altcoininformer", "INBOX", "4")
    fake_him.store["altcoininformer"]["INBOX"].pop("4")
    v = verify(fake_him, "altcoininformer", "INBOX", "4")
    assert not v.ok
    assert "no longer at" in v.reason


def test_verify_rejects_wrong_folder(ident_db, fake_him):
    fake_him.add_message("altcoininformer", "INBOX", "8", subject="stay", sender="a@b.co")
    record_identity(fake_him, "altcoininformer", "INBOX", "8")
    v = verify(fake_him, "altcoininformer", "INBOX.P1-Important", "8")
    assert not v.ok


def test_fallback_to_classification_row_for_inbox(ident_db, fake_him):
    from mailops.db import set_message_location

    fake_him.add_message("altcoininformer", "INBOX", "3", subject="how much", sender="buyer@x.co")
    set_message_location(
        "altcoininformer", "3", "INBOX.P1-Important",
        subject="how much", sender="buyer@x.co", priority=1,
    )
    got = recall("altcoininformer", "INBOX", "3")
    assert got is not None and got.sender == "buyer@x.co"
    assert verify(fake_him, "altcoininformer", "INBOX", "3").ok
    # A classification row keyed by an INBOX id must not authorize another folder.
    assert recall("altcoininformer", "INBOX.P1-Important", "3") is None


def test_locate_finds_same_message_after_folder_id_change(ident_db, fake_him):
    fake_him.add_message(
        "altcoininformer", "INBOX", "1", subject="Rates", sender="a@b.co",
        message_id="<stable@x>",
    )
    ident = identity_from_envelope(
        "altcoininformer", "INBOX", fake_him.store["altcoininformer"]["INBOX"]["1"]
    )
    remember(ident)
    fake_him.move("altcoininformer", "1", "INBOX.P1-Important", "INBOX")
    # Destination folder assigned a new id (FakeHimalaya keeps the same id; still locatable).
    found = locate(fake_him, ident, "INBOX.P1-Important")
    assert found is not None
    assert found.message_id == "<stable@x>"
    assert found.mailbox == "INBOX.P1-Important"


class _Msg:
    def __init__(self):
        self.sent: list[str] = []

    async def reply_text(self, text, reply_markup=None):
        self.sent.append(text)


class _Query:
    def __init__(self, data):
        self.data = data
        self.edits: list[str] = []
        self.answers: list[tuple] = []
        self.message = _Msg()

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edits.append("")


def _press(data, fake_him, monkeypatch, user_id=111):
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    q = _Query(data)
    update = SimpleNamespace(
        callback_query=q,
        effective_user=SimpleNamespace(id=user_id),
        effective_message=q.message,
    )
    _run(app_mod.on_callback(update, SimpleNamespace(user_data={})))
    return q


def test_send_refused_when_message_was_replaced(ident_db, fake_him, monkeypatch):
    monkeypatch.setenv("MAILOPS_TEST_MODE", "0")
    from mailops.config import load_settings, save_settings

    s = load_settings()
    s["account_policies"] = {"sales": {"organize": True, "send": True}}
    save_settings(s)
    fake_him.add_message("sales", "INBOX", "1", subject="Rates?", sender="buyer@x.co")
    remember_all(fake_him)
    save_draft(111, "sales", "INBOX", "1", "Thanks, here are our rates")
    fake_him.store["sales"]["INBOX"]["1"]["subject"] = "Unrelated"
    fake_him.store["sales"]["INBOX"]["1"]["from"] = [{"email": "other@z.co"}]
    fake_him.store["sales"]["INBOX"]["1"]["message-id"] = "<replaced@x>"
    q = _press("send:sales|INBOX|1", fake_him, monkeypatch)
    assert fake_him.sent == []
    said = " ".join(q.message.sent + [t or "" for t, _ in q.answers] + q.edits)
    assert "Stale or mismatched" in said
    assert "nothing was changed or sent" in said.lower()


def test_setprio_refused_when_message_gone(ident_db, fake_him, monkeypatch):
    monkeypatch.setenv("MAILOPS_TEST_MODE", "0")
    from mailops.config import load_settings, save_settings

    s = load_settings()
    s["account_policies"] = {"sales": {"organize": True, "send": True}}
    save_settings(s)
    fake_him.add_message("sales", "INBOX", "1", subject="Rates?", sender="buyer@x.co")
    remember_all(fake_him)
    fake_him.store["sales"]["INBOX"].pop("1")
    q = _press("setprio:sales|INBOX|1|0", fake_him, monkeypatch)
    assert fake_him.mutations == []
    said = " ".join(q.message.sent + [t or "" for t, _ in q.answers])
    assert "Stale or mismatched" in said


def test_stale_text_template_mentions_no_mutation():
    text = STALE_TEXT.format(why="The message is no longer at INBOX")
    assert "nothing was changed or sent" in text.lower()
