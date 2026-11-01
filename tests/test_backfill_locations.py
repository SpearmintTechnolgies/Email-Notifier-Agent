"""Regression: messages processed before message_locations existed (the
8-row gap seen in production) never got a location, so the test-mode
dashboard showed all zeros. The backfill repairs exactly those rows —
read-only, notification-free, idempotent."""
from __future__ import annotations

import asyncio

from mailops.dashboard import collect_counts
from mailops.db import (
    already_notified,
    get_message_locations,
    init_db,
    is_message_processed,
    list_pending_notifications,
    mark_message_processed,
    mark_notified,
)
from mailops.sync_worker import backfill_all_locations, backfill_message_locations, process_account


def _run(coro):
    return asyncio.run(coro)


def _simulate_legacy_processed(fake_him, account: str, msg_id: str, subject: str, sender: str):
    """A message processed before message_locations existed: present in
    INBOX (test mode never moved it), marked processed, no location row."""
    fake_him.add_message(account, "INBOX", msg_id, subject=subject, sender=sender)
    mark_message_processed(account, msg_id)


def test_backfill_records_locations_for_legacy_processed(tmp_profile, fake_him):
    init_db()
    _simulate_legacy_processed(fake_him, "sales", "a1", "security alert breach", "a@b.co")
    _simulate_legacy_processed(fake_him, "sales", "b2", "how much to advertise", "c@d.co")
    assert get_message_locations("sales") == {}

    written = _run(backfill_message_locations(fake_him, "sales"))
    assert written == 2

    locs = get_message_locations("sales")
    assert locs["a1"]["mailbox"] == "INBOX.P0-Critical"
    assert locs["b2"]["mailbox"] == "INBOX.P1-Important"

    # Dashboard now reflects the repaired locations.
    counts = {c.account_id: c for c in collect_counts(force=True)}
    assert (counts["sales"].p0, counts["sales"].p1) == (1, 1)


def test_backfill_does_not_touch_gmail(tmp_profile, fake_him):
    """No move/copy/delete: everything stays exactly where it was."""
    init_db()
    _simulate_legacy_processed(fake_him, "sales", "a1", "security alert breach", "a@b.co")
    _run(backfill_message_locations(fake_him, "sales"))

    assert len(fake_him.envelopes("sales", "INBOX")) == 1
    assert fake_him.envelopes("sales", "INBOX.P0-Critical") == []
    assert fake_him.flags == {}  # no flags added


def test_backfill_sends_no_notifications(tmp_profile, fake_him):
    """Backfill must never alert — no pending rows, no notified keys created."""
    init_db()
    _simulate_legacy_processed(fake_him, "sales", "a1", "security alert breach", "a@b.co")
    _run(backfill_message_locations(fake_him, "sales"))

    assert list_pending_notifications() == []
    assert not already_notified("sales:INBOX.P0-Critical:a1")


def test_backfill_skips_unprocessed_mail(tmp_profile, fake_him):
    """New, never-processed mail is left for the normal sync path."""
    init_db()
    fake_him.add_message("sales", "INBOX", "new1", subject="security alert breach", sender="a@b.co")

    written = _run(backfill_message_locations(fake_him, "sales"))
    assert written == 0
    assert get_message_locations("sales") == {}
    assert not is_message_processed("sales", "new1")


def test_backfill_is_idempotent(tmp_profile, fake_him):
    init_db()
    _simulate_legacy_processed(fake_him, "sales", "a1", "security alert breach", "a@b.co")

    first = _run(backfill_message_locations(fake_him, "sales"))
    second = _run(backfill_message_locations(fake_him, "sales"))
    assert first == 1
    assert second == 0
    assert len(get_message_locations("sales")) == 1


def test_backfill_preserves_processed_and_notification_history(tmp_profile, fake_him):
    """processed_messages rows and prior notified keys survive untouched."""
    init_db()
    _simulate_legacy_processed(fake_him, "sales", "a1", "security alert breach", "a@b.co")
    mark_notified("sales:INBOX.P0-Critical:a1")  # alert was delivered pre-backfill

    _run(backfill_message_locations(fake_him, "sales"))

    assert is_message_processed("sales", "a1")
    assert already_notified("sales:INBOX.P0-Critical:a1")


def test_backfill_all_covers_every_account(tmp_profile, fake_him):
    init_db()
    _simulate_legacy_processed(fake_him, "sales", "a1", "security alert breach", "a@b.co")
    _simulate_legacy_processed(fake_him, "info", "i1", "how much to advertise", "c@d.co")

    results = _run(backfill_all_locations())
    assert results == {"sales": 1, "info": 1}
    assert get_message_locations("sales")["a1"]["mailbox"] == "INBOX.P0-Critical"
    assert get_message_locations("info")["i1"]["mailbox"] == "INBOX.P1-Important"


def test_backfill_then_sync_no_duplicate_processing(tmp_profile, fake_him):
    """After backfill, the normal sync must not re-notify or re-process the
    repaired messages."""
    init_db()
    _simulate_legacy_processed(fake_him, "sales", "a1", "security alert breach", "a@b.co")
    _run(backfill_message_locations(fake_him, "sales"))

    events = []

    async def notify(ev):
        events.append(ev)

    items = _run(process_account(fake_him, "sales", "sales@example.com", notify))
    assert items == []
    assert events == []
    assert list_pending_notifications() == []
