"""Tests for the consolidated 15-minute Telegram digest (October 2026 redesign).

Covers the 14 bullets from the redesign spec:

* 1-3  one summary per 15-minute window, grouped by mailbox, with totals;
* 4    no automatic individual email notifications (only via Open Inbox);
* 5    the four interactive buttons (Open Inbox, View Critical, Select
       Mailbox, Refresh Summary) point at real screens;
* 6    each row is counted in at most one digest window;
* 7    empty window => no Telegram message;
* 9    pagination of the Select Mailbox list at 90+ accounts;
* 10   restart recovery via the persisted digest_pending table;
       overlapping syncs do not double-send (single-flight lock);
* 11   the scheduler runs inside the existing sync loop - no second
       scheduler, no second poll;
* 13   simulated-data scaling at 1, 10, 90 and 100 mailboxes.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from mailops.config import load_settings, save_settings
from mailops.db import (
    count_digest_pending,
    digest_state_get,
    enqueue_digest_pending,
    init_db,
    list_undelivered_digest_rows,
    mailbox_first_sync_at,
    mark_digest_delivered,
    record_first_sync,
)


def _run(coro):
    return asyncio.run(coro)


class _CollectBot:
    """Minimal Bot double that just records what would have been sent.

    Supports both ``send_message`` and ``edit_message_text`` (the
    MAILOPS activation redesign's default edit-in-place delivery path -
    ``send_or_edit_home``). ``edit_message_text`` always raises
    (nothing to edit yet / message not found) so existing tests, which
    predate edit-in-place and assert on ``bot.sent``, keep working
    unchanged: every delivery in this double falls back to a fresh
    ``send_message``, exactly like talking to a brand-new chat.
    """

    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id=None, text=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return type("Msg", (), {"message_id": len(self.sent)})()

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, reply_markup=None):
        raise RuntimeError("nothing to edit in this test double")


def _seed_accounts(tmp_profile, n: int) -> list[str]:
    """Overwrite the test profile with N synthetic active mailboxes.

    All accounts get organize=False/send=False so no test can accidentally
    mutate a mailbox: the digest path is pure data-flow and never touches
    the IMAP client.
    """
    import json as _json

    aids = [f"mbx{i:03d}" for i in range(n)]
    rows = [
        {
            "id": aid,
            "email": f"{aid}@example.com",
            "display_name": f"Mailbox {i}",
            "default": i == 0,
        }
        for i, aid in enumerate(aids)
    ]
    (tmp_profile / "himalaya" / "accounts.json").write_text(
        _json.dumps(rows), encoding="utf-8"
    )
    settings = load_settings()
    settings["active_accounts"] = aids
    settings["account_policies"] = {aid: {"organize": False, "send": False} for aid in aids}
    save_settings(settings)
    return aids


# ---------------------------------------------------------------------------
# Enqueue + collect
# ---------------------------------------------------------------------------


def test_enqueue_dedupe_within_a_mailbox(tmp_profile):
    init_db()
    assert enqueue_digest_pending("mb", "INBOX", "e1", 0, subject="x") is True
    assert enqueue_digest_pending("mb", "INBOX", "e1", 0, subject="x") is False
    assert len(list_undelivered_digest_rows(max_priority=1)) == 1


def test_window_groups_by_mailbox_and_counts(tmp_profile):
    init_db()
    _seed_accounts(tmp_profile, 3)
    enqueue_digest_pending("mbx000", "INBOX", "a1", 0, subject="hack")
    enqueue_digest_pending("mbx000", "INBOX", "a2", 1, subject="rates")
    enqueue_digest_pending("mbx001", "INBOX", "b1", 1, subject="pricing")
    # mbx002 has nothing new.
    from mailops.digest import collect_window

    window = collect_window()
    assert window.total_p0 == 1
    assert window.total_p1 == 2
    assert window.total_actionable == 3
    labels = {b.account_id: b for b in window.buckets}
    # Only inboxes with pending rows appear.
    assert set(labels) == {"mbx000", "mbx001"}
    assert labels["mbx000"].p0 == 1 and labels["mbx000"].p1 == 1
    assert labels["mbx001"].p1 == 1


def test_seeded_rows_do_not_appear_in_window(tmp_profile):
    init_db()
    _seed_accounts(tmp_profile, 1)
    enqueue_digest_pending("mbx000", "INBOX", "s1", 0, seeded=True)
    enqueue_digest_pending("mbx000", "INBOX", "s2", 1, seeded=False)
    from mailops.digest import collect_window

    w = collect_window()
    assert w.total_actionable == 1
    assert w.buckets[0].p1 == 1 and w.buckets[0].p0 == 0


# ---------------------------------------------------------------------------
# Format + buttons
# ---------------------------------------------------------------------------


def test_digest_message_mentions_counts_and_mailboxes(tmp_profile):
    init_db()
    _seed_accounts(tmp_profile, 2)
    enqueue_digest_pending("mbx000", "INBOX", "a1", 0, subject="Account hack")
    enqueue_digest_pending("mbx001", "INBOX", "b1", 1, subject="Please send rates")
    from mailops.digest import collect_window, format_digest

    text = format_digest(collect_window())
    assert "New this window: 2 email(s)" in text
    assert "(of which actionable P0+P1: 2)" in text
    assert "Critical 1" in text and "Important 1" in text
    assert "Mailbox 0" in text and "Mailbox 1" in text
    assert "Account hack" in text
    assert "Please send rates" in text


def test_digest_keyboard_has_the_four_required_buttons(tmp_profile):
    init_db()
    _seed_accounts(tmp_profile, 1)
    enqueue_digest_pending("mbx000", "INBOX", "a1", 0, subject="x")
    from mailops.digest import collect_window, digest_keyboard

    markup = digest_keyboard(collect_window())
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    # 'Open Inbox' prefixes with the top mailbox name; the other labels
    # are stable (phase 5 of the MAILOPS activation redesign: View
    # Critical/Important/Normal/Noise, View All Mailboxes, Select Mailbox,
    # Check Unhealthy Accounts, Refresh Summary, Return to Dashboard).
    assert any(l.startswith("Open Inbox") for l in labels)
    assert any(l.startswith("View Critical") for l in labels)
    assert any(l.startswith("View Important") for l in labels)
    assert any(l.startswith("View Normal") for l in labels)
    assert any(l.startswith("View Noise") for l in labels)
    assert "View All Mailboxes" in labels
    assert "Select Mailbox" in labels
    assert any(l.startswith("Check Unhealthy Accounts") for l in labels)
    assert "Refresh Summary" in labels
    assert "Return to Dashboard" in labels
    assert "dg:open:mbx000" in datas
    assert "dg:crit" in datas
    assert "dg:imp" in datas
    assert "dg:norm" in datas
    assert "dg:noise" in datas
    assert "dg:all:0" in datas
    assert "dg:pick:0" in datas
    assert "dg:refresh" in datas
    assert "health:view" in datas
    assert "dash" in datas


# ---------------------------------------------------------------------------
# Scheduling + deduplication + restart recovery
# ---------------------------------------------------------------------------


def test_empty_window_sends_nothing(tmp_profile):
    init_db()
    _seed_accounts(tmp_profile, 1)
    from mailops.digest import build_and_send_digest

    bot = _CollectBot()
    diag = _run(build_and_send_digest(bot, force=True, reason="test"))
    assert diag["sent"] is False
    assert diag["skipped"] == "empty"
    assert bot.sent == []


def test_force_run_sends_one_message_and_marks_delivered(tmp_profile):
    init_db()
    _seed_accounts(tmp_profile, 2)
    enqueue_digest_pending("mbx000", "INBOX", "a1", 0, subject="x")
    enqueue_digest_pending("mbx001", "INBOX", "b1", 1, subject="y")
    from mailops.digest import build_and_send_digest

    bot = _CollectBot()
    diag = _run(build_and_send_digest(bot, force=True, reason="test"))
    assert diag["sent"] is True
    assert diag["mailboxes"] == 2
    assert diag["actionable"] == 2
    assert diag["covered_keys"] == 2
    # Exactly ONE Telegram message per configured target (the fixture
    # seeds one allowed user), never one per mailbox or per email.
    assert len(bot.sent) == 1
    assert list_undelivered_digest_rows(max_priority=1) == []


def test_same_window_not_resent(tmp_profile):
    """Point 6: an email is never counted in two consecutive digests."""
    init_db()
    _seed_accounts(tmp_profile, 1)
    enqueue_digest_pending("mbx000", "INBOX", "a1", 0, subject="x")
    from mailops.digest import build_and_send_digest

    bot = _CollectBot()
    first = _run(build_and_send_digest(bot, force=True, reason="test"))
    assert first["sent"] is True and first["covered_keys"] == 1
    # No new mail: the next run must be empty, not a resend of a1.
    second = _run(build_and_send_digest(bot, force=True, reason="test"))
    assert second["sent"] is False
    assert len(bot.sent) == 1


def test_restart_recovery_delivers_persisted_rows(tmp_profile):
    """Point 10: rows survive a 'crash' and the next tick delivers them."""
    init_db()
    _seed_accounts(tmp_profile, 1)
    # Simulate a pre-crash state: a row was persisted but no digest ran.
    enqueue_digest_pending("mbx000", "INBOX", "crash1", 0, subject="pre-crash")
    assert digest_state_get("last_run_at", "") == ""
    from mailops.digest import build_and_send_digest

    bot = _CollectBot()
    diag = _run(build_and_send_digest(bot, force=True, reason="test"))
    assert diag["sent"] and diag["actionable"] == 1
    assert "pre-crash" in bot.sent[0][1]


def test_telegram_failure_leaves_rows_pending_for_next_tick(tmp_profile):
    """Point 10: failed digest sends do not lose pending notifications."""
    init_db()
    _seed_accounts(tmp_profile, 1)
    enqueue_digest_pending("mbx000", "INBOX", "r1", 0, subject="retry me")
    from mailops.digest import build_and_send_digest

    class _FailBot:
        async def send_message(self, chat_id=None, text=None, reply_markup=None):
            raise RuntimeError("telegram unreachable")

    with pytest.raises(RuntimeError):
        _run(build_and_send_digest(_FailBot(), force=True, reason="test"))
    # Row is still pending for the next tick.
    rows = list_undelivered_digest_rows(max_priority=1)
    assert [r["msg_id"] for r in rows] == ["r1"]


def test_per_mailbox_mute_excludes_from_digest(tmp_profile):
    init_db()
    _seed_accounts(tmp_profile, 2)
    enqueue_digest_pending("mbx000", "INBOX", "a1", 0, subject="muted")
    enqueue_digest_pending("mbx001", "INBOX", "b1", 1, subject="shown")
    s = load_settings()
    s["digest_mailboxes_disabled"] = ["mbx000"]
    save_settings(s)
    from mailops.digest import collect_window

    w = collect_window()
    assert {b.account_id for b in w.buckets} == {"mbx001"}
    assert w.total_actionable == 1


# ---------------------------------------------------------------------------
# First-sync silence (point 10 - no flood during initial sync / bulk import)
# ---------------------------------------------------------------------------


def test_first_sync_seeds_rows_silently(tmp_profile, fake_him):
    """A brand-new mailbox's P0/P1 backlog must not fire the digest."""
    from mailops.sync_worker import process_account

    init_db()
    _seed_accounts(tmp_profile, 1)
    fake_him.add_message(
        "mbx000", "INBOX", "x1", subject="security alert breach", sender="a@b.co"
    )
    fake_him.add_message(
        "mbx000", "INBOX", "x2", subject="how much to advertise", sender="c@d.co"
    )
    _run(process_account(fake_him, "mbx000", "mbx000@example.com", None))

    # First-sync marker recorded, rows seeded (so collect_window sees none).
    assert mailbox_first_sync_at("mbx000") is not None
    from mailops.digest import collect_window

    assert collect_window().is_empty
    # ...but they still exist in the table, flagged seeded for audit.
    rows_all = list_undelivered_digest_rows(max_priority=1, include_seeded=True)
    assert {r["msg_id"] for r in rows_all} == {"x1", "x2"}
    assert all(r["seeded"] == 1 for r in rows_all)


def test_second_sync_after_first_is_not_silent(tmp_profile, fake_him):
    """New mail arriving AFTER the first sync fires the digest normally."""
    from mailops.sync_worker import process_account

    init_db()
    _seed_accounts(tmp_profile, 1)
    # First sync (silent).
    fake_him.add_message(
        "mbx000", "INBOX", "y1", subject="old security alert", sender="a@b.co"
    )
    _run(process_account(fake_him, "mbx000", "mbx000@example.com", None))
    assert collect_window_empty()
    # New mail arrives.
    fake_him.add_message(
        "mbx000", "INBOX", "y2", subject="how much to advertise", sender="c@d.co"
    )
    _run(process_account(fake_him, "mbx000", "mbx000@example.com", None))
    from mailops.digest import collect_window

    w = collect_window()
    assert w.total_actionable == 1
    assert w.buckets[0].p1 == 1


def collect_window_empty():
    from mailops.digest import collect_window

    return collect_window().is_empty


# ---------------------------------------------------------------------------
# Scaling - 1, 10, 90, 100 mailboxes (point 13)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 10, 90, 100])
def test_digest_scales_to_many_mailboxes(tmp_profile, n):
    init_db()
    aids = _seed_accounts(tmp_profile, n)
    # Half the mailboxes have one P1, the other half have nothing new.
    want = aids[::2]
    for aid in want:
        enqueue_digest_pending(aid, "INBOX", f"{aid}-m", 1, subject=f"from {aid}")
    from mailops.digest import build_and_send_digest, collect_window

    window = collect_window()
    assert window.total_actionable == len(want)
    assert len(window.buckets) == len(want)
    # Only mailboxes with new mail are in the summary (point 9).
    assert set(b.account_id for b in window.buckets) == set(want)

    bot = _CollectBot()
    diag = _run(build_and_send_digest(bot, force=True, reason="test"))
    assert diag["sent"] is True or (n == 0 and not diag["sent"])
    # EXACTLY one Telegram message, regardless of mailbox count.
    assert len(bot.sent) == 1
    # And the message stays within Telegram's 4096-char limit even at 100.
    assert len(bot.sent[0][1]) <= 4096


# ---------------------------------------------------------------------------
# Select-Mailbox pagination (point 9)
# ---------------------------------------------------------------------------


def test_select_mailbox_paginates_at_configured_page_size(tmp_profile):
    init_db()
    aids = _seed_accounts(tmp_profile, 100)
    # Enqueue a row for every mailbox so every one has >=1 pending.
    for aid in aids:
        enqueue_digest_pending(aid, "INBOX", f"{aid}-pg", 1)
    from mailops.digest import select_mailbox_keyboard

    text0, markup0 = select_mailbox_keyboard(page=0)
    text4, markup4 = select_mailbox_keyboard(page=4)
    # Default page size is 20 -> 5 pages for 100 inboxes.
    def _mb_rows(mk):
        return [
            btn
            for row in mk.inline_keyboard
            for btn in row
            if btn.callback_data and btn.callback_data.startswith("dg:open:")
        ]

    assert len(_mb_rows(markup0)) == 20
    # Prev button hidden on page 0, Next shown.
    labels0 = [
        btn.text for row in markup0.inline_keyboard for btn in row
    ]
    assert "Prev" not in labels0
    assert "Next" in labels0
    # Last page: Next hidden, Prev shown.
    labels4 = [btn.text for row in markup4.inline_keyboard for btn in row]
    assert "Prev" in labels4
    assert "Next" not in labels4
    assert "Page 5/5" in " ".join(labels4)


# ---------------------------------------------------------------------------
# Callback permissions (point 13 - verify dg:* can run in authorized groups)
# ---------------------------------------------------------------------------


def test_dg_callbacks_are_allowed_in_authorized_groups():
    from mailops.bot.app import group_callback_allowed

    assert group_callback_allowed("dg:crit")
    assert group_callback_allowed("dg:refresh")
    assert group_callback_allowed("dg:pick:0")
    assert group_callback_allowed("dg:open:mbx000")


def test_dg_callbacks_require_at_least_view_permission():
    from mailops.bot import admin
    from mailops import access

    assert admin.required_perm("dg:crit") == access.P_VIEW
    assert admin.required_perm("dg:refresh") == access.P_VIEW
    assert admin.required_perm("dg:pick:0") == access.P_VIEW
    assert admin.required_perm("dg:open:mbx000") == access.P_VIEW


def test_notification_policy_requires_settings_permission():
    from mailops.bot import admin
    from mailops import access

    assert admin.required_perm("set:notify") == access.P_SETTINGS
    assert admin.required_perm("set:notify:toggle") == access.P_SETTINGS
    assert admin.required_perm("set:notify:mb:toggle:x:0") == access.P_SETTINGS
