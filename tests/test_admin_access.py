"""Bot handlers: admin menu, RBAC enforcement, group security, replay."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import mailops.bot.app as app_mod
from mailops import access
from mailops.bot import admin
from mailops.db import init_db
from tests.test_telegram_group import (
    GROUP,
    OTHER_GROUP,
    STRANGER,
    USER_A,
    USER_B,
    callback_datas,
    callback_update,
    command_update,
    ctx,
    set_env,
)


def _run(coro):
    return asyncio.run(coro)


def _setup(profile, *, allowed=f"{USER_A},{USER_B}", home=f"{USER_A},{GROUP}"):
    init_db()
    set_env(profile, allowed=allowed, home=home)
    access.ensure()


def test_admin_menu_private_owner_only(tmp_profile):
    _setup(tmp_profile)
    upd, msg = command_update(USER_A, USER_A, "private")
    _run(admin.cmd_admin(upd, ctx()))
    assert msg.sent and "Access management" in msg.sent[0][0]
    datas = callback_datas(msg.sent[0][1])
    assert "ac:au" in datas and "ac:ag" in datas and "ac:audit" in datas

    upd_op, msg_op = command_update(USER_B, USER_B, "private")
    _run(admin.cmd_admin(upd_op, ctx()))
    assert msg_op.sent and "administrators only" in msg_op.sent[0][0].lower()

    upd_g, msg_g = command_update(USER_A, GROUP, "group")
    _run(admin.cmd_admin(upd_g, ctx()))
    assert msg_g.sent and "private-chat only" in msg_g.sent[0][0].lower()


def test_admin_add_user_by_numeric_id(tmp_profile):
    _setup(tmp_profile)
    upd2, q = callback_update("ac:au", USER_A, USER_A, "private")
    _run(app_mod.on_callback(upd2, ctx()))
    assert any("numeric Telegram user id" in t for t, _ in q.edits)

    upd3, msg3 = command_update(USER_A, USER_A, "private")
    upd3.effective_message.text = str(STRANGER)
    upd3.message.text = str(STRANGER)
    _run(app_mod.on_text(upd3, ctx()))
    assert access.role_of(STRANGER) == "operator"
    assert not access.can(STRANGER, access.P_SEND)
    assert any(str(STRANGER) in t for t, _ in msg3.sent)


def test_unauthorized_user_rejected_from_start_and_callback(tmp_profile):
    _setup(tmp_profile)
    upd, msg = command_update(STRANGER, STRANGER, "private")
    _run(app_mod.cmd_start(upd, ctx()))
    assert msg.sent and msg.sent[0][0].startswith("Unauthorized.")
    assert str(STRANGER) in msg.sent[0][0]
    assert "Self-registration" in msg.sent[0][0]

    upd2, q = callback_update("dash", STRANGER, STRANGER, "private")
    _run(app_mod.on_callback(upd2, ctx()))
    assert any("Unauthorized" in (t or "") for t, _ in q.message.sent)


def test_whitelisted_group_is_read_only_and_members_not_auto_authorized(
    tmp_profile, fake_him
):
    _setup(tmp_profile)
    access.add_group(USER_A, OTHER_GROUP, "second")
    upd, msg = command_update(USER_A, OTHER_GROUP, "supergroup")
    _run(app_mod.cmd_start(upd, ctx()))
    assert msg.sent and "Priority dashboard" in msg.sent[0][0]
    assert "Group view" in msg.sent[0][0]
    datas = callback_datas(msg.sent[0][1])
    assert not any(d.startswith(("open:", "reply:", "send:", "ac:")) for d in datas)

    stranger, smsg = command_update(STRANGER, OTHER_GROUP, "supergroup")
    _run(app_mod.cmd_start(stranger, ctx()))
    assert smsg.sent == []


def test_viewer_cannot_open_or_draft(tmp_profile, fake_him, monkeypatch):
    _setup(tmp_profile)
    access.add_user(USER_A, STRANGER, "viewer")
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    fake_him.add_message("sales", "INBOX.P1-Important", "5", subject="rates?", sender="b@b.co")
    upd, q = callback_update("open:sales|INBOX.P1-Important|5", STRANGER, STRANGER, "private")
    _run(app_mod.on_callback(upd, ctx()))
    assert q.answers and "read" in (q.answers[0][0] or "").lower()
    assert q.edits == []

    upd2, q2 = callback_update("aidraft:sales|INBOX.P1-Important|5", STRANGER, STRANGER, "private")
    _run(app_mod.on_callback(upd2, ctx()))
    assert q2.answers and "draft" in (q2.answers[0][0] or "").lower()


def test_operator_without_send_cannot_approve(tmp_profile, fake_him, monkeypatch):
    _setup(tmp_profile)
    access.set_can_send(USER_A, USER_B, False)
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    fake_him.add_message("sales", "INBOX.P1-Important", "5", subject="rates?", sender="b@b.co")
    upd, q = callback_update("send:sales|INBOX.P1-Important|5", USER_B, USER_B, "private")
    _run(app_mod.on_callback(upd, ctx()))
    assert q.answers and "send" in (q.answers[0][0] or "").lower()
    assert fake_him.sent == []


def test_operator_cannot_escalate_via_admin_callback(tmp_profile):
    _setup(tmp_profile)
    upd, q = callback_update("ac:au", USER_B, USER_B, "private")
    _run(app_mod.on_callback(upd, ctx()))
    assert q.answers and "administrators only" in (q.answers[0][0] or "").lower()
    assert not access.is_authorized_user(STRANGER)


def test_revoked_user_loses_access_immediately(tmp_profile, fake_him):
    _setup(tmp_profile)
    upd, msg = command_update(USER_B, USER_B, "private")
    _run(app_mod.cmd_start(upd, ctx()))
    assert any("Email Agent ready" in t or "dashboard" in t.lower() for t, _ in msg.sent)

    access.remove_user(USER_A, USER_B)
    upd2, msg2 = command_update(USER_B, USER_B, "private")
    _run(app_mod.cmd_start(upd2, ctx()))
    assert msg2.sent and msg2.sent[0][0].startswith("Unauthorized.")

    upd3, q = callback_update("dash", USER_B, GROUP, "group")
    _run(app_mod.on_callback(upd3, ctx()))
    assert q.answers == [("Unauthorized.", True)]
    assert q.edits == []


def test_duplicate_admin_callback_is_rejected(tmp_profile):
    _setup(tmp_profile)
    access.add_user(USER_A, STRANGER, "viewer")
    token = access.issue_action(USER_A, "remove_user", {"user_id": STRANGER})
    data = f"ac:tok:{token}"
    upd, q = callback_update(data, USER_A, USER_A, "private")
    _run(app_mod.on_callback(upd, ctx()))
    assert not access.is_authorized_user(STRANGER)

    upd2, q2 = callback_update(data, USER_A, USER_A, "private")
    _run(app_mod.on_callback(upd2, ctx()))
    texts = [t or "" for t, _alert in q2.answers] + [t for t, _m in q2.message.sent]
    assert any("already used" in t.lower() for t in texts)


def test_mailbox_onboarding_screen_refuses_passwords(tmp_profile):
    _setup(tmp_profile)
    text, markup = admin.mailbox_screen()
    lower = text.lower()
    assert "mailbox" in lower
    assert "password" in lower
    assert "never" in lower or "will not collect" in lower
    # The admin menu now hands off to the full Mailboxes panel; the deferred
    # wording lives on the Mailboxes screens themselves.
    assert "mailboxes panel" in lower or "mailboxes" in lower
    callback_datas = {
        b.callback_data for row in markup.inline_keyboard for b in row
        if b.callback_data
    }
    assert "mb:m" in callback_datas


def test_myid_is_available_without_self_registration(tmp_profile):
    _setup(tmp_profile)
    upd, msg = command_update(STRANGER, STRANGER, "private")
    _run(admin.cmd_myid(upd, ctx()))
    assert str(STRANGER) in msg.sent[0][0]
    assert not access.is_authorized_user(STRANGER)


def test_allowgroup_command_in_group_does_not_authorize_members(tmp_profile):
    _setup(tmp_profile)
    upd, msg = command_update(USER_A, OTHER_GROUP, "supergroup")
    upd.effective_chat.title = "Ops"
    _run(admin.cmd_allowgroup(upd, SimpleNamespace(args=[], user_data={})))
    assert access.group_allowed(OTHER_GROUP)
    assert "not auto-authorized" in msg.sent[0][0].lower()
    stranger, smsg = command_update(STRANGER, OTHER_GROUP, "supergroup")
    _run(app_mod.cmd_start(stranger, ctx()))
    assert smsg.sent == []


def test_admin_and_myid_commands_registered(tmp_profile):
    from tests.test_telegram_group import _app_with_bot_username, _matching_commands, _text_update

    application = _app_with_bot_username()
    for cmd in ("/admin", "/myid", "/adduser", "/allowgroup"):
        upd = _text_update(application, cmd, USER_A, chat_type="private", user_id=USER_A)
        assert _matching_commands(application, upd) == [cmd.lstrip("/")]


def test_start_menu_shows_admin_only_for_owner(tmp_profile, fake_him):
    _setup(tmp_profile)
    upd, msg = command_update(USER_A, USER_A, "private")
    _run(app_mod.cmd_start(upd, ctx()))
    labels = [b.text for row in msg.sent[0][1].keyboard for b in row]
    assert "Admin" in labels

    upd2, msg2 = command_update(USER_B, USER_B, "private")
    _run(app_mod.cmd_start(upd2, ctx()))
    labels2 = [b.text for row in msg2.sent[0][1].keyboard for b in row]
    assert "Admin" not in labels2
