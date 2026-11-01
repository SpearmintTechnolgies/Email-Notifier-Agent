"""Telegram-initiated mailbox onboarding + lifecycle tests.

Covers the new modules introduced to let an authorized admin add, enable,
disable, health-check and manage business mailboxes from the Telegram group
control center without touching the terminal, config files or passwords
in chat.

Scope:
* ``mailops.onboarding_session`` - short-lived single-use sessions
  (TTL, user binding, nonce/CSRF, rate limits, state machine, replay).
* ``mailops.mailbox_admin`` - list / enable / disable / health / audit,
  with the Gmail account kept locked read-only and the Altcoin Informer
  production account preserved.
* ``mailops.webmail_onboarding.run_pending`` - the non-interactive bridge
  between the Telegram session and the hardened add_account flow (success,
  IMAP-fail rollback, SMTP-fail rollback, raised-exception rollback,
  duplicate id/email refused, no credentials ever persisted to the session).
* ``mailops.bot.mailboxes`` - the private-chat screens, admin-only guard,
  group-chat refusal, DM-capture env gating.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

import mailops.paths as paths
from mailops import access, mailbox_admin
from mailops import onboarding_session as os_mod
from mailops import webmail_onboarding as wm
from mailops.bot import mailboxes as mb_bot
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
    # The default conftest profile seeds "sales" + "info" accounts; blow
    # those away and start with an empty registry so each test writes its
    # own set of mailboxes.
    paths.ACCOUNTS_JSON.write_text("[]", encoding="utf-8")
    paths.CONFIG_TOML.write_text("", encoding="utf-8")
    from mailops.config import SETTINGS_JSON
    SETTINGS_JSON.write_text(
        json.dumps({"active_accounts": [], "account_policies": {}}),
        encoding="utf-8",
    )
    # Avoid Windows icacls side-effects when writing secret files.
    import mailops.webmail_onboarding as wm_mod

    wm_mod._restrict_to_current_user = lambda p: None  # type: ignore[assignment]
    # Isolate the mailbox AWAITING table across tests.
    mb_bot.AWAITING.clear()
    os.environ.pop("MAILOPS_ALLOW_DM_CREDENTIALS", None)


class FakeCheckHim:
    """Himalaya double for the add_account flow."""

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


# --------------------------------------------------------------------------- onboarding_session


def test_session_user_binding_and_single_use(tmp_profile):
    _setup(tmp_profile)
    session = os_mod.start_session(USER_A)
    assert session.state == os_mod.STATE_DRAFT
    assert session.user_id == USER_A
    with pytest.raises(os_mod.SessionError):
        os_mod.get_session(session.token, USER_B)
    # Correct user still works
    reloaded = os_mod.get_session(session.token, USER_A)
    assert reloaded.token == session.token


def test_session_nonce_prevents_replay(tmp_profile):
    _setup(tmp_profile)
    session = os_mod.start_session(USER_A)
    # First write succeeds and rotates the nonce.
    s2 = os_mod.set_fields(
        session.token, USER_A, nonce=session.nonce, patch={"account_id": "coinography"}
    )
    assert s2.nonce != session.nonce
    # Replaying the previous nonce fails.
    with pytest.raises(os_mod.SessionError, match="step was already used|step expired"):
        os_mod.set_fields(
            session.token, USER_A, nonce=session.nonce, patch={"email": "a@b.example"}
        )


def test_session_refuses_password_fields(tmp_profile):
    _setup(tmp_profile)
    session = os_mod.start_session(USER_A)
    for key in ("password", "imap_password", "smtp_password", "token"):
        with pytest.raises(os_mod.SessionError, match="Passwords are never stored"):
            os_mod.set_fields(
                session.token, USER_A, nonce=session.nonce, patch={key: "secret"}
            )


def test_session_expiry_marks_expired(tmp_profile, monkeypatch):
    _setup(tmp_profile)
    session = os_mod.start_session(USER_A)
    future = os_mod._now() + os_mod.SESSION_TTL_S + 60
    monkeypatch.setattr(os_mod, "_now", lambda: future)
    with pytest.raises(os_mod.SessionError, match="expired"):
        os_mod.get_session(session.token, USER_A)
    # Row is persisted as expired, not deleted (for audit).
    reloaded = os_mod.list_sessions(USER_A)[0]
    assert reloaded.state == os_mod.STATE_EXPIRED


def test_session_rate_limit(tmp_profile):
    _setup(tmp_profile)
    for _ in range(os_mod.MAX_OPEN_PER_USER):
        os_mod.start_session(USER_A)
    with pytest.raises(os_mod.SessionError, match="open onboarding sessions"):
        os_mod.start_session(USER_A)


def test_session_cancel_is_terminal(tmp_profile):
    _setup(tmp_profile)
    session = os_mod.start_session(USER_A)
    os_mod.cancel(session.token, USER_A, nonce=session.nonce)
    with pytest.raises(os_mod.SessionError, match="closed"):
        os_mod.arm_for_credentials(session.token, USER_A, nonce=session.nonce)


# --------------------------------------------------------------------------- webmail_onboarding.run_pending


def _complete_wizard(fields_patch: dict | None = None) -> os_mod.OnboardingSession:
    """Walk a session through the wizard to awaiting_credentials."""
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


def test_run_pending_success_registers_account_and_marks_done(tmp_profile):
    _setup(tmp_profile)
    session = _complete_wizard()
    him = FakeCheckHim()
    result = wm.run_pending(session.token, imap_password=PASSWORD, him=him)
    assert result.ok and result.imap_ok and result.smtp_ok
    assert him.calls == [["-a", "coinography", "account", "check"]]
    # Session is now done
    final = os_mod.list_sessions(USER_A)[0]
    assert final.state == os_mod.STATE_DONE
    # Account is registered
    rows = json.loads(paths.ACCOUNTS_JSON.read_text(encoding="utf-8"))
    assert [r["id"] for r in rows] == ["coinography"]
    # Credentials saved locally; password never in config
    assert wm.load_secret("coinography", "imap") == PASSWORD
    assert PASSWORD not in paths.CONFIG_TOML.read_text(encoding="utf-8")
    assert PASSWORD not in paths.ACCOUNTS_JSON.read_text(encoding="utf-8")


def test_run_pending_imap_failure_rolls_back_and_marks_failed(tmp_profile):
    _setup(tmp_profile)
    session = _complete_wizard()
    before = paths.ACCOUNTS_JSON.read_text(encoding="utf-8")
    him = FakeCheckHim(imap="FAIL (authentication failed)", smtp="OK", code=1)
    result = wm.run_pending(session.token, imap_password=PASSWORD, him=him)
    assert not result.ok and not result.imap_ok
    final = os_mod.list_sessions(USER_A)[0]
    assert final.state == os_mod.STATE_FAILED
    assert "auth" in final.last_error.lower()
    # Account was NOT registered.
    assert paths.ACCOUNTS_JSON.read_text(encoding="utf-8") == before
    assert wm.load_secret("coinography", "imap") == ""


def test_run_pending_smtp_failure_rolls_back_and_marks_failed(tmp_profile):
    _setup(tmp_profile)
    session = _complete_wizard()
    him = FakeCheckHim(imap="OK", smtp="FAIL (535 bad login)", code=1)
    wm.run_pending(session.token, imap_password=PASSWORD, him=him)
    final = os_mod.list_sessions(USER_A)[0]
    assert final.state == os_mod.STATE_FAILED
    assert "coinography" not in paths.ACCOUNTS_JSON.read_text(encoding="utf-8")
    assert wm.load_secret("coinography", "smtp") == ""


def test_run_pending_exception_rolls_back_and_marks_failed(tmp_profile):
    _setup(tmp_profile)
    session = _complete_wizard()

    class Boom:
        def run(self, *a, **k):
            raise RuntimeError("himalaya exploded in validation")

    with pytest.raises(wm.OnboardingError):
        wm.run_pending(session.token, imap_password=PASSWORD, him=Boom())
    final = os_mod.list_sessions(USER_A)[0]
    assert final.state == os_mod.STATE_FAILED
    assert "coinography" not in paths.ACCOUNTS_JSON.read_text(encoding="utf-8")


def test_run_pending_duplicate_id_refused_and_other_preserved(tmp_profile):
    _setup(tmp_profile)
    # Pretend coinography is already registered
    paths.ACCOUNTS_JSON.write_text(
        json.dumps(
            [
                {
                    "id": "coinography",
                    "email": "ads@coinography.example",
                    "display_name": "x",
                }
            ]
        ),
        encoding="utf-8",
    )
    session = _complete_wizard()
    with pytest.raises(wm.OnboardingError, match="already exists"):
        wm.run_pending(session.token, imap_password=PASSWORD, him=FakeCheckHim())
    final = os_mod.list_sessions(USER_A)[0]
    assert final.state == os_mod.STATE_FAILED
    # Existing registry row untouched.
    assert json.loads(paths.ACCOUNTS_JSON.read_text(encoding="utf-8"))[0]["display_name"] == "x"


def test_run_pending_rejects_wrong_state(tmp_profile):
    _setup(tmp_profile)
    session = os_mod.start_session(USER_A)
    with pytest.raises(wm.OnboardingError, match="not awaiting credentials"):
        wm.run_pending(session.token, imap_password=PASSWORD, him=FakeCheckHim())


def test_run_pending_rejects_empty_password(tmp_profile):
    _setup(tmp_profile)
    session = _complete_wizard()
    with pytest.raises(wm.OnboardingError, match="empty"):
        wm.run_pending(session.token, imap_password="", him=FakeCheckHim())


def test_run_pending_rejects_unknown_token(tmp_profile):
    _setup(tmp_profile)
    with pytest.raises(wm.OnboardingError, match="unknown"):
        wm.run_pending("not-a-token", imap_password=PASSWORD, him=FakeCheckHim())


def test_run_pending_session_never_stores_password(tmp_profile):
    _setup(tmp_profile)
    session = _complete_wizard()
    wm.run_pending(session.token, imap_password=PASSWORD, him=FakeCheckHim())
    row = os_mod.list_sessions(USER_A)[0]
    blob = json.dumps(row.fields) + row.last_error
    assert PASSWORD not in blob


# --------------------------------------------------------------------------- mailbox_admin


def _onboard_coinography(tmp_profile):
    session = _complete_wizard()
    wm.run_pending(session.token, imap_password=PASSWORD, him=FakeCheckHim())


def test_list_mailboxes_shows_registry_state(tmp_profile):
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)
    boxes = mailbox_admin.list_mailboxes()
    assert [b.account_id for b in boxes] == ["coinography"]
    cog = boxes[0]
    assert cog.active
    assert cog.has_credentials
    assert not cog.organize and not cog.send  # deny-by-default


def test_disable_mailbox_preserves_credentials_and_resets_policy(tmp_profile):
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)
    # Pretend an admin had enabled organize/send already.
    from mailops.config import load_settings, save_settings
    s = load_settings()
    s["account_policies"]["coinography"] = {"organize": True, "send": True}
    save_settings(s)
    mailbox_admin.disable_mailbox(USER_A, "coinography")
    cog = mailbox_admin.find_mailbox("coinography")
    assert not cog.active
    # Credentials are untouched (so re-enable is one tap)
    assert wm.load_secret("coinography", "imap") == PASSWORD
    # Policy reset to deny-by-default
    s = load_settings()
    assert s["account_policies"]["coinography"] == {"organize": False, "send": False}
    # Audit event recorded
    actions = [r.action for r in mailbox_admin.audit_log()]
    assert "disable" in actions


def test_enable_refuses_without_credentials(tmp_profile):
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)
    mailbox_admin.disable_mailbox(USER_A, "coinography")
    # Delete the stored secrets to simulate a cleaned-up disabled account.
    # Covers both legacy ``.secret`` and new ``.enc`` envelope locations.
    for kind in ("imap", "smtp"):
        for suffix in ("secret", "enc"):
            p = paths.SECRETS_DIR / f"coinography.{kind}.{suffix}"
            p.unlink(missing_ok=True)
    with pytest.raises(mailbox_admin.MailboxAdminError, match="no stored credentials"):
        mailbox_admin.enable_mailbox(USER_A, "coinography")


def test_enable_re_adds_to_allowlist(tmp_profile):
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)
    mailbox_admin.disable_mailbox(USER_A, "coinography")
    cog = mailbox_admin.enable_mailbox(USER_A, "coinography")
    assert cog.active
    # Either the account id is explicitly listed, or the allowlist is empty
    # (= every registered account is active). Both outcomes mean the sync
    # loop will pick coinography up again on the next tick.
    from mailops.config import active_account_ids

    assert "coinography" in active_account_ids()


def test_gmail_is_locked_and_cannot_be_touched(tmp_profile):
    _setup(tmp_profile)
    paths.ACCOUNTS_JSON.write_text(
        json.dumps(
            [
                {
                    "id": "gmail",
                    "email": "foo@gmail.com",
                    "display_name": "Gmail",
                    "default": True,
                }
            ]
        ),
        encoding="utf-8",
    )
    boxes = mailbox_admin.list_mailboxes()
    assert boxes[0].locked
    for fn in (mailbox_admin.enable_mailbox, mailbox_admin.disable_mailbox):
        with pytest.raises(mailbox_admin.MailboxAdminError, match="Gmail"):
            fn(USER_A, "gmail")


def test_health_check_delegates_to_webmail_handshake(tmp_profile, monkeypatch):
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)
    him = FakeCheckHim()
    status = mailbox_admin.health_check("coinography", him=him)
    assert status["ok"] and status["imap"] and status["smtp"]
    assert him.calls == [["-a", "coinography", "account", "check"]]


def test_health_check_reports_no_credentials(tmp_profile):
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)
    for kind in ("imap", "smtp"):
        for suffix in ("secret", "enc"):
            (paths.SECRETS_DIR / f"coinography.{kind}.{suffix}").unlink(missing_ok=True)
    status = mailbox_admin.health_check("coinography")
    assert not status["ok"]
    assert "no stored credentials" in status["detail"]


# ---------------------------------------------------------------------------
# Regression: the private "Check connection" button (``mb:health:<id>``)
# called ``mailbox_admin.health_check`` with no try/except, unlike its
# siblings Enable/Disable. A stale button for a mailbox that was since
# deleted raised ``MailboxAdminError`` straight out of the dispatcher - the
# tap just hung (no answer, no edit) instead of showing a clear message.
# Routed through the real ``mailboxes.handle_callback`` so this is a
# dispatcher-level proof, not just a unit test on mailbox_admin.
# ---------------------------------------------------------------------------
def test_health_button_on_deleted_mailbox_shows_error_not_crash(tmp_profile, fake_him):
    _setup(tmp_profile)
    upd, q = callback_update("mb:health:ghost-mailbox", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, SimpleNamespace(user_data={}, bot=None)))
    assert q.edits, "stale Check connection tap produced no screen at all (dispatcher may have crashed)"
    text, _markup = q.edits[-1]
    assert "not a registered mailbox" in text


def test_audit_log_never_contains_passwords(tmp_profile):
    """Production code paths MUST NOT write a mailbox password into the
    audit log. We exercise every path that could receive one and prove
    it did not land in audit output or in the session row.
    """
    _setup(tmp_profile)
    # 1. Successful onboarding via Telegram-initiated session
    _onboard_coinography(tmp_profile)
    # 2. Failed onboarding (triggers finish_failure with exception detail)
    session = _complete_wizard(fields_patch={"account_id": "memecoinist", "email": "ads@memecoinist.example"})
    wm.run_pending(
        session.token,
        imap_password=PASSWORD,
        him=FakeCheckHim(imap="FAIL", smtp="FAIL", code=1),
    )
    # 3. Exception-based rollback
    session = _complete_wizard(fields_patch={"account_id": "altcoinnew", "email": "ads@altcoin.example"})

    class Boom:
        def run(self, *a, **k):
            raise RuntimeError(f"server rejected {PASSWORD}")

    with pytest.raises(wm.OnboardingError):
        wm.run_pending(session.token, imap_password=PASSWORD, him=Boom())

    # Audit entries must not contain the password even though the exception
    # message did.
    detail = " ".join(r.detail for r in mailbox_admin.audit_log(100))
    assert PASSWORD not in detail
    # Session rows (fields, last_error) must not contain the password either.
    for s in os_mod.list_sessions(USER_A):
        blob = json.dumps(s.fields) + s.last_error
        assert PASSWORD not in blob


def test_set_purpose_is_per_account_and_resets_confirmation(tmp_profile):
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)
    from mailops.config import load_settings, save_settings

    s = load_settings()
    s["context_confirmed"] = True
    save_settings(s)
    mailbox_admin.set_purpose(USER_A, "coinography", "Coinography paid-ad inbox")
    s = load_settings()
    assert s["context_confirmed"] is False
    assert s["account_purposes"]["coinography"].startswith("Coinography")


# --------------------------------------------------------------------------- Telegram UI


def _ctx():
    return SimpleNamespace(user_data={})


def test_mailboxes_private_screen_requires_admin(tmp_profile):
    _setup(tmp_profile)
    # Operator (USER_B) has no P_ADMIN.
    upd, q = callback_update("mb:m", USER_B, USER_B, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    assert q.edits == []
    denials = [t for t, alert in q.answers if alert]
    assert any("administrator" in (t or "").lower() for t in denials)


def test_mailboxes_group_callback_safe_mb_is_allowed_for_admin(tmp_profile):
    """GROUP-FIRST: safe mb:* subcommands (``mb:m``, ``mb:add``, ``mb:add:*``,
    ``mb:arm:*``, ``mb:cancel:*``, ``mb:edit:*``, ``mb:skip:*``, ``mb:poll:*``,
    ``mb:credsvia:*``) run inside the whitelisted group when the tapper has
    P_ADMIN. Everything that would leak an existing mailbox's details or
    capture a credential (``view``, ``enable``, ``disable``, ``health``,
    ``purpose``, ``audit``, ``dm_prompt``) is still private-only."""
    _setup(tmp_profile)
    import mailops.bot.app as app_mod

    # Safe entry: Mailboxes list runs in group for the admin.
    upd, q = callback_update("mb:m", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))
    assert q.answers != [(app_mod.GROUP_DENIED_ALERT, True)], \
        "mb:m should be group-safe for admins"
    assert q.edits, "mb:m should render the mailboxes list in the group"

    # Unsafe entries that MUST still deny in group.
    for data in (
        "mb:view:coinography",
        "mb:enable:coinography",
        "mb:disable:coinography",
        "mb:health:coinography",
        "mb:purpose:coinography",
        "mb:audit:coinography",
        "mb:dm_prompt:tkn:nce",
    ):
        upd2, q2 = callback_update(data, USER_A, GROUP, "group")
        _run(app_mod.on_callback(upd2, _ctx()))
        assert q2.edits == [], f"{data} must not render in group"
        assert q2.answers == [(app_mod.GROUP_DENIED_ALERT, True)], \
            f"{data} must emit the group-denied alert"


def test_mailboxes_group_callback_denies_non_admin(tmp_profile):
    """Even for a group-safe mb:* subcommand, a non-admin (operator) in
    the whitelisted group is refused and no screen is rendered."""
    _setup(tmp_profile)
    import mailops.bot.app as app_mod

    upd, q = callback_update("mb:m", USER_B, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))
    assert q.edits == []
    # The in-mailboxes _guard_admin surfaces its own admin-only alert.
    denials = [t for t, alert in q.answers if alert and t]
    assert any("admin" in t.lower() for t in denials)


def test_mailboxes_main_screen_lists_registered_accounts(tmp_profile):
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)
    upd, q = callback_update("mb:m", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    text, markup = q.edits[-1]
    assert "coinography" in text.lower()
    assert any("mb:add" in (b.callback_data or "") for row in markup.inline_keyboard for b in row)


def test_mailboxes_add_wizard_hostinger_prefills_defaults(tmp_profile):
    _setup(tmp_profile)
    upd, q = callback_update("mb:add:hostinger", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    text, _markup = q.edits[-1]
    # The screen asks for the first missing field - account_id - but
    # Hostinger defaults are already stored for host/port.
    sessions = os_mod.list_sessions(USER_A)
    assert sessions, "wizard must open a session"
    fields = sessions[0].fields
    assert fields["imap_host"] == "imap.hostinger.com"
    assert fields["imap_port"] == 993
    assert fields["smtp_host"] == "smtp.hostinger.com"
    assert fields["smtp_port"] == 465


def test_mailboxes_add_wizard_rejects_bad_port(tmp_profile):
    _setup(tmp_profile)
    upd, q = callback_update("mb:add:custom", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    # Walk the wizard with a bad port in the imap_port step.
    sessions = os_mod.list_sessions(USER_A)
    token = sessions[0].token
    # Jump straight to imap_port by filling earlier fields.
    os_mod.set_fields(
        token,
        USER_A,
        nonce=os_mod.get_session(token, USER_A).nonce,
        patch={
            "account_id": "coinography",
            "email": "ads@coinography.example",
            "display_name": "",
            "imap_host": "mail.coinography.example",
        },
    )
    mb_bot.AWAITING[USER_A] = {"token": token, "field": "imap_port"}
    upd, msg = command_update(USER_A, USER_A, "private")
    upd.effective_message.text = "99999"
    upd.message.text = "99999"
    _run(mb_bot.handle_text(upd, _ctx()))
    assert any("port" in t.lower() for t, _ in msg.sent)


def test_mailboxes_add_wizard_full_run_captures_credentials_via_cli(
    tmp_profile, monkeypatch
):
    """Walk the wizard end-to-end from Telegram, then simulate the server-side
    CLI handoff finishing the validation."""
    _setup(tmp_profile)
    # Admin taps Add email -> Custom
    upd, q = callback_update("mb:add:custom", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    token = os_mod.list_sessions(USER_A)[0].token

    # Supply every field via text messages (the wizard primes the slot after
    # each one).
    values = {
        "account_id": "coinography",
        "email": "ads@coinography.example",
        "display_name": "Coinography",
        "imap_host": "mail.coinography.example",
        "imap_port": "993",
        "imap_user": "ads@coinography.example",
        "smtp_host": "mail.coinography.example",
        "smtp_port": "465",
        "smtp_user": "ads@coinography.example",
    }
    for field in [k for k, _, _ in mb_bot.STEPS]:
        pending = mb_bot.AWAITING.get(USER_A)
        assert pending and pending["field"] == field, pending
        upd, msg = command_update(USER_A, USER_A, "private")
        upd.effective_message.text = values[field]
        upd.message.text = values[field]
        _run(mb_bot.handle_text(upd, _ctx()))

    # Now the wizard should be at Review (state=draft, all fields set).
    session = os_mod.get_session(token, USER_A)
    assert mb_bot._next_pending_step(session.fields) is None

    # Arm credential capture
    upd, q = callback_update(
        f"mb:arm:{token}:{session.nonce}", USER_A, USER_A, "private"
    )
    _run(mb_bot.handle_callback(upd, _ctx()))
    assert os_mod.get_session(token, USER_A).state == os_mod.STATE_AWAITING_CREDS

    # Server-side CLI finishes it (simulating `python -m mailops webmail-run-pending`)
    result = wm.run_pending(token, imap_password=PASSWORD, him=FakeCheckHim())
    assert result.ok

    # The session shows "done" when the admin refreshes.
    upd, q = callback_update(f"mb:poll:{token}", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    text, _m = q.edits[-1]
    assert "validated and registered" in text.lower()


def test_mailboxes_dm_capture_requires_env_flag(tmp_profile):
    _setup(tmp_profile)
    session = _complete_wizard()
    # Env flag NOT set
    upd, q = callback_update(
        f"mb:dm_prompt:{session.token}:{session.nonce}",
        USER_A,
        USER_A,
        "private",
    )
    _run(mb_bot.handle_callback(upd, _ctx()))
    text = q.edits[-1][0].lower()
    assert "disabled on this server" in text or "webmail-run-pending" in text
    # AWAITING slot was NOT armed for a password.
    assert mb_bot.AWAITING.get(USER_A, {}).get("field") != "__dm_password__"


def test_mailboxes_dm_capture_runs_add_account_and_wipes(tmp_profile, monkeypatch):
    _setup(tmp_profile)
    os.environ["MAILOPS_ALLOW_DM_CREDENTIALS"] = "1"
    session = _complete_wizard()
    # Admin asks to send password via DM
    upd, q = callback_update(
        f"mb:dm_prompt:{session.token}:{session.nonce}",
        USER_A,
        USER_A,
        "private",
    )
    _run(mb_bot.handle_callback(upd, _ctx()))
    assert mb_bot.AWAITING[USER_A]["field"] == "__dm_password__"

    # The real add_account path builds a Himalaya() internally when ``him`` is
    # None. Monkey-patch check_account (its only side-effect) so no subprocess
    # is launched.
    fake_result = wm.CheckResult(ok=True, imap_ok=True, smtp_ok=True, detail={})
    monkeypatch.setattr(wm, "check_account", lambda *a, **k: fake_result)

    # Admin sends the password; the message should be deleted.
    deletes: list[bool] = []

    class RecMsg:
        def __init__(self):
            self.sent: list[tuple] = []

        async def reply_text(self, text, reply_markup=None):
            self.sent.append((text, reply_markup))

        async def delete(self):
            deletes.append(True)

    msg = RecMsg()
    upd = SimpleNamespace(
        callback_query=None,
        effective_user=SimpleNamespace(id=USER_A),
        effective_chat=SimpleNamespace(id=USER_A, type="private"),
        effective_message=msg,
        message=msg,
    )
    upd.effective_message.text = PASSWORD
    upd.message.text = PASSWORD

    _run(mb_bot.handle_text(upd, _ctx()))

    assert deletes, "the password message should be deleted"
    # Session is DONE
    assert os_mod.list_sessions(USER_A)[0].state == os_mod.STATE_DONE
    # Password did not land in any outgoing message or audit entry
    joined = " ".join(t for t, _ in msg.sent)
    assert PASSWORD not in joined
    assert all(PASSWORD not in r.detail for r in mailbox_admin.audit_log())
    # Password stored in local secrets file only (same as CLI path).
    assert wm.load_secret("coinography", "imap") == PASSWORD


# --------------------------------------------------------------------------- worker reload


def test_sync_worker_reload_picks_up_new_account_without_restart(tmp_profile):
    """``mailops.config.load_accounts`` is called every sync tick: the sync
    loop therefore sees a freshly onboarded mailbox on the next pass with
    no restart. We just check the config layer rebinds live."""
    _setup(tmp_profile)
    from mailops.config import load_accounts, active_account_ids

    assert load_accounts() == []
    _onboard_coinography(tmp_profile)
    # Fresh call reads disk again.
    acc_ids = active_account_ids()
    assert acc_ids == {"coinography"}
    # Disabling drops it from the next tick without touching credentials.
    mailbox_admin.disable_mailbox(USER_A, "coinography")
    assert active_account_ids() == set()
    # Re-enable - sync loop will see it again.
    mailbox_admin.enable_mailbox(USER_A, "coinography")
    assert active_account_ids() == {"coinography"}


def test_add_account_starts_read_only_with_sending_off(tmp_profile):
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)
    from mailops.policy import policy_for

    pol = policy_for("coinography")
    assert not pol.organize
    assert not pol.send


def test_altcoininformer_is_preserved_across_lifecycle_ops(tmp_profile):
    """Altcoin Informer is the live production account: adding a second
    mailbox and even disabling/re-enabling that second mailbox must leave
    the Altcoin Informer registry row and credentials untouched."""
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
                "account_policies": {"altcoininformer": {"organize": False, "send": True}},
            }
        ),
        encoding="utf-8",
    )
    # Seed fake credentials so enable/disable both succeed.
    (paths.SECRETS_DIR / "altcoininformer.imap.secret").parent.mkdir(parents=True, exist_ok=True)
    (paths.SECRETS_DIR / "altcoininformer.imap.secret").write_text("already-set", encoding="utf-8")
    (paths.SECRETS_DIR / "altcoininformer.smtp.secret").write_text("already-set", encoding="utf-8")
    before = paths.ACCOUNTS_JSON.read_text(encoding="utf-8")

    # Add Coinography.
    _onboard_coinography(tmp_profile)

    # Altcoin row unchanged.
    rows = json.loads(paths.ACCOUNTS_JSON.read_text(encoding="utf-8"))
    altcoin = [r for r in rows if r["id"] == "altcoininformer"][0]
    assert altcoin["email"] == "info@altcoininformer.com"
    assert altcoin.get("default") is True
    # Both active, both discovered by the sync loop.
    from mailops.config import active_account_ids
    assert active_account_ids() == {"altcoininformer", "coinography"}

    # Disable Coinography: Altcoin Informer stays active and keeps its send policy.
    mailbox_admin.disable_mailbox(USER_A, "coinography")
    from mailops.config import load_settings
    s = load_settings()
    assert "altcoininformer" in s["active_accounts"]
    assert s["account_policies"]["altcoininformer"]["send"] is True

    # Altcoin credentials never touched.
    assert (paths.SECRETS_DIR / "altcoininformer.imap.secret").read_text() == "already-set"


def test_concurrent_arm_only_one_succeeds(tmp_profile):
    """Two concurrent taps on 'Confirm & arm credentials' must only
    advance the state once; the second one gets 'already used'."""
    _setup(tmp_profile)
    session = os_mod.start_session(USER_A)
    session = os_mod.set_fields(
        session.token,
        USER_A,
        nonce=session.nonce,
        patch={"account_id": "x"},
    )
    # Both tap simultaneously with the same nonce.
    arm1 = os_mod.arm_for_credentials(session.token, USER_A, nonce=session.nonce)
    with pytest.raises(os_mod.SessionError, match="already used|not"):
        os_mod.arm_for_credentials(session.token, USER_A, nonce=session.nonce)
    assert arm1.state == os_mod.STATE_AWAITING_CREDS


def test_group_dashboard_mailboxes_deeplink_mints_private_url(tmp_profile):
    """Admin tap on Mailboxes in the group posts a one-shot t.me/?start=mdl_
    URL and NOTHING sensitive in the group chat."""
    _setup(tmp_profile)
    import mailops.bot.app as app_mod

    upd, q = callback_update("gdl:mailboxes", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))
    assert q.edits, "owner tap should produce a private-link message"
    text, markup = q.edits[-1]
    assert "Mailboxes" in text
    urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
    assert len(urls) == 1
    assert urls[0].startswith("https://t.me/")
    assert "?start=mdl_" in urls[0]


def test_group_dashboard_mailboxes_deeplink_denied_for_operator(tmp_profile):
    _setup(tmp_profile)
    import mailops.bot.app as app_mod

    upd, q = callback_update("gdl:mailboxes", USER_B, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))
    assert q.edits == []
    denials = [t for t, alert in q.answers if alert and t]
    assert any("admin" in t.lower() for t in denials)


def test_deep_link_mailboxes_private_handoff(tmp_profile):
    """/start mdl_<token> for the mailboxes action opens the Mailboxes screen."""
    _setup(tmp_profile)
    import mailops.bot.app as app_mod
    from mailops import group_links

    token = group_links.issue(USER_A, GROUP, "mailboxes")
    upd, msg = command_update(USER_A, USER_A, "private")
    upd.effective_message.text = f"/start mdl_{token}"
    upd.message.text = f"/start mdl_{token}"
    _run(
        app_mod.cmd_start(
            upd,
            SimpleNamespace(user_data={}, args=[f"mdl_{token}"], bot=None),
        )
    )
    assert msg.sent
    text, markup = msg.sent[0]
    assert text.startswith("Mailboxes")
    datas = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    assert "mb:add" in datas


def test_cli_run_pending_rejects_pipe_input(tmp_profile):
    """The server-side CLI must refuse non-TTY input so passwords can
    never arrive via a pipe or argument."""
    import io

    monkeypatch_output = io.StringIO()
    import sys as _sys

    old_stdin = _sys.stdin
    old_stdout = _sys.stdout
    try:
        _sys.stdin = io.StringIO("secret\nsecret\n")
        _sys.stdout = monkeypatch_output
        rc = wm.cli_run_pending(["some-token"])
    finally:
        _sys.stdin = old_stdin
        _sys.stdout = old_stdout
    assert rc == 2
    assert "interactive terminal" in monkeypatch_output.getvalue()


def test_add_wizard_text_in_group_is_ignored(tmp_profile):
    """A DM-bound wizard slot cannot be advanced by text in a group.

    GROUP-FIRST allows wizard text in the group when the slot's chat_id
    pins to that group. The point of this test is the OTHER direction:
    an admin with a slot bound to their private chat must never have a
    group message steal their wizard state."""
    _setup(tmp_profile)
    session = os_mod.start_session(USER_A)
    # Pin the slot to USER_A's private chat.
    mb_bot.AWAITING[USER_A] = {
        "token": session.token,
        "field": "account_id",
        "chat_id": USER_A,
    }
    upd, msg = command_update(USER_A, GROUP, "group")
    upd.effective_message.text = "coinography"
    upd.message.text = "coinography"
    consumed = _run(mb_bot.handle_text(upd, _ctx()))
    assert consumed is False
    # Session must still be in draft state (no field persisted).
    assert os_mod.get_session(session.token, USER_A).fields.get("account_id", "") == ""


# =============================================================================
#  Hardened DM-password capture (regression tests for the pre-exposure fixes)
# =============================================================================


def _dm_arm(session: os_mod.OnboardingSession):
    """Fire the dm_prompt callback with the flag enabled; returns the slot."""
    upd, q = callback_update(
        f"mb:dm_prompt:{session.token}:{session.nonce}",
        USER_A, USER_A, "private",
    )
    _run(mb_bot.handle_callback(upd, _ctx()))
    return mb_bot.AWAITING.get(USER_A), q


def _dm_send(text):
    """Simulate the admin sending a DM text, with delete() succeeding."""
    deletes: list[bool] = []

    class RecMsg:
        def __init__(self):
            self.sent: list[tuple] = []

        async def reply_text(self, t, reply_markup=None):
            self.sent.append((t, reply_markup))

        async def delete(self):
            deletes.append(True)

    msg = RecMsg()
    upd = SimpleNamespace(
        callback_query=None,
        effective_user=SimpleNamespace(id=USER_A),
        effective_chat=SimpleNamespace(id=USER_A, type="private"),
        effective_message=msg,
        message=msg,
    )
    upd.effective_message.text = text
    upd.message.text = text
    _run(mb_bot.handle_text(upd, _ctx()))
    return msg, deletes


def _dm_send_delete_fails(text):
    """Simulate the admin sending a DM text where delete() raises."""
    class RecMsg:
        def __init__(self):
            self.sent: list[tuple] = []

        async def reply_text(self, t, reply_markup=None):
            self.sent.append((t, reply_markup))

        async def delete(self):
            raise RuntimeError("message is too old")

    msg = RecMsg()
    upd = SimpleNamespace(
        callback_query=None,
        effective_user=SimpleNamespace(id=USER_A),
        effective_chat=SimpleNamespace(id=USER_A, type="private"),
        effective_message=msg,
        message=msg,
    )
    upd.effective_message.text = text
    upd.message.text = text
    _run(mb_bot.handle_text(upd, _ctx()))
    return msg


def test_dm_capture_blocks_overwrite_by_purpose_edit(tmp_profile):
    """H1: Edit purpose must NOT clobber the DM-password AWAITING slot.

    Without the guard, the admin's next text would be stored as a brand
    purpose in settings.json (plaintext on disk). After the fix, the
    purpose prompt is refused with a 'password capture is already armed'
    notice and AWAITING keeps the __dm_password__ slot.
    """
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)  # creates a real mailbox to edit
    os.environ["MAILOPS_ALLOW_DM_CREDENTIALS"] = "1"
    session = _complete_wizard(
        fields_patch={"account_id": "memecoinist", "email": "a@mc.example"}
    )
    slot, _q = _dm_arm(session)
    assert slot is not None and slot["field"] == mb_bot._DM_PW_FIELD

    # Admin taps Edit purpose on an EXISTING mailbox while DM-arm is pending.
    upd, q = callback_update("mb:purpose:coinography", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))

    # AWAITING is still the DM password slot.
    assert mb_bot.AWAITING[USER_A]["field"] == mb_bot._DM_PW_FIELD
    # The user was told why.
    text, _mk = q.edits[-1]
    assert "password capture is already armed" in text.lower()


def test_purpose_edit_prompt_expires_after_ttl(tmp_profile, monkeypatch):
    """Pre-deployment audit fix: ``mb:purpose:*`` has no onboarding-session
    token backing it, so unlike password capture (DM_ARM_TTL_S) it had NO
    expiry at all. A stale slot from hours/days earlier would silently
    persist an unrelated later DM message as the mailbox's AI-classifier
    purpose. UNTOKENED_FIELD_TTL_S now expires it instead."""
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)

    upd, q = callback_update("mb:purpose:coinography", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    assert mb_bot.AWAITING[USER_A]["field"] == mb_bot._PURPOSE_FIELD

    real_time = __import__("time")
    future = real_time.time() + mb_bot.UNTOKENED_FIELD_TTL_S + 10
    monkeypatch.setattr(mb_bot.time, "time", lambda: future)

    msg, _deletes = _dm_send("This text must NOT become the mailbox purpose")
    # The slot was dropped silently (handle_text returns False so the
    # message falls through to any other handler); purpose is untouched.
    assert USER_A not in mb_bot.AWAITING
    assert mailbox_admin.get_purpose("coinography") != (
        "This text must NOT become the mailbox purpose"
    )


def test_mailbox_search_prompt_expires_after_ttl(tmp_profile, monkeypatch):
    """Same TTL fix as above, applied to ``mb:search`` (free-text mailbox
    filter capture), which also had no expiry."""
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)

    upd, q = callback_update("mb:switch:search", USER_A, USER_A, "private")
    _run(mb_bot.handle_callback(upd, _ctx()))
    assert mb_bot.AWAITING[USER_A]["field"] == mb_bot._MB_SEARCH_FIELD

    real_time = __import__("time")
    future = real_time.time() + mb_bot.UNTOKENED_FIELD_TTL_S + 10
    monkeypatch.setattr(mb_bot.time, "time", lambda: future)

    _dm_send("stale search text")
    assert USER_A not in mb_bot.AWAITING
    # The stale text must not have been applied as the manager's filter.
    state = mb_bot._get_manager_state(USER_A)
    assert state.get("query") != "stale search text"


def test_dm_capture_rejects_after_60s_arm_window(tmp_profile, monkeypatch):
    """H2: Password text arriving after DM_ARM_TTL_S is NOT consumed.

    The slot is cleared and the admin is told to re-arm. No validation
    runs, no password reaches add_account.
    """
    _setup(tmp_profile)
    os.environ["MAILOPS_ALLOW_DM_CREDENTIALS"] = "1"
    session = _complete_wizard()
    _dm_arm(session)

    # Force wall-clock past the arm TTL.
    real_time = __import__("time")
    future = real_time.time() + mb_bot.DM_ARM_TTL_S + 10
    monkeypatch.setattr(mb_bot.time, "time", lambda: future)

    # If validation runs, this fake will record the call. It MUST stay empty.
    called = []
    monkeypatch.setattr(
        wm, "run_pending",
        lambda *a, **k: called.append(1) or wm.CheckResult(True, True, True, {}),
    )

    msg, deletes = _dm_send(PASSWORD)
    assert deletes, "expired password must still be deleted from chat"
    assert called == [], "run_pending must NOT be invoked after the arm window closes"
    joined = " ".join(t for t, _ in msg.sent)
    assert "60-second window" in joined.lower() or "closed" in joined.lower()
    assert PASSWORD not in joined
    # Session is untouched (still awaiting credentials).
    assert os_mod.get_session(session.token, USER_A).state == os_mod.STATE_AWAITING_CREDS


def test_dm_capture_rejects_when_nonce_rotated(tmp_profile, monkeypatch):
    """H3: If the session's nonce rotates between arm and send, refuse.

    Scenario: admin arms DM capture, then (from a second device) does
    something that rotates the nonce (e.g. an edit callback). The pending
    AWAITING slot now carries a stale nonce. When the password arrives,
    we must refuse rather than consume it against a different arm.
    """
    _setup(tmp_profile)
    os.environ["MAILOPS_ALLOW_DM_CREDENTIALS"] = "1"
    session = _complete_wizard()
    _dm_arm(session)
    assert mb_bot.AWAITING[USER_A]["nonce"] == session.nonce

    # Rotate the session nonce out-of-band (simulates a concurrent action).
    reloaded = os_mod.get_session(session.token, USER_A)
    # arm_for_credentials rejects when already in that state; use cancel+re-start
    # to force a nonce change on the same session isn't allowed (terminal).
    # Instead, directly patch the DB row's nonce via a legitimate transition:
    # go back to draft is not supported, so we simulate by writing the slot
    # with a fake stale nonce.
    mb_bot.AWAITING[USER_A]["nonce"] = "stale-nonce-xxxxxxxxxxxxxxxx"

    called = []
    monkeypatch.setattr(
        wm, "run_pending",
        lambda *a, **k: called.append(1) or wm.CheckResult(True, True, True, {}),
    )
    msg, _d = _dm_send(PASSWORD)
    assert called == [], "run_pending must NOT run when the nonce has rotated"
    joined = " ".join(t for t, _ in msg.sent)
    assert PASSWORD not in joined
    assert "arm the capture again" in joined.lower() or "changed since you tapped" in joined.lower()


def test_dm_capture_rejects_when_session_no_longer_awaiting(tmp_profile, monkeypatch):
    """H3 variant: an already-done session cannot accept a late password."""
    _setup(tmp_profile)
    os.environ["MAILOPS_ALLOW_DM_CREDENTIALS"] = "1"
    session = _complete_wizard()
    _dm_arm(session)

    # Complete the session via the CLI path.
    wm.run_pending(session.token, imap_password="first", him=FakeCheckHim())
    assert os_mod.get_session(session.token, USER_A).state == os_mod.STATE_DONE

    called = []
    monkeypatch.setattr(
        wm, "run_pending",
        lambda *a, **k: called.append(1) or wm.CheckResult(True, True, True, {}),
    )
    msg, _d = _dm_send("another-password")
    assert called == []
    joined = " ".join(t for t, _ in msg.sent)
    assert "another-password" not in joined


def test_dm_capture_warns_when_delete_fails(tmp_profile, monkeypatch):
    """M2: delete() failure must be surfaced to the admin so they can act."""
    _setup(tmp_profile)
    os.environ["MAILOPS_ALLOW_DM_CREDENTIALS"] = "1"
    session = _complete_wizard()
    _dm_arm(session)

    monkeypatch.setattr(
        wm, "run_pending",
        lambda *a, **k: wm.CheckResult(True, True, True, {}),
    )
    msg = _dm_send_delete_fails(PASSWORD)
    joined = " ".join(t for t, _ in msg.sent)
    # The admin must be told to delete manually.
    assert "could not delete" in joined.lower()
    assert "manually" in joined.lower()
    # Password text was never echoed back to the admin.
    assert PASSWORD not in joined


def test_dm_capture_cancel_word_aborts(tmp_profile):
    """Sending 'cancel' as the password must NOT run validation."""
    _setup(tmp_profile)
    os.environ["MAILOPS_ALLOW_DM_CREDENTIALS"] = "1"
    session = _complete_wizard()
    _dm_arm(session)
    msg, deletes = _dm_send("cancel")
    assert deletes
    joined = " ".join(t for t, _ in msg.sent)
    assert "cancelled" in joined.lower()
    # Session is still awaiting_credentials (admin may re-arm).
    assert os_mod.get_session(session.token, USER_A).state == os_mod.STATE_AWAITING_CREDS


def test_dm_capture_slot_cleared_before_validation_runs(tmp_profile, monkeypatch):
    """Reliability: a concurrent second DM must NOT double-consume.

    After the first password text is accepted, AWAITING is cleared BEFORE
    run_pending is awaited. We assert the slot is empty inside the
    monkey-patched run_pending.
    """
    _setup(tmp_profile)
    os.environ["MAILOPS_ALLOW_DM_CREDENTIALS"] = "1"
    session = _complete_wizard()
    _dm_arm(session)

    observed_awaiting: list = []

    def _fake_run_pending(token, *, imap_password, smtp_password=None, him=None):
        observed_awaiting.append(dict(mb_bot.AWAITING))
        # Mark the session done so the follow-up get_session returns cleanly.
        os_mod.begin_validation(token, USER_A)
        os_mod.finish_success(token, USER_A)
        return wm.CheckResult(True, True, True, {})

    monkeypatch.setattr(wm, "run_pending", _fake_run_pending)
    _dm_send(PASSWORD)
    assert observed_awaiting and USER_A not in observed_awaiting[0]


def test_dm_capture_denied_for_non_admin(tmp_profile):
    """Only P_ADMIN can arm the DM capture, even if the flag is on.

    USER_B is a non-admin seeded user; the dm_prompt callback must be
    answered with 'Only administrators can manage mailboxes.' and
    AWAITING for USER_B must remain empty.
    """
    _setup(tmp_profile)
    os.environ["MAILOPS_ALLOW_DM_CREDENTIALS"] = "1"
    # Even though we never create a session for USER_B, the admin guard
    # fires first, so the callback is rejected before token parsing.
    upd, q = callback_update(
        "mb:dm_prompt:any:anynonce", USER_B, USER_B, "private",
    )
    _run(mb_bot.handle_callback(upd, _ctx()))
    assert USER_B not in mb_bot.AWAITING
    alerts = [t for t, is_alert in q.answers if is_alert]
    assert any("administrator" in (t or "").lower() for t in alerts)


def test_dm_capture_denied_in_group_chat(tmp_profile):
    """Credentials in a GROUP chat are refused regardless of the flag."""
    _setup(tmp_profile)
    os.environ["MAILOPS_ALLOW_DM_CREDENTIALS"] = "1"
    import mailops.bot.app as app_mod

    upd, q = callback_update(
        "mb:dm_prompt:any:anynonce", USER_A, GROUP, "group",
    )
    _run(app_mod.on_callback(upd, _ctx()))
    # The app-level group guard denies before mailboxes dispatch.
    assert USER_A not in mb_bot.AWAITING
    assert q.edits == []
    assert q.answers == [(app_mod.GROUP_DENIED_ALERT, True)]


def test_dm_capture_rolls_back_on_imap_auth_failure(tmp_profile, monkeypatch):
    """Auth-failure path: add_account rolls back everything.

    After a DM password submission that IMAP rejects, there must be:
    * NO entry in accounts.json for this id
    * NO stored secret file for this id
    * session state == FAILED
    * audit entry recorded, with the password redacted (no raw password
      bytes anywhere in audit detail)
    """
    _setup(tmp_profile)
    os.environ["MAILOPS_ALLOW_DM_CREDENTIALS"] = "1"
    session = _complete_wizard()
    _dm_arm(session)

    # Make the IMAP handshake fail.
    bad = wm.CheckResult(
        ok=False, imap_ok=False, smtp_ok=True,
        detail={"imap": "AUTHENTICATIONFAILED"}, raw_error="AUTHENTICATIONFAILED",
    )
    monkeypatch.setattr(wm, "check_account", lambda *a, **k: bad)

    msg, deletes = _dm_send(PASSWORD)
    assert deletes

    # Rollback assertions.
    rows = json.loads(paths.ACCOUNTS_JSON.read_text(encoding="utf-8"))
    assert all(r.get("id") != "coinography" for r in rows), rows
    assert wm.load_secret("coinography", "imap") == ""
    assert wm.load_secret("coinography", "smtp") == ""

    final = [s for s in os_mod.list_sessions(USER_A) if s.token == session.token][0]
    assert final.state == os_mod.STATE_FAILED

    # Audit must not contain the raw password.
    audit_blob = " ".join(r.detail for r in mailbox_admin.audit_log())
    assert PASSWORD not in audit_blob


def test_dm_capture_refuses_duplicate_registration(tmp_profile, monkeypatch):
    """Duplicate account id (second DM capture against an id that already
    exists) must be refused by add_account's existing-id check. No
    secret is written, no settings.json entry is added."""
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)
    os.environ["MAILOPS_ALLOW_DM_CREDENTIALS"] = "1"

    # Try to onboard the SAME id again via the DM path.
    session = _complete_wizard()  # defaults to account_id=coinography
    _dm_arm(session)

    monkeypatch.setattr(
        wm, "check_account", lambda *a, **k: wm.CheckResult(True, True, True, {})
    )
    msg, _d = _dm_send(PASSWORD)

    joined = " ".join(t for t, _ in msg.sent)
    assert "already exists" in joined.lower() or "rolled back" in joined.lower()
    # Session recorded as failed.
    final = [s for s in os_mod.list_sessions(USER_A) if s.token == session.token][0]
    assert final.state == os_mod.STATE_FAILED
    # Password never surfaced.
    assert PASSWORD not in joined


def test_dm_capture_flag_off_remains_default(tmp_profile):
    """Default (.env missing MAILOPS_ALLOW_DM_CREDENTIALS) MUST keep DM
    capture disabled. The arm callback redirects to CLI instructions and
    AWAITING is left untouched."""
    _setup(tmp_profile)
    assert os.environ.get("MAILOPS_ALLOW_DM_CREDENTIALS", "") == ""
    session = _complete_wizard()
    upd, q = callback_update(
        f"mb:dm_prompt:{session.token}:{session.nonce}",
        USER_A, USER_A, "private",
    )
    _run(mb_bot.handle_callback(upd, _ctx()))
    assert USER_A not in mb_bot.AWAITING
    text, _mk = q.edits[-1]
    assert "disabled on this server" in text.lower()


def test_dm_capture_offloads_blocking_check_to_thread(tmp_profile, monkeypatch):
    """M1: run_pending (which calls himalaya) must run on a worker thread,
    not block the asyncio handler. We detect this by asserting the call
    happens from a thread whose name is NOT the main one.
    """
    _setup(tmp_profile)
    os.environ["MAILOPS_ALLOW_DM_CREDENTIALS"] = "1"
    session = _complete_wizard()
    _dm_arm(session)

    import threading
    seen: list[str] = []

    def _record_thread(token, *, imap_password, smtp_password=None, him=None):
        seen.append(threading.current_thread().name)
        os_mod.begin_validation(token, USER_A)
        os_mod.finish_success(token, USER_A)
        return wm.CheckResult(True, True, True, {})

    monkeypatch.setattr(wm, "run_pending", _record_thread)
    _dm_send(PASSWORD)
    assert seen, "run_pending was not called"
    assert seen[0] != threading.main_thread().name, (
        f"run_pending ran on the asyncio thread ({seen[0]}); the password "
        "would block the Telegram poller."
    )


# =============================================================================
#  GROUP-FIRST onboarding (Mail agentt group)
# =============================================================================
#
# These tests cover the "wizard runs inside the whitelisted group, credentials
# step hands off to the admin's private DM" workflow. The behavioral contract:
#
#   * mb:m, mb:add, mb:add:*, mb:arm:*, mb:cancel:*, mb:edit:*, mb:skip:*,
#     mb:poll:*, mb:credsvia:*  — ALLOWED in group for P_ADMIN
#   * mb:view/enable/disable/health/purpose/audit/dm_prompt — DENIED in group
#   * Wizard text input accepted in group only when the slot's chat_id matches
#   * mb:arm:* in group EDITS the group message to a bland handoff notice and
#     sends the real armed screen to the admin's private DM
#   * ownership: a second admin tapping an in-flight session's buttons is
#     refused by the onboarding_session user_binding, surfaced as an alert
#   * failure text shown in group is passed through redact_text


class _RecordingBot:
    """Context.bot double that captures send_message calls."""
    def __init__(self, username="ourmailagent_bot", fail=False):
        self.username = username
        self.sent: list[dict] = []
        self.fail = fail

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail:
            from telegram.error import TelegramError
            raise TelegramError("Forbidden: bot can't initiate conversation")
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})


def _group_ctx(bot=None):
    return SimpleNamespace(user_data={}, bot=bot or _RecordingBot())


def test_group_mb_main_screen_hides_private_only_buttons(tmp_profile):
    """In a group render, the per-mailbox view buttons and the
    'Onboarding errors' button must be absent (both can leak details into
    group history). Add email must be present."""
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)
    text, markup = mb_bot.main_screen(in_group=True)
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "mb:add" in datas
    assert "mb:errors" not in datas
    assert not any((d or "").startswith("mb:view:") for d in datas)
    assert "passwords never appear in this group" in text.lower()


def test_group_mb_main_screen_private_render_keeps_full_controls(tmp_profile):
    """Private render keeps per-mailbox view + errors button."""
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)
    _text, markup = mb_bot.main_screen(in_group=False)
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "mb:errors" in datas
    assert any((d or "").startswith("mb:view:") for d in datas)


def test_group_wizard_full_non_secret_flow_runs_in_group(tmp_profile):
    """End-to-end: admin opens Mailboxes, taps Add email -> Custom, then
    fills every non-secret field by sending text in the group. The session
    advances to AWAITING_CREDS without any credential ever touching the
    group. Models the CEO's SiteGround scenario (alienfi / info@alien.fi /
    gcam1108.siteground.biz / 993,465). No values are hardcoded in the
    code path — they come purely from the admin's text messages."""
    _setup(tmp_profile)
    import mailops.bot.app as app_mod

    # 1. Mailboxes list in group.
    upd, q = callback_update("mb:m", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, _group_ctx()))
    assert q.edits, "mb:m must render in the group for an admin"

    # 2. Add email -> Custom provider.
    upd, q = callback_update("mb:add:custom", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, _group_ctx()))
    sessions = os_mod.list_sessions(USER_A)
    assert sessions, "wizard must open a session"
    session = sessions[-1]
    token = session.token

    # 3. The AWAITING slot must be pinned to the GROUP chat so wizard
    #    text input is accepted only in this group.
    slot = mb_bot.AWAITING.get(USER_A)
    assert slot is not None
    assert slot["chat_id"] == GROUP, f"slot chat_id={slot.get('chat_id')} must equal GROUP"

    # 4. Walk every non-secret step by sending text messages in the group.
    values = {
        "account_id": "alienfi",
        "display_name": "Alien.fi",
        "email": "info@alien.fi",
        "imap_host": "gcam1108.siteground.biz",
        "imap_port": "993",
        "imap_user": "info@alien.fi",
        "smtp_host": "gcam1108.siteground.biz",
        "smtp_port": "465",
        "smtp_user": "info@alien.fi",
    }
    for _ in range(20):
        slot = mb_bot.AWAITING.get(USER_A)
        if slot is None:
            break
        field = slot["field"]
        if field in (mb_bot._DM_PW_FIELD, mb_bot._PURPOSE_FIELD):
            break
        v = values.get(field)
        if v is None:
            # Skippable field with no provided value.
            v = "skip"
        upd, msg = command_update(USER_A, GROUP, "group")
        upd.effective_message.text = v
        upd.message.text = v
        _run(mb_bot.handle_text(upd, _group_ctx()))

    # 5. All collected fields visible in the session, state still DRAFT.
    session = os_mod.get_session(token, USER_A)
    assert session.state == os_mod.STATE_DRAFT
    for k, v in values.items():
        got = session.fields.get(k)
        if k in ("imap_port", "smtp_port"):
            assert int(got) == int(v)
        else:
            assert got == v, f"field {k}: {got!r} != {v!r}"


def _filled_draft_session() -> os_mod.OnboardingSession:
    """Like _complete_wizard but stops at state=DRAFT, ready for arm."""
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


def test_group_arm_hands_off_to_private_dm(tmp_profile):
    """Tapping Confirm & arm in the GROUP must NOT render the credential
    screen in group history. It must:
      * edit the group message to a bland 'continues in private' notice
      * DM the admin the real armed screen
    """
    _setup(tmp_profile)
    session = _filled_draft_session()
    token, nonce = session.token, session.nonce

    bot = _RecordingBot()
    ctx = _group_ctx(bot=bot)
    upd, q = callback_update(f"mb:arm:{token}:{nonce}", USER_A, GROUP, "group")
    _run(mb_bot.handle_callback(upd, ctx))

    # Group side: edit text is the bland handoff. No password / nonce / token
    # characters leaked.
    assert q.edits, "group arm must edit the message to the handoff notice"
    group_text, _mk = q.edits[-1]
    assert "private chat" in group_text.lower()
    assert "passwords never appear in this group" in group_text.lower()
    assert nonce not in group_text
    assert token not in group_text
    # No credential-capture controls rendered in the group.
    assert "send password" not in group_text.lower()

    # DM side: the admin receives the real armed screen in their private DM.
    assert len(bot.sent) == 1
    dm = bot.sent[0]
    assert dm["chat_id"] == USER_A
    assert "Credentials step" in dm["text"] or "password" in dm["text"].lower()

    # Session must now be AWAITING_CREDS.
    assert os_mod.get_session(token, USER_A).state == os_mod.STATE_AWAITING_CREDS


def test_group_arm_warns_when_dm_cannot_be_delivered(tmp_profile):
    """If the admin never DMed the bot before, send_message raises. The
    group notice should still never include credential UI, and must tell
    the admin to open the bot DM first."""
    _setup(tmp_profile)
    session = _filled_draft_session()
    bot = _RecordingBot(fail=True)
    ctx = _group_ctx(bot=bot)
    upd, q = callback_update(
        f"mb:arm:{session.token}:{session.nonce}", USER_A, GROUP, "group"
    )
    _run(mb_bot.handle_callback(upd, ctx))
    group_text, _ = q.edits[-1]
    assert "could not DM" in group_text or "could not dm" in group_text.lower()
    # Still safe: no credential UI leaked in the group.
    assert "send password" not in group_text.lower()


def test_group_wizard_text_from_other_chat_is_rejected(tmp_profile):
    """A slot pinned to GROUP must not consume a text message sent in
    a DM (or in a different group)."""
    _setup(tmp_profile)
    session = os_mod.start_session(USER_A)
    mb_bot.AWAITING[USER_A] = {
        "token": session.token,
        "field": "account_id",
        "chat_id": GROUP,
    }
    # Same user, DIFFERENT chat (DM):
    upd, _m = command_update(USER_A, USER_A, "private")
    upd.effective_message.text = "foo"
    upd.message.text = "foo"
    consumed = _run(mb_bot.handle_text(upd, _group_ctx()))
    assert consumed is False
    assert os_mod.get_session(session.token, USER_A).fields.get("account_id", "") == ""


def test_group_session_ownership_blocks_second_admin(tmp_profile):
    """A second admin (USER_B) must not be able to advance USER_A's open
    session even if they tap the inline buttons on the same group message."""
    _setup(tmp_profile)
    # Promote USER_B to owner so the group guard passes; ownership of the
    # SESSION must still refuse the arm because the session is bound to
    # USER_A at start_session() time.
    access.set_role(USER_A, USER_B, "owner")
    assert access.can(USER_B, access.P_ADMIN)
    session = _filled_draft_session()
    upd, q = callback_update(
        f"mb:arm:{session.token}:{session.nonce}", USER_B, GROUP, "group"
    )
    _run(mb_bot.handle_callback(upd, _group_ctx()))
    # Session must still be in DRAFT (never armed).
    assert os_mod.get_session(session.token, USER_A).state == os_mod.STATE_DRAFT
    # And the second admin must have been told it's not their session.
    assert any(alert for _t, alert in q.answers if alert)


def test_group_credentials_never_leak_even_if_typed_as_text(tmp_profile):
    """A password typed in the group during the wizard must NEVER be
    interpreted as a password. If the admin types it when the slot is
    expecting a non-secret field, it goes into that field (user error) —
    which is why validation is deferred until the private handoff. If the
    slot is cleared, the message is simply ignored. Credential CAPTURE
    (mb:dm_prompt:*) is denied in a group outright."""
    _setup(tmp_profile)
    os.environ["MAILOPS_ALLOW_DM_CREDENTIALS"] = "1"
    import mailops.bot.app as app_mod

    # 1. Arm attempt via dm_prompt in the group must be denied.
    upd, q = callback_update(
        "mb:dm_prompt:any:anynonce", USER_A, GROUP, "group",
    )
    _run(app_mod.on_callback(upd, _group_ctx()))
    assert USER_A not in mb_bot.AWAITING
    assert q.edits == []
    assert q.answers == [(app_mod.GROUP_DENIED_ALERT, True)]

    # 2. With NO slot active, a stray text in the group is ignored (never
    #    stored anywhere).
    upd, msg = command_update(USER_A, GROUP, "group")
    upd.effective_message.text = PASSWORD
    upd.message.text = PASSWORD
    consumed = _run(mb_bot.handle_text(upd, _group_ctx()))
    assert consumed is False


def test_group_concurrent_arm_only_one_succeeds(tmp_profile):
    """Two tappers racing on the SAME group arm button: one succeeds
    (hands off to DM), the second gets a 'session already armed' /
    nonce-rotated error. Credential-capture state is still single-use."""
    _setup(tmp_profile)
    session = _filled_draft_session()
    first = _RecordingBot()
    upd1, _q1 = callback_update(
        f"mb:arm:{session.token}:{session.nonce}", USER_A, GROUP, "group"
    )
    _run(mb_bot.handle_callback(upd1, _group_ctx(bot=first)))
    assert first.sent, "first tap must trigger the DM handoff"

    # Session now has a rotated nonce; replaying the OLD nonce must fail.
    second = _RecordingBot()
    upd2, q2 = callback_update(
        f"mb:arm:{session.token}:{session.nonce}", USER_A, GROUP, "group"
    )
    _run(mb_bot.handle_callback(upd2, _group_ctx(bot=second)))
    # Second DM must NOT be sent again.
    assert not second.sent, "replay of stale nonce must not trigger a second DM"


def test_group_failure_screen_is_redacted(tmp_profile):
    """_failed_screen reasons go through redact_text so sensitive tokens
    accidentally echoed by the server (``password=…`` / ``token=…`` / a
    Telegram bot token / Google OAuth token) never land in group history.
    Arbitrary emails and hostnames are not scrubbed by redact_text —
    they are the fields the admin typed themselves, so that is intentional."""
    _setup(tmp_profile)
    session = _filled_draft_session()
    session = os_mod.arm_for_credentials(session.token, USER_A, nonce=session.nonce)
    os_mod.begin_validation(session.token, USER_A)
    # Use a shape the project's _KV / token patterns actually catch.
    os_mod.finish_failure(
        session.token, USER_A,
        reason="IMAP auth failed password=HUNTER2SUPERSECRETXYZ token=sk_live_abc123xyz",
    )
    reloaded = os_mod.get_session(session.token, USER_A)
    text, _mk = mb_bot._failed_screen(reloaded)
    assert "HUNTER2SUPERSECRETXYZ" not in text, text
    # token=... matches the KV regex only when followed by =: value, so the
    # 'token=sk_live_abc123xyz' substring must be redacted.
    assert "sk_live_abc123xyz" not in text, text
    assert "<redacted>" in text


def test_group_cancel_in_group_returns_group_main_screen(tmp_profile):
    """Cancelling a session from the group must re-render the GROUP main
    screen (not the private one). Verifies in_group is threaded through."""
    _setup(tmp_profile)
    _onboard_coinography(tmp_profile)  # non-empty mailboxes list
    session = os_mod.start_session(USER_A)
    upd, q = callback_update(
        f"mb:cancel:{session.token}:{session.nonce}", USER_A, GROUP, "group"
    )
    _run(mb_bot.handle_callback(upd, _group_ctx()))
    text, _mk = q.edits[-1]
    # The group variant uses the group-specific footer.
    assert "passwords never appear in this group" in text.lower()


def test_group_whitelist_revocation_blocks_pending_slot(tmp_profile):
    """If the admin's slot was installed in group X but the group's
    whitelist is revoked, text arriving in group X stops being accepted
    (handle_text's defense-in-depth re-check)."""
    _setup(tmp_profile)
    # Make sure GROUP is whitelisted (bootstrap of _setup wires it).
    try:
        access.add_group(USER_A, GROUP, title="Mail agentt")
    except Exception:  # noqa: BLE001  — may already exist
        pass
    assert access.group_allowed(GROUP) is True
    session = os_mod.start_session(USER_A)
    mb_bot.AWAITING[USER_A] = {
        "token": session.token,
        "field": "account_id",
        "chat_id": GROUP,
    }
    # Revoke:
    access.remove_group(USER_A, GROUP)
    assert access.group_allowed(GROUP) is False
    upd, _m = command_update(USER_A, GROUP, "group")
    upd.effective_message.text = "shouldnotstick"
    upd.message.text = "shouldnotstick"
    consumed = _run(mb_bot.handle_text(upd, _group_ctx()))
    assert consumed is False
    assert os_mod.get_session(session.token, USER_A).fields.get("account_id", "") == ""


def test_group_mb_safe_body_filters():
    """Pure unit test on _group_safe_body — asserts the exact allow/deny set."""
    allowed = [
        "m",
        "add",
        "add:hostinger",
        "add:custom",
        "arm:tok:nce",
        "cancel:tok:nce",
        "edit:tok:nce",
        "skip:tok:nce",
        "poll:tok",
        "credsvia:tok:dm",
    ]
    denied = [
        "view:coinography",
        "enable:coinography",
        "disable:coinography",
        "health:coinography",
        "purpose:coinography",
        "audit:coinography",
        "dm_prompt:tok:nce",
        "errors",  # errors is private-only in group (hostnames in audit)
    ]
    for body in allowed:
        assert mb_bot._group_safe_body(body) is True, f"{body!r} should be group-safe"
    for body in denied:
        assert mb_bot._group_safe_body(body) is False, f"{body!r} should NOT be group-safe"






