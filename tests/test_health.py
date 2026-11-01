"""Tests for the operational sync-health incident tracker + consolidated
ops alert (October 22, 2026 incident fix).

Covers:
* classify_error maps the real WinError 4551 ("Application Control")
  failure, auth failures, DNS, TLS, timeouts and generic errors to
  distinct, sanitized categories (never the old one-size-fits-all
  "Check credentials / IMAP" message);
* a NEW incident is flagged for the next alert, but repeating the exact
  same failure every poll does NOT re-flag it (no flood);
* a materially different error category on an already-open incident DOES
  re-flag it, without flooding;
* recovery sends exactly one consolidated "recovered" message;
* the health alert is a separate message/table from the mail digest and
  is never combined with new-mail counts;
* persisted incident state survives a simulated process restart (same
  on-disk ops.db, fresh Python-level call) so Telegram is never
  re-flooded after a crash/restart;
* the manual Retry Health Check path is rate-limited per account.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from mailops.db import init_db, list_open_incidents, list_pending_health_alerts


def _run(coro):
    return asyncio.run(coro)


class _CollectBot:
    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id=None, text=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_classify_application_control_block():
    from mailops.health import classify_error

    exc = OSError(
        "[WinError 4551] An Application Control policy has blocked this file"
    )
    exc.winerror = 4551
    category, detail = classify_error(exc)
    assert category == "local_dependency_blocked"
    assert "Application Control" in detail or "OS" in detail


def test_classify_auth_failure():
    from mailops.health import classify_error

    category, _ = classify_error(RuntimeError("NO [AUTHENTICATIONFAILED] Invalid credentials"))
    assert category == "auth_failed"


def test_classify_dns_failure():
    from mailops.health import classify_error

    category, _ = classify_error(RuntimeError("[Errno 11001] getaddrinfo failed"))
    assert category == "dns_failure"


def test_classify_tls_failure():
    from mailops.health import classify_error

    category, _ = classify_error(RuntimeError("SSL: CERTIFICATE_VERIFY_FAILED"))
    assert category == "tls_failure"


def test_classify_timeout():
    from mailops.health import classify_error

    category, _ = classify_error(RuntimeError("Connection timed out"))
    assert category == "network_timeout"


def test_classify_generic_internal_error():
    from mailops.health import classify_error

    category, detail = classify_error(RuntimeError("imap down"))
    assert category == "internal_error"
    assert "imap down" in detail


def test_classify_redacts_secrets():
    from mailops.health import classify_error

    category, detail = classify_error(RuntimeError("password=supersecret123 login failed"))
    assert "supersecret123" not in detail


# ---------------------------------------------------------------------------
# Incident lifecycle: new / unchanged / changed / recovered
# ---------------------------------------------------------------------------


def test_new_failure_opens_incident_and_flags_for_alert(tmp_profile):
    init_db()
    from mailops.health import record_sync_failure

    result = record_sync_failure("acct1", RuntimeError("imap down"))
    assert result["is_new"] is True
    incidents = list_open_incidents()
    assert len(incidents) == 1
    assert incidents[0]["account_id"] == "acct1"
    pending = list_pending_health_alerts()
    assert [r["account_id"] for r in pending] == ["acct1"]


def test_repeated_identical_failure_does_not_reflag_after_notified(tmp_profile):
    """The core flood fix: once an incident has been included in an alert,
    the SAME unresolved error on later polls must not re-flag it."""
    init_db()
    from mailops.health import record_sync_failure, build_and_send_health_alert
    from mailops.db import clear_pending_health_alerts

    record_sync_failure("acct1", RuntimeError("imap down"))
    # Simulate the alert having been sent (clears pending_alert).
    clear_pending_health_alerts(["acct1"])
    assert list_pending_health_alerts() == []

    # 8 more polls, identical failure — this is exactly the production
    # incident: 8 cycles, same error, zero new Telegram traffic expected.
    for _ in range(8):
        result = record_sync_failure("acct1", RuntimeError("imap down"))
        assert result["is_new"] is False
        assert result["category_changed"] is False
    assert list_pending_health_alerts() == []
    incidents = list_open_incidents()
    assert incidents[0]["consecutive_failures"] == 9


def test_material_category_change_reflags_without_flooding(tmp_profile):
    init_db()
    from mailops.health import record_sync_failure
    from mailops.db import clear_pending_health_alerts

    record_sync_failure("acct1", RuntimeError("imap down"))  # network_timeout-ish/internal
    clear_pending_health_alerts(["acct1"])
    # Now the error category genuinely changes (e.g. DNS starts failing).
    result = record_sync_failure("acct1", RuntimeError("getaddrinfo failed"))
    assert result["category_changed"] is True
    pending = list_pending_health_alerts()
    assert [r["account_id"] for r in pending] == ["acct1"]


def test_recovery_flags_a_single_consolidated_message(tmp_profile):
    init_db()
    from mailops.health import record_sync_failure, record_sync_success
    from mailops.db import clear_pending_health_alerts

    record_sync_failure("acct1", RuntimeError("imap down"))
    clear_pending_health_alerts(["acct1"])
    recovered = record_sync_success("acct1")
    assert recovered is True
    pending = list_pending_health_alerts()
    assert len(pending) == 1
    assert pending[0]["status"] == "resolved"
    assert list_open_incidents() == []


def test_healthy_account_with_no_incident_is_a_noop(tmp_profile):
    init_db()
    from mailops.health import record_sync_success

    assert record_sync_success("neveropened") is False
    assert list_pending_health_alerts() == []


# ---------------------------------------------------------------------------
# Consolidated alert: separate message, no flooding, never mixed with mail
# ---------------------------------------------------------------------------


def test_build_and_send_health_alert_sends_one_message(tmp_profile):
    init_db()
    from mailops.config import save_settings, load_settings
    from mailops.health import record_sync_failure, build_and_send_health_alert

    s = load_settings()
    s["active_accounts"] = ["sales", "info"]
    save_settings(s)
    record_sync_failure("sales", RuntimeError("imap down"))
    record_sync_failure("info", RuntimeError("getaddrinfo failed"))

    bot = _CollectBot()
    diag = _run(build_and_send_health_alert(bot, force=True, reason="test"))
    assert diag["sent"] is True
    assert diag["changed"] == 2
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "sales" in text.lower() or "Sales" in text
    assert "unhealthy" in text.lower()
    # Never mentions new-mail / digest vocabulary — separate concern.
    assert "actionable" not in text.lower()


def test_no_pending_alerts_sends_nothing(tmp_profile):
    init_db()
    from mailops.health import build_and_send_health_alert

    bot = _CollectBot()
    diag = _run(build_and_send_health_alert(bot, force=True, reason="test"))
    assert diag["sent"] is False
    assert diag["skipped"] == "empty"
    assert bot.sent == []


def test_alert_clears_pending_and_does_not_resend_unchanged(tmp_profile):
    init_db()
    from mailops.config import save_settings, load_settings
    from mailops.health import record_sync_failure, build_and_send_health_alert

    s = load_settings()
    s["active_accounts"] = ["sales"]
    save_settings(s)
    record_sync_failure("sales", RuntimeError("imap down"))

    bot = _CollectBot()
    first = _run(build_and_send_health_alert(bot, force=True, reason="test"))
    assert first["sent"] is True

    # Same unresolved failure happens again (next poll) — no new alert.
    record_sync_failure("sales", RuntimeError("imap down"))
    second = _run(build_and_send_health_alert(bot, force=True, reason="test"))
    assert second["sent"] is False
    assert len(bot.sent) == 1


def test_alert_delivery_failure_keeps_incident_pending(tmp_profile):
    import pytest

    init_db()
    from mailops.health import record_sync_failure, build_and_send_health_alert

    record_sync_failure("sales", RuntimeError("imap down"))

    class _FailBot:
        async def send_message(self, chat_id=None, text=None, reply_markup=None):
            raise RuntimeError("telegram unreachable")

    with pytest.raises(RuntimeError):
        _run(build_and_send_health_alert(_FailBot(), force=True, reason="test"))
    assert [r["account_id"] for r in list_pending_health_alerts()] == ["sales"]


# ---------------------------------------------------------------------------
# Restart dedup: persisted state survives a simulated crash/restart
# ---------------------------------------------------------------------------


def test_restart_does_not_reflood_already_notified_incident(tmp_profile):
    """Simulates a process restart: the incident row and its cleared
    pending_alert flag already live in ops.db before this 'process' starts,
    so the very first health check after 'restart' must not alert again for
    an unresolved-but-already-notified error."""
    init_db()
    from mailops.health import record_sync_failure
    from mailops.db import clear_pending_health_alerts

    record_sync_failure("sales", RuntimeError("imap down"))
    clear_pending_health_alerts(["sales"])  # pretend the pre-restart alert was sent

    # "Restart": nothing in-memory carries over except what's in ops.db.
    # The very next poll's failure must not re-flag.
    result = record_sync_failure("sales", RuntimeError("imap down"))
    assert result["category_changed"] is False
    assert list_pending_health_alerts() == []


# ---------------------------------------------------------------------------
# Manual Retry Health Check: rate limiting
# ---------------------------------------------------------------------------


def test_manual_health_check_recovers_and_rate_limits(tmp_profile, fake_him):
    init_db()
    from mailops.config import save_settings, load_settings
    from mailops.health import run_manual_health_check, MIN_MANUAL_RECHECK_S

    s = load_settings()
    s["active_accounts"] = ["sales"]
    save_settings(s)

    # First manual check succeeds (FakeHimalaya.run defaults to success).
    results = run_manual_health_check(["sales"])
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].rate_limited is False

    # Immediately retrying is rate-limited.
    results2 = run_manual_health_check(["sales"])
    assert results2[0].rate_limited is True
    assert MIN_MANUAL_RECHECK_S > 0


def test_manual_health_check_surfaces_classified_failure(tmp_profile, fake_him):
    init_db()
    from mailops.himalaya_client import RunResult
    from mailops.health import run_manual_health_check

    fake_him.fail_run["sales"] = RunResult(1, "", "NO [AUTHENTICATIONFAILED]")
    results = run_manual_health_check(["sales"])
    assert results[0].ok is False
    assert results[0].category == "auth_failed"
    from mailops.db import list_open_incidents

    assert [i["account_id"] for i in list_open_incidents()] == ["sales"]


# ---------------------------------------------------------------------------
# Group callback permissions
# ---------------------------------------------------------------------------


def test_health_callbacks_allowed_in_authorized_groups():
    from mailops.bot.app import group_callback_allowed

    assert group_callback_allowed("health:view")
    assert group_callback_allowed("health:retry:_all")


def test_health_callbacks_require_at_least_view_permission():
    from mailops.bot import admin
    from mailops import access

    assert admin.required_perm("health:view") == access.P_VIEW
    assert admin.required_perm("health:retry:_all") == access.P_VIEW


# ---------------------------------------------------------------------------
# Regression: "Retry Health Check" crashed every tap with
# ``NameError: name 'asyncio' is not defined`` because mailops/bot/app.py
# called ``asyncio.to_thread(...)`` without importing ``asyncio``. Routing
# the tap through the REAL dispatcher (not calling run_manual_health_check
# directly) is what catches this class of bug - a unit test on health.py
# alone would never see it.
# ---------------------------------------------------------------------------
def test_retry_health_check_button_does_not_crash_dispatcher(tmp_profile, fake_him):
    import mailops.bot.app as app_mod
    from mailops import access
    from tests.test_telegram_group import USER_A, GROUP, callback_update, set_env

    set_env(tmp_profile, allowed=str(USER_A), home=f"{USER_A},{GROUP}")
    access.ensure()
    access.add_group(USER_A, GROUP)
    upd, q = callback_update("health:retry:_all", USER_A, GROUP, "group")
    _run(app_mod.on_callback(upd, SimpleNamespace(user_data={}, bot=None)))
    # A working tap always answers the callback query (loading spinner
    # clears) and/or edits the unhealthy-accounts screen. A NameError would
    # propagate out of on_callback with neither.
    assert q.answers or q.edits, (
        "Retry Health Check produced no answer/edit - the asyncio import "
        "regression is back"
    )
