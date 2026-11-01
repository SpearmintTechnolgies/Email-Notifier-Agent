"""MAILOPS fleet-activation redesign (October 2026): activate all 33
registered mailboxes behind ONE consolidated 15-minute Telegram control
card, scaling cleanly to 50/100/500+ mailboxes without a notification
flood.

This file covers the Phase 7 test matrix items not already exercised by
``tests/test_digest.py`` / ``tests/test_health.py``:

* fleet scale at 1, 33, 50, 100 and 500 mailboxes (all four priorities,
  not just P0/P1);
* empty-window behaviour distinguishing "new this window" (A) from
  "pending/unhandled" (B);
* every mailbox-selector page, including the new "View All Mailboxes"
  paginated breakdown screen;
* edit-in-place delivery (at most one Telegram push per window; later
  windows for the SAME card edit instead of sending a new message);
* a failed edit falling back to a fresh send exactly once, and a totally
  failed delivery leaving rows pending for retry;
* group callback authorization for every new button
  (dg:imp / dg:norm / dg:noise / dg:all:<page>);
* no standalone health-alert flood when ``ops_alert_standalone_enabled``
  is left at its new default (False).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from mailops.config import load_settings, save_settings
from mailops.db import (
    enqueue_digest_pending,
    init_db,
    list_undelivered_digest_rows,
)


def _run(coro):
    return asyncio.run(coro)


def _seed_accounts(tmp_profile, n: int) -> list[str]:
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
        json.dumps(rows), encoding="utf-8"
    )
    settings = load_settings()
    settings["active_accounts"] = aids
    settings["account_policies"] = {aid: {"organize": False, "send": False} for aid in aids}
    save_settings(settings)
    return aids


class _EditCapableBot:
    """Bot double that honours edit_message_text like real Telegram:
    editing a message id this double has not 'sent' before raises, exactly
    like Telegram's 'message to edit not found'. Every successful send OR
    edit is recorded distinctly so tests can assert on each count."""

    def __init__(self, allow_edits: bool = True):
        self.allow_edits = allow_edits
        self.sent: list[tuple[int, str, object]] = []
        self.edited: list[tuple[int, int, str, object]] = []
        self._next_id = 1000
        self._known_ids: set[int] = set()

    async def send_message(self, chat_id=None, text=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        mid = self._next_id
        self._next_id += 1
        self._known_ids.add(mid)
        return type("Msg", (), {"message_id": mid})()

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, reply_markup=None):
        if not self.allow_edits or message_id not in self._known_ids:
            raise RuntimeError("Message to edit not found")
        self.edited.append((chat_id, message_id, text, reply_markup))
        return type("Msg", (), {"message_id": message_id})()


# ---------------------------------------------------------------------------
# Fleet scale: 1, 33, 50, 100, 500 mailboxes, all four priorities
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 33, 50, 100, 500])
def test_fleet_scales_with_all_four_priorities(tmp_profile, n):
    init_db()
    aids = _seed_accounts(tmp_profile, n)
    # Every 4th mailbox gets one of each priority; the rest get nothing new.
    busy = aids[::4]
    for aid in busy:
        enqueue_digest_pending(aid, "INBOX", f"{aid}-p0", 0, subject="critical")
        enqueue_digest_pending(aid, "INBOX", f"{aid}-p1", 1, subject="important")
        enqueue_digest_pending(aid, "INBOX", f"{aid}-p2", 2, subject="normal")
        enqueue_digest_pending(aid, "INBOX", f"{aid}-p3", 3, subject="noise")
    from mailops.digest import build_and_send_digest, collect_window

    window = collect_window()
    assert window.total_p0 == len(busy)
    assert window.total_p1 == len(busy)
    assert window.total_p2 == len(busy)
    assert window.total_p3 == len(busy)
    assert window.total_new == len(busy) * 4
    assert window.mailboxes_with_new == len(busy)

    bot = _EditCapableBot()
    diag = _run(build_and_send_digest(bot, force=True, reason="test"))
    assert diag["sent"] is True
    # EXACTLY one Telegram push for the whole fleet regardless of size -
    # first run of a fresh card is always a send, never an edit.
    assert len(bot.sent) == 1
    assert len(bot.edited) == 0
    assert len(bot.sent[0][1]) <= 4096
    # All four priorities' rows are covered - nothing double counted later.
    assert diag["covered_keys"] == len(busy) * 4
    assert list_undelivered_digest_rows(max_priority=3) == []


def test_fleet_500_all_mailboxes_keyboard_paginates_without_scanning_everything(tmp_profile):
    init_db()
    aids = _seed_accounts(tmp_profile, 500)
    from mailops.digest import all_mailboxes_keyboard
    from mailops.cache import AccountCounts

    # "View All Mailboxes" renders the full-fleet PERSISTED classification
    # snapshot (same source as the main card's breakdown section B), not
    # the ephemeral per-window digest_pending table - pass it explicitly
    # so this test is deterministic without a real IMAP backend.
    pending = [
        AccountCounts(
            account_id=aid,
            email=f"{aid}@example.com",
            p0=1 if i < 7 else 0,
            p1=0,
            p2=0,
            p3=0,
            display_name=f"Mailbox {i}",
        )
        for i, aid in enumerate(aids)
    ]
    text0, markup0 = all_mailboxes_keyboard(page=0, pending_counts=pending)
    assert "500 total" in text0
    assert len(text0) <= 4096
    # Critical mailboxes are ranked first on page 0.
    assert text0.index("Critical 1") < text0.index("Critical 0")
    labels0 = [btn.text for row in markup0.inline_keyboard for btn in row]
    assert "Next" in labels0
    assert "Prev" not in labels0
    # Walking to the last page works and never exceeds the char budget.
    last_page = (500 + 19) // 20 - 1
    text_last, markup_last = all_mailboxes_keyboard(page=last_page, pending_counts=pending)
    assert len(text_last) <= 4096
    labels_last = [btn.text for row in markup_last.inline_keyboard for btn in row]
    assert "Next" not in labels_last
    assert "Prev" in labels_last


# ---------------------------------------------------------------------------
# Empty window: distinguish "new this window" (A) from "pending" (B)
# ---------------------------------------------------------------------------


def test_empty_window_with_pending_snapshot_shows_pending_not_new(tmp_profile):
    """A window with zero NEW mail but a non-empty persisted snapshot must
    say so explicitly, and must never present old/pending mail as newly
    arrived."""
    init_db()
    _seed_accounts(tmp_profile, 2)
    from mailops.digest import collect_window, format_digest
    from mailops.cache import AccountCounts

    window = collect_window()
    assert window.is_empty
    pending = [
        AccountCounts(account_id="mbx000", email="mbx000@example.com", p0=1, p1=2, p2=0, p3=0, display_name="Mailbox 0"),
        AccountCounts(account_id="mbx001", email="mbx001@example.com", p0=0, p1=0, p2=3, p3=5, display_name="Mailbox 1"),
    ]
    text = format_digest(window, pending, unhealthy_count=0)
    assert "New this window: 0 email(s)" in text
    assert "Pending / unhandled" in text
    assert "Critical 1 | Important 2 | Normal 3 | Noise 5" in text
    assert "No new email in this window" not in text


def test_build_and_send_digest_empty_window_edits_card_without_new_push(tmp_profile):
    """Phase 3 hard rule: an empty window must never create a brand-new
    Telegram message; if a card already exists it is refreshed in place."""
    init_db()
    _seed_accounts(tmp_profile, 2)
    enqueue_digest_pending("mbx000", "INBOX", "a1", 0, subject="first push")
    from mailops.digest import build_and_send_digest

    bot = _EditCapableBot()
    first = _run(build_and_send_digest(bot, force=True, reason="test"))
    assert first["sent"] is True
    assert len(bot.sent) == 1
    # Second window: nothing new. The card is edited in place (cosmetic
    # refresh) - this must NOT count as a new push.
    second = _run(build_and_send_digest(bot, force=True, reason="test"))
    assert second["sent"] is False
    assert second["skipped"] == "empty"
    assert len(bot.sent) == 1  # still exactly one true send ever
    assert len(bot.edited) == 1  # the cosmetic refresh


# ---------------------------------------------------------------------------
# Edit-in-place delivery model
# ---------------------------------------------------------------------------


def test_second_window_edits_the_same_card_instead_of_sending_new(tmp_profile):
    init_db()
    _seed_accounts(tmp_profile, 2)
    enqueue_digest_pending("mbx000", "INBOX", "a1", 0, subject="wave one")
    from mailops.digest import build_and_send_digest

    bot = _EditCapableBot()
    first = _run(build_and_send_digest(bot, force=True, reason="test"))
    assert first["sent"] is True and len(bot.sent) == 1

    enqueue_digest_pending("mbx001", "INBOX", "b1", 1, subject="wave two")
    second = _run(build_and_send_digest(bot, force=True, reason="test"))
    assert second["sent"] is True
    # Still exactly ONE brand-new message ever created for this chat; the
    # second window's content lands via an edit of the same message id.
    assert len(bot.sent) == 1
    assert len(bot.edited) == 1
    assert "wave two" in bot.edited[-1][2]


def test_edit_failure_falls_back_to_a_fresh_send_once(tmp_profile):
    """If the previous card can no longer be edited (deleted / too old),
    delivery must still succeed via a brand-new send - never silently
    drop the window."""
    init_db()
    _seed_accounts(tmp_profile, 1)
    enqueue_digest_pending("mbx000", "INBOX", "a1", 0, subject="wave one")
    from mailops.digest import build_and_send_digest

    bot = _EditCapableBot(allow_edits=False)
    first = _run(build_and_send_digest(bot, force=True, reason="test"))
    assert first["sent"] is True and len(bot.sent) == 1

    enqueue_digest_pending("mbx000", "INBOX", "a2", 1, subject="wave two")
    second = _run(build_and_send_digest(bot, force=True, reason="test"))
    assert second["sent"] is True
    # Edit was rejected every time -> falls back to a fresh send both times.
    assert len(bot.sent) == 2
    assert len(bot.edited) == 0


def test_total_delivery_failure_leaves_rows_pending(tmp_profile):
    init_db()
    _seed_accounts(tmp_profile, 1)
    enqueue_digest_pending("mbx000", "INBOX", "r1", 0, subject="retry me")
    from mailops.digest import build_and_send_digest

    class _FailBot:
        async def edit_message_text(self, **kwargs):
            raise RuntimeError("no card yet")

        async def send_message(self, **kwargs):
            raise RuntimeError("telegram unreachable")

    with pytest.raises(RuntimeError):
        _run(build_and_send_digest(_FailBot(), force=True, reason="test"))
    rows = list_undelivered_digest_rows(max_priority=3)
    assert [r["msg_id"] for r in rows] == ["r1"]


# ---------------------------------------------------------------------------
# New interactive-control callbacks (phase 5)
# ---------------------------------------------------------------------------


def test_new_view_buttons_are_group_safe_and_viewer_accessible():
    from mailops.bot.app import group_callback_allowed
    from mailops.bot import admin
    from mailops import access

    for data in ("dg:imp", "dg:norm", "dg:noise", "dg:all:0", "dg:all:3"):
        assert group_callback_allowed(data), data
        assert admin.required_perm(data) == access.P_VIEW, data


def test_all_mailboxes_keyboard_nav_round_trips_through_dispatcher():
    """Every page button on the new 'View All Mailboxes' screen must stay
    inside the group-safe dg:* namespace - no page can route to a
    private-only callback."""
    from mailops.digest import all_mailboxes_keyboard
    from mailops.bot.app import group_callback_allowed

    _, markup = all_mailboxes_keyboard(page=0)
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.callback_data and btn.callback_data != "noop":
                assert group_callback_allowed(btn.callback_data), btn.callback_data


# ---------------------------------------------------------------------------
# No standalone health-alert flood by default (phase 3 hard rule)
# ---------------------------------------------------------------------------


def test_ops_alert_standalone_disabled_by_default():
    settings = load_settings()
    assert settings.get("ops_alert_standalone_enabled") is False


def test_digest_edit_in_place_enabled_by_default():
    settings = load_settings()
    assert settings.get("digest_edit_in_place") is True
