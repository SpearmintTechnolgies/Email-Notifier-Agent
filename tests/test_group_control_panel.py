"""Group control-panel + private deep-link bridge tests.

Covers:
* Every new group callback (``gdl:*``, ``g:all``, ``g:all:n|p``) is permitted
  by the group callback allowlist.
* Server-side permission checks block ``gdl:*`` taps by anyone who does not
  own the required permission - even when they are in the whitelisted group.
* ``_mint_deep_link`` posts a ``t.me/<bot>?start=mdl_<token>`` URL button and
  nothing sensitive in the group chat.
* ``/start mdl_<token>`` in private is single-use, user-scoped, expires, and
  routes to the right private screen (admin users / groups / audit, settings,
  reply picker, approvals).
* Group-whitelisting still does not authorize members.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from telegram import InlineKeyboardMarkup

import mailops.bot.app as app_mod
from mailops import access, group_links
from mailops.bot import keyboards as kb
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


def _button_urls(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.url for row in markup.inline_keyboard for b in row if b.url]


# --- callback allowlist -----------------------------------------------------
@pytest.mark.parametrize(
    "data",
    [
        "g:all",
        "g:all:n",
        "g:all:p",
        "gdl:reply_pick",
        "gdl:ai_drafts",
        "gdl:approvals",
        "gdl:admin_users",
        "gdl:admin_groups",
        "gdl:settings",
        "gdl:audit",
        "gdl:open_pick",
        "gdl:mailboxes",
    ],
)
def test_new_group_callbacks_are_group_allowed(data):
    assert app_mod.group_callback_allowed(data), data


def test_group_keyboard_only_emits_group_allowed_callbacks(tmp_profile, fake_him):
    """Every ``callback_data`` the group panel can produce is allowed in groups."""
    _setup(tmp_profile)
    from mailops.cache import AccountCounts

    counts = [AccountCounts("sales", "sales@example.com", 1, 2, 3, 4)]
    markup = kb.group_dashboard_keyboard(
        counts,
        is_admin=True,
        can_settings=True,
        can_draft=True,
        can_send=True,
    )
    for data in callback_datas(markup):
        # ``mb:*`` goes through its own narrower allow-list dispatched by
        # app._on_callback before the general group_callback_allowed check.
        if data.startswith("mb:"):
            assert app_mod.group_mailbox_callback_allowed(data), data
            continue
        assert app_mod.group_callback_allowed(data), data


# --- gdl: deep-link minting -------------------------------------------------
def test_group_gdl_admin_shows_private_url_for_owner(tmp_profile, fake_him):
    _setup(tmp_profile)
    upd, q = callback_update("gdl:admin_users", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, SimpleNamespace(user_data={}, bot=None)))
    assert q.edits, "owner tap should produce a private-link message"
    text, markup = q.edits[-1]
    assert "Manage Users" in text
    assert "private" in text.lower()
    urls = _button_urls(markup)
    assert len(urls) == 1
    assert urls[0].startswith("https://t.me/")
    assert "?start=mdl_" in urls[0]


def test_group_gdl_admin_denied_for_operator(tmp_profile, fake_him):
    """USER_B seeded as operator: admin deep links require P_ADMIN server-side."""
    _setup(tmp_profile)
    upd, q = callback_update("gdl:admin_users", USER_B, GROUP, "group")
    _run(app_mod.on_callback(upd, SimpleNamespace(user_data={}, bot=None)))
    assert q.edits == []
    # No deep-link URL button posted anywhere.
    assert not any(
        "mdl_" in (b.url or "")
        for text, markup in q.message.sent
        if markup is not None
        for row in markup.inline_keyboard
        for b in row
    )
    denials = [t for t, alert in q.answers if alert and t]
    joined = " ".join(denials + [t for t, _ in q.message.sent]).lower()
    assert "admin" in joined


def test_group_gdl_reply_requires_draft_perm(tmp_profile, fake_him):
    _setup(tmp_profile)
    access.add_user(USER_A, STRANGER, "viewer")  # viewer has only P_VIEW
    upd, q = callback_update("gdl:reply_pick", STRANGER, GROUP, "group")
    _run(app_mod.on_callback(upd, SimpleNamespace(user_data={}, bot=None)))
    assert q.edits == []
    denials = [t for t, alert in q.answers if alert and t]
    joined = " ".join(denials).lower()
    assert "draft" in joined


def test_group_gdl_stranger_in_whitelisted_group_still_denied(tmp_profile, fake_him):
    """Group whitelisting never authorizes a stranger."""
    _setup(tmp_profile)
    upd, q = callback_update("gdl:settings", STRANGER, GROUP, "group")
    _run(app_mod.on_callback(upd, SimpleNamespace(user_data={}, bot=None)))
    assert q.edits == []
    assert q.answers == [("Unauthorized.", True)]


def test_group_gdl_does_not_touch_mailbox(tmp_profile, fake_him, monkeypatch):
    """Minting a deep link does not read or mutate the mailbox."""
    _setup(tmp_profile)
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    upd, q = callback_update("gdl:settings", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, SimpleNamespace(user_data={}, bot=None)))
    assert q.edits
    assert fake_him.mutations == []
    assert fake_him.sent == []


# --- group all-emails listing -----------------------------------------------
def test_group_all_emails_lists_subjects_without_open_buttons(
    tmp_profile, fake_him
):
    _setup(tmp_profile)
    fake_him.add_message("sales", "INBOX.P0-Critical", "1", subject="breach", sender="a@b.co")
    fake_him.add_message("sales", "INBOX.P1-Important", "2", subject="rates?", sender="c@d.co")
    from mailops.cache import refresh_all

    refresh_all()
    upd, q = callback_update("g:all", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, SimpleNamespace(user_data={}, bot=None)))
    text, markup = q.edits[-1]
    assert "All Emails" in text
    assert "breach" in text or "rates?" in text
    datas = callback_datas(markup)
    # No "Open N" buttons in group listings
    assert not any(d.startswith("n:") for d in datas)
    # But users can request a private "Open" picker via deep link
    assert "gdl:open_pick" in datas
    assert "dash" in datas


# --- deep-link token lifecycle ---------------------------------------------
def test_deep_link_consume_single_use(tmp_profile):
    _setup(tmp_profile)
    token = group_links.issue(USER_A, GROUP, "settings")
    link = group_links.consume(USER_A, token)
    assert link.action == "settings"
    assert link.user_id == USER_A
    assert link.chat_id == GROUP
    with pytest.raises(group_links.LinkError):
        group_links.consume(USER_A, token)


def test_deep_link_rejects_other_user(tmp_profile):
    _setup(tmp_profile)
    token = group_links.issue(USER_A, GROUP, "settings")
    with pytest.raises(group_links.LinkError):
        group_links.consume(USER_B, token)
    # Original user can still spend it exactly once.
    assert group_links.consume(USER_A, token).action == "settings"


def test_deep_link_expiry(tmp_profile, monkeypatch):
    _setup(tmp_profile)
    token = group_links.issue(USER_A, GROUP, "settings")
    future = time.time() + 10_000
    monkeypatch.setattr(group_links.time, "time", lambda: future)
    with pytest.raises(group_links.LinkError):
        group_links.consume(USER_A, token)


def test_deep_link_revoked_on_user_removal(tmp_profile):
    _setup(tmp_profile)
    access.add_user(USER_A, STRANGER, "operator", can_send=True)
    token = group_links.issue(STRANGER, GROUP, "settings")
    access.remove_user(USER_A, STRANGER)
    with pytest.raises(group_links.LinkError):
        group_links.consume(STRANGER, token)


def test_deep_link_rejects_unknown_token(tmp_profile):
    _setup(tmp_profile)
    with pytest.raises(group_links.LinkError):
        group_links.consume(USER_A, "not-a-real-token")


# --- /start mdl_<token> private handoff -------------------------------------
def _start_update(user_id: int, token: str):
    """Private /start deep-link update."""
    upd, msg = command_update(user_id, user_id, "private")
    text = f"/start mdl_{token}"
    upd.effective_message.text = text
    upd.message.text = text
    return upd, msg


def _ctx_with_args(*args: str):
    return SimpleNamespace(user_data={}, args=list(args), bot=None)


def test_start_deep_link_opens_admin_users_for_owner(tmp_profile):
    _setup(tmp_profile)
    token = group_links.issue(USER_A, GROUP, "admin_users")
    upd, msg = _start_update(USER_A, token)
    _run(app_mod.cmd_start(upd, _ctx_with_args(f"mdl_{token}")))
    assert msg.sent
    text, markup = msg.sent[0]
    assert "Authorized users" in text or "No authorized" in text
    # Token is single-use after /start.
    with pytest.raises(group_links.LinkError):
        group_links.consume(USER_A, token)


def test_start_deep_link_rejects_other_user(tmp_profile):
    _setup(tmp_profile)
    token = group_links.issue(USER_A, GROUP, "admin_users")
    upd, msg = _start_update(USER_B, token)
    _run(app_mod.cmd_start(upd, _ctx_with_args(f"mdl_{token}")))
    text = msg.sent[0][0]
    assert "cannot be used" in text.lower() or "different" in text.lower()
    # Token still unused; original user can spend it.
    link = group_links.consume(USER_A, token)
    assert link.action == "admin_users"


def test_start_deep_link_rejects_stranger(tmp_profile):
    _setup(tmp_profile)
    token = group_links.issue(USER_A, GROUP, "admin_users")
    upd, msg = _start_update(STRANGER, token)
    _run(app_mod.cmd_start(upd, _ctx_with_args(f"mdl_{token}")))
    # Guard rejects stranger before the deep link is even inspected.
    assert msg.sent and msg.sent[0][0].startswith("Unauthorized.")


def test_start_deep_link_rejects_demoted_user(tmp_profile):
    """Permission is re-checked at consume time, not at mint time.

    An operator who was shown an admin button in the group (they shouldn't
    be, but if a crafted callback or stale state slipped through) cannot
    reach the admin screen even with a valid token.
    """
    _setup(tmp_profile)
    # Operator mints an admin link (shouldn't normally happen; verifies the
    # consume-side guard, not the mint-side).
    token = group_links.issue(USER_B, GROUP, "admin_users")
    upd, msg = _start_update(USER_B, token)
    _run(app_mod.cmd_start(upd, _ctx_with_args(f"mdl_{token}")))
    joined = " ".join(t for t, _ in msg.sent).lower()
    assert "admin" in joined


def test_start_deep_link_opens_settings(tmp_profile):
    _setup(tmp_profile)
    token = group_links.issue(USER_A, GROUP, "settings")
    upd, msg = _start_update(USER_A, token)
    _run(app_mod.cmd_start(upd, _ctx_with_args(f"mdl_{token}")))
    text, markup = msg.sent[0]
    assert text.startswith("Settings")
    assert "nav:home" in callback_datas(markup) or any(
        d.startswith("set:") or d == "dash" for d in callback_datas(markup)
    )


def test_start_deep_link_opens_audit(tmp_profile):
    _setup(tmp_profile)
    token = group_links.issue(USER_A, GROUP, "audit")
    upd, msg = _start_update(USER_A, token)
    _run(app_mod.cmd_start(upd, _ctx_with_args(f"mdl_{token}")))
    text = msg.sent[0][0]
    assert "audit" in text.lower()


def test_start_deep_link_opens_reply_pick_without_bodies(
    tmp_profile, fake_him, monkeypatch
):
    _setup(tmp_profile)
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    fake_him.add_message("sales", "INBOX.P1-Important", "7", subject="rates?", sender="x@y.co")
    from mailops.cache import refresh_all

    refresh_all()
    token = group_links.issue(USER_A, GROUP, "reply_pick")
    upd, msg = _start_update(USER_A, token)
    _run(app_mod.cmd_start(upd, _ctx_with_args(f"mdl_{token}")))
    text, markup = msg.sent[0]
    assert "Reply" in text
    datas = callback_datas(markup)
    # Picker exposes real open: callbacks (private can act); no AI/send yet.
    assert any(d.startswith("open:") for d in datas)
    assert not any(d.startswith(("send:", "go:")) for d in datas)


def test_start_deep_link_opens_approvals_empty_list(tmp_profile):
    _setup(tmp_profile)
    token = group_links.issue(USER_A, GROUP, "approvals")
    upd, msg = _start_update(USER_A, token)
    _run(app_mod.cmd_start(upd, _ctx_with_args(f"mdl_{token}")))
    text = msg.sent[0][0]
    assert "Approvals" in text
    assert "No pending" in text


def test_start_without_deep_link_still_shows_dashboard(tmp_profile, fake_him):
    """Backwards-compat: bare /start keeps working in private."""
    _setup(tmp_profile)
    upd, msg = command_update(USER_A, USER_A, "private")
    _run(app_mod.cmd_start(upd, _ctx_with_args()))
    # Private cmd_start sends menu + dashboard
    assert len(msg.sent) == 2
