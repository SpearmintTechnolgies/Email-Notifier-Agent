"""Altcoin-Informer-only operation.

Mailops manages exactly one inbox: ``altcoininformer``. The former Gmail
(Growth Pilot) account must never be synced, cached, notified about, shown, or
reachable from an old Telegram button - even though its historical rows are
deliberately kept in the database and an old registry entry may still exist.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import mailops.bot.app as app_mod
from mailops import cache as cache_mod
from mailops import config
from mailops.bot import keyboards as kb
from mailops.config import active_account_ids, load_accounts
from mailops.dashboard import collect_counts, format_dashboard, format_page
from mailops.db import (
    init_db,
    list_pending_notifications,
    mark_message_processed,
    pop_batch,
    queue_batch,
    queue_notification,
    save_draft,
    set_message_location,
)
from tests.conftest import FakeHimalaya
from tests.test_telegram_group import (
    USER_A,
    button_labels,
    callback_datas,
    callback_update,
    ctx,
    set_env,
)

GMAIL = "gmail"
ALT = "altcoininformer"
ALT_EMAIL = "info@altcoininformer.com"
GMAIL_EMAIL = "growthpilotforyou@gmail.com"


def _run(coro):
    return asyncio.run(coro)


class RecordingHimalaya(FakeHimalaya):
    """FakeHimalaya that remembers which accounts were touched at all."""

    def __init__(self):
        super().__init__()
        self.touched: set[str] = set()

    def envelopes(self, account, mailbox="INBOX", page_size=25, page=1):
        self.touched.add(account)
        return super().envelopes(account, mailbox, page_size, page)

    def read_message(self, account, msg_id, mailbox="INBOX"):
        self.touched.add(account)
        return super().read_message(account, msg_id, mailbox)

    def read_card(self, account, msg_id, mailbox="INBOX"):
        self.touched.add(account)
        return super().read_card(account, msg_id, mailbox)

    def mailbox_list(self, account):
        self.touched.add(account)
        return super().mailbox_list(account)

    def create_mailbox(self, account, name):
        self.touched.add(account)
        return super().create_mailbox(account, name)

    def reply_send(self, account, *a, **k):
        self.touched.add(account)
        return super().reply_send(account, *a, **k)


class FakeBot:
    username = "ourmailagent_bot"

    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


@pytest.fixture()
def altcoin_only(tmp_profile):
    """Registry still lists Gmail (as a stale entry would), but settings.json
    activates only altcoininformer. The DB holds historical Gmail rows."""
    (tmp_profile / "himalaya" / "accounts.json").write_text(
        json.dumps(
            [
                {"id": GMAIL, "email": GMAIL_EMAIL, "display_name": "growthpilotforyou", "default": True},
                {"id": ALT, "email": ALT_EMAIL, "display_name": "Altcoin Informer", "default": False},
            ]
        ),
        encoding="utf-8",
    )
    config.SETTINGS_JSON.write_text(
        json.dumps(
            {
                "active_accounts": [ALT],
                "account_policies": {ALT: {"organize": False, "send": False}},
                "p2_batch_seconds": 0,
            }
        ),
        encoding="utf-8",
    )
    set_env(tmp_profile)
    init_db()
    # Historical Gmail data kept in the DB on purpose.
    for mid, prio in (("7", 0), ("g2", 0), ("g3", 1)):
        set_message_location(GMAIL, mid, "INBOX.P0-Critical" if prio == 0 else "INBOX.P1-Important",
                             subject="old gmail mail", sender="x@y.z", priority=prio)
        mark_message_processed(GMAIL, mid)
    return tmp_profile


# --- registry ------------------------------------------------------------------
def test_only_altcoin_is_active_even_if_registry_lists_gmail(altcoin_only):
    assert [a.id for a in load_accounts()] == [ALT]
    assert active_account_ids() == {ALT}


def test_cli_accounts_lists_only_altcoin(altcoin_only, capsys):
    from mailops.main import cmd_accounts

    cmd_accounts()
    out = capsys.readouterr().out
    assert ALT in out and ALT_EMAIL in out
    assert GMAIL not in out and "gmail" not in out.lower()


def test_without_allowlist_gmail_stays_excluded(altcoin_only):
    config.SETTINGS_JSON.write_text("{}", encoding="utf-8")
    assert [a.id for a in load_accounts()] == [ALT]


def test_enabled_false_disables_an_account(altcoin_only):
    config.SETTINGS_JSON.write_text("{}", encoding="utf-8")
    (config.ACCOUNTS_JSON).write_text(
        json.dumps(
            [
                {"id": GMAIL, "email": GMAIL_EMAIL, "enabled": False},
                {"id": ALT, "email": ALT_EMAIL, "display_name": "Altcoin Informer", "default": True},
            ]
        ),
        encoding="utf-8",
    )
    assert [a.id for a in load_accounts()] == [ALT]


def test_unreadable_allowlist_does_not_hide_business_accounts(altcoin_only):
    config.SETTINGS_JSON.write_text("{not json", encoding="utf-8")
    assert [a.id for a in load_accounts()] == [ALT]


# --- workers never touch Gmail -----------------------------------------------------
def test_sync_and_cache_never_touch_gmail(altcoin_only, monkeypatch):
    from mailops import sync_worker

    rec = RecordingHimalaya()
    rec.add_message(GMAIL, "INBOX", "7", subject="gmail mail", sender="g@gmail.com")
    rec.add_message(ALT, "INBOX", "a1", subject="security alert breach", sender="a@b.co")
    monkeypatch.setattr(sync_worker, "Himalaya", lambda: rec)
    cache_mod.set_provider(rec)
    try:
        _run(sync_worker.sync_once(None))
        cache_mod.refresh_all()
        _run(sync_worker.rescan_all())
        _run(sync_worker.backfill_all_locations(rec))
    finally:
        cache_mod.set_provider(None)
    assert rec.touched == {ALT}
    assert rec.mutations == []  # read-only account + test mode: nothing changed
    # Gmail's historical rows are untouched (not deleted).
    from mailops.db import get_message_locations

    assert set(get_message_locations(GMAIL)) == {"7", "g2", "g3"}


def test_gmail_history_cannot_change_altcoin_counts(altcoin_only):
    rec = RecordingHimalaya()
    # Same message id "7" exists in both mailboxes; only Gmail's was classified.
    rec.add_message(ALT, "INBOX", "7", subject="hello", sender="a@b.co")
    rec.add_message(ALT, "INBOX", "a2", subject="rates?", sender="a@b.co")
    set_message_location(ALT, "a2", "INBOX.P1-Important", subject="rates?", sender="a@b.co", priority=1)
    cache_mod.set_provider(rec)
    try:
        counts = collect_counts(force=True)
    finally:
        cache_mod.set_provider(None)
    assert [c.account_id for c in counts] == [ALT]
    c = counts[0]
    assert (c.p0, c.p1, c.p2, c.p3) == (0, 1, 0, 0)
    assert rec.touched == {ALT}


# --- queued alerts of a retired account stay queued ------------------------------
def test_pending_alerts_for_gmail_are_never_delivered(altcoin_only):
    """Digest-redesign version of the Gmail-isolation guarantee.

    Pre-redesign Mailops delivered individual per-mail retry pings via
    ``retry_pending_notifications``. The new behaviour migrates legacy
    rows into the digest table instead - but only for ACTIVE accounts.
    The disabled Gmail row stays in ``pending_notifications`` untouched
    (and never enters ``digest_pending``) so retired accounts cannot
    produce any Telegram traffic.
    """
    from mailops.db import list_undelivered_digest_rows
    from mailops.sync_worker import retry_pending_notifications

    for acc in (GMAIL, ALT):
        queue_notification(
            f"{acc}:INBOX:1", acc, "INBOX", "1",
            {"type": "new_mail", "account_id": acc, "msg_id": "1", "mailbox": "INBOX",
             "key": f"{acc}:INBOX:1", "priority": 0, "subject": "x", "sender": "y"},
        )
    events: list[dict] = []

    async def notify(ev):
        events.append(ev)

    # One ALT row migrated; zero per-mail Telegram sends.
    assert _run(retry_pending_notifications(notify)) == 1
    assert events == []
    # The Altcoin Informer row now lives in the digest queue, not pending.
    digest_rows = list_undelivered_digest_rows(max_priority=1)
    assert [r["account_id"] for r in digest_rows] == [ALT]
    # The Gmail row is preserved (never migrated, never alerted).
    assert [r["account_id"] for r in list_pending_notifications()] == [GMAIL]


def test_p2_batch_skips_and_keeps_gmail_rows(altcoin_only):
    from mailops.notify import flush_p2_batch

    queue_batch("gmail:INBOX:1", GMAIL, "INBOX", "1", "old gmail subject", "x@y.z", 2)
    queue_batch("altcoininformer:INBOX:2", ALT, "INBOX", "2", "ad rates question", "a@b.co", 2)
    bot = FakeBot()
    _run(flush_p2_batch(bot))
    assert bot.sent, "altcoin P2 batch should be delivered"
    assert all("old gmail subject" not in text for _, text in bot.sent)
    assert any("ad rates question" in text for _, text in bot.sent)
    assert [r["account_id"] for r in pop_batch()] == [GMAIL]


def test_events_for_inactive_account_are_dropped(altcoin_only):
    from mailops.notify import handle_event

    bot = FakeBot()
    for etype in ("new_mail", "sync_error", "sent", "send_failed"):
        _run(handle_event(bot, {"type": etype, "account_id": GMAIL, "email": GMAIL_EMAIL,
                                "msg_id": "1", "mailbox": "INBOX", "key": f"k:{etype}"}))
    assert bot.sent == []


def test_altcoin_alert_names_altcoin_informer(altcoin_only):
    from mailops.notify import handle_event

    bot = FakeBot()
    _run(handle_event(bot, {"type": "new_mail", "account_id": ALT, "email": ALT_EMAIL, "msg_id": "1",
                            "mailbox": "INBOX", "priority": 1, "subject": "Rates?", "sender": "a@b.co",
                            "key": "alt:1"}))
    assert bot.sent
    for _, text in bot.sent:
        assert "Altcoin Informer" in text and "gmail" not in text.lower()


# --- Telegram views --------------------------------------------------------------
def test_dashboards_show_only_altcoin_informer(altcoin_only):
    rec = RecordingHimalaya()
    cache_mod.set_provider(rec)
    try:
        counts = collect_counts(force=True)
    finally:
        cache_mod.set_provider(None)
    for group in (False, True):
        text = format_dashboard(counts, group=group)
        assert "Altcoin Informer" in text and ALT_EMAIL in text
        assert "gmail" not in text.lower() and "growthpilot" not in text.lower()
    for markup in (kb.dashboard_keyboard(counts), kb.group_dashboard_keyboard(counts)):
        datas = callback_datas(markup)
        assert not any(d.startswith("ab:") for d in datas), "no per-account picker"
        assert not any(GMAIL in d for d in datas)
        assert not any("gmail" in b.lower() for b in button_labels(markup))
        assert {"b:p0", "b:p1", "b:p2", "b:p3"} <= set(datas)


def test_group_dashboard_stays_read_only(altcoin_only):
    counts = [cache_mod.AccountCounts(ALT, ALT_EMAIL, 1, 2, 3, 4, display_name="Altcoin Informer")]
    datas = callback_datas(kb.group_dashboard_keyboard(counts))
    assert all(app_mod.group_callback_allowed(d) for d in datas)
    assert not any(d.startswith(("set:", "nav:settings", "open:", "reply:", "send:")) for d in datas)


def test_main_menu_has_no_account_picker(altcoin_only):
    labels = [b.text for row in kb.main_menu().keyboard for b in row]
    assert "Inboxes" not in labels
    assert {"Dashboard", "Critical", "Important", "Search", "Settings", "Manual refresh"} <= set(labels)


def test_page_lines_do_not_show_account_tag(altcoin_only):
    items = [{"account_id": ALT, "subject": "Hello", "from": "a@b.co", "ref": "x", "mailbox": "INBOX", "msg_id": "1"}]
    text = format_page("Critical", items, 0, 1, 1)
    assert "1. Hello" in text and "[altcoininformer]" not in text


def test_start_greets_altcoin_informer_email_agent(altcoin_only, monkeypatch):
    from tests.test_telegram_group import command_update

    rec = RecordingHimalaya()
    cache_mod.set_provider(rec)
    try:
        upd, msg = command_update(USER_A, USER_A, "private")
        _run(app_mod.cmd_start(upd, ctx()))
    finally:
        cache_mod.set_provider(None)
    assert msg.sent[0][0].startswith("Altcoin Informer Email Agent ready.")
    assert "gmail" not in " ".join(t for t, _ in msg.sent).lower()


def test_stale_inboxes_button_just_shows_the_dashboard(altcoin_only):
    from types import SimpleNamespace

    from tests.test_telegram_group import FakeMessage, chat

    rec = RecordingHimalaya()
    cache_mod.set_provider(rec)
    msg = FakeMessage()
    msg.text = "Inboxes"
    upd = SimpleNamespace(
        callback_query=None,
        effective_user=SimpleNamespace(id=USER_A),
        effective_chat=chat(USER_A, "private"),
        effective_message=msg,
        message=msg,
    )
    try:
        _run(app_mod.on_text(upd, ctx()))
    finally:
        cache_mod.set_provider(None)
    texts = [t for t, _ in msg.sent]
    assert any("only inbox" in t for t in texts)
    assert any("Priority dashboard" in t for t in texts)
    assert not any("Accounts" == t for t in texts)


# --- stale Gmail buttons are refused before any mailbox call -------------------------
@pytest.mark.parametrize(
    "data",
    [
        f"open:{GMAIL}|INBOX|1",
        f"reply:{GMAIL}|INBOX|1",
        f"send:{GMAIL}|INBOX|1",
        f"aidraft:{GMAIL}|INBOX|1",
        f"writeme:{GMAIL}|INBOX|1",
        f"done:{GMAIL}|INBOX|1",
        f"setprio:{GMAIL}|INBOX|1|0",
        f"browse:{GMAIL}|INBOX|1|n",
        f"ab:{GMAIL}|p0",
        f"acct:{GMAIL}",
        f"list:{GMAIL}|INBOX",
        f"set:purpose:{GMAIL}",
        f"set:kb:view:{GMAIL}",
        f"set:kb:note:{GMAIL}",
    ],
)
def test_stale_gmail_callbacks_are_refused(altcoin_only, monkeypatch, data):
    rec = RecordingHimalaya()
    monkeypatch.setattr(app_mod, "Himalaya", lambda: rec)
    cache_mod.set_provider(rec)
    # Even a leftover Gmail draft must not be sendable.
    save_draft(USER_A, GMAIL, "INBOX", "1", "hello")
    upd, q = callback_update(data, USER_A, USER_A, "private")
    try:
        _run(app_mod.on_callback(upd, ctx()))
    finally:
        cache_mod.set_provider(None)
    assert rec.touched == set(), "the mailbox client must not be used for Gmail"
    assert rec.sent == [] and rec.mutations == []
    assert any("no longer manages" in (t or "") for t, _ in q.answers) or any(
        "no longer manages" in t for t, _ in q.message.sent
    )
    assert app_mod.WAITING_TEXT.get(USER_A) is None


def test_altcoin_buttons_still_work(altcoin_only, monkeypatch):
    rec = RecordingHimalaya()
    rec.add_message(ALT, "INBOX", "5", subject="rates?", sender="b@b.co")
    monkeypatch.setattr(app_mod, "Himalaya", lambda: rec)
    upd, q = callback_update(f"open:{ALT}|INBOX|5", USER_A, USER_A, "private")
    _run(app_mod.on_callback(upd, ctx()))
    assert rec.touched == {ALT}
    assert q.edits and "rates?" in q.edits[-1][0]


def test_altcoin_send_stays_disabled_until_approved(altcoin_only, monkeypatch):
    """Draft + Confirm send for Altcoin Informer is refused by policy (send off)."""
    rec = RecordingHimalaya()
    rec.add_message(ALT, "INBOX", "5", subject="rates?", sender="b@b.co")
    monkeypatch.setattr(app_mod, "Himalaya", lambda: rec)
    save_draft(USER_A, ALT, "INBOX", "5", "Thanks, here are our rates")
    upd, q = callback_update(f"send:{ALT}|INBOX|5", USER_A, USER_A, "private")
    _run(app_mod.on_callback(upd, ctx()))
    assert rec.sent == []
    # The refusal reaches the user either as the callback's alert popup (fast
    # refusals answer the query themselves) or as a chat message (when the
    # query was already acknowledged). Both are acceptable; silence is not.
    told = [t for t, _ in q.message.sent] + [t for t, _ in q.answers if t]
    assert any("Sending is disabled" in t for t in told), told


# --- db helpers ------------------------------------------------------------------
def test_db_filters_leave_other_accounts_rows_in_place(altcoin_only):
    queue_batch("a", GMAIL, "INBOX", "1", "s", "f", 2)
    queue_batch("b", ALT, "INBOX", "2", "s", "f", 2)
    assert [r["key"] for r in pop_batch(0, {ALT})] == ["b"]
    assert [r["key"] for r in pop_batch(0)] == ["a"]
