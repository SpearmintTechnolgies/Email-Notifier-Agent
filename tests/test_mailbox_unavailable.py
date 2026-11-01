"""Regression tests for the "0 emails but the mailbox has mail" incident.

Live symptom (reported 26 Oct): /dashboard -> coinography -> Important
showed "Page 1 of 1, 0 emails" while ``message_locations`` held 9 recorded
P1 messages for that account.

Root cause chain, all three links covered here:

  1. ``Himalaya.envelopes`` returns ``[]`` for ANY failure, so an
     unreachable IMAP host was indistinguishable from an empty folder.
  2. ``sync_worker.process_account`` hit its "inbox is empty" early return
     and completed successfully, so ``sync_once`` recorded a health SUCCESS
     and no incident was ever opened - 32 of 39 mailboxes silently went
     28+ hours without syncing.
  3. ``cache`` published zero counts and empty buckets over good data, so
     the dashboard reported no mail.
"""
from __future__ import annotations

import asyncio

import pytest

from mailops import cache as cache_mod
from mailops.db import (
    get_incident,
    get_message_locations,
    init_db,
    list_open_incidents,
    locations_by_priority,
    set_message_location,
)
from mailops.himalaya_client import MailboxUnavailable, RunResult, is_transport_failure


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_cache():
    """``mailops.cache`` is process-global; every test here starts clean.

    Without this, counts seeded by one test leak into the next and mask the
    very mismatch these tests exist to detect.
    """
    def _clear():
        cache_mod._counts = []
        cache_mod._buckets = {}
        cache_mod._updated_at = 0.0
        cache_mod._hydrated = False
        cache_mod._progress = (0, 0)
        cache_mod._unreachable.clear()
        cache_mod._warned_unavailable.clear()

    _clear()
    yield
    _clear()


# ------------------------------------------------------- 1. the transport layer
@pytest.mark.parametrize(
    "stderr",
    [
        'connect mail.coinography.com:993',
        "A connection attempt failed ... (os error 10060)",
        "connection refused",
        "authentication failed",
        "certificate verify failed",
        "could not resolve host",
    ],
)
def test_server_and_auth_failures_are_recognised(stderr):
    assert is_transport_failure(RunResult(1, "", stderr)) is True


@pytest.mark.parametrize(
    "stderr",
    [
        "mailbox does not exist",
        "no such mailbox: INBOX.P0-Critical",
    ],
)
def test_a_missing_folder_is_not_a_transport_failure(stderr):
    """An absent priority folder must still read as 'no mail here'."""
    assert is_transport_failure(RunResult(1, "", stderr)) is False


def test_success_is_never_a_transport_failure():
    assert is_transport_failure(RunResult(0, '{"envelopes":[]}', "")) is False


def test_our_own_timeout_and_missing_binary_are_transport_failures():
    assert is_transport_failure(RunResult(124, "", "himalaya timed out")) is True
    assert is_transport_failure(RunResult(127, "", "himalaya not found")) is True


def test_checked_listing_raises_while_lenient_one_returns_empty(tmp_profile, fake_him):
    """The two contracts must differ: that difference IS the fix."""
    fake_him.unreachable = {"sales"}
    assert fake_him.envelopes("sales", "INBOX") == []
    with pytest.raises(MailboxUnavailable):
        fake_him.envelopes_checked("sales", "INBOX")


def _sync_with(fake_him, monkeypatch):
    """Run one real ``sync_once`` pass against the injected fake client."""
    import mailops.sync_worker as sw

    monkeypatch.setattr(sw, "Himalaya", lambda: fake_him)
    return _run(sw.sync_once(notify=None))


# --------------------------------------------- 2. health must notice an outage
def test_unreachable_mailbox_records_a_health_failure_not_a_success(
    tmp_profile, fake_him, monkeypatch
):
    """THE silent failure. A dead host must open an incident.

    Before the fix an unreachable server produced an empty envelope list,
    process_account returned [] through its "empty inbox" path, and
    sync_once recorded SUCCESS - so 32 mailboxes went stale for over a day
    with no alert and a resolved incident table.
    """
    init_db()
    fake_him.unreachable = {"sales"}
    _sync_with(fake_him, monkeypatch)

    incident = get_incident("sales")
    assert incident is not None, "no incident was opened for a dead mailbox"
    assert incident["status"] == "open", incident
    assert int(incident["consecutive_failures"] or 0) >= 1, incident
    assert "sales" in {r["account_id"] for r in list_open_incidents()}


def test_reachable_mailbox_still_records_success(tmp_profile, fake_him, monkeypatch):
    """Guard the other direction: a healthy pass must not look like a failure."""
    init_db()
    fake_him.add_message("sales", "INBOX", "1", subject="hi", sender="a@b.c")
    _sync_with(fake_him, monkeypatch)
    incident = get_incident("sales")
    assert incident is None or incident["status"] == "resolved", incident


# ------------------------------------- 3. the cache must not publish zeroes
def test_refresh_keeps_last_known_counts_when_a_host_goes_down(
    tmp_profile, fake_him
):
    """A refresh during an outage must not overwrite good counts with 0."""
    init_db()
    fake_him.add_message(
        "sales", "INBOX.P1-Important", "1", subject="real mail", sender="a@b.c"
    )
    cache_mod.set_provider(fake_him)
    try:
        cache_mod.refresh_all()
        before = {c.account_id: c.p1 for c in cache_mod.get_counts()}
        assert before["sales"] == 1, before

        # The host goes down; the next pass must preserve the number.
        fake_him.unreachable = {"sales"}
        result = cache_mod.refresh_all()
        after = {c.account_id: c.p1 for c in cache_mod.get_counts()}
        assert after["sales"] == 1, (
            f"counts were zeroed by an outage: {before} -> {after}"
        )
        assert "sales" in result["unreachable"]
        assert "sales" in cache_mod.unreachable_accounts()
    finally:
        cache_mod.set_provider(None)


def test_bucket_falls_back_to_recorded_classifications_during_an_outage(
    tmp_profile, fake_him
):
    """THE reported symptom: a non-zero count with an empty list screen."""
    init_db()
    set_message_location(
        "sales", "398", "INBOX.P1-Important", subject="Rate card", sender="f@x.com", priority=1
    )
    set_message_location(
        "sales", "409", "INBOX.P1-Important", subject="Invitation", sender="l@y.com", priority=1
    )
    cache_mod.set_provider(fake_him)
    try:
        fake_him.unreachable = {"sales"}
        # Seed counts the way the live process does, then ask for the list.
        assert cache_mod.seed_from_db() is True
        counts = {c.account_id: c.p1 for c in cache_mod.get_counts()}
        items = cache_mod.get_bucket(1, "sales")
        assert counts["sales"] == 2, counts
        assert len(items) == 2, (
            f"dashboard said {counts['sales']} but the list had {len(items)}"
        )
        subjects = {i["subject"] for i in items}
        assert subjects == {"Rate card", "Invitation"}, subjects
        # Marked so the UI can say these are local records, not live IMAP.
        assert all(i.get("stale") == "1" for i in items)
    finally:
        cache_mod.set_provider(None)


def test_count_and_list_never_disagree_during_an_outage(tmp_profile, fake_him):
    """The invariant the bug violated, stated directly."""
    init_db()
    for i in range(4):
        set_message_location(
            "sales", str(i), "INBOX.P0-Critical", subject=f"s{i}", sender="a@b.c", priority=0
        )
    cache_mod.set_provider(fake_him)
    try:
        fake_him.unreachable = {"sales"}
        cache_mod.seed_from_db()
        count = {c.account_id: c.p0 for c in cache_mod.get_counts()}["sales"]
        listed = len(cache_mod.get_bucket(0, "sales"))
        assert count == listed == 4, f"count={count} listed={listed}"
    finally:
        cache_mod.set_provider(None)


def test_manual_refresh_reports_unreachable_mailboxes(tmp_profile, fake_him):
    """Task 6: Manual refresh must not claim success during an outage."""
    init_db()
    from mailops.dashboard import format_dashboard

    set_message_location(
        "sales", "1", "INBOX.P1-Important", subject="x", sender="a@b.c", priority=1
    )
    cache_mod.set_provider(fake_him)
    try:
        fake_him.unreachable = {"sales"}
        result = cache_mod.refresh_all()
        assert "sales" in result["unreachable"]
        # The dashboard itself must say the numbers are not live.
        body = format_dashboard(cache_mod.get_counts(), group=True)
        assert "unreachable" in body.lower(), body[:300]
        assert "last-known" in body.lower(), body[:300]
    finally:
        cache_mod.set_provider(None)


def test_dashboard_has_no_warning_when_every_host_is_reachable(
    tmp_profile, fake_him
):
    """The warning must not cry wolf on a healthy fleet."""
    init_db()
    from mailops.dashboard import format_dashboard

    fake_him.add_message(
        "sales", "INBOX.P1-Important", "1", subject="x", sender="a@b.c"
    )
    cache_mod.set_provider(fake_him)
    try:
        cache_mod.refresh_all()
        body = format_dashboard(cache_mod.get_counts(), group=True)
        assert "unreachable" not in body.lower(), body[:300]
    finally:
        cache_mod.set_provider(None)


# ---------------------------------------------------- the db helpers themselves
def test_locations_by_priority_filters_correctly(tmp_profile):
    """Classification filters: each priority returns only its own mail."""
    init_db()
    set_message_location("sales", "1", "INBOX.P0-Critical", subject="c", sender="a@b.c", priority=0)
    set_message_location("sales", "2", "INBOX.P1-Important", subject="i", sender="a@b.c", priority=1)
    set_message_location("sales", "3", "INBOX.P2-Normal", subject="n", sender="a@b.c", priority=2)
    set_message_location("sales", "4", "INBOX.P3-Noise", subject="z", sender="a@b.c", priority=3)
    set_message_location("info", "5", "INBOX.P1-Important", subject="other", sender="a@b.c", priority=1)

    assert [r["subject"] for r in locations_by_priority("sales", 0)] == ["c"]
    assert [r["subject"] for r in locations_by_priority("sales", 1)] == ["i"]
    assert [r["subject"] for r in locations_by_priority("sales", 2)] == ["n"]
    assert [r["subject"] for r in locations_by_priority("sales", 3)] == ["z"]
    # Account scoping: info's mail must not leak into sales.
    assert [r["subject"] for r in locations_by_priority("info", 1)] == ["other"]


def test_existing_records_are_never_modified_by_a_failed_pass(
    tmp_profile, fake_him, monkeypatch
):
    """Safety: an outage must not delete or rewrite recorded mail."""
    init_db()
    set_message_location(
        "sales", "398", "INBOX.P1-Important", subject="keep me", sender="a@b.c", priority=1
    )
    before = get_message_locations("sales")
    fake_him.unreachable = {"sales"}
    _sync_with(fake_him, monkeypatch)
    after = get_message_locations("sales")
    assert after == before, "a failed sync altered stored mail records"
