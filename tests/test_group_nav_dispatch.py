"""Dispatcher-level regression tests for the group control panel.

These complement the existing screen-level tests (which call keyboard
builders directly) by routing every group-visible callback through
``mailops.bot.app.on_callback`` with mock Telegram update objects. This
is the layer that broke in October 2026 when the dispatcher's
``GROUP_CALLBACK_PREFIXES`` fell out of sync with the digest keyboard -
callers rendered working buttons, screen tests passed, but the live bot
answered every tap with "Private chat only - open the bot directly for
this." These tests tap the real dispatcher so that class of bug cannot
recur silently.

Spec coverage
-------------
* Phase 1: digest's four buttons (Open Inbox, View Critical, Select
           Mailbox, Refresh Summary) and the P0-P3 priority browser.
* Phase 2: every ``mb:*`` entry visible in a group reaches a group-safe
           screen; every private-only ``mb:*`` is denied.
* Phase 3: group-first navigation does not drop ordinary admins into a
           private handoff for non-sensitive flows.
* Phase 5: pagination of Select Mailbox at 1, 10, 90 and 100 mailboxes,
           both production owners, and stale/stranger callbacks.

Nothing here touches a real mailbox, writes to settings.json or sends a
real Telegram message - the mailbox client is replaced by
``FakeHimalaya`` and the FakeQuery/FakeMessage doubles from
``test_telegram_group`` collect what the dispatcher would have sent.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import mailops.bot.app as app_mod
from mailops import access
from mailops.config import load_settings, save_settings
from mailops.db import enqueue_digest_pending, init_db

from tests.test_telegram_group import (
    GROUP,
    STRANGER,
    USER_A,
    USER_B,
    callback_update,
    set_env,
)


def _run(coro):
    return asyncio.run(coro)


def _ctx():
    return SimpleNamespace(user_data={}, bot=None)


def _seed(tmp_profile, n: int) -> list[str]:
    """Register N synthetic mailboxes in the test profile.

    Mirrors the helper in ``test_digest`` but kept local so this test
    file is self-contained and can be run on its own.
    """
    init_db()
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
    s = load_settings()
    s["active_accounts"] = aids
    s["account_policies"] = {aid: {"organize": False, "send": False} for aid in aids}
    save_settings(s)
    set_env(tmp_profile, allowed=f"{USER_A},{USER_B}", home=f"{USER_A},{GROUP}")
    access.ensure()
    return aids


def _tap(data: str, user_id: int = USER_A, chat_id: int = GROUP, kind: str = "group"):
    """Fire one callback through the live dispatcher and return the FakeQuery."""
    upd, q = callback_update(data, user_id, chat_id, kind)
    _run(app_mod.on_callback(upd, _ctx()))
    return q


def _assert_not_group_denied(q, label: str) -> None:
    """Fail if the dispatcher answered this group tap with the generic
    'Private chat only' alert (= group routing is broken for this
    callback). Admin-specific alerts and the ``_group_safe_body`` ones
    are checked separately by the callers that expect them."""
    bad = [t for t, show in q.answers if show and t == app_mod.GROUP_DENIED_ALERT]
    assert not bad, (
        f"group tap {label!r} was answered with GROUP_DENIED_ALERT - "
        f"check GROUP_CALLBACK_PREFIXES / GROUP_MB_* allow-lists in "
        f"mailops/bot/app.py. answers={q.answers!r}"
    )


# ---------------------------------------------------------------------------
# 1. The four digest buttons work in a group chat.
#    (This is the specific bug the user reported for Select Mailbox.)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "data,label",
    [
        ("dg:pick:0",       "Select Mailbox"),
        ("dg:pick:1",       "Select Mailbox page 2"),
        ("dg:crit",         "View Critical"),
        ("dg:refresh",      "Refresh Summary"),
        ("dg:open:mbx000",  "Open Inbox"),
        ("dg:open:_top",    "Open Inbox (no top mailbox)"),
    ],
)
def test_digest_buttons_work_in_group_for_owner(tmp_profile, fake_him, data, label):
    _seed(tmp_profile, 10)
    # Give the digest something to find so Refresh Summary has data.
    for i in range(2):
        enqueue_digest_pending(f"mbx00{i}", "INBOX", f"seed-{i}-0", 0,
                               subject="x", sender="y")

    q = _tap(data)
    _assert_not_group_denied(q, label)
    assert q.edits or q.answers, (
        f"{label} ({data}) produced neither edit nor answer: {q}"
    )


# ---------------------------------------------------------------------------
# 1b. The MAILOPS activation redesign's new buttons (View
#     Important/Normal/Noise, View All Mailboxes) route through the live
#     dispatcher in a group exactly like 'View Critical' already does.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "data,label",
    [
        ("dg:imp", "View Important"),
        ("dg:norm", "View Normal"),
        ("dg:noise", "View Noise"),
        ("dg:all:0", "View All Mailboxes"),
        ("dg:all:1", "View All Mailboxes page 2"),
    ],
)
def test_new_fleet_buttons_work_in_group_for_owner(tmp_profile, fake_him, data, label):
    _seed(tmp_profile, 25)
    for i in range(4):
        enqueue_digest_pending(f"mbx{i:03d}", "INBOX", f"seed-{i}-1", i % 4,
                               subject="x", sender="y")
    q = _tap(data)
    _assert_not_group_denied(q, label)
    assert q.edits or q.answers, (
        f"{label} ({data}) produced neither edit nor answer: {q}"
    )


@pytest.mark.parametrize("data", ["dg:imp", "dg:norm", "dg:noise", "dg:all:0"])
def test_new_fleet_buttons_also_work_in_private(tmp_profile, fake_him, data):
    _seed(tmp_profile, 5)
    q = _tap(data, chat_id=USER_A, kind="private")
    denied = [t for t, show in q.answers if show and t]
    assert not any(t == app_mod.GROUP_DENIED_ALERT for t in denied), denied
    assert q.edits or q.answers, q


# ---------------------------------------------------------------------------
# 2. Both configured owners (points A + G of Phase 2) can navigate.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("user_id", [USER_A, USER_B])
@pytest.mark.parametrize(
    "data",
    ["dash", "nav:home", "dg:pick:0", "dg:crit", "dg:refresh",
     "b:p0", "b:p1", "b:p2", "b:p3", "g:all", "mb:m", "mb:switch"],
)
def test_group_nav_works_for_both_owners(tmp_profile, fake_him, user_id, data):
    _seed(tmp_profile, 5)
    q = _tap(data, user_id=user_id)
    _assert_not_group_denied(q, data)


# ---------------------------------------------------------------------------
# 3. Unauthorized strangers see a private 'Unauthorized' and never reach a
#    screen (point C: whitelisting the group does not authorize members).
# ---------------------------------------------------------------------------
def test_stranger_in_group_cannot_navigate(tmp_profile, fake_him):
    _seed(tmp_profile, 3)
    for data in ("dg:pick:0", "dg:crit", "dg:refresh", "dash", "mb:m"):
        q = _tap(data, user_id=STRANGER)
        assert q.edits == [], f"stranger reached a screen via {data}: {q.edits}"
        # The dispatcher is allowed to post a 'Unauthorized' alert.
        texts = [t for t, _ in q.answers if t]
        if texts:
            joined = " ".join(texts).lower()
            assert "unauthorized" in joined or "admin" in joined, texts


# ---------------------------------------------------------------------------
# 4. The group's Mailboxes entry reaches the group-safe screen (no credentials
#    exposed, no per-mailbox detail buttons, Bulk/Quick Add available).
# ---------------------------------------------------------------------------
def test_mailboxes_main_screen_in_group_is_group_safe(tmp_profile, fake_him):
    _seed(tmp_profile, 3)
    q = _tap("mb:m")
    assert q.edits, "Mailboxes main screen did not render"
    _text, markup = q.edits[-1]
    cb = {b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data}
    # Group-safe entries we DO expect:
    assert "mb:add" in cb
    assert "mb:qadd" in cb
    assert "mb:bulk" in cb
    assert "mb:switch" in cb
    # Back to dashboard must point to a group-safe destination.
    assert "nav:home" in cb
    # Private-only entries we must NOT render in a group:
    assert "mb:errors" not in cb        # audit text can leak hostnames
    assert "ac:m" not in cb             # access-management is DM only
    assert not any(c.startswith("mb:view:") for c in cb)
    assert not any(c.startswith("mb:health:") for c in cb)


# ---------------------------------------------------------------------------
# 5. The private-only mb:* callbacks are denied in a group even when the
#    tapper is an owner (defence-in-depth check for the mailboxes allow-list).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "data",
    ["mb:view:mbx000", "mb:health:mbx000", "mb:enable:mbx000",
     "mb:disable:mbx000", "mb:purpose:mbx000", "mb:audit:mbx000",
     "mb:dm_prompt:xx"],
)
def test_private_only_mb_callbacks_denied_in_group(tmp_profile, fake_him, data):
    _seed(tmp_profile, 3)
    q = _tap(data)
    # Either the general group-denied alert OR the mailbox-specific
    # "that screen is private only" alert is acceptable - both block the
    # action and surface a clear message.
    denied = [t for t, show in q.answers if show and t]
    assert denied, f"{data} silently did nothing in a group"
    joined = " ".join(denied).lower()
    assert "private" in joined or "chat" in joined, denied
    assert q.edits == [], f"{data} unexpectedly rendered a screen: {q.edits}"


# ---------------------------------------------------------------------------
# 6. Select Mailbox pagination works at the sizes the production fleet hits:
#    1, 10, 90, 100 mailboxes. The user requested 90-100+ scaling.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("n", [1, 10, 90, 100])
def test_select_mailbox_pagination_scales(tmp_profile, fake_him, n):
    _seed(tmp_profile, n)
    # Seed digest counts for the first 5 mailboxes so some show "(n new)".
    for i in range(min(5, n)):
        enqueue_digest_pending(f"mbx{i:03d}", "INBOX", f"pg-{i}", 1)
    q = _tap("dg:pick:0")
    _assert_not_group_denied(q, f"dg:pick:0 n={n}")
    assert q.edits, "no screen rendered"
    text, markup = q.edits[-1]
    datas = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    # Every mailbox entry is callable (dg:open:<aid>) and leads to a real
    # handler. Pagination caps the number per page at the configured size.
    opens = [d for d in datas if d.startswith("dg:open:")]
    page_size = int(load_settings().get("digest_mailbox_page_size", 20))
    assert 0 < len(opens) <= page_size, (len(opens), page_size, n)
    # Spot-check: tapping the first mailbox entry opens its priority view.
    if opens:
        q2 = _tap(opens[0])
        _assert_not_group_denied(q2, opens[0])
        assert q2.edits, f"tapping {opens[0]} produced no screen"

    # Walk every page by chaining "Next" taps and prove no page denies.
    pages_seen = 1
    cur = q
    while True:
        nexts = [
            b.callback_data
            for row in cur.edits[-1][1].inline_keyboard
            for b in row
            if b.text == "Next" and b.callback_data
        ]
        if not nexts:
            break
        cur = _tap(nexts[0])
        _assert_not_group_denied(cur, nexts[0])
        pages_seen += 1
        if pages_seen > 50:
            pytest.fail("select-mailbox pagination seems to loop forever")
    expected_pages = max(1, (n + page_size - 1) // page_size)
    assert pages_seen == expected_pages, (pages_seen, expected_pages, n)


# ---------------------------------------------------------------------------
# 7. Select Mailbox lists EVERY registered mailbox (not just those with
#    pending counts). Point 4 of the user's spec.
# ---------------------------------------------------------------------------
def test_select_mailbox_lists_every_registered_mailbox(tmp_profile, fake_him):
    _seed(tmp_profile, 25)
    # Pending counts for ONLY three mailboxes.
    for i in (0, 1, 2):
        enqueue_digest_pending(f"mbx{i:03d}", "INBOX", f"p-{i}", 1)
    q = _tap("dg:pick:0")
    _, markup = q.edits[-1]
    labels = [btn.text for row in markup.inline_keyboard for btn in row
              if btn.callback_data and btn.callback_data.startswith("dg:open:")]
    # Page 1 should contain 20 mailboxes (the default page size).
    assert len(labels) == 20
    # Three must show "(n new)", seventeen must show "(no new)".
    assert sum(1 for l in labels if "new)" in l and "no new" not in l) == 3
    assert sum(1 for l in labels if "(no new)" in l) == 17


# ---------------------------------------------------------------------------
# 8. The digest's "Select Mailbox -> open mailbox" chain works even when
#    the chosen inbox has NO pending digest rows (now that we list all).
# ---------------------------------------------------------------------------
def test_dg_open_handles_mailbox_with_empty_window(tmp_profile, fake_him):
    _seed(tmp_profile, 5)
    # No digest rows for any mailbox: window is empty.
    q = _tap("dg:open:mbx003")
    _assert_not_group_denied(q, "dg:open:mbx003 empty")
    assert q.edits, "did not navigate when window is empty"


# ---------------------------------------------------------------------------
# 9. The dashboard dg:pick:0 overflow entry appears when there are >12
#    mailboxes and routes correctly in a group.
# ---------------------------------------------------------------------------
def test_dashboard_overflow_button_routes_to_select_mailbox(tmp_profile, fake_him):
    _seed(tmp_profile, 90)
    # Warm the counts first. The dashboard renders from cache and never runs
    # a full-fleet IMAP pass inline (that inline rebuild is what made
    # /dashboard hang for minutes on a 39-mailbox fleet), so the 90-mailbox
    # keyboard this test is about only exists once a pass has completed.
    from mailops.cache import refresh_all

    refresh_all()
    q = _tap("dash")
    _assert_not_group_denied(q, "dash")
    _text, markup = q.edits[-1]
    cb = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    # Over-12 fleet: the overflow row points at Select Mailbox.
    assert "dg:pick:0" in cb, cb
    # The dashboard does not render 90 per-account rows (Telegram cap).
    per_account = [c for c in cb if c.startswith("ab:")]
    assert len(per_account) <= 2 * 12, (
        "dashboard rendered too many per-account rows: it must cap and "
        "defer the rest to the Select Mailbox screen"
    )


# ---------------------------------------------------------------------------
# 10. Pagination 'Prev' / 'Next' round-trip never gets stuck: Back to
#     dashboard from any Select Mailbox page reaches the group dashboard.
# ---------------------------------------------------------------------------
def test_select_mailbox_back_to_dashboard_round_trip(tmp_profile, fake_him):
    _seed(tmp_profile, 50)
    q = _tap("dg:pick:1")  # jump to page 2
    _, markup = q.edits[-1]
    datas = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    assert "dash" in datas, datas
    q2 = _tap("dash")
    _assert_not_group_denied(q2, "dash from Select Mailbox")
    text2, _ = q2.edits[-1]
    assert "dashboard" in text2.lower() or "critical" in text2.lower()


# ---------------------------------------------------------------------------
# 11. Private-chat sanity: all four digest buttons also work in a DM, so an
#     admin opening the bot privately after a group handoff lands correctly.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "data", ["dg:pick:0", "dg:crit", "dg:refresh", "dg:open:mbx000"]
)
def test_digest_buttons_also_work_in_private(tmp_profile, fake_him, data):
    _seed(tmp_profile, 3)
    q = _tap(data, chat_id=USER_A, kind="private")
    denied = [t for t, show in q.answers if show and t]
    assert not any(
        t == app_mod.GROUP_DENIED_ALERT for t in denied
    ), f"private tap denied as if group: {denied}"
    # Something was shown.
    assert q.edits or q.answers, q
