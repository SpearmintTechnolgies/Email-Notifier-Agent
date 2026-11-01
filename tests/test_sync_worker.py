from __future__ import annotations

import asyncio

from mailops.db import (
    already_notified,
    init_db,
    is_message_processed,
    list_undelivered_digest_rows,
    pop_batch,
)
from mailops.sync_worker import process_account, sync_once


def _run(coro):
    return asyncio.run(coro)


def _disable_first_sync_silence():
    """Opt out of silent-first-sync so test assertions can see the digest rows.

    The default (``digest_initial_sync_silent=True``) was added as part of
    the 15-minute consolidated digest redesign: a mailbox's first sync
    seeds its backlog as 'already covered' so onboarding 90 inboxes does
    not fire 500 Telegram messages. The behaviours covered by this file
    (classify + move + enqueue for the digest, dedupe across runs, retry
    semantics on Telegram failure) are orthogonal to that onboarding
    guard, so each test flips the setting off up front.
    """
    from mailops.config import load_settings, save_settings

    s = load_settings()
    s["digest_initial_sync_silent"] = False
    save_settings(s)


def test_sync_moves_and_notifies(tmp_profile, fake_him, monkeypatch):
    init_db()
    _disable_first_sync_silence()
    fake_him.add_message("sales", "INBOX", "1", subject="security alert breach", sender="a@b.co", uid=1)
    fake_him.add_message("sales", "INBOX", "2", subject="how much to advertise", sender="c@d.co", uid=2)

    events = []

    async def notify(ev):
        events.append(ev)

    items = _run(process_account(fake_him, "sales", "sales@example.com", notify))
    assert len(items) == 2
    by_subject = {i.subject: i for i in items}
    assert by_subject["security alert breach"].priority == 0
    assert by_subject["how much to advertise"].priority == 1
    # Messages moved out of INBOX into priority folders
    assert fake_him.envelopes("sales", "INBOX") == []
    assert len(fake_him.envelopes("sales", "INBOX.P0-Critical")) == 1
    assert len(fake_him.envelopes("sales", "INBOX.P1-Important")) == 1
    # needs_reply flagged
    assert ("sales", "INBOX.P0-Critical", "1") in fake_him.flags
    # P0/P1 are now consolidated into the next digest instead of individual
    # Telegram messages - no notify() calls, two digest_pending rows.
    assert events == []
    rows = list_undelivered_digest_rows(max_priority=1)
    assert {r["msg_id"] for r in rows} == {"1", "2"}
    assert {r["priority"] for r in rows} == {0, 1}
    assert all(r["seeded"] == 0 for r in rows)
    # Both marked processed by their string IDs
    assert is_message_processed("sales", "1")
    assert is_message_processed("sales", "2")


def test_sync_second_run_only_new(tmp_profile, fake_him):
    init_db()
    fake_him.add_message("sales", "INBOX", "1", subject="how much to advertise", sender="c@d.co", uid=1)
    _run(process_account(fake_him, "sales", "sales@example.com", None))
    fake_him.add_message("sales", "INBOX", "5", subject="newsletter digest", sender="n@r.co", uid=5)
    items = _run(process_account(fake_him, "sales", "sales@example.com", None))
    assert [i.msg_id for i in items] == ["5"]
    assert is_message_processed("sales", "5")


def test_hexadecimal_gmail_ids_not_missed(tmp_profile, fake_him):
    """Regression: Gmail hex IDs must not be int-compared. A hex ID like
    '18f3a2b1c9d4e5f6' would previously be mangled by int() parsing and the
    numeric cursor, causing new mail to be skipped."""
    init_db()
    fake_him.add_message("sales", "INBOX", "18f3a2b1c9d4e5f6", subject="security alert", sender="a@b.co")
    fake_him.add_message("sales", "INBOX", "18f3a2b1c9d4e5f7", subject="newsletter", sender="n@r.co")

    items = _run(process_account(fake_him, "sales", "sales@example.com", None))
    assert {i.msg_id for i in items} == {"18f3a2b1c9d4e5f6", "18f3a2b1c9d4e5f7"}
    assert is_message_processed("sales", "18f3a2b1c9d4e5f6")
    assert is_message_processed("sales", "18f3a2b1c9d4e5f7")

    # A second run must not reprocess them (dedupe across restarts).
    items2 = _run(process_account(fake_him, "sales", "sales@example.com", None))
    assert items2 == []


def test_hex_id_arriving_after_higher_hex_id(tmp_profile, fake_him):
    """Numeric cursors break when IDs aren't ordered as ints. Process a high
    hex ID first, then a 'lower' one — both must be seen exactly once."""
    init_db()
    fake_him.add_message("sales", "INBOX", "ffffffffffffffff", subject="first", sender="a@b.co")
    _run(process_account(fake_him, "sales", "sales@example.com", None))
    fake_him.add_message("sales", "INBOX", "0000000000000001", subject="second", sender="c@d.co")
    items = _run(process_account(fake_him, "sales", "sales@example.com", None))
    assert [i.msg_id for i in items] == ["0000000000000001"]


def test_move_failure_keeps_message_and_continues(tmp_profile, fake_him):
    init_db()
    fake_him.add_message("sales", "INBOX", "1", subject="security alert", sender="a@b.co", uid=1)
    fake_him.fail_moves = True
    items = _run(process_account(fake_him, "sales", "sales@example.com", None))
    assert items == []
    # message stays in INBOX and is NOT marked processed (retried next run)
    assert len(fake_him.envelopes("sales", "INBOX")) == 1
    assert not is_message_processed("sales", "1")


def test_notification_failure_does_not_reprocess_or_move(tmp_profile, fake_him):
    """Digest-redesign retry semantics.

    Pre-redesign this test asserted that a failing Telegram notify leaves
    a row in ``pending_notifications``. The new path never calls
    ``notify()`` for P0/P1 automatically (that was the Alien FI flood);
    the digest table is the only source of truth. The invariant we DO
    want to preserve is: 'a failing Telegram handoff never makes us
    re-move a message, re-flag it, or forget about it'. So the test
    now verifies: the message was processed + moved exactly once AND
    the digest row survives for the next tick.
    """
    init_db()
    _disable_first_sync_silence()
    fake_him.add_message("sales", "INBOX", "9", subject="security alert breach", sender="a@b.co", uid=9)

    async def bad_notify(ev):
        raise RuntimeError("telegram down")

    _run(process_account(fake_him, "sales", "sales@example.com", bad_notify))
    # Message processed and moved exactly once despite the Telegram failure.
    assert is_message_processed("sales", "9")
    assert fake_him.envelopes("sales", "INBOX") == []
    assert len(fake_him.envelopes("sales", "INBOX.P0-Critical")) == 1
    # Digest row persisted for the next 15-minute tick.
    rows = list_undelivered_digest_rows(max_priority=1)
    assert [r["msg_id"] for r in rows] == ["9"]

    # Second sync: no reprocessing, no second move, no duplicate row.
    _run(process_account(fake_him, "sales", "sales@example.com", bad_notify))
    rows2 = list_undelivered_digest_rows(max_priority=1)
    assert [r["msg_id"] for r in rows2] == ["9"]
    assert len(fake_him.envelopes("sales", "INBOX.P0-Critical")) == 1


def test_p2_is_batched_when_min_alert_is_important(tmp_profile, fake_him):
    """min_alert_priority=1 must still queue P2 for the digest, not skip it."""
    from mailops.config import load_settings, save_settings

    init_db()
    s = load_settings()
    s["min_alert_priority"] = 1
    save_settings(s)
    fake_him.add_message(
        "sales", "INBOX", "n1", subject="hello there, random note", sender="a@b.co"
    )
    events = []

    async def notify(ev):
        events.append(ev)

    items = _run(process_account(fake_him, "sales", "sales@example.com", notify))
    assert len(items) == 1
    assert items[0].priority == 2
    assert events == []
    key = "sales:INBOX.P2-Normal:n1"
    assert not already_notified(key)
    rows = pop_batch(0)
    assert [r["msg_id"] for r in rows] == ["n1"]


def test_no_duplicate_notifications_across_runs(tmp_profile, fake_him):
    init_db()
    _disable_first_sync_silence()
    fake_him.add_message("sales", "INBOX", "3", subject="how much to advertise", sender="c@d.co", uid=3)
    events = []

    async def notify(ev):
        events.append(ev)

    _run(process_account(fake_him, "sales", "sales@example.com", notify))
    _run(process_account(fake_him, "sales", "sales@example.com", notify))
    # No per-mail notify() calls happen on either run (digest-only path).
    assert events == []
    # The digest table carries exactly ONE row for this message: the second
    # pass must not create a duplicate even on the same msg_id.
    rows = list_undelivered_digest_rows(max_priority=1)
    assert [r["msg_id"] for r in rows] == ["3"]


def test_sync_once_reports_account_errors(tmp_profile, fake_him, monkeypatch):
    """October 2026 incident fix: sync_once no longer calls notify() with a
    'sync_error' event per failing account per poll (that was the 24-message
    flood). Failures are now recorded as persisted incidents via
    mailops.health; the consolidated alert is sent separately, once per
    poll, only on a state change."""
    init_db()
    import mailops.sync_worker as sw
    from mailops.db import list_open_incidents

    async def boom(him, account_id, email, notify=None):
        raise RuntimeError("imap down")

    monkeypatch.setattr(sw, "process_account", boom)
    events = []

    async def notify(ev):
        events.append(ev)

    out = _run(sync_once(notify))
    assert out == []
    # No per-account Telegram events fired directly from sync_once anymore.
    assert events == []
    # Both fixture accounts now have an OPEN incident recorded.
    incidents = list_open_incidents()
    assert {i["account_id"] for i in incidents} == {"sales", "info"}
    assert all(i["category"] == "internal_error" for i in incidents)
