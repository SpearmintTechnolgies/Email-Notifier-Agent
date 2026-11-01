"""Regression: dashboard showed all-zero priorities in test mode because
guarded (simulated) moves left messages physically in INBOX while the cache
only counted the (empty) priority folders."""
from __future__ import annotations

import asyncio

from mailops.dashboard import collect_bucket, collect_counts, format_dashboard
from mailops.db import get_message_locations, init_db
from mailops.himalaya_client import RunResult
from mailops.sync_worker import process_account


def _run(coro):
    return asyncio.run(coro)


def _block_moves(fake_him, monkeypatch):
    """Simulate the real client's test-mode guard: move reports success but
    nothing physically moves."""
    monkeypatch.setattr(
        fake_him,
        "move",
        lambda *a, **k: RunResult(0, "test-mode: skipped move", ""),
    )


def test_dashboard_reflects_classified_mail_when_moves_simulated(tmp_profile, fake_him, monkeypatch):
    """Test mode (MAILOPS_TEST_MODE=1 from conftest): moves are no-ops, mail
    stays in INBOX, yet the dashboard must show the classified priorities."""
    init_db()
    _block_moves(fake_him, monkeypatch)
    fake_him.add_message("sales", "INBOX", "1", subject="security alert breach", sender="a@b.co")
    fake_him.add_message("sales", "INBOX", "2", subject="how much to advertise", sender="c@d.co")
    fake_him.add_message("sales", "INBOX", "3", subject="newsletter digest", sender="n@r.co")

    _run(process_account(fake_him, "sales", "sales@example.com", None))

    # Physically nothing moved — Gmail untouched.
    assert len(fake_him.envelopes("sales", "INBOX")) == 3
    assert fake_him.envelopes("sales", "INBOX.P0-Critical") == []

    # Logical locations recorded at classification time.
    locs = get_message_locations("sales")
    assert locs["1"]["mailbox"] == "INBOX.P0-Critical"
    assert locs["2"]["mailbox"] == "INBOX.P1-Important"

    # Dashboard counts follow the recorded locations, not the empty folders.
    # ("newsletter digest" is P3 Noise per the default priority rules.)
    counts = {c.account_id: c for c in collect_counts(force=True)}
    sales = counts["sales"]
    assert (sales.p0, sales.p1, sales.p2, sales.p3) == (1, 1, 0, 1)

    # Bucket pages list the virtual contents with correct refs.
    p0 = collect_bucket(0, "sales", force=True)
    assert [i["msg_id"] for i in p0] == ["1"]
    # The ref names the mailbox where the message REALLY is (INBOX), so
    # opening it reads the real message; the bucket is carried separately.
    assert p0[0]["mailbox"] == "INBOX"
    assert p0[0]["folder"] == "INBOX.P0-Critical"
    assert p0[0]["ref"] == "sales|INBOX|1"
    assert p0[0]["subject"] == "security alert breach"


def test_unclassified_inbox_mail_not_counted(tmp_profile, fake_him, monkeypatch):
    """Mail never processed has no recorded location and must not leak into
    priority counts."""
    init_db()
    _block_moves(fake_him, monkeypatch)
    fake_him.add_message("sales", "INBOX", "9", subject="security alert breach", sender="a@b.co")

    counts = {c.account_id: c for c in collect_counts(force=True)}
    sales = counts["sales"]
    assert (sales.p0, sales.p1, sales.p2, sales.p3) == (0, 0, 0, 0)


def test_recorded_location_wins_over_physical_folder(tmp_profile, fake_him, monkeypatch):
    """If a message was physically moved earlier (e.g. test mode toggled) but
    a rescan re-classified it elsewhere, the recorded location wins so the
    message is counted exactly once, in the latest folder."""
    init_db()
    # Real move first (fake performs it): message lands physically in P0.
    fake_him.add_message("sales", "INBOX", "7", subject="security alert breach", sender="a@b.co")
    _run(process_account(fake_him, "sales", "sales@example.com", None))
    assert len(fake_him.envelopes("sales", "INBOX.P0-Critical")) == 1

    # Re-classified into P1 while the physical copy still sits in P0.
    from mailops.db import set_message_location

    set_message_location("sales", "7", "INBOX.P1-Important", subject="security alert breach", priority=1)

    counts = {c.account_id: c for c in collect_counts(force=True)}
    sales = counts["sales"]
    assert sales.p0 == 0
    assert sales.p1 == 1  # counted once, at the recorded location


def test_production_counts_use_physical_folders(tmp_profile, fake_him, monkeypatch):
    """Outside test mode the cache must read the real folders exactly as
    before — recorded locations are ignored."""
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    init_db()
    fake_him.add_message("sales", "INBOX", "1", subject="security alert breach", sender="a@b.co")
    fake_him.add_message("sales", "INBOX", "3", subject="newsletter digest", sender="n@r.co")
    _run(process_account(fake_him, "sales", "sales@example.com", None))

    # Fake really moved the mail; physical folders hold it.
    assert len(fake_him.envelopes("sales", "INBOX.P0-Critical")) == 1
    assert len(fake_him.envelopes("sales", "INBOX.P3-Noise")) == 1

    counts = {c.account_id: c for c in collect_counts(force=True)}
    sales = counts["sales"]
    assert (sales.p0, sales.p1, sales.p2, sales.p3) == (1, 0, 0, 1)

    bucket = collect_bucket(0, "sales", force=True)
    assert [i["msg_id"] for i in bucket] == ["1"]


def test_dashboard_text_reports_missing_purposes_but_counts_still_work(tmp_profile, fake_him, monkeypatch):
    """'Inbox purposes missing' is an onboarding hint for LLM ranking quality
    only — rule-based classification and counts work without purposes."""
    init_db()
    _block_moves(fake_him, monkeypatch)
    fake_him.add_message("sales", "INBOX", "1", subject="security alert breach", sender="a@b.co")
    _run(process_account(fake_him, "sales", "sales@example.com", None))

    text = format_dashboard(collect_counts(force=True))
    assert "Critical 1" in text
    assert "Inbox purposes missing: sales, info" in text
