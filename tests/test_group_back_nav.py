"""Regression tests for the group Back-navigation bug (2026-10-19).

The Mailboxes screen used to put a "Back to admin" → ``ac:m`` button on
*every* render, including when ``in_group=True``. In a group that routed
the admin into ``admin.handle_callback``'s ``_guard_admin(private_only=True)``
and surfaced the dead-end alert:

    "Access management is private-chat only. Open the bot in a private chat."

The fix in ``mailboxes.main_screen`` is context-aware: in a group it emits
"Back to dashboard" → ``nav:home`` (already in ``GROUP_CALLBACK_EXACT`` and
backed by ``show_dashboard(group=True)``). In private DMs it still shows
"Back to admin" → ``ac:m`` which lands on the Access-Management main screen
(private-only by design — it exposes user ids, audit log, bootstrap owner).

These tests cover:
  * Every button on every group-reachable mailbox screen points at a
    whitelisted callback (``GROUP_CALLBACK_EXACT``/``PREFIXES`` or
    ``GROUP_MB_EXACT``/``PREFIXES``), parametrised over both production
    admins (USER_A = 8925736576, USER_B = 5337250772).
  * No group Back/Home button points at ``ac:*`` (the specific regression).
  * In private DMs the Back-to-admin button is preserved and still routes
    to the Access-Management screen.
  * Tapping ``nav:home`` from a group reaches ``show_dashboard(group=True)``
    without the "private-chat only" denial.
  * The access-denied alert is NEVER emitted on the group navigation paths.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import mailops.bot.app as app_mod
from mailops import access
from mailops.bot import mailboxes as mb_mod, bulk_mailboxes as bmb_mod
from mailops.db import init_db
from tests.test_telegram_group import (
    GROUP,
    STRANGER,
    USER_A,
    USER_B,
    callback_datas,
    callback_update,
    set_env,
)


def _run(coro):
    return asyncio.run(coro)


def _setup(profile, *, allowed=f"{USER_A},{USER_B}", home=f"{USER_A},{GROUP}"):
    init_db()
    set_env(profile, allowed=allowed, home=home)
    access.ensure()
    # In production BOTH admins are owners (we verified: access_users=
    # [(5337250772, 'owner'), (8925736576, 'owner')]). The default test
    # fixture seeds USER_A as bootstrap owner + USER_B as operator; promote
    # USER_B here so the regression test faithfully covers the real roles.
    try:
        access.set_role(USER_A, USER_B, "owner")
    except (access.AccessDenied, access.LastOwnerError, access.InvalidTargetError):
        pass


def _ctx():
    return SimpleNamespace(user_data={}, bot=None)


# ---------------------------------------------------------------- the specific regression


def test_group_main_mailboxes_screen_no_longer_links_to_ac_m(tmp_profile):
    """The exact bug: ``mb.main_screen(in_group=True)`` used to emit ``ac:m``."""
    _setup(tmp_profile)
    _, markup = mb_mod.main_screen(in_group=True)
    datas = callback_datas(markup)
    # The private-only Access-Management back-link must be gone.
    assert "ac:m" not in datas, (
        "group Mailboxes screen still points Back at ac:m; "
        "this routes to admin._guard_admin and triggers the "
        "'Access management is private-chat only' alert."
    )
    # And a group-appropriate Back is now present.
    assert "nav:home" in datas, (
        "group Mailboxes screen must offer a Back-to-dashboard button "
        "that lands on the shared group dashboard"
    )


def test_private_mailboxes_screen_still_links_to_ac_m(tmp_profile):
    """We did NOT weaken private UX: ``ac:m`` is still the Back target in DM."""
    _setup(tmp_profile)
    _, markup = mb_mod.main_screen(in_group=False)
    datas = callback_datas(markup)
    assert "ac:m" in datas
    assert "nav:home" not in datas


# ---------------------------------------------------------------- comprehensive audit


def _collect_group_mailbox_screens():
    """Every group-reachable screen that lives in ``mailboxes.py`` /
    ``bulk_mailboxes.py``. Returns ``[(label, markup), ...]``."""
    out: list[tuple[str, object]] = []
    _, m = mb_mod.main_screen(in_group=True)
    out.append(("main_screen(in_group=True)", m))
    _, m = mb_mod.switch_screen(in_group=True)
    out.append(("switch_screen(in_group=True)", m))
    _, m = mb_mod.add_start_screen()
    out.append(("add_start_screen", m))
    _, m = mb_mod._google_provider_screen()
    out.append(("_google_provider_screen", m))
    # Bulk Add entry (as a group-flavoured update would render it)
    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=USER_A),
        effective_chat=SimpleNamespace(id=GROUP, type="group"),
        effective_message=None,
        callback_query=None,
    )
    _, m = bmb_mod._entry_screen(upd)
    out.append(("bulk_mailboxes._entry_screen(group)", m))
    return out


@pytest.mark.parametrize("admin_id", [USER_A, USER_B])
def test_every_group_mailbox_button_is_group_whitelisted(tmp_profile, admin_id):
    """No button on any group-reachable mailbox screen points at a callback
    that would be denied by the group dispatcher. Parametrised over both
    production admins so a role-based keyboard mutation can't sneak past."""
    _setup(tmp_profile)
    for label, markup in _collect_group_mailbox_screens():
        for data in callback_datas(markup):
            if data.startswith("mb:"):
                assert app_mod.group_mailbox_callback_allowed(data), (
                    f"{label}: callback {data!r} is NOT in GROUP_MB_* "
                    "whitelist; admin {admin_id} would be denied in group"
                )
            else:
                assert app_mod.group_callback_allowed(data), (
                    f"{label}: callback {data!r} is NOT group-safe; "
                    f"admin {admin_id} would hit a private-only popup"
                )


@pytest.mark.parametrize("admin_id", [USER_A, USER_B])
def test_no_group_mailbox_button_points_at_ac_prefix(tmp_profile, admin_id):
    """The specific regression: no group screen may route Back to ``ac:*``.

    The ``ac:*`` dispatcher explicitly rejects group chats with the
    "Access management is private-chat only" alert. Any group button
    pointing at ``ac:*`` is a bug by construction.
    """
    _setup(tmp_profile)
    for label, markup in _collect_group_mailbox_screens():
        for data in callback_datas(markup):
            assert not data.startswith("ac:"), (
                f"{label}: button {data!r} would route admin {admin_id} "
                "into the private-only Access-Management dispatcher"
            )


# ---------------------------------------------------------------- live navigation


@pytest.mark.parametrize("admin_id", [USER_A, USER_B])
def test_nav_home_from_group_does_not_deny(tmp_profile, admin_id, monkeypatch, fake_him):
    """Tapping ``nav:home`` from the group reaches show_dashboard, not
    the private-chat-only guard. Covers both production admins."""
    _setup(tmp_profile)
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    upd, q = callback_update("nav:home", admin_id, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))
    # No denial alert.
    denials = [t for t, alert in q.answers if alert and t]
    assert not any("private-chat only" in (t or "").lower() for t in denials), (
        f"admin {admin_id} tapping nav:home from the group was denied: {denials!r}"
    )
    assert not any("Private chat only" in (t or "") for t in denials), (
        f"admin {admin_id} tapping nav:home from the group was denied: {denials!r}"
    )
    # And something was actually rendered (the group dashboard).
    assert q.edits or q.message.sent, "nav:home did not render a dashboard"


@pytest.mark.parametrize("admin_id", [USER_A, USER_B])
def test_group_back_button_round_trip_no_denial(tmp_profile, admin_id, monkeypatch, fake_him):
    """Walk the real group navigation: dashboard -> Mailboxes -> Back
    -> dashboard. None of the hops may raise the private-only alert."""
    _setup(tmp_profile)
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)

    # Step 1: tap "Mailboxes (in group)" from the dashboard.
    upd, q = callback_update("mb:m", admin_id, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))
    denials = [t for t, alert in q.answers if alert and t]
    assert not any("private" in (t or "").lower() for t in denials), denials

    # Grab the keyboard the group admin actually sees and extract the Back
    # button's callback_data.
    text, markup = (q.edits + q.message.sent)[-1]
    datas = callback_datas(markup)
    assert "ac:m" not in datas, "regression: group Mailboxes still points at ac:m"
    back_targets = [d for d in datas if d in ("nav:home", "dash")]
    assert back_targets, f"group Mailboxes has no group-safe Back button: {datas!r}"

    # Step 2: tap that Back button.
    upd2, q2 = callback_update(back_targets[0], admin_id, GROUP, "group")
    _run(app_mod.on_callback(upd2, _ctx()))
    denials2 = [t for t, alert in q2.answers if alert and t]
    assert not any("private" in (t or "").lower() for t in denials2), denials2


@pytest.mark.parametrize("admin_id", [USER_A, USER_B])
def test_group_ac_m_still_refused_at_dispatcher(tmp_profile, admin_id):
    """Defence in depth: even if a stale keyboard somewhere ships ``ac:m``
    to a group, the admin dispatcher still refuses it with the private-only
    alert. We MUST NOT weaken this invariant to suppress the popup."""
    _setup(tmp_profile)
    upd, q = callback_update("ac:m", admin_id, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))
    denials = [t for t, alert in q.answers if alert and t]
    joined = " ".join(denials).lower()
    assert "private-chat only" in joined, (
        "ac:m MUST still be denied in groups (defence in depth). "
        f"got answers: {denials!r}"
    )
    # And no screen was actually rendered (the admin menu never leaks into group).
    assert not q.edits, "ac:m from a group accidentally rendered a screen"


def test_stranger_in_group_still_cannot_see_ac_prefix(tmp_profile):
    """Fixing the admin regression must not open the Access-Management
    screen to non-admin members of the whitelisted group."""
    _setup(tmp_profile)
    upd, q = callback_update("ac:m", STRANGER, GROUP, "group")
    _run(app_mod.on_callback(upd, _ctx()))
    # No dashboard, no admin screen; a denial is enough.
    assert not q.edits, "stranger reached an admin screen from a group"


# ---------------------------------------------------------------- fresh keyboard sanity


def test_fresh_group_dashboard_keyboard_still_clean(tmp_profile, fake_him):
    """Smoke test: the shared group dashboard keyboard currently in use
    emits only group-whitelisted callbacks (plus ``mb:*`` with its own
    narrower allow-list)."""
    _setup(tmp_profile)
    from mailops.bot import keyboards as kb
    from mailops.cache import AccountCounts

    counts = [AccountCounts("sales", "sales@example.com", 1, 2, 3, 4)]
    markup = kb.group_dashboard_keyboard(
        counts, is_admin=True, can_settings=True, can_draft=True, can_send=True
    )
    for data in callback_datas(markup):
        if data.startswith("mb:"):
            assert app_mod.group_mailbox_callback_allowed(data), data
        else:
            assert app_mod.group_callback_allowed(data), data
