"""Regression tests for the Smart Add Email flow, server resolution, and
the opt-in group password-entry feature (MAILOPS Phase 2/4/5/8).

Scope:
  * ``mailops.server_resolution`` - VERIFIED / SUGGESTED / UNKNOWN tiers,
    never inferring a SiteGround-style per-tenant host from the domain.
  * ``mailops.bot.mailboxes`` Smart Add Email screens/callbacks/text slots
    (mb:smart, mb:smart:pick:*, mb:smart:change:*) - email-first flow that
    reuses the existing onboarding_session + arm + credential-capture
    machinery untouched.
  * ``mailops.group_credentials`` policy helpers.
  * The opt-in, owner-only, single-pinned-group password capture
    (mb:group_pw:*) - privacy-mode-compatible reply binding, immediate
    delete-before-confirmation, stop-on-delete-failure, replay/expiry
    protection, never logging/echoing the password.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

import mailops.paths as paths
from mailops import access, group_credentials as gc, mailbox_admin
from mailops import onboarding_session as os_mod
from mailops import server_resolution as sr
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
    wm._restrict_to_current_user = lambda p: None  # type: ignore[assignment]
    mb_bot.AWAITING.clear()
    mb_bot._QADD_PRESET_CHOICE.clear()
    mb_bot._SMART_RESOLUTION_CACHE.clear()
    for var in (
        "MAILOPS_ALLOW_DM_CREDENTIALS",
        "MAILOPS_ALLOW_GROUP_CREDENTIALS",
        "MAILOPS_GROUP_CREDENTIALS_GROUP_ID",
        "MAILOPS_AUTOCONFIG_MX",
    ):
        os.environ.pop(var, None)


def _ctx():
    return SimpleNamespace(user_data={}, bot=None)


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


def _text_update(user_id: int, chat_id: int, kind: str, text: str):
    upd, msg = command_update(user_id, chat_id, kind)
    upd.effective_message.text = text
    upd.message.text = text
    return upd, msg


def _onboard(account_id: str, email: str, imap_host: str, smtp_host: str = None):
    """Register a real mailbox end to end so a later Smart Add on the same
    domain can be offered a VERIFIED reuse (tier 1)."""
    smtp_host = smtp_host or imap_host
    session = os_mod.start_session(USER_A)
    session = os_mod.set_fields(
        session.token,
        USER_A,
        nonce=session.nonce,
        patch={
            "account_id": account_id,
            "email": email,
            "display_name": account_id,
            "imap_host": imap_host,
            "imap_port": 993,
            "smtp_host": smtp_host,
            "smtp_port": 465,
        },
    )
    session = os_mod.arm_for_credentials(session.token, USER_A, nonce=session.nonce)
    result = wm.run_pending(session.token, imap_password=PASSWORD, him=FakeCheckHim())
    assert result.ok
    return session


# =============================================================================
#  mailops.server_resolution
# =============================================================================


def test_resolve_unknown_when_nothing_matches(tmp_profile):
    _setup(tmp_profile)
    res = sr.resolve_for_email("info@totally-unknown-domain.example", allow_mx_autoconfig=False)
    assert res.status == sr.STATUS_UNKNOWN
    assert not res.complete
    assert res.needs_hostname is True


def test_resolve_never_infers_siteground_or_any_server_from_domain_alone(tmp_profile):
    """Even an email at a domain that LOOKS like a hosting brand must not
    get a server guessed purely from the domain - no provider_hint was
    given, so only ask/suggest tiers may fire."""
    _setup(tmp_profile)
    res = sr.resolve_for_email("hello@my-siteground-site.com", allow_mx_autoconfig=False)
    assert res.status == sr.STATUS_UNKNOWN
    assert res.imap_host == ""
    assert res.smtp_host == ""


def test_resolve_hostinger_hint_is_verified_and_complete(tmp_profile):
    _setup(tmp_profile)
    res = sr.resolve_for_email("info@anything.example", provider_hint="hostinger")
    assert res.status == sr.STATUS_VERIFIED
    assert res.complete
    assert res.imap_host == "imap.hostinger.com"
    assert res.imap_port == 993
    assert res.smtp_host == "smtp.hostinger.com"
    assert res.smtp_port == 465


def test_resolve_siteground_hint_fills_ports_only_never_hostname(tmp_profile):
    _setup(tmp_profile)
    res = sr.resolve_for_email("info@alien.fi", provider_hint="siteground")
    assert res.status == sr.STATUS_UNKNOWN
    assert res.needs_hostname is True
    assert res.imap_host == ""
    assert res.smtp_host == ""
    assert res.imap_port == 993
    assert res.smtp_port == 465


def test_resolve_reuses_verified_domain_from_prior_mailbox(tmp_profile):
    """Tier 1: a second mailbox on the SAME domain is offered the exact
    server a prior successful onboarding used - no provider hint needed."""
    _setup(tmp_profile)
    _onboard("alienfi", "info@alien.fi", "gcam1108.siteground.biz")
    res = sr.resolve_for_email("sales@alien.fi")
    assert res.status == sr.STATUS_VERIFIED
    assert res.complete
    assert res.imap_host == "gcam1108.siteground.biz"
    assert res.imap_port == 993
    assert res.smtp_host == "gcam1108.siteground.biz"
    assert "alienfi" in res.source


def test_resolve_does_not_leak_across_different_domains(tmp_profile):
    """A verified server for one tenant's domain must never be offered to
    an unrelated domain."""
    _setup(tmp_profile)
    _onboard("alienfi", "info@alien.fi", "gcam1108.siteground.biz")
    res = sr.resolve_for_email("info@completely-different.example", allow_mx_autoconfig=False)
    assert res.status == sr.STATUS_UNKNOWN
    assert res.imap_host == ""


def test_resolve_preset_domain_tag_is_verified(tmp_profile):
    """Tier 1b: an admin-tagged preset domain is a VERIFIED match even
    with no mailbox registered yet."""
    _setup(tmp_profile)
    mb_quick.preset_save(
        "SiteGround A",
        {
            "imap_host": "gcam1092.siteground.biz",
            "imap_port": 993,
            "smtp_host": "gcam1092.siteground.biz",
            "smtp_port": 465,
            "domains": ["memecoinist.com"],
        },
    )
    res = sr.resolve_for_email("ads@memecoinist.com")
    assert res.status == sr.STATUS_VERIFIED
    assert res.preset_name == "SiteGround A"
    assert res.imap_host == "gcam1092.siteground.biz"


def test_resolve_existing_mailbox_fallback_tier2(tmp_profile):
    """Tier 2: even without the settings.json ledger, an already-registered
    mailbox on the same domain (read straight from config.toml) is offered
    as a VERIFIED match."""
    _setup(tmp_profile)
    _onboard("coinography", "ads@coinography.com", "mail.coinography.com")
    # Simulate an older deployment with no verified_mail_servers ledger yet.
    from mailops import config

    settings = config.load_settings()
    settings.pop("verified_mail_servers", None)
    config.save_settings(settings)
    res = sr.resolve_for_email("info@coinography.com")
    assert res.status == sr.STATUS_VERIFIED
    assert res.imap_host == "mail.coinography.com"
    assert "coinography" in res.source


def test_resolve_mx_suggestion_is_opt_in_and_labelled_suggested(tmp_profile):
    _setup(tmp_profile)
    os.environ["MAILOPS_AUTOCONFIG_MX"] = "1"
    res = sr.resolve_for_email(
        "hi@brand.example",
        mx_resolver=lambda domain: ["mx1.zoho.com"],
    )
    assert res.status == sr.STATUS_SUGGESTED
    assert res.complete
    assert res.imap_host == "imap.zoho.com"
    assert "Unverified suggestion" in res.source


def test_resolve_mx_suggestion_disabled_without_env_flag(tmp_profile):
    _setup(tmp_profile)
    res = sr.resolve_for_email(
        "hi@brand.example",
        mx_resolver=lambda domain: ["mx1.zoho.com"],
    )
    assert res.status == sr.STATUS_UNKNOWN


def test_reject_webmail_portal_url_used_everywhere():
    assert wm.reject_webmail_portal_url("https://webmail.example.com/login") is not None
    assert wm.reject_webmail_portal_url("imap.hostinger.com") is None


# =============================================================================
#  Smart Add Email - UI flow (mb:smart, mb:smart:pick:*, mb:smart:change:*)
# =============================================================================


def test_smart_add_entry_group_requires_admin(tmp_profile):
    _setup(tmp_profile)
    upd, q = callback_update("mb:smart", USER_B, GROUP, "group")
    import mailops.bot.app as app_mod

    _run(app_mod.on_callback(upd, _ctx()))
    assert q.edits == []


def test_smart_add_entry_arms_email_slot(tmp_profile):
    _setup(tmp_profile)
    upd, q = callback_update("mb:smart", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    assert q.edits, "Smart Add Email must render the intro screen"
    text, _markup = q.edits[-1]
    assert "Smart Add Email" in text
    slot = mb_bot.AWAITING.get(USER_A)
    assert slot and slot["field"] == mb_bot._SMART_EMAIL_FIELD


def test_smart_add_hostinger_domain_reaches_review_card_verified(tmp_profile):
    """Email at a brand-new domain + no provider hint -> UNKNOWN -> provider
    pick screen; picking Hostinger completes the server and reaches the
    VERIFIED review card without ever asking for IMAP/SMTP host/port."""
    _setup(tmp_profile)
    upd, q = callback_update("mb:smart", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))

    upd, msg = _text_update(USER_A, USER_A, "private", "info@newbrand.example")
    _run(mb_bot.handle_text(upd, _ctx()))
    text, markup = msg.sent[-1]
    assert "No verified or reliable server match" in text
    datas = callback_datas(markup)
    hostinger_cb = next(d for d in datas if d.startswith("mb:smart:pick:hostinger:"))

    upd, q = callback_update(hostinger_cb, USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    text, markup = q.edits[-1]
    assert "VERIFIED" in text
    assert "Confirm & arm credentials" in [b.text for row in markup.inline_keyboard for b in row]
    sessions = os_mod.list_sessions(USER_A)
    assert sessions[0].fields["imap_host"] == "imap.hostinger.com"
    assert sessions[0].fields["smtp_host"] == "smtp.hostinger.com"


def test_smart_add_verified_domain_reuse_skips_provider_pick(tmp_profile):
    """Second mailbox on an already-onboarded domain goes straight from the
    email message to the VERIFIED review card - no provider pick screen."""
    _setup(tmp_profile)
    _onboard("alienfi", "info@alien.fi", "gcam1108.siteground.biz")

    upd, q = callback_update("mb:smart", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    upd, msg = _text_update(USER_A, USER_A, "private", "sales@alien.fi")
    _run(mb_bot.handle_text(upd, _ctx()))
    text, markup = msg.sent[-1]
    assert "VERIFIED" in text
    assert "Confirm & arm credentials" in [b.text for row in markup.inline_keyboard for b in row]
    sessions = os_mod.list_sessions(USER_A)
    newest = sessions[0]
    assert newest.fields["email"] == "sales@alien.fi"
    assert newest.fields["imap_host"] == "gcam1108.siteground.biz"


def test_smart_add_siteground_pick_asks_hostname_once_applies_to_both(tmp_profile):
    """SiteGround still needs the per-tenant hostname; it must be asked
    exactly once and applied to BOTH imap_host and smtp_host."""
    _setup(tmp_profile)
    upd, q = callback_update("mb:smart", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    upd, msg = _text_update(USER_A, USER_A, "private", "info@othersite.example")
    _run(mb_bot.handle_text(upd, _ctx()))
    text, markup = msg.sent[-1]
    datas = callback_datas(markup)
    sg_cb = next(d for d in datas if d.startswith("mb:smart:pick:siteground:"))

    upd, q = callback_update(sg_cb, USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    text, _mk = q.edits[-1]
    assert "mail server hostname" in text.lower()
    slot = mb_bot.AWAITING.get(USER_A)
    assert slot and slot["field"] == mb_bot._SMART_HOST_FIELD

    upd, msg = _text_update(USER_A, USER_A, "private", "gcam1234.siteground.biz")
    _run(mb_bot.handle_text(upd, _ctx()))
    text, markup = msg.sent[-1]
    assert "Confirm & arm credentials" in [b.text for row in markup.inline_keyboard for b in row]
    sessions = os_mod.list_sessions(USER_A)
    newest = sessions[-1]
    assert newest.fields["imap_host"] == "gcam1234.siteground.biz"
    assert newest.fields["smtp_host"] == "gcam1234.siteground.biz"
    assert int(newest.fields["imap_port"]) == 993
    assert int(newest.fields["smtp_port"]) == 465


def test_smart_add_siteground_host_rejects_webmail_url(tmp_profile):
    _setup(tmp_profile)
    upd, q = callback_update("mb:smart", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    upd, msg = _text_update(USER_A, USER_A, "private", "info@othersite2.example")
    _run(mb_bot.handle_text(upd, _ctx()))
    text, markup = msg.sent[-1]
    sg_cb = next(d for d in callback_datas(markup) if d.startswith("mb:smart:pick:siteground:"))
    upd, q = callback_update(sg_cb, USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))

    upd, msg = _text_update(
        USER_A, USER_A, "private", "https://webmail.othersite2.example/login"
    )
    _run(mb_bot.handle_text(upd, _ctx()))
    text, _mk = msg.sent[-1]
    assert "web URL" in text
    # The admin can retry: slot is re-armed.
    assert mb_bot.AWAITING.get(USER_A) and mb_bot.AWAITING[USER_A]["field"] == mb_bot._SMART_HOST_FIELD


def test_smart_add_custom_pick_hands_off_to_field_wizard(tmp_profile):
    _setup(tmp_profile)
    upd, q = callback_update("mb:smart", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    upd, msg = _text_update(USER_A, USER_A, "private", "info@customdomain.example")
    _run(mb_bot.handle_text(upd, _ctx()))
    text, markup = msg.sent[-1]
    custom_cb = next(d for d in callback_datas(markup) if d.startswith("mb:smart:pick:custom:"))

    upd, q = callback_update(custom_cb, USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    text, _mk = q.edits[-1]
    # account_id + email are already filled by Smart Add; display_name is
    # the next unresolved (skippable) field, then the host/port fields
    # Smart Add could not resolve. Nothing already-known is re-asked.
    assert "Next step: Display name" in text
    slot = mb_bot.AWAITING.get(USER_A)
    assert slot and slot["field"] == "display_name"


def test_smart_add_preset_pick_reaches_verified_review(tmp_profile):
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
    upd, q = callback_update("mb:smart", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    upd, msg = _text_update(USER_A, USER_A, "private", "info@brandnewbiz.example")
    _run(mb_bot.handle_text(upd, _ctx()))
    text, markup = msg.sent[-1]
    preset_cb = next(d for d in callback_datas(markup) if d.startswith("mb:smart:pickpreset:SiteGround A:"))

    upd, q = callback_update(preset_cb, USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    text, markup = q.edits[-1]
    assert "VERIFIED" in text
    assert "SiteGround A" in text
    sessions = os_mod.list_sessions(USER_A)
    assert sessions[-1].fields["imap_host"] == "gcam1108.siteground.biz"


def test_smart_add_change_server_returns_to_provider_pick(tmp_profile):
    _setup(tmp_profile)
    upd, q = callback_update("mb:smart", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    upd, msg = _text_update(USER_A, USER_A, "private", "info@changeme.example")
    _run(mb_bot.handle_text(upd, _ctx()))
    text, markup = msg.sent[-1]
    hostinger_cb = next(d for d in callback_datas(markup) if d.startswith("mb:smart:pick:hostinger:"))
    upd, q = callback_update(hostinger_cb, USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    text, markup = q.edits[-1]
    change_cb = next(d for d in callback_datas(markup) if d.startswith("mb:smart:change:"))

    upd, q = callback_update(change_cb, USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    text, _mk = q.edits[-1]
    assert "Pick the provider" in text
    sessions = os_mod.list_sessions(USER_A)
    assert sessions[-1].fields.get("imap_host", "") == ""


def test_smart_add_rejects_bad_email(tmp_profile):
    _setup(tmp_profile)
    upd, q = callback_update("mb:smart", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    upd, msg = _text_update(USER_A, USER_A, "private", "not-an-email")
    _run(mb_bot.handle_text(upd, _ctx()))
    text, _mk = msg.sent[-1]
    assert "invalid" in text.lower()
    # Slot stays armed so the admin can retry.
    assert mb_bot.AWAITING.get(USER_A) and mb_bot.AWAITING[USER_A]["field"] == mb_bot._SMART_EMAIL_FIELD


def test_smart_add_cancel_word_aborts(tmp_profile):
    _setup(tmp_profile)
    upd, q = callback_update("mb:smart", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    upd, msg = _text_update(USER_A, USER_A, "private", "cancel")
    _run(mb_bot.handle_text(upd, _ctx()))
    assert "Cancelled" in msg.sent[-1][0]
    assert USER_A not in mb_bot.AWAITING


def test_smart_add_full_flow_through_arm_and_group_handoff(tmp_profile):
    """End-to-end group-first Smart Add: email -> Hostinger -> review ->
    Confirm & arm in the group hands off to DM exactly like the other
    onboarding entry points."""
    _setup(tmp_profile)
    upd, q = callback_update("mb:smart", USER_A, GROUP, "group")
    _run(mb_bot.handle_callback(upd, _ctx()))
    assert mb_bot.AWAITING[USER_A]["chat_id"] == GROUP

    upd, msg = _text_update(USER_A, GROUP, "group", "info@groupbrand.example")
    _run(mb_bot.handle_text(upd, _ctx()))
    text, markup = msg.sent[-1]
    hostinger_cb = next(d for d in callback_datas(markup) if d.startswith("mb:smart:pick:hostinger:"))

    upd, q = callback_update(hostinger_cb, USER_A, GROUP, "group")
    _run(mb_bot.handle_callback(upd, _ctx()))
    text, markup = q.edits[-1]
    arm_cb = next(d for d in callback_datas(markup) if d.startswith("mb:arm:"))

    from tests.test_mailbox_onboarding import _RecordingBot, _group_ctx

    bot = _RecordingBot()
    ctx = _group_ctx(bot=bot)
    upd, q = callback_update(arm_cb, USER_A, GROUP, "group")
    _run(mb_bot.handle_callback(upd, ctx))
    group_text, _mk = q.edits[-1]
    assert "private chat" in group_text.lower()
    assert len(bot.sent) == 1
    token = arm_cb.split(":")[2]
    assert os_mod.get_session(token, USER_A).state == os_mod.STATE_AWAITING_CREDS


def test_smart_add_unauthorized_other_admin_blocked_from_provider_pick(tmp_profile):
    """Session-owner binding applies to Smart Add callbacks exactly like
    the field-by-field wizard: a second admin tapping a provider-pick
    button bound to USER_A's token must be refused."""
    _setup(tmp_profile)
    access.set_role(USER_A, USER_B, "owner")
    upd, q = callback_update("mb:smart", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    upd, msg = _text_update(USER_A, USER_A, "private", "info@ownertest.example")
    _run(mb_bot.handle_text(upd, _ctx()))
    text, markup = msg.sent[-1]
    hostinger_cb = next(d for d in callback_datas(markup) if d.startswith("mb:smart:pick:hostinger:"))

    upd, q = callback_update(hostinger_cb, USER_B, USER_B, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    assert q.edits == []
    alerts = [t for t, is_alert in q.answers if is_alert]
    assert alerts


# =============================================================================
#  mailops.group_credentials - policy helpers (pure, no Telegram)
# =============================================================================


def test_group_credentials_disabled_by_default(tmp_profile):
    _setup(tmp_profile)
    assert gc.enabled() is False
    assert gc.pinned_group_id() is None
    assert gc.allowed_for_group(GROUP) is False


def test_group_credentials_requires_both_flag_and_exact_pin(tmp_profile, monkeypatch):
    _setup(tmp_profile)
    monkeypatch.setenv("MAILOPS_ALLOW_GROUP_CREDENTIALS", "1")
    # Flag alone (no pin) stays inert.
    assert gc.allowed_for_group(GROUP) is False
    monkeypatch.setenv("MAILOPS_GROUP_CREDENTIALS_GROUP_ID", str(GROUP))
    assert gc.allowed_for_group(GROUP) is True
    # A DIFFERENT group id, even with the flag on, is refused.
    assert gc.allowed_for_group(GROUP - 1) is False
    assert gc.allowed_for_group(None) is False


def test_group_credentials_pin_alone_without_flag_stays_inert(tmp_profile, monkeypatch):
    _setup(tmp_profile)
    monkeypatch.setenv("MAILOPS_GROUP_CREDENTIALS_GROUP_ID", str(GROUP))
    assert gc.allowed_for_group(GROUP) is False


# =============================================================================
#  Group password entry (opt-in, owner-only, single pinned group)
# =============================================================================


class _GroupPwBot:
    """context.bot double: send_message returns an object with a real
    message_id so the prompt<->reply binding can be exercised, and
    records every outbound text (so a test can assert the password is
    never echoed in any of them)."""

    def __init__(self):
        self.sent: list[dict] = []
        self._next_id = 500

    async def send_message(self, chat_id, text, reply_markup=None):
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text})
        return SimpleNamespace(message_id=self._next_id)


def _group_pw_ctx():
    return SimpleNamespace(user_data={}, bot=_GroupPwBot())


class _GroupReplyMessage:
    """A simulated group message, optionally a reply to another message,
    with a controllable delete() outcome."""

    def __init__(self, text, *, reply_to_id=None, delete_ok=True, delete_exc=None):
        self.text = text
        self.reply_to_message = (
            SimpleNamespace(message_id=reply_to_id) if reply_to_id is not None else None
        )
        self._delete_ok = delete_ok
        self._delete_exc = delete_exc
        self.deleted = False
        self.replies: list[tuple] = []

    async def delete(self):
        if not self._delete_ok:
            raise (self._delete_exc or RuntimeError("cannot delete in this chat"))
        self.deleted = True

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


def _group_pw_update(user_id, text, *, reply_to_id=None, delete_ok=True):
    msg = _GroupReplyMessage(text, reply_to_id=reply_to_id, delete_ok=delete_ok)
    upd = SimpleNamespace(
        callback_query=None,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=GROUP, type="supergroup"),
        effective_message=msg,
        message=msg,
    )
    return upd, msg


def _enable_group_pw(monkeypatch):
    monkeypatch.setenv("MAILOPS_ALLOW_GROUP_CREDENTIALS", "1")
    monkeypatch.setenv("MAILOPS_GROUP_CREDENTIALS_GROUP_ID", str(GROUP))


def _arm_group_pw(session, ctx=None, *, user_id=USER_A):
    """Fire mb:group_pw:<token>:<nonce>; returns (slot, query, prompt_id)."""
    ctx = ctx or _group_pw_ctx()
    upd, q = callback_update(
        f"mb:group_pw:{session.token}:{session.nonce}", user_id, GROUP, "group"
    )
    _run(mb_bot.handle_callback(upd, ctx))
    slot = mb_bot.AWAITING.get(user_id)
    prompt_id = slot.get("prompt_message_id") if slot else None
    return slot, q, prompt_id, ctx


def test_group_pw_arm_denied_when_feature_disabled(tmp_profile):
    _setup(tmp_profile)
    session = _complete_wizard_session()
    slot, q, _pid, _ctx = _arm_group_pw(session)
    assert slot is None
    alerts = [t for t, is_alert in q.answers if is_alert]
    assert any("not enabled" in (t or "").lower() for t in alerts)


def test_group_pw_arm_denied_for_non_owner(tmp_profile, monkeypatch):
    """P_ADMIN alone (there is no non-owner admin in this role model, but
    we still exercise the explicit is_owner re-check) is not enough — a
    stranger / operator must never reach the arm step at all."""
    _setup(tmp_profile)
    _enable_group_pw(monkeypatch)
    session = _complete_wizard_session()
    # USER_B is an operator (no P_ADMIN) by default -> blocked by the
    # top-level admin guard before even reaching _arm_group_pw_prompt.
    upd, q = callback_update(
        f"mb:group_pw:{session.token}:{session.nonce}", USER_B, GROUP, "group"
    )
    _run(mb_bot.handle_callback(upd, _group_pw_ctx()))
    assert USER_B not in mb_bot.AWAITING
    assert q.edits == []


def test_group_pw_arm_posts_prompt_and_binds_slot_to_owner(tmp_profile, monkeypatch):
    _setup(tmp_profile)
    _enable_group_pw(monkeypatch)
    session = _complete_wizard_session()
    slot, q, prompt_id, ctx = _arm_group_pw(session)
    assert slot is not None
    assert slot["field"] == mb_bot._GROUP_PW_FIELD
    assert slot["chat_id"] == GROUP
    assert prompt_id is not None
    assert len(ctx.bot.sent) == 1
    warning_text = ctx.bot.sent[0]["text"]
    assert "REPLY DIRECTLY" in warning_text
    assert "NOT guaranteed erasure" in warning_text or "retain a copy" in warning_text


def test_group_pw_plain_non_reply_message_is_never_used_as_password(tmp_profile, monkeypatch):
    """Telegram privacy-mode compatibility: even if a bare group message
    somehow reached handle_text, it must never be treated as a password
    unless it is a reply to the bot's own prompt."""
    _setup(tmp_profile)
    _enable_group_pw(monkeypatch)
    session = _complete_wizard_session()
    _slot, _q, prompt_id, ctx = _arm_group_pw(session)

    upd, msg = _group_pw_update(USER_A, PASSWORD, reply_to_id=None)
    _run(mb_bot._handle_group_password(upd, ctx, mb_bot.AWAITING[USER_A], PASSWORD))
    assert msg.deleted is False  # never even attempted
    assert os_mod.get_session(session.token, USER_A).state == os_mod.STATE_AWAITING_CREDS
    joined = " ".join(m["text"] for m in ctx.bot.sent)
    assert PASSWORD not in joined
    assert "not recognised as a reply" in joined.lower()


def test_group_pw_reply_to_wrong_message_id_is_refused(tmp_profile, monkeypatch):
    _setup(tmp_profile)
    _enable_group_pw(monkeypatch)
    session = _complete_wizard_session()
    _slot, _q, prompt_id, ctx = _arm_group_pw(session)

    upd, msg = _group_pw_update(USER_A, PASSWORD, reply_to_id=prompt_id + 999)
    _run(mb_bot._handle_group_password(upd, ctx, mb_bot.AWAITING[USER_A], PASSWORD))
    assert msg.deleted is False
    assert os_mod.get_session(session.token, USER_A).state == os_mod.STATE_AWAITING_CREDS


def test_group_pw_correct_reply_deletes_validates_and_registers(tmp_profile, monkeypatch):
    _setup(tmp_profile)
    _enable_group_pw(monkeypatch)
    session = _complete_wizard_session()
    _slot, _q, prompt_id, ctx = _arm_group_pw(session)
    slot = mb_bot.AWAITING[USER_A]

    monkeypatch.setattr(wm, "run_pending", _make_fake_run_pending())
    upd, msg = _group_pw_update(USER_A, PASSWORD, reply_to_id=prompt_id)
    _run(mb_bot._handle_group_password(upd, ctx, slot, PASSWORD))

    assert msg.deleted is True
    assert USER_A not in mb_bot.AWAITING
    joined = " ".join(m["text"] for m in ctx.bot.sent)
    assert PASSWORD not in joined
    final = os_mod.get_session(session.token, USER_A)
    assert final.state == os_mod.STATE_DONE


def test_group_pw_delete_failure_stops_onboarding_and_invalidates_session(tmp_profile, monkeypatch):
    """Hard requirement: if Mailops cannot delete the password reply,
    onboarding STOPS, the session is invalidated, and the password is
    never used - never a silent continue."""
    _setup(tmp_profile)
    _enable_group_pw(monkeypatch)
    session = _complete_wizard_session()
    _slot, _q, prompt_id, ctx = _arm_group_pw(session)
    slot = mb_bot.AWAITING[USER_A]

    called = {"n": 0}

    def _should_not_run(*a, **k):
        called["n"] += 1
        raise AssertionError("run_pending must never be called when delete() failed")

    monkeypatch.setattr(wm, "run_pending", _should_not_run)
    upd, msg = _group_pw_update(USER_A, PASSWORD, reply_to_id=prompt_id)
    msg._delete_ok = False
    _run(mb_bot._handle_group_password(upd, ctx, slot, PASSWORD))

    assert called["n"] == 0
    assert USER_A not in mb_bot.AWAITING
    final = os_mod.get_session(session.token, USER_A)
    assert final.state == os_mod.STATE_CANCELLED
    joined = " ".join(m["text"] for m in ctx.bot.sent)
    assert PASSWORD not in joined
    assert "could not delete" in joined.lower() or "could not" in joined.lower()
    assert "stopped" in joined.lower() or "cancelled" in joined.lower() or "discarded" in joined.lower()


def test_group_pw_cancel_word_aborts_without_validating(tmp_profile, monkeypatch):
    _setup(tmp_profile)
    _enable_group_pw(monkeypatch)
    session = _complete_wizard_session()
    _slot, _q, prompt_id, ctx = _arm_group_pw(session)
    slot = mb_bot.AWAITING[USER_A]

    def _should_not_run(*a, **k):
        raise AssertionError("cancel must never reach run_pending")

    monkeypatch.setattr(wm, "run_pending", _should_not_run)
    upd, msg = _group_pw_update(USER_A, "cancel", reply_to_id=prompt_id)
    _run(mb_bot._handle_group_password(upd, ctx, slot, "cancel"))
    assert msg.deleted is True
    assert os_mod.get_session(session.token, USER_A).state == os_mod.STATE_AWAITING_CREDS


def test_group_pw_replay_after_ttl_expired_is_refused(tmp_profile, monkeypatch):
    _setup(tmp_profile)
    _enable_group_pw(monkeypatch)
    session = _complete_wizard_session()
    _slot, _q, prompt_id, ctx = _arm_group_pw(session)
    slot = dict(mb_bot.AWAITING[USER_A])
    slot["armed_at"] = time.time() - gc.ARM_TTL_S - 5

    def _should_not_run(*a, **k):
        raise AssertionError("expired capture must never reach run_pending")

    monkeypatch.setattr(wm, "run_pending", _should_not_run)
    upd, msg = _group_pw_update(USER_A, PASSWORD, reply_to_id=prompt_id)
    mb_bot.AWAITING[USER_A] = slot
    _run(mb_bot._handle_group_password(upd, ctx, slot, PASSWORD))
    assert msg.deleted is True
    assert os_mod.get_session(session.token, USER_A).state == os_mod.STATE_AWAITING_CREDS
    joined = " ".join(m["text"] for m in ctx.bot.sent)
    assert PASSWORD not in joined


def test_group_pw_stale_nonce_after_rearm_is_refused(tmp_profile, monkeypatch):
    """If the session re-arms (nonce rotates) between the prompt and the
    reply arriving, the old slot's nonce no longer matches - refuse."""
    _setup(tmp_profile)
    _enable_group_pw(monkeypatch)
    session = _complete_wizard_session()
    _slot, _q, prompt_id, ctx = _arm_group_pw(session)
    stale_slot = dict(mb_bot.AWAITING[USER_A])

    # Re-arm: nonce rotates to a new value server-side via cancel (any
    # state transition rotates the session so the old slot's bound nonce
    # becomes stale).
    session2 = os_mod.get_session(session.token, USER_A)
    os_mod.cancel(session.token, USER_A, nonce=session2.nonce)
    cancelled = os_mod.get_session(session.token, USER_A)
    assert cancelled.state == os_mod.STATE_CANCELLED
    assert cancelled.nonce != stale_slot["nonce"]

    def _should_not_run(*a, **k):
        raise AssertionError("stale-nonce capture must never reach run_pending")

    monkeypatch.setattr(wm, "run_pending", _should_not_run)
    upd, msg = _group_pw_update(USER_A, PASSWORD, reply_to_id=prompt_id)
    _run(mb_bot._handle_group_password(upd, ctx, stale_slot, PASSWORD))
    assert msg.deleted is True


def test_group_pw_feature_revoked_mid_flow_blocks_even_with_valid_reply(tmp_profile, monkeypatch):
    _setup(tmp_profile)
    _enable_group_pw(monkeypatch)
    session = _complete_wizard_session()
    _slot, _q, prompt_id, ctx = _arm_group_pw(session)
    slot = mb_bot.AWAITING[USER_A]

    monkeypatch.delenv("MAILOPS_ALLOW_GROUP_CREDENTIALS", raising=False)

    def _should_not_run(*a, **k):
        raise AssertionError("revoked feature must never reach run_pending")

    monkeypatch.setattr(wm, "run_pending", _should_not_run)
    upd, msg = _group_pw_update(USER_A, PASSWORD, reply_to_id=prompt_id)
    _run(mb_bot._handle_group_password(upd, ctx, slot, PASSWORD))
    assert os_mod.get_session(session.token, USER_A).state == os_mod.STATE_AWAITING_CREDS


def test_group_pw_owner_demoted_mid_flow_blocks(tmp_profile, monkeypatch):
    _setup(tmp_profile, allowed=f"{USER_B},{USER_A}", home=f"{USER_B},{GROUP}")
    _enable_group_pw(monkeypatch)
    # USER_B is the bootstrap owner here; promote USER_A to a (non-
    # bootstrap) owner so it can later be demoted mid-flow.
    access.set_role(USER_B, USER_A, "owner")
    session = _complete_wizard_session()
    _slot, _q, prompt_id, ctx = _arm_group_pw(session)
    slot = mb_bot.AWAITING[USER_A]

    access.set_role(USER_B, USER_A, "operator")

    def _should_not_run(*a, **k):
        raise AssertionError("demoted admin's capture must never reach run_pending")

    monkeypatch.setattr(wm, "run_pending", _should_not_run)
    upd, msg = _group_pw_update(USER_A, PASSWORD, reply_to_id=prompt_id)
    _run(mb_bot._handle_group_password(upd, ctx, slot, PASSWORD))


def test_group_pw_validation_failure_still_deletes_and_never_leaks_password(tmp_profile, monkeypatch):
    _setup(tmp_profile)
    _enable_group_pw(monkeypatch)
    session = _complete_wizard_session()
    _slot, _q, prompt_id, ctx = _arm_group_pw(session)
    slot = mb_bot.AWAITING[USER_A]

    def _fail_run_pending(token, *, imap_password, smtp_password=None, him=None):
        os_mod.begin_validation(token, USER_A)
        os_mod.finish_failure(token, USER_A, "imap: FAIL (authentication failed)")
        return wm.CheckResult(False, False, True, {"imap": "FAIL"})

    monkeypatch.setattr(wm, "run_pending", _fail_run_pending)
    upd, msg = _group_pw_update(USER_A, PASSWORD, reply_to_id=prompt_id)
    _run(mb_bot._handle_group_password(upd, ctx, slot, PASSWORD))
    assert msg.deleted is True
    final = os_mod.get_session(session.token, USER_A)
    assert final.state == os_mod.STATE_FAILED
    joined = " ".join(m["text"] for m in ctx.bot.sent) + final.last_error
    assert PASSWORD not in joined


def test_group_pw_disabled_by_default_main_dm_fallback_still_offered(tmp_profile, monkeypatch):
    """Default-off: the group handoff screen must still fall back to the
    existing private-DM credential path, with no group-entry button."""
    _setup(tmp_profile)
    session = _filled_draft_session()
    bot = _GroupPwBot()
    ctx = SimpleNamespace(user_data={}, bot=bot)
    upd, q = callback_update(
        f"mb:arm:{session.token}:{session.nonce}", USER_A, GROUP, "group"
    )
    _run(mb_bot.handle_callback(upd, ctx))
    group_text, markup = q.edits[-1]
    datas = callback_datas(markup) if markup else []
    assert not any(d.startswith("mb:group_pw:") for d in datas)
    assert len(bot.sent) == 1  # DM fallback still fires


def test_group_pw_enabled_offers_both_dm_and_group_button_to_owner(tmp_profile, monkeypatch):
    _setup(tmp_profile)
    _enable_group_pw(monkeypatch)
    session = _filled_draft_session()
    bot = _GroupPwBot()
    ctx = SimpleNamespace(user_data={}, bot=bot)
    upd, q = callback_update(
        f"mb:arm:{session.token}:{session.nonce}", USER_A, GROUP, "group"
    )
    _run(mb_bot.handle_callback(upd, ctx))
    group_text, markup = q.edits[-1]
    datas = callback_datas(markup)
    assert any(d.startswith("mb:group_pw:") for d in datas)
    assert len(bot.sent) == 1  # DM is still offered as the primary path


def _filled_draft_session() -> os_mod.OnboardingSession:
    """Like _complete_wizard_session but stops at state=DRAFT, ready for
    the mb:arm: tap itself (used to test what the arm button offers)."""
    session = os_mod.start_session(USER_A)
    fields = {
        "account_id": "coinography",
        "email": "ads@coinography.example",
        "display_name": "Coinography",
        "imap_host": "mail.coinography.example",
        "imap_port": 993,
        "imap_user": "ads@coinography.example",
        "smtp_host": "mail.coinography.example",
        "smtp_port": 465,
        "smtp_user": "ads@coinography.example",
    }
    session = os_mod.set_fields(session.token, USER_A, nonce=session.nonce, patch=fields)
    assert session.state == os_mod.STATE_DRAFT
    return session


def _complete_wizard_session(fields_patch: dict | None = None) -> os_mod.OnboardingSession:
    session = os_mod.start_session(USER_A)
    fields = {
        "account_id": "coinography",
        "email": "ads@coinography.example",
        "display_name": "Coinography",
        "imap_host": "mail.coinography.example",
        "imap_port": 993,
        "imap_user": "ads@coinography.example",
        "smtp_host": "mail.coinography.example",
        "smtp_port": 465,
        "smtp_user": "ads@coinography.example",
    }
    if fields_patch:
        fields.update(fields_patch)
    session = os_mod.set_fields(session.token, USER_A, nonce=session.nonce, patch=fields)
    session = os_mod.arm_for_credentials(session.token, USER_A, nonce=session.nonce)
    assert session.state == os_mod.STATE_AWAITING_CREDS
    return session


def _make_fake_run_pending():
    def _fake(token, *, imap_password, smtp_password=None, him=None):
        os_mod.begin_validation(token, USER_A)
        os_mod.finish_success(token, USER_A)
        return wm.CheckResult(True, True, True, {})

    return _fake


# =============================================================================
#  Phase 7 - navigation / callback-routing regression (group allowlist)
# =============================================================================


def test_smart_and_preset_callbacks_are_in_group_allowlist():
    import mailops.bot.app as app_mod

    for data in (
        "mb:smart",
        "mb:smart:pick:hostinger:tok:nonce",
        "mb:smart:pick:siteground:tok:nonce",
        "mb:smart:pick:custom:tok:nonce",
        "mb:smart:pickpreset:My Preset:tok:nonce",
        "mb:smart:change:tok:nonce",
        "mb:preset",
        "mb:preset:p:1",
        "mb:preset:view:My Preset",
        "mb:preset:new",
        "mb:preset:edit:My Preset",
        "mb:preset:delask:My Preset",
        "mb:preset:delconfirm:My Preset",
        "mb:group_pw:tok:nonce",
    ):
        assert app_mod.group_mailbox_callback_allowed(data), data


def test_smart_and_preset_group_safe_body_matches_app_allowlist():
    """mailboxes._group_safe_body (the local defence-in-depth check) must
    agree with app.group_mailbox_callback_allowed for every Smart Add /
    Preset body - no silent divergence between the two allow-lists."""
    import mailops.bot.app as app_mod

    bodies = [
        "smart",
        "smart:pick:hostinger:tok:nonce",
        "smart:pickpreset:x:tok:nonce",
        "smart:change:tok:nonce",
        "preset",
        "preset:p:0",
        "preset:view:x",
        "preset:new",
        "preset:edit:x",
        "preset:delask:x",
        "preset:delconfirm:x",
        "group_pw:tok:nonce",
    ]
    for body in bodies:
        assert mb_bot._group_safe_body(body) is True, body
        assert app_mod.group_mailbox_callback_allowed("mb:" + body), body


def test_mailboxes_main_screen_exposes_smart_add_and_manage_presets():
    """Phase 9 success criterion: Smart Add and Manage Presets appear on
    the Mailboxes screen in BOTH private and group renders."""
    for in_group in (False, True):
        text, markup = mb_bot.main_screen(in_group=in_group)
        datas = callback_datas(markup)
        assert "mb:smart" in datas
        assert "mb:preset" in datas


def test_main_dashboard_mailboxes_entry_still_reachable_from_group(tmp_profile):
    """Dead-end check: tapping Mailboxes from the group dashboard must
    land on a screen that itself offers Smart Add / Manage Presets."""
    _setup(tmp_profile)
    upd, q = callback_update("mb:m", USER_A, GROUP, "group")
    _run(mb_bot.handle_callback(upd, _ctx()))
    text, markup = q.edits[-1]
    datas = callback_datas(markup)
    assert "mb:smart" in datas
    assert "mb:preset" in datas


def test_smart_add_email_prompt_expires_after_ttl(tmp_profile, monkeypatch):
    """Pre-deployment audit fix (generalized UNTOKENED_FIELD_TTL_S): the
    first-message arm of Smart Add ("mb:smart") has no onboarding-session
    token yet to bound it, so — like purpose-edit and mailbox-search — a
    stale slot must expire rather than silently treat a message sent hours
    later as the email address to resolve."""
    _setup(tmp_profile)
    upd, q = callback_update("mb:smart", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    slot = mb_bot.AWAITING.get(USER_A)
    assert slot and slot["field"] == mb_bot._SMART_EMAIL_FIELD
    assert slot.get("armed_at") is not None

    real_time = __import__("time")
    future = real_time.time() + mb_bot.UNTOKENED_FIELD_TTL_S + 10
    monkeypatch.setattr(mb_bot.time, "time", lambda: future)

    upd2, _msg2 = _text_update(USER_A, USER_A, "private", "stale@example.com")
    consumed = _run(mb_bot.handle_text(upd2, _ctx()))
    assert consumed is False
    assert USER_A not in mb_bot.AWAITING
    # No onboarding session was ever started from the stale text.
    assert os_mod.list_sessions(USER_A) == []


def test_preset_create_prompt_expires_after_ttl(tmp_profile, monkeypatch):
    """Same TTL fix applied to ``mb:preset:new`` (Manage Server Presets'
    free-text "name | host | port..." capture), which also had no expiry
    before the generalized ``UNTOKENED_FIELD_TTL_S`` fix in
    ``mailboxes._set_awaiting``."""
    _setup(tmp_profile)
    import mailops.bot.presets_ui as presets_ui

    upd, q = callback_update("mb:preset:new", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    slot = mb_bot.AWAITING.get(USER_A)
    assert slot and slot["field"] == presets_ui.CREATE_FIELD
    assert slot.get("armed_at") is not None

    real_time = __import__("time")
    future = real_time.time() + mb_bot.UNTOKENED_FIELD_TTL_S + 10
    monkeypatch.setattr(mb_bot.time, "time", lambda: future)

    upd2, _msg2 = _text_update(USER_A, USER_A, "private", "Stale Preset | mail.example.com | 993")
    consumed = _run(mb_bot.handle_text(upd2, _ctx()))
    assert consumed is False
    assert USER_A not in mb_bot.AWAITING
    from mailops.bot import mb_quick
    assert mb_quick.preset_get("Stale Preset") is None


# =============================================================================
#  Phase 8 - no accidental sending / preserved production safety defaults
# =============================================================================


def test_smart_add_never_sends_real_email_only_login_handshake(tmp_profile):
    """FakeCheckHim never exposes a send-capable method; running Smart Add
    end to end through run_pending must only ever call account check."""
    _setup(tmp_profile)
    upd, q = callback_update("mb:smart", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    upd, msg = _text_update(USER_A, USER_A, "private", "info@sendcheck.example")
    _run(mb_bot.handle_text(upd, _ctx()))
    text, markup = msg.sent[-1]
    hostinger_cb = next(d for d in callback_datas(markup) if d.startswith("mb:smart:pick:hostinger:"))
    upd, q = callback_update(hostinger_cb, USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    sessions = os_mod.list_sessions(USER_A)
    session = sessions[0]
    session = os_mod.arm_for_credentials(session.token, USER_A, nonce=session.nonce)
    him = FakeCheckHim()
    result = wm.run_pending(session.token, imap_password=PASSWORD, him=him)
    assert result.ok
    assert him.calls == [["-a", session.fields["account_id"], "account", "check"]]


def test_smart_add_registered_mailbox_starts_read_only_sending_off(tmp_profile):
    _setup(tmp_profile)
    session = _onboard("sendoffcheck", "info@sendoffcheck.example", "mail.sendoffcheck.example")
    from mailops.config import load_settings

    policies = load_settings().get("account_policies", {})
    pol = policies.get("sendoffcheck", {})
    assert pol.get("send") in (False, None)
    assert pol.get("organize") in (False, None)


