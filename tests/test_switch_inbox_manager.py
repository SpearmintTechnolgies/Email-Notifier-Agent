"""Interactive Switch Inbox / Account Status manager tests.

These exercise the paginated, selectable mailbox grid that replaced the
former plain-text status report (October 2026). The tests go through
``mailops.bot.app.on_callback`` so pagination, per-mailbox drill-down,
health check caching and the DM-handoff for sensitive actions are all
verified against the LIVE dispatcher - the same code path a real
Telegram tap takes.

Nothing here touches a real mailbox: the Himalaya client is replaced by
``FakeHimalaya`` from conftest and the Telegram API is replaced by the
FakeQuery / FakeMessage doubles from test_telegram_group.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import mailops.bot.app as app_mod
import mailops.bot.mailboxes as mailboxes_mod
from mailops import access
from mailops.config import load_settings, save_settings
from mailops.db import init_db

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
    # Clear in-memory state so tests do not leak into each other.
    mailboxes_mod._MANAGER_STATE.clear()
    mailboxes_mod._HEALTH_CACHE.clear()
    return aids


def _tap(data: str, user_id: int = USER_A, chat_id: int = GROUP, kind: str = "group"):
    upd, q = callback_update(data, user_id, chat_id, kind)
    _run(app_mod.on_callback(upd, _ctx()))
    return q


def _buttons(markup):
    out = []
    for row in markup.inline_keyboard:
        for b in row:
            out.append((b.text, b.callback_data))
    return out


# ---------------------------------------------------------------------------
# Baseline: Switch Inbox renders an INTERACTIVE grid (not just text) in
# both private and group. Each mailbox is a selectable button.
# ---------------------------------------------------------------------------
def test_switch_inbox_in_group_renders_selectable_buttons(tmp_profile, fake_him):
    _seed(tmp_profile, 3)
    q = _tap("mb:switch")
    assert q.edits, "mb:switch did not render in group"
    text, markup = q.edits[-1]
    assert "Switch Inbox / Account Status" in text
    # Every seeded mailbox is a tappable button.
    datas = {d for _t, d in _buttons(markup)}
    assert "mb:mbx:mbx000" in datas
    assert "mb:mbx:mbx001" in datas
    assert "mb:mbx:mbx002" in datas
    # Back to mailboxes is reachable.
    assert "mb:m" in datas
    # Nav row shows a page indicator (noop so Telegram ack is instant).
    assert "noop" in datas


def test_switch_inbox_in_private_renders_selectable_buttons(tmp_profile, fake_him):
    _seed(tmp_profile, 2)
    q = _tap("mb:switch", chat_id=USER_A, kind="private")
    text, markup = q.edits[-1]
    assert "mbx000" in text and "mbx001" in text
    datas = {d for _t, d in _buttons(markup)}
    assert "mb:mbx:mbx000" in datas
    assert "mb:mbx:mbx001" in datas


# ---------------------------------------------------------------------------
# Pagination: 90-100+ mailboxes paginate at the manager page size.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("n", [1, 10, 90, 100])
def test_manager_paginates_at_expected_sizes(tmp_profile, fake_him, n):
    _seed(tmp_profile, n)
    q = _tap("mb:switch")
    _text, markup = q.edits[-1]
    opens = [d for _t, d in _buttons(markup) if d and d.startswith("mb:mbx:")]
    page_size = mailboxes_mod._MANAGER_PAGE_SIZE
    assert 0 < len(opens) <= page_size

    # Walk every page via Next.
    pages_seen = 1
    cur = q
    while True:
        nexts = [
            d for t, d in _buttons(cur.edits[-1][1])
            if t == "Next" and d and d.startswith("mb:switch:p:")
        ]
        if not nexts:
            break
        cur = _tap(nexts[0])
        pages_seen += 1
        if pages_seen > 50:
            pytest.fail("manager pagination appears to loop")
    expected = max(1, (n + page_size - 1) // page_size)
    assert pages_seen == expected


# ---------------------------------------------------------------------------
# Every registered mailbox appears across pages, not just those with
# pending counts. (Spec point 2.)
# ---------------------------------------------------------------------------
def test_manager_lists_every_registered_mailbox(tmp_profile, fake_him):
    aids = _seed(tmp_profile, 25)
    seen: set[str] = set()
    q = _tap("mb:switch")
    while True:
        _text, markup = q.edits[-1]
        for _label, d in _buttons(markup):
            if d and d.startswith("mb:mbx:"):
                seen.add(d.split(":", 2)[2])
        nexts = [
            d for t, d in _buttons(markup)
            if t == "Next" and d and d.startswith("mb:switch:p:")
        ]
        if not nexts:
            break
        q = _tap(nexts[0])
    assert seen == set(aids)


# ---------------------------------------------------------------------------
# Per-mailbox control screen: all the required buttons are present and are
# group-safe (every callback either stays in the ``mb:`` whitelist or
# re-uses an existing GROUP_CALLBACK prefix).
# ---------------------------------------------------------------------------
def test_mailbox_screen_in_group_has_all_required_controls(tmp_profile, fake_him):
    _seed(tmp_profile, 3)
    q = _tap("mb:mbx:mbx000")
    assert q.edits, "mb:mbx:<aid> produced no screen"
    text, markup = q.edits[-1]
    assert "Mailbox: " in text and "mbx000" in text
    datas = {d for _t, d in _buttons(markup)}
    # Required controls per spec.
    assert "ab:mbx000|p0" in datas         # Critical
    assert "ab:mbx000|p1" in datas         # Important / Open Inbox default
    assert "ab:mbx000|p2" in datas         # Normal
    assert "ab:mbx000|p3" in datas         # Noise
    assert "mb:mbx:mbx000:refresh" in datas
    assert "mb:mbx:mbx000:health" in datas
    assert "mb:mbx:mbx000:settings" in datas
    assert "mb:switch" in datas  # Back to Mailboxes


def test_mailbox_screen_buttons_are_all_group_whitelisted(tmp_profile, fake_him):
    _seed(tmp_profile, 2)
    q = _tap("mb:mbx:mbx000")
    _text, markup = q.edits[-1]
    for _label, data in _buttons(markup):
        if not data or data == "noop":
            continue
        if data.startswith("mb:"):
            assert app_mod.group_mailbox_callback_allowed(data), (
                f"{data!r} is NOT in GROUP_MB_* whitelist"
            )
        else:
            assert app_mod.group_callback_allowed(data), (
                f"{data!r} is NOT in GROUP_CALLBACK_* whitelist"
            )


# ---------------------------------------------------------------------------
# Group dispatch: every new callback reaches a screen in a group tap and
# NEVER triggers the generic 'Private chat only' alert.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "data",
    [
        "mb:switch",
        "mb:switch:p:0",
        "mb:switch:refresh",
        "mb:mbx:mbx000",
        "mb:mbx:mbx000:refresh",
        "mb:mbx:mbx000:health",
    ],
)
def test_group_tap_reaches_a_screen_without_private_only_alert(
    tmp_profile, fake_him, data
):
    _seed(tmp_profile, 3)
    q = _tap(data)
    denied = [t for t, show in q.answers if show and t == app_mod.GROUP_DENIED_ALERT]
    assert not denied, (
        f"{data} was answered with GROUP_DENIED_ALERT; group routing is broken"
    )


# ---------------------------------------------------------------------------
# Group tap for ``mb:switch:search`` must NOT open a text slot in the
# group (would attract typing in a shared chat); it must alert the admin
# to open a DM. ``mb:mbx:<aid>:settings`` must do the same for sensitive
# write actions.
# ---------------------------------------------------------------------------
def test_search_in_group_alerts_to_open_dm(tmp_profile, fake_him):
    _seed(tmp_profile, 2)
    q = _tap("mb:switch:search")
    # No new text slot was armed in the group.
    assert mailboxes_mod.AWAITING.get(USER_A) is None
    # The admin was told to open a DM.
    answers = [t for t, show in q.answers if show and t]
    assert any("DM" in t for t in answers), answers


def test_settings_in_group_alerts_to_open_dm(tmp_profile, fake_him):
    _seed(tmp_profile, 2)
    q = _tap("mb:mbx:mbx000:settings")
    assert q.edits == [], "settings must not render in group"
    answers = [t for t, show in q.answers if show and t]
    assert any("private" in t.lower() or "dm" in t.lower() for t in answers), answers


def test_settings_in_private_opens_the_full_view(tmp_profile, fake_him):
    _seed(tmp_profile, 2)
    q = _tap("mb:mbx:mbx000:settings", chat_id=USER_A, kind="private")
    assert q.edits, "settings in private must render the mb:view:* screen"
    text, markup = q.edits[-1]
    # The classic view screen shows the mailbox header and enable/disable
    # (or 'onboard again') plus audit/purpose buttons.
    assert "Mailbox: mbx000" in text
    datas = {d for _t, d in _buttons(markup)}
    assert any(d.startswith("mb:audit:mbx000") for d in datas)


# ---------------------------------------------------------------------------
# Health check: the on-demand button fills _HEALTH_CACHE from a REAL
# health_check() call (which, under the fake Himalaya + no stored
# credentials, returns ok=False with 'no stored credentials'). The UI
# must then show that result; it must NEVER fabricate a 'Ready' status.
# ---------------------------------------------------------------------------
def test_health_check_populates_cache_and_renders_result(tmp_profile, fake_him):
    _seed(tmp_profile, 2)
    assert "mbx000" not in mailboxes_mod._HEALTH_CACHE
    q = _tap("mb:mbx:mbx000:health")
    assert "mbx000" in mailboxes_mod._HEALTH_CACHE
    cached = mailboxes_mod._HEALTH_CACHE["mbx000"]
    # The fake fixture has no stored credentials, so health_check
    # short-circuits to ok=False with a short detail. The UI must have
    # re-rendered with that result (not with a fabricated 'Ready').
    assert cached["ok"] is False
    text, _markup = q.edits[-1]
    assert "Last health check" in text


def test_health_check_in_group_also_works(tmp_profile, fake_him):
    _seed(tmp_profile, 2)
    q = _tap("mb:mbx:mbx000:health")
    denied = [t for t, show in q.answers if show and t == app_mod.GROUP_DENIED_ALERT]
    assert not denied
    assert q.edits


# ---------------------------------------------------------------------------
# Pagination state is remembered per admin, so Refresh re-renders the
# same page the admin was on.
# ---------------------------------------------------------------------------
def test_refresh_re_renders_same_page(tmp_profile, fake_him):
    _seed(tmp_profile, 25)
    # Jump to page 2.
    _tap("mb:switch:p:1")
    # Refresh should re-render page 2, NOT page 1.
    q = _tap("mb:switch:refresh")
    text, markup = q.edits[-1]
    assert "Page 2/" in text
    # Prev button is present (we're not on page 0).
    labels = {t for t, _d in _buttons(markup)}
    assert "Prev" in labels


# ---------------------------------------------------------------------------
# Strangers can never navigate the manager, even if they somehow guess a
# callback. The dispatcher must refuse before mailboxes.handle_callback
# gets to render anything.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "data", ["mb:switch", "mb:switch:p:1", "mb:mbx:mbx000", "mb:mbx:mbx000:health"]
)
def test_stranger_cannot_use_manager(tmp_profile, fake_him, data):
    _seed(tmp_profile, 3)
    q = _tap(data, user_id=STRANGER)
    assert q.edits == [], f"stranger reached a manager screen via {data}"


# ---------------------------------------------------------------------------
# No fake statuses: a mailbox without stored credentials renders as
# Disconnected, never as Ready. (Spec point 10.)
# ---------------------------------------------------------------------------
def test_no_credentials_renders_as_disconnected(tmp_profile, fake_him):
    _seed(tmp_profile, 2)
    q = _tap("mb:switch")
    text, _markup = q.edits[-1]
    # The fake has no credentials on disk, so every row should surface
    # the Disconnected branch.
    assert "Disconnected" in text


# ---------------------------------------------------------------------------
# Switch and detail screens are reachable for both configured admins.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("user_id", [USER_A, USER_B])
@pytest.mark.parametrize(
    "data", ["mb:switch", "mb:mbx:mbx000", "mb:mbx:mbx000:refresh"]
)
def test_both_admins_can_navigate(tmp_profile, fake_him, user_id, data):
    _seed(tmp_profile, 5)
    q = _tap(data, user_id=user_id)
    denied = [t for t, show in q.answers if show and t == app_mod.GROUP_DENIED_ALERT]
    assert not denied
    # Something rendered (or at least alerted the admin).
    assert q.edits or q.answers
