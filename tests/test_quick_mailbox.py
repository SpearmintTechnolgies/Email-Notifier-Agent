"""Quick Add Email (one-message group onboarding) tests.

Covers parsing errors, missing fields, saved presets, duplicate addresses,
authorization, session ownership, safe rollback, and the private-DM
password handoff. Also verifies that passwords are never accepted via the
group paste path and that production-sensitive settings (notifications,
Groq, Altcoin Informer, group whitelisting, send-off/organize-off defaults)
are preserved end to end.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import mailops.paths as paths
from mailops import access, mailbox_admin
from mailops import onboarding_session as os_mod
from mailops import webmail_onboarding as wm
from mailops.bot import mailboxes as mb_bot
from mailops.bot import mb_quick
from mailops.db import init_db
from mailops.himalaya_client import RunResult
from tests.test_telegram_group import (
    GROUP,
    STRANGER,
    USER_A,
    USER_B,
    callback_update,
    command_update,
    callback_datas,
    set_env,
)


PASSWORD = "N0t-A-Real-Secret!#"


def _run(coro):
    return asyncio.run(coro)


def _setup(profile, *, allowed=f"{USER_A},{USER_B}", home=f"{USER_A},{GROUP}"):
    init_db()
    set_env(profile, allowed=allowed, home=home)
    access.ensure()
    paths.ACCOUNTS_JSON.write_text("[]", encoding="utf-8")
    paths.CONFIG_TOML.write_text("", encoding="utf-8")
    from mailops.config import SETTINGS_JSON
    SETTINGS_JSON.write_text(
        json.dumps({"active_accounts": [], "account_policies": {}}),
        encoding="utf-8",
    )
    import mailops.webmail_onboarding as wm_mod
    wm_mod._restrict_to_current_user = lambda p: None  # type: ignore[assignment]
    mb_bot.AWAITING.clear()
    mb_bot._QADD_PRESET_CHOICE.clear()
    import os
    os.environ.pop("MAILOPS_ALLOW_DM_CREDENTIALS", None)


class FakeCheckHim:
    def __init__(self, imap="OK", smtp="OK", code=0):
        self.imap, self.smtp, self.code = imap, smtp, code
        self.calls: list[list[str]] = []

    def run(self, args, timeout=60, input_text=None):
        self.calls.append(list(args))
        return RunResult(
            self.code,
            f"Account: {args[1]}\n  imap: {self.imap}\n  smtp: {self.smtp}\n",
            "",
        )


QUICK_MSG = (
    "Email: info@alien.fi\n"
    "Name: Alien FI\n"
    "IMAP: gcam1108.siteground.biz\n"
    "IMAP Port: 993\n"
    "SMTP: gcam1108.siteground.biz\n"
    "SMTP Port: 465\n"
)


def _ctx():
    return SimpleNamespace(user_data={}, bot=None)


# --------------------------------------------------------------------------- parser


def test_parse_quick_add_text_basic():
    r = mb_quick.parse_quick_add_text(QUICK_MSG, taken_ids=set())
    assert r.fields["email"] == "info@alien.fi"
    assert r.fields["display_name"] == "Alien FI"
    assert r.fields["imap_host"] == "gcam1108.siteground.biz"
    assert r.fields["imap_port"] == 993
    assert r.fields["smtp_host"] == "gcam1108.siteground.biz"
    assert r.fields["smtp_port"] == 465
    # Defaults: both usernames default to the full email.
    assert r.fields["imap_user"] == "info@alien.fi"
    assert r.fields["smtp_user"] == "info@alien.fi"
    # Auto account id: 'info' is a generic local-part, falls back to domain.
    assert r.fields["account_id"] == "alien"
    assert r.account_id_overridden is False
    assert r.save_preset_suggested is True


def test_parse_rejects_password_lines():
    text = QUICK_MSG + "Password: hunter2\n"
    with pytest.raises(mb_quick.QuickAddError, match="(?i)never accepted"):
        mb_quick.parse_quick_add_text(text, taken_ids=set())
    # And the alias forms.
    for alias in ("Pass", "PWD", "Secret", "IMAP Password", "SMTP Pass", "API Key"):
        with pytest.raises(mb_quick.QuickAddError, match="(?i)never accepted"):
            mb_quick.parse_quick_add_text(
                QUICK_MSG + f"{alias}: hunter2\n", taken_ids=set()
            )


def test_parse_missing_required_fields():
    partial = (
        "Email: info@alien.fi\n"
        "Name: Alien FI\n"
    )
    with pytest.raises(mb_quick.QuickAddError, match="Missing required"):
        mb_quick.parse_quick_add_text(partial, taken_ids=set())


def test_parse_empty_message():
    with pytest.raises(mb_quick.QuickAddError, match="[Ee]mpty"):
        mb_quick.parse_quick_add_text("", taken_ids=set())
    with pytest.raises(mb_quick.QuickAddError, match="No 'Key: value'"):
        mb_quick.parse_quick_add_text("hello world no fields here", taken_ids=set())


def test_parse_bad_port_rejected():
    bad = QUICK_MSG.replace("IMAP Port: 993", "IMAP Port: 99999")
    with pytest.raises(mb_quick.QuickAddError, match="between 1 and 65535"):
        mb_quick.parse_quick_add_text(bad, taken_ids=set())
    bad2 = QUICK_MSG.replace("SMTP Port: 465", "SMTP Port: not-a-number")
    with pytest.raises(mb_quick.QuickAddError, match="plain number"):
        mb_quick.parse_quick_add_text(bad2, taken_ids=set())


def test_parse_bad_hostname_rejected():
    bad = QUICK_MSG.replace(
        "IMAP: gcam1108.siteground.biz",
        "IMAP: https://gcam1108.siteground.biz:993/path",
    )
    with pytest.raises(mb_quick.QuickAddError, match="plain hostname"):
        mb_quick.parse_quick_add_text(bad, taken_ids=set())


def test_parse_malformed_email():
    bad = QUICK_MSG.replace("Email: info@alien.fi", "Email: not-an-email")
    with pytest.raises(mb_quick.QuickAddError, match="invalid"):
        mb_quick.parse_quick_add_text(bad, taken_ids=set())


def test_parse_overrides_account_id_and_users():
    text = QUICK_MSG + (
        "Account ID: alienfi-main\n"
        "IMAP user: imap-user@alien.fi\n"
        "SMTP user: smtp-user@alien.fi\n"
    )
    r = mb_quick.parse_quick_add_text(text, taken_ids=set())
    assert r.fields["account_id"] == "alienfi-main"
    assert r.account_id_overridden is True
    assert r.fields["imap_user"] == "imap-user@alien.fi"
    assert r.fields["smtp_user"] == "smtp-user@alien.fi"


def test_parse_unknown_keys_silently_ignored():
    text = QUICK_MSG + "Mailbox size: 10GB\nSupport: help@provider.example\n"
    r = mb_quick.parse_quick_add_text(text, taken_ids=set())
    assert r.fields["email"] == "info@alien.fi"


def test_derive_account_id_uniquifies():
    taken = {"alien", "alien2"}
    aid = mb_quick.derive_account_id("info@alien.fi", taken)
    assert aid == "alien3"
    aid = mb_quick.derive_account_id("sales@alien.fi", taken)
    assert aid == "alien3"  # generic 'sales' local-part → domain fallback


def test_parse_with_preset_only_requires_email_and_name():
    preset = {
        "imap_host": "gcam1108.siteground.biz",
        "imap_port": 993,
        "smtp_host": "gcam1108.siteground.biz",
        "smtp_port": 465,
    }
    text = "Email: info@alien.fi\nName: Alien FI\n"
    r = mb_quick.parse_quick_add_text(text, preset=preset, taken_ids=set())
    assert r.fields["imap_host"] == "gcam1108.siteground.biz"
    assert r.fields["imap_port"] == 993
    assert r.save_preset_suggested is False


def test_parse_user_overrides_preset():
    preset = {
        "imap_host": "mail.serverA.example",
        "imap_port": 993,
        "smtp_host": "mail.serverA.example",
        "smtp_port": 465,
    }
    text = QUICK_MSG  # user supplies serverB hosts
    r = mb_quick.parse_quick_add_text(text, preset=preset, taken_ids=set())
    # User's hosts win.
    assert r.fields["imap_host"] == "gcam1108.siteground.biz"
    assert r.fields["smtp_host"] == "gcam1108.siteground.biz"


# --------------------------------------------------------------------------- presets store


def test_preset_save_and_load_roundtrip(tmp_profile):
    _setup(tmp_profile)
    mb_quick.preset_save(
        "SiteGround A",
        {
            "imap_host": "gcam1108.siteground.biz",
            "imap_port": 993,
            "smtp_host": "gcam1108.siteground.biz",
            "smtp_port": 465,
        },
    )
    presets = mb_quick.presets_load()
    assert "SiteGround A" in presets
    assert presets["SiteGround A"]["imap_port"] == 993


def test_preset_save_rejects_bad_hostname(tmp_profile):
    _setup(tmp_profile)
    with pytest.raises(mb_quick.QuickAddError, match="web URL|valid hostname"):
        mb_quick.preset_save(
            "bad",
            {
                "imap_host": "http://host/with/path",
                "imap_port": 993,
                "smtp_host": "mail.example.com",
                "smtp_port": 465,
            },
        )


def test_preset_save_rejects_bad_name(tmp_profile):
    _setup(tmp_profile)
    with pytest.raises(mb_quick.QuickAddError, match="Preset name"):
        mb_quick.preset_save(
            "has/slash",
            {
                "imap_host": "mail.example.com",
                "imap_port": 993,
                "smtp_host": "mail.example.com",
                "smtp_port": 465,
            },
        )


def test_preset_delete(tmp_profile):
    _setup(tmp_profile)
    mb_quick.preset_save(
        "SGA",
        {
            "imap_host": "mail.example.com",
            "imap_port": 993,
            "smtp_host": "mail.example.com",
            "smtp_port": 465,
        },
    )
    assert mb_quick.preset_delete("SGA") is True
    assert mb_quick.preset_delete("SGA") is False
    assert mb_quick.presets_load() == {}


def test_malformed_preset_in_settings_is_ignored(tmp_profile):
    _setup(tmp_profile)
    from mailops.config import load_settings, save_settings
    s = load_settings()
    s["mailbox_presets"] = {
        "good": {
            "imap_host": "mail.example.com",
            "imap_port": 993,
            "smtp_host": "mail.example.com",
            "smtp_port": 465,
        },
        "bad-missing-ports": {"imap_host": "mail.example.com"},
        "weird": "not-a-dict",
    }
    save_settings(s)
    out = mb_quick.presets_load()
    assert list(out.keys()) == ["good"]


# --------------------------------------------------------------------------- Telegram flow


def _text_update(user_id: int, chat_id: int, kind: str, text: str):
    upd, msg = command_update(user_id, chat_id, kind)
    upd.effective_message.text = text
    upd.message.text = text
    return upd, msg


def test_qadd_entry_in_group_requires_admin(tmp_profile):
    _setup(tmp_profile)
    # Operator (USER_B) has no P_ADMIN.
    upd, q = callback_update("mb:qadd", USER_B, GROUP, "group")
    import mailops.bot.app as app_mod
    _run(app_mod.on_callback(upd, _ctx()))
    assert q.edits == []
    denials = [t for t, alert in q.answers if alert and t]
    assert any("admin" in t.lower() for t in denials)


def test_qadd_entry_in_group_rejects_stranger(tmp_profile):
    _setup(tmp_profile)
    upd, q = callback_update("mb:qadd", STRANGER, GROUP, "group")
    import mailops.bot.app as app_mod
    _run(app_mod.on_callback(upd, _ctx()))
    # Stranger refused at the top-level guard, before the mailbox screens.
    assert q.edits == []


def test_qadd_entry_in_group_admin_sees_quick_add_screen(tmp_profile):
    _setup(tmp_profile)
    upd, q = callback_update("mb:qadd", USER_A, GROUP, "group")
    import mailops.bot.app as app_mod
    _run(app_mod.on_callback(upd, _ctx()))
    assert q.edits, "admin should see the quick-add paste prompt"
    text, markup = q.edits[-1]
    assert "Quick Add Email" in text
    # Slot armed for the admin, pinned to the group chat.
    pending = mb_bot.AWAITING.get(USER_A)
    assert pending and pending["field"] == mb_bot.QUICK_ADD_FIELD
    assert pending["chat_id"] == GROUP


def test_qadd_full_group_flow_opens_review_then_hands_off_to_dm(
    tmp_profile, monkeypatch
):
    """End-to-end CEO flow: tap Quick Add in group, paste one message,
    confirm, and have the password step moved to a private DM.
    """
    _setup(tmp_profile)
    import mailops.bot.app as app_mod

    # Admin taps Quick Add in the group.
    upd, q = callback_update("mb:qadd", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))

    # Admin pastes the one-message spec into the group.
    upd, msg = _text_update(USER_A, GROUP, "group", QUICK_MSG)
    _run(mb_bot.handle_text(upd, _ctx()))
    assert msg.sent, "review screen should post into the group"
    review_text, review_markup = msg.sent[-1]
    assert "Review the new mailbox" in review_text
    assert "info@alien.fi" in review_text
    assert "gcam1108.siteground.biz" in review_text

    # Session opened and ready to arm.
    sessions = os_mod.list_sessions(USER_A)
    assert sessions and sessions[0].state == os_mod.STATE_DRAFT
    token = sessions[0].token
    nonce = sessions[0].nonce

    # Simulate the DM channel so the group → private handoff can "send".
    dm_sent: list[tuple] = []

    class FakeBot:
        username = "testbot"
        async def send_message(self, chat_id, text, reply_markup=None):
            dm_sent.append((chat_id, text, reply_markup))

    ctx = SimpleNamespace(user_data={}, bot=FakeBot())

    # Admin taps Confirm (= mb:arm). Must trigger group → DM handoff.
    upd, q = callback_update(f"mb:arm:{token}:{nonce}", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, ctx))

    # Session advanced to awaiting_credentials.
    final = os_mod.get_session(token, USER_A)
    assert final.state == os_mod.STATE_AWAITING_CREDS

    # The group screen posts a bland hand-off notice; the DM carries the
    # actual armed screen with credential-capture buttons.
    group_notice = q.edits[-1][0]
    assert "private" in group_notice.lower()
    assert "info@alien.fi" not in group_notice.lower()  # no mailbox leak in group
    assert dm_sent, "the password-step DM must be sent"
    _dm_chat_id, dm_text, _dm_markup = dm_sent[-1]
    assert _dm_chat_id == USER_A
    assert "credentials" in dm_text.lower() or "password" in dm_text.lower()

    # Finish via the server-side CLI path (no password in Telegram).
    result = wm.run_pending(token, imap_password=PASSWORD, him=FakeCheckHim())
    assert result.ok

    # Mailbox registered, starts with sending OFF and organize OFF.
    boxes = mailbox_admin.list_mailboxes()
    assert [b.account_id for b in boxes] == ["alien"]
    assert boxes[0].active
    assert not boxes[0].send
    assert not boxes[0].organize
    # Password never landed in session row or audit.
    s = os_mod.list_sessions(USER_A)[0]
    assert PASSWORD not in (json.dumps(s.fields) + s.last_error)
    assert all(PASSWORD not in r.detail for r in mailbox_admin.audit_log())


def test_qadd_group_rejects_password_in_pasted_message(tmp_profile):
    """A paste that contains 'Password:' must be refused with an admin-safe
    alert, and NO onboarding session must be opened."""
    _setup(tmp_profile)
    import mailops.bot.app as app_mod
    upd, q = callback_update("mb:qadd", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))

    bad = QUICK_MSG + "Password: hunter2\n"
    upd, msg = _text_update(USER_A, GROUP, "group", bad)
    _run(mb_bot.handle_text(upd, _ctx()))
    assert msg.sent
    reply = msg.sent[-1][0]
    assert "refused" in reply.lower()
    assert "hunter2" not in reply  # the password never echoes back
    # No session created.
    assert os_mod.list_sessions(USER_A) == []


def test_qadd_duplicate_email_rejected_at_arm(tmp_profile):
    """A quick-add that targets an already-registered email is rejected
    at the add_account layer and rolled back cleanly."""
    _setup(tmp_profile)
    paths.ACCOUNTS_JSON.write_text(
        json.dumps(
            [
                {
                    "id": "alien",
                    "email": "info@alien.fi",
                    "display_name": "Alien FI",
                }
            ]
        ),
        encoding="utf-8",
    )
    import mailops.bot.app as app_mod
    upd, q = callback_update("mb:qadd", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))

    # Paste the same email.
    upd, msg = _text_update(USER_A, GROUP, "group", QUICK_MSG)
    _run(mb_bot.handle_text(upd, _ctx()))
    # Parser auto-derived "alien" but it's taken; parser should uniquify.
    sessions = os_mod.list_sessions(USER_A)
    assert sessions, "session should still open — only the id auto-collides"
    assert sessions[0].fields["account_id"] != "alien"

    # ...however the EMAIL collision is detected only at add_account time.
    # Arm first (quick-add stops at the draft/review step).
    session = os_mod.arm_for_credentials(
        sessions[0].token, USER_A, nonce=sessions[0].nonce
    )
    with pytest.raises(wm.OnboardingError, match="already registered"):
        wm.run_pending(session.token, imap_password=PASSWORD, him=FakeCheckHim())
    # Session rolls back to failed; existing row unchanged.
    final = os_mod.list_sessions(USER_A)[0]
    assert final.state == os_mod.STATE_FAILED
    rows = json.loads(paths.ACCOUNTS_JSON.read_text(encoding="utf-8"))
    assert len(rows) == 1
    assert rows[0]["id"] == "alien"


def test_qadd_session_ownership_prevents_second_admin_from_confirming(tmp_profile):
    """Only the admin who initiated the session can advance it."""
    _setup(tmp_profile, allowed=f"{USER_A},{USER_B}")
    # Promote USER_B to owner so they would normally pass the admin guard.
    access.add_user(USER_A, USER_B, "owner")
    import mailops.bot.app as app_mod
    # USER_A opens quick-add and submits.
    upd, q = callback_update("mb:qadd", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))
    upd, msg = _text_update(USER_A, GROUP, "group", QUICK_MSG)
    _run(mb_bot.handle_text(upd, _ctx()))
    session = os_mod.list_sessions(USER_A)[0]
    # USER_B tries to arm.
    upd2, q2 = callback_update(
        f"mb:arm:{session.token}:{session.nonce}", USER_B, GROUP, "group"
    )
    _run(app_mod.on_callback(upd2, _ctx()))
    # Rejected via session-owner alert; state unchanged.
    assert os_mod.get_session(session.token, USER_A).state == os_mod.STATE_DRAFT


def test_qadd_private_flow_also_works(tmp_profile):
    """Quick Add should also work in a private DM for a traveling CEO."""
    _setup(tmp_profile)
    upd, q = callback_update("mb:qadd", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    assert q.edits
    upd, msg = _text_update(USER_A, USER_A, "private", QUICK_MSG)
    _run(mb_bot.handle_text(upd, _ctx()))
    assert msg.sent
    assert "Review the new mailbox" in msg.sent[-1][0]


def test_qadd_dm_slot_untouched_by_group_paste(tmp_profile):
    """A quick-add slot pinned to the group must not be advanced by a
    private DM message, and vice versa."""
    _setup(tmp_profile)
    import mailops.bot.app as app_mod
    upd, q = callback_update("mb:qadd", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))
    # The admin somehow sends the paste into their private chat instead.
    upd, msg = _text_update(USER_A, USER_A, "private", QUICK_MSG)
    consumed = _run(mb_bot.handle_text(upd, _ctx()))
    assert consumed is False, "private DM must not satisfy a group-bound slot"
    assert os_mod.list_sessions(USER_A) == []


def test_qadd_preset_only_requires_email_and_name(tmp_profile):
    """Admin picks a preset, then can onboard with just email + name."""
    _setup(tmp_profile)
    mb_quick.preset_save(
        "SiteGround A",
        {
            "imap_host": "gcam1108.siteground.biz",
            "imap_port": 993,
            "smtp_host": "gcam1108.siteground.biz",
            "smtp_port": 465,
        },
    )
    import mailops.bot.app as app_mod
    upd, q = callback_update("mb:qadd", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))
    # Pick the preset.
    upd, q = callback_update(
        "mb:qadd:preset:SiteGround A", USER_A, GROUP, "group"
    )
    _run(app_mod.on_callback(upd, _ctx()))
    # Minimal paste.
    upd, msg = _text_update(
        USER_A, GROUP, "group",
        "Email: info@alien.fi\nName: Alien FI\n",
    )
    _run(mb_bot.handle_text(upd, _ctx()))
    assert msg.sent, "review screen should post after a preset-only paste"
    text = msg.sent[-1][0]
    assert "gcam1108.siteground.biz" in text
    session = os_mod.list_sessions(USER_A)[0]
    assert session.fields["imap_port"] == 993
    assert session.fields["smtp_port"] == 465


def test_qadd_save_preset_persists_hosts(tmp_profile):
    """Admin pastes a full message then saves the server as a preset."""
    _setup(tmp_profile)
    import mailops.bot.app as app_mod
    upd, q = callback_update("mb:qadd", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))
    upd, msg = _text_update(USER_A, GROUP, "group", QUICK_MSG)
    _run(mb_bot.handle_text(upd, _ctx()))
    session = os_mod.list_sessions(USER_A)[0]
    # Tap Save preset.
    upd, q = callback_update(
        f"mb:qadd:save_preset:{session.token}:{session.nonce}",
        USER_A, GROUP, "group",
    )
    _run(app_mod.on_callback(upd, _ctx()))
    # Admin sends the preset name.
    upd, msg = _text_update(USER_A, GROUP, "group", "SiteGround A")
    _run(mb_bot.handle_text(upd, _ctx()))
    presets = mb_quick.presets_load()
    assert "SiteGround A" in presets
    assert presets["SiteGround A"]["imap_host"] == "gcam1108.siteground.biz"
    assert presets["SiteGround A"]["imap_port"] == 993


def test_qadd_parse_error_does_not_consume_rate_limit(tmp_profile):
    """A failed parse must not consume one of the admin's MAX_OPEN_PER_USER slots."""
    _setup(tmp_profile)
    import mailops.bot.app as app_mod
    upd, q = callback_update("mb:qadd", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))
    upd, msg = _text_update(USER_A, GROUP, "group", "nonsense without any colon")
    _run(mb_bot.handle_text(upd, _ctx()))
    # No session should have been created.
    assert os_mod.list_sessions(USER_A) == []


def test_qadd_group_callbacks_are_in_allowlist():
    """All mb:qadd:* callbacks must be in the group allowlist."""
    import mailops.bot.app as app_mod
    for data in (
        "mb:qadd",
        "mb:qadd:preset:foo",
        "mb:qadd:preset:",
        "mb:qadd:edit:tok:nonce",
        "mb:qadd:save_preset:tok:nonce",
        "mb:qadd:back_to_review:tok",
    ):
        assert app_mod.group_mailbox_callback_allowed(data), data


def test_qadd_altcoin_informer_preserved(tmp_profile):
    """Running the quick-add flow must leave the live Altcoin Informer
    production mailbox and its send policy completely untouched."""
    _setup(tmp_profile)
    paths.ACCOUNTS_JSON.write_text(
        json.dumps(
            [
                {
                    "id": "altcoininformer",
                    "email": "info@altcoininformer.com",
                    "display_name": "Altcoin Informer",
                    "default": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    from mailops.config import SETTINGS_JSON
    SETTINGS_JSON.write_text(
        json.dumps(
            {
                "active_accounts": ["altcoininformer"],
                "account_policies": {
                    "altcoininformer": {"organize": False, "send": True}
                },
            }
        ),
        encoding="utf-8",
    )
    (paths.SECRETS_DIR / "altcoininformer.imap.secret").parent.mkdir(
        parents=True, exist_ok=True
    )
    (paths.SECRETS_DIR / "altcoininformer.imap.secret").write_text(
        "already-set", encoding="utf-8"
    )
    (paths.SECRETS_DIR / "altcoininformer.smtp.secret").write_text(
        "already-set", encoding="utf-8"
    )
    import mailops.bot.app as app_mod
    upd, q = callback_update("mb:qadd", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))
    upd, msg = _text_update(USER_A, GROUP, "group", QUICK_MSG)
    _run(mb_bot.handle_text(upd, _ctx()))
    session = os_mod.list_sessions(USER_A)[0]
    session = os_mod.arm_for_credentials(session.token, USER_A, nonce=session.nonce)
    wm.run_pending(session.token, imap_password=PASSWORD, him=FakeCheckHim())
    rows = json.loads(paths.ACCOUNTS_JSON.read_text(encoding="utf-8"))
    altcoin = [r for r in rows if r["id"] == "altcoininformer"][0]
    assert altcoin["email"] == "info@altcoininformer.com"
    assert altcoin.get("default") is True
    assert (paths.SECRETS_DIR / "altcoininformer.imap.secret").read_text() == "already-set"
    from mailops.config import load_settings
    s = load_settings()
    # Altcoin Informer policy unchanged, new mailbox starts OFF.
    assert s["account_policies"]["altcoininformer"]["send"] is True
    new_aid = session.fields["account_id"]
    assert s["account_policies"][new_aid] == {"organize": False, "send": False}


def test_qadd_rollback_on_check_failure_leaves_registry_clean(tmp_profile):
    """IMAP/SMTP check failure during quick-add → account NOT registered."""
    _setup(tmp_profile)
    import mailops.bot.app as app_mod
    upd, q = callback_update("mb:qadd", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))
    upd, msg = _text_update(USER_A, GROUP, "group", QUICK_MSG)
    _run(mb_bot.handle_text(upd, _ctx()))
    session = os_mod.list_sessions(USER_A)[0]
    session = os_mod.arm_for_credentials(session.token, USER_A, nonce=session.nonce)
    before = paths.ACCOUNTS_JSON.read_text(encoding="utf-8")
    result = wm.run_pending(
        session.token,
        imap_password=PASSWORD,
        him=FakeCheckHim(imap="FAIL (auth)", smtp="OK", code=1),
    )
    assert not result.ok
    assert paths.ACCOUNTS_JSON.read_text(encoding="utf-8") == before
    assert os_mod.list_sessions(USER_A)[0].state == os_mod.STATE_FAILED

