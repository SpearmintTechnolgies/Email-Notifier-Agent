"""Approved business webmail multi-account support, with Gmail excluded.

Altcoin Informer stays the current active inbox. Coinography and Memecoinist
can be allowlisted independently after IMAP/SMTP validation. Growth Pilot
Gmail is never an active Mailops account.
"""
from __future__ import annotations

import json

import pytest

from mailops import config
from mailops.bot import keyboards as kb
from mailops.config import active_account_ids, load_accounts
from mailops.dashboard import collect_counts, format_dashboard, format_page
from mailops.db import init_db
from tests.conftest import FakeHimalaya
from mailops.bot.app import group_callback_allowed
from tests.test_telegram_group import callback_datas, set_env

GMAIL = "gmail"
ALT = "altcoininformer"
COIN = "coinography"
MEME = "memecoinist"


@pytest.fixture()
def brands(tmp_profile):
    (tmp_profile / "himalaya" / "accounts.json").write_text(
        json.dumps(
            [
                {"id": GMAIL, "email": "growthpilotforyou@gmail.com", "display_name": "growthpilotforyou", "default": True},
                {"id": ALT, "email": "info@altcoininformer.com", "display_name": "Altcoin Informer"},
                {"id": COIN, "email": "info@coinography.com", "display_name": "Coinography"},
                {"id": MEME, "email": "sales@memecoinist.com", "display_name": "Memecoinist"},
            ]
        ),
        encoding="utf-8",
    )
    config.SETTINGS_JSON.write_text(
        json.dumps(
            {
                "active_accounts": [ALT],
                "account_policies": {
                    ALT: {"organize": False, "send": False},
                    COIN: {"organize": False, "send": False},
                    MEME: {"organize": False, "send": False},
                },
            }
        ),
        encoding="utf-8",
    )
    set_env(tmp_profile)
    init_db()
    return tmp_profile


def test_gmail_flag_is_disabled():
    from mailops.config import GMAIL_ENABLED

    assert GMAIL_ENABLED is False


def test_gmail_is_never_active_even_when_listed_in_allowlist(brands):
    config.SETTINGS_JSON.write_text(
        json.dumps({"active_accounts": [GMAIL, ALT, COIN, MEME]}), encoding="utf-8"
    )
    assert [a.id for a in load_accounts()] == [ALT, COIN, MEME]
    assert GMAIL not in active_account_ids()


def test_gmail_stays_excluded_without_allowlist(brands):
    config.SETTINGS_JSON.write_text("{}", encoding="utf-8")
    assert [a.id for a in load_accounts()] == [ALT, COIN, MEME]


def test_unreadable_allowlist_still_excludes_gmail(brands):
    config.SETTINGS_JSON.write_text("{not json", encoding="utf-8")
    assert [a.id for a in load_accounts()] == [ALT, COIN, MEME]


def test_inactive_business_accounts_are_completely_excluded(brands, monkeypatch):
    from mailops import cache as cache_mod
    from mailops import sync_worker

    rec = FakeHimalaya()
    rec.add_message(COIN, "INBOX", "1", subject="how much to advertise", sender="a@b.co")
    rec.add_message(MEME, "INBOX", "1", subject="how much to advertise", sender="a@b.co")
    rec.add_message(ALT, "INBOX", "1", subject="how much to advertise", sender="a@b.co")
    rec.add_message(GMAIL, "INBOX", "1", subject="how much to advertise", sender="g@gmail.com")
    monkeypatch.setattr(sync_worker, "Himalaya", lambda: rec)
    cache_mod.set_provider(rec)
    try:
        import asyncio

        asyncio.run(sync_worker.sync_once(None))
        cache_mod.refresh_all()
    finally:
        cache_mod.set_provider(None)
    counts = collect_counts(force=True)
    assert [c.account_id for c in counts] == [ALT]
    text = format_dashboard(counts)
    assert "Altcoin Informer" in text
    assert "coinography" not in text.lower()
    assert "memecoinist" not in text.lower()
    assert "gmail" not in text.lower()


def test_dashboard_lists_every_approved_business_account(brands):
    data = json.loads(config.SETTINGS_JSON.read_text(encoding="utf-8"))
    data["active_accounts"] = [ALT, COIN, MEME]
    config.SETTINGS_JSON.write_text(json.dumps(data), encoding="utf-8")
    rec = FakeHimalaya()
    from mailops import cache as cache_mod

    cache_mod.set_provider(rec)
    try:
        counts = collect_counts(force=True)
    finally:
        cache_mod.set_provider(None)
    assert [c.account_id for c in counts] == [ALT, COIN, MEME]
    text = format_dashboard(counts)
    assert "info@altcoininformer.com" in text
    assert "info@coinography.com" in text
    assert "sales@memecoinist.com" in text
    assert "gmail" not in text.lower()
    markup = kb.dashboard_keyboard(counts)
    datas = callback_datas(markup)
    assert f"ab:{COIN}|p0" in datas and f"ab:{MEME}|p1" in datas
    assert not any(GMAIL in d for d in datas)
    labels = [b.text for row in kb.main_menu().keyboard for b in row]
    assert "Inboxes" in labels
    group = kb.group_dashboard_keyboard(counts)
    assert all(group_callback_allowed(d) for d in callback_datas(group))


def test_page_lines_tag_account_when_several_are_active(brands):
    data = json.loads(config.SETTINGS_JSON.read_text(encoding="utf-8"))
    data["active_accounts"] = [ALT, COIN]
    config.SETTINGS_JSON.write_text(json.dumps(data), encoding="utf-8")
    items = [
        {"account_id": ALT, "subject": "Hello", "from": "a@b.co", "ref": "x", "mailbox": "INBOX", "msg_id": "1"},
        {"account_id": COIN, "subject": "Rates", "from": "c@d.co", "ref": "y", "mailbox": "INBOX", "msg_id": "2"},
    ]
    text = format_page("Important", items, 0, 1, 2)
    assert "[altcoininformer]" in text and "[coinography]" in text


def test_enabled_false_keeps_a_registered_business_account_out(brands):
    (config.ACCOUNTS_JSON).write_text(
        json.dumps(
            [
                {"id": ALT, "email": "info@altcoininformer.com", "display_name": "Altcoin Informer"},
                {"id": COIN, "email": "info@coinography.com", "enabled": False},
            ]
        ),
        encoding="utf-8",
    )
    config.SETTINGS_JSON.write_text("{}", encoding="utf-8")
    assert [a.id for a in load_accounts()] == [ALT]


def test_cli_accounts_lists_only_active_business_mailboxes(brands, capsys):
    from mailops.main import cmd_accounts

    cmd_accounts()
    out = capsys.readouterr().out
    assert ALT in out and "info@altcoininformer.com" in out
    assert COIN not in out and MEME not in out
    assert "gmail" not in out.lower()
