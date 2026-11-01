"""Human approval is single-use, and an unclear SMTP result is not retried."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from mailops.db import db, get_draft, init_db, save_draft
from mailops.himalaya_client import RunResult
from mailops.outbound import claim_approval, classify_smtp, create_pending, plan_reply
from tests.conftest import remember_all
from tests.test_bot_actions import _go, _press

import mailops.bot.app as app_mod


def _ready(fake_him, monkeypatch, *, body="Thanks, our rates are...", subject="rates?", sender="b@b.co"):
    init_db()
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    monkeypatch.setenv("MAILOPS_TEST_MODE", "0")
    fake_him.add_message("sales", "INBOX", "5", subject=subject, sender=sender)
    remember_all(fake_him)
    save_draft(111, "sales", "INBOX", "5", body)
    return body


def _said(q) -> str:
    parts: list[str] = []
    for item in q.message.sent:
        parts.append(item[0] if isinstance(item, tuple) else str(item))
    for item in q.edits:
        parts.append(item[0] if isinstance(item, tuple) else str(item))
    for text, _alert in q.answers:
        if text:
            parts.append(text)
    return " ".join(parts)


def _statuses():
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM send_approvals ORDER BY created_at")]


def test_classify_smtp_accepts_only_a_clear_success():
    assert classify_smtp(0, "Message successfully saved and sent", "") == "accepted"
    assert classify_smtp(0, "Message successfully sent!", "") == "accepted"
    assert classify_smtp(0, "test-mode: send blocked (not sent)", "") == "rejected"
    assert classify_smtp(1, "", "smtp auth failed") == "rejected"
    assert classify_smtp(1, "", "policy: sending is disabled") == "rejected"
    assert classify_smtp(1, "", "550 mailbox unavailable") == "rejected"
    assert classify_smtp(124, "", "himalaya timed out") == "ambiguous"
    assert classify_smtp(0, "", "") == "ambiguous"
    assert classify_smtp(1, "", "") == "ambiguous"
    assert classify_smtp(0, "250 queued", "successfully sent but also 550") == "ambiguous"


def test_preview_does_not_send_and_second_press_does_not_duplicate(tmp_profile, fake_him, monkeypatch):
    _ready(fake_him, monkeypatch)
    first = _press("send:sales|INBOX|5")
    second = _press("send:sales|INBOX|5")
    assert fake_him.sent == []
    assert _go(first) == _go(second)  # repeated preview reuses the one approval
    _press(_go(first))
    _press(_go(second))
    assert len(fake_him.sent) == 1
    assert _statuses()[0]["status"] == "accepted"


def test_threaded_claim_is_single_use(tmp_profile, fake_him, monkeypatch):
    _ready(fake_him, monkeypatch)
    preview = _press("send:sales|INBOX|5")
    token = _go(preview).split(":", 1)[1]

    def claim(_n):
        return claim_approval(token, 111).ok

    with ThreadPoolExecutor(max_workers=2) as pool:
        wins = sum(1 for ok in pool.map(claim, (1, 2)) if ok)
    assert wins == 1
    assert fake_him.sent == []
    assert _statuses()[0]["status"] == "claimed"


def test_concurrent_handlers_send_once(tmp_profile, fake_him, monkeypatch):
    body = _ready(fake_him, monkeypatch)
    preview = _press("send:sales|INBOX|5")
    token = _go(preview).split(":", 1)[1]
    from tests.test_bot_actions import FakeQuery

    async def race():
        await asyncio.gather(
            app_mod._commit_send(FakeQuery("go:" + token), fake_him, 111, token),
            app_mod._commit_send(FakeQuery("go:" + token), fake_him, 111, token),
        )

    asyncio.run(race())
    assert len(fake_him.sent) == 1
    assert fake_him.sent[0]["body"] == body

    # A second pair of claims, from separate threads, cannot take the used token.
    def claim(_n):
        return claim_approval(token, 111).ok

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(1 for ok in pool.map(claim, (1, 2)) if ok) == 0
    assert "SUPER" not in " ".join(r["smtp_note"] or "" for r in _statuses())


def test_restart_leaves_a_claimed_send_unrepeatable(tmp_profile, fake_him, monkeypatch):
    _ready(fake_him, monkeypatch)
    preview = _press("send:sales|INBOX|5")
    token = _go(preview).split(":", 1)[1]
    assert claim_approval(token, 111).ok
    # A new process sees the claimed row and must not create another approval.
    plan, why = plan_reply(
        fake_him, "sales", "INBOX", "5", "Thanks, our rates are...", "sales@example.com"
    )
    assert why == "" and plan is not None
    again, reason = create_pending(111, plan)
    assert again is None and reason == "blocked"
    assert claim_approval(token, 111).ok is False
    assert fake_him.sent == []


def test_ambiguous_smtp_is_not_retried(tmp_profile, fake_him, monkeypatch):
    calls = []

    def timeout(*_a, **_k):
        calls.append(1)
        return RunResult(124, "", "himalaya timed out")

    monkeypatch.setattr(fake_him, "reply_send", timeout)
    _ready(fake_him, monkeypatch, body="UNIQUE-DRAFT-BODY")
    preview = _press("send:sales|INBOX|5")
    result = _press(_go(preview))
    assert calls == [1]
    assert "not retried" in result.edits[-1][0]
    assert "UNIQUE-DRAFT-BODY" not in result.edits[-1][0]
    blocked = _press("send:sales|INBOX|5")
    assert "not sent again" in _said(blocked)
    assert calls == [1]
    assert _statuses()[0]["status"] == "ambiguous"
    assert "UNIQUE-DRAFT-BODY" not in (_statuses()[0]["smtp_note"] or "")


def test_clear_failure_can_be_approved_again_explicitly(tmp_profile, fake_him, monkeypatch):
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return RunResult(1, "", "smtp auth failed")
        return RunResult(0, "Message successfully saved and sent", "")

    monkeypatch.setattr(fake_him, "reply_send", flaky)
    _ready(fake_him, monkeypatch)
    first = _press("send:sales|INBOX|5")
    failed = _press(_go(first))
    assert "Send failed" in failed.edits[-1][0]
    assert get_draft(111) is not None
    second = _press("send:sales|INBOX|5")
    _press(_go(second))
    assert calls["n"] == 2
    assert [r["status"] for r in _statuses()] == ["rejected", "accepted"]


def test_changed_draft_is_not_what_gets_sent(tmp_profile, fake_him, monkeypatch):
    _ready(fake_him, monkeypatch, body="reviewed text")
    preview = _press("send:sales|INBOX|5")
    save_draft(111, "sales", "INBOX", "5", "different text")
    q = _press(_go(preview))
    assert fake_him.sent == []
    assert "no longer matches" in _said(q)
    assert _statuses()[0]["status"] == "rejected"
    assert _statuses()[0]["smtp_note"] == "draft_mismatch"


def test_reply_recipient_and_threading_are_rechecked(tmp_profile, fake_him, monkeypatch):
    _ready(fake_him, monkeypatch)
    fake_him.store["sales"]["INBOX"]["5"]["reply-to"] = [{"email": "desk@partner.example"}]
    preview = _press("send:sales|INBOX|5")
    shown = preview.edits[-1][0]
    assert "To: desk@partner.example" in shown
    assert "In-Reply-To: <5@mailops.test>" in shown
    assert "References: <5@mailops.test>" in shown
    fake_him.store["sales"]["INBOX"]["5"]["reply-to"] = [{"email": "other@partner.example"}]
    q = _press(_go(preview))
    assert fake_him.sent == []
    assert "Nothing was sent" in _said(q)


def test_missing_message_id_refuses_before_any_approval(tmp_profile, fake_him, monkeypatch):
    init_db()
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    monkeypatch.setenv("MAILOPS_TEST_MODE", "0")
    fake_him.add_message("sales", "INBOX", "5", subject="rates?", sender="b@b.co", message_id="")
    remember_all(fake_him)
    save_draft(111, "sales", "INBOX", "5", "body")
    q = _press("send:sales|INBOX|5")
    assert fake_him.sent == []
    assert _statuses() == []
    said = _said(q)
    assert "Message-ID" in said and "Nothing was sent" in said


def test_several_reply_to_addresses_are_refused(tmp_profile, fake_him, monkeypatch):
    _ready(fake_him, monkeypatch)
    fake_him.store["sales"]["INBOX"]["5"]["reply-to"] = [
        {"email": "one@partner.example"},
        {"email": "two@partner.example"},
    ]
    q = _press("send:sales|INBOX|5")
    assert fake_him.sent == []
    assert "more than one Reply-To" in _said(q)


def test_cancel_and_test_mode_do_not_send(tmp_profile, fake_him, monkeypatch):
    _ready(fake_him, monkeypatch)
    preview = _press("send:sales|INBOX|5")
    token = _go(preview)
    cancelled = _press("nogo:" + token.split(":", 1)[1])
    assert "Cancelled" in cancelled.edits[-1][0]
    _press(token)
    assert fake_him.sent == []
    preview = _press("send:sales|INBOX|5")
    monkeypatch.setenv("MAILOPS_TEST_MODE", "1")
    blocked = _press(_go(preview))
    assert fake_him.sent == []
    assert any("TEST MODE" in t for t, _ in blocked.edits)
    assert _statuses()[-1]["status"] == "pending"


def test_logs_do_not_contain_the_draft(tmp_profile, fake_him, monkeypatch, caplog):
    _ready(fake_him, monkeypatch, body="SUPER_SECRET_BODY")
    preview = _press("send:sales|INBOX|5")
    with caplog.at_level(logging.INFO, logger="mailops.bot"):
        _press(_go(preview))
    assert "SUPER_SECRET_BODY" not in caplog.text
    assert "smtp result" in caplog.text
    assert "accepted" in caplog.text


def test_wrong_account_and_unauthorized_still_send_nothing(tmp_profile, fake_him, monkeypatch):
    _ready(fake_him, monkeypatch)
    other = _press("send:info|INBOX|5")
    assert fake_him.sent == []
    assert "different account" in _said(other)
    _press("send:sales|INBOX|5", user_id=999)
    assert fake_him.sent == []
