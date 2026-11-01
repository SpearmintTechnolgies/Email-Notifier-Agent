"""Regression tests for:

1. ``python -m mailops policy`` reporting test mode correctly when it comes from
   the .env file only (the CLI checks it before .env is loaded into os.environ).
2. Importing ``mailops.main`` never attaching a handler to the live
   data/mailops.log (tests and tooling used to pollute it).
3. Historical Gmail-API-era rows (hex message ids) in the ops database being
   inert: no dashboard counts, no duplicate alerts, no openable refs.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mailops.config import load_settings, save_settings
from mailops.dashboard import collect_bucket, collect_counts
from mailops.db import (
    db,
    get_message_locations,
    init_db,
    is_message_processed,
    mark_message_processed,
    mark_notified,
    set_message_location,
)
from mailops.safe_mode import guard_mutation
from mailops.safe_mode import test_mode_enabled as _mode_on  # alias: pytest collects test_* names
from mailops.sync_worker import process_account

ROOT = Path(__file__).resolve().parents[1]
GMAIL = "gmail"
LEGACY_IDS = [
    "1a0f26ae220181fc", "1a0f26df8ef48e36", "1a0f2738afa0d5d3", "1a0f27b7ffce9ab0",
    "1a11b1b1be69fd2e", "1a11beb2b5f0357e", "1a11c9ae76637d7f", "1a11c9b4bed024c2",
]
LEGACY_BOX = {  # mirrors the real historical rows
    LEGACY_IDS[0]: "INBOX.P0-Critical", LEGACY_IDS[1]: "INBOX.P2-Normal",
    LEGACY_IDS[2]: "INBOX.P2-Normal", LEGACY_IDS[3]: "INBOX.P3-Noise",
    LEGACY_IDS[4]: "INBOX.P3-Noise", LEGACY_IDS[5]: "INBOX.P0-Critical",
    LEGACY_IDS[6]: "INBOX.P2-Normal", LEGACY_IDS[7]: "INBOX.P2-Normal",
}


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# 1. policy CLI / test-mode resolution
# --------------------------------------------------------------------------
@pytest.fixture()
def gmail_profile(tmp_profile):
    (tmp_profile / "himalaya" / "accounts.json").write_text(
        json.dumps(
            [
                {"id": GMAIL, "email": "growthpilotforyou@gmail.com", "default": True},
                {"id": "info", "email": "info@example.com"},
            ]
        ),
        encoding="utf-8",
    )
    init_db()
    return tmp_profile


def _write_env(profile: Path, text: str) -> None:
    (profile / ".env").write_text(text, encoding="utf-8")


def test_policy_cli_reports_test_mode_when_only_dotenv_sets_it(gmail_profile, monkeypatch, capsys):
    """The exact production situation: no MAILOPS_TEST_MODE in the shell, ON in .env."""
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    _write_env(gmail_profile, "TELEGRAM_BOT_TOKEN=x\nMAILOPS_TEST_MODE=1\n")
    from mailops.main import main

    assert main(["policy"]) == 0
    out = capsys.readouterr().out
    assert "test mode: ON" in out
    assert f"{GMAIL}:" not in out
    assert "info:" in out
    # Reading the file must not leak .env values into the process environment.
    assert "MAILOPS_TEST_MODE" not in os.environ


def test_policy_cli_reports_off_when_nothing_enables_it(gmail_profile, monkeypatch, capsys):
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    _write_env(gmail_profile, "TELEGRAM_BOT_TOKEN=x\n")
    from mailops.main import main

    assert main(["policy"]) == 0
    assert "test mode: off" in capsys.readouterr().out


@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ('"on"', True), ("0", False), ("", False)])
def test_dotenv_value_is_parsed_like_the_env_var(gmail_profile, monkeypatch, value, expected):
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    _write_env(gmail_profile, f"MAILOPS_TEST_MODE={value}\n")
    assert _mode_on() is expected


def test_process_env_var_overrides_dotenv(gmail_profile, monkeypatch):
    _write_env(gmail_profile, "MAILOPS_TEST_MODE=1\n")
    monkeypatch.setenv("MAILOPS_TEST_MODE", "0")
    assert _mode_on() is False
    monkeypatch.setenv("MAILOPS_TEST_MODE", "1")
    _write_env(gmail_profile, "MAILOPS_TEST_MODE=0\n")
    assert _mode_on() is True


def test_dotenv_test_mode_blocks_mutations_in_every_command(gmail_profile, monkeypatch):
    """sync/rescan/backfill never load .env first; the guard must still fire."""
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    _write_env(gmail_profile, "MAILOPS_TEST_MODE=1\n")
    assert guard_mutation("move", "info INBOX/1") is True


def test_settings_flag_still_works_when_dotenv_is_silent(gmail_profile, monkeypatch):
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    _write_env(gmail_profile, "TELEGRAM_BOT_TOKEN=x\n")
    s = load_settings()
    s["test_mode"] = True
    save_settings(s)
    assert _mode_on() is True


def test_unreadable_dotenv_never_raises(gmail_profile, monkeypatch):
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    (gmail_profile / ".env").write_bytes(b"\xff\xfe\x00 not utf-8 \x81")
    assert _mode_on() in (True, False)  # no exception


# --------------------------------------------------------------------------
# 2. logging isolation
# --------------------------------------------------------------------------
def test_importing_main_does_not_touch_the_live_log():
    code = (
        "import logging, mailops.main\n"
        "kinds = [type(h).__name__ for h in logging.getLogger().handlers]\n"
        "print('KINDS=' + ','.join(kinds))\n"
    )
    # (Handler inspection, not file size: the live bot legitimately appends to
    # data/mailops.log at any moment.)
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    kinds = proc.stdout.strip().split("KINDS=")[-1]
    assert "FileHandler" not in kinds


def test_cli_entrypoint_configures_logging_then_runs(monkeypatch):
    import mailops.main as main_mod

    order: list[str] = []
    monkeypatch.setattr(main_mod, "_setup_logging", lambda: order.append("logging"))
    monkeypatch.setattr(main_mod, "main", lambda argv=None: order.append("main") or 7)
    assert main_mod.cli(["policy"]) == 7
    assert order == ["logging", "main"]


def test_package_entrypoint_uses_cli():
    text = (ROOT / "mailops" / "__main__.py").read_text(encoding="utf-8")
    assert "cli" in text and "main()" not in text.replace("cli()", "")


# --------------------------------------------------------------------------
# 3. historical hex-id rows are inert
# --------------------------------------------------------------------------
def _seed_legacy_rows() -> None:
    now = time.time()
    for mid, box in LEGACY_BOX.items():
        set_message_location(GMAIL, mid, box, subject=f"old {mid[-4:]}", sender="old@x.example", priority=2)
        mark_message_processed(GMAIL, mid)
        mark_notified(f"{GMAIL}:{box}:{mid}")
    assert len(get_message_locations(GMAIL)) == 8


def _live(fake_him):
    fake_him.add_message(GMAIL, "INBOX", "1", subject="security alert breach", sender="a@ext.example", uid=1)
    fake_him.add_message(GMAIL, "INBOX", "2", subject="how much to advertise", sender="b@ext.example", uid=2)
    fake_him.add_message(GMAIL, "INBOX", "3", subject="newsletter digest", sender="c@ext.example", uid=3)


@pytest.fixture()
def gmail_active(gmail_profile, monkeypatch):
    """Defense-in-depth: hex-id Gmail rows must stay inert even if Gmail were re-enabled."""
    import mailops.config as cfg

    monkeypatch.setattr(cfg, "GMAIL_ENABLED", True)
    return gmail_profile


def test_legacy_rows_do_not_change_dashboard_counts(gmail_active, fake_him):
    _seed_legacy_rows()
    _live(fake_him)
    events: list[dict] = []

    async def notify(ev):
        events.append(ev)

    _run(process_account(fake_him, GMAIL, "growthpilotforyou@gmail.com", notify))
    counts = {c.account_id: c for c in collect_counts(force=True)}[GMAIL]
    assert (counts.p0, counts.p1, counts.p2, counts.p3) == (1, 1, 0, 1)  # only the 3 live messages
    assert counts.p0 + counts.p1 + counts.p2 + counts.p3 == 3
    # 8 legacy rows + 3 new rows coexist untouched in the database
    assert len(get_message_locations(GMAIL)) == 11
    for mid in LEGACY_IDS:
        assert mid in get_message_locations(GMAIL)


def test_legacy_rows_never_appear_in_bucket_lists_or_refs(gmail_active, fake_him):
    _seed_legacy_rows()
    _live(fake_him)
    _run(process_account(fake_him, GMAIL, "growthpilotforyou@gmail.com", None))
    collect_counts(force=True)
    live_ids = {"1", "2", "3"}
    seen = []
    for p in (0, 1, 2, 3):
        for it in collect_bucket(p, GMAIL):
            seen.append(it)
            assert it["msg_id"] in live_ids
            assert it["mailbox"] == "INBOX"  # openable: a real mailbox, not a virtual folder
            assert not any(legacy in it["ref"] for legacy in LEGACY_IDS)
    assert {it["msg_id"] for it in seen} == live_ids


def test_legacy_rows_cause_no_duplicate_or_missing_alerts(gmail_profile, fake_him):
    """Legacy hex-id rows for a retired Gmail inbox must not trigger alerts.

    Digest-redesign variant: the sync worker no longer calls ``notify()``
    for each P0/P1; it writes rows to ``digest_pending`` instead. The
    guarantee is the same (each new message produces exactly one new
    actionable record, re-syncing never duplicates, legacy ids never
    appear) but we check the digest queue, not the ``events`` list.
    """
    from mailops.config import load_settings, save_settings
    from mailops.db import list_undelivered_digest_rows

    s = load_settings()
    s["digest_initial_sync_silent"] = False
    save_settings(s)

    _seed_legacy_rows()
    _live(fake_him)
    events: list[dict] = []

    async def notify(ev):
        events.append(ev)

    first = _run(process_account(fake_him, GMAIL, "growthpilotforyou@gmail.com", notify))
    assert {i.msg_id for i in first} == {"1", "2", "3"}
    # No per-mail Telegram sends on the automatic path.
    assert events == []
    # The P0 + P1 rows live in the digest queue (P3 '3' does not).
    rows = list_undelivered_digest_rows(
        account_ids=[GMAIL], max_priority=1
    )
    msg_ids = {r["msg_id"] for r in rows}
    assert msg_ids == {"1", "2"}
    assert not any(mid in msg_ids for mid in LEGACY_IDS)

    second = _run(process_account(fake_him, GMAIL, "growthpilotforyou@gmail.com", notify))
    assert second == []
    # Still exactly two digest rows after the second pass - no duplicates.
    rows2 = list_undelivered_digest_rows(account_ids=[GMAIL], max_priority=1)
    assert {r["msg_id"] for r in rows2} == {"1", "2"}
    for mid in LEGACY_IDS:
        assert is_message_processed(GMAIL, mid)


def test_legacy_rows_are_inert_when_the_account_organizes_physically(gmail_profile, fake_him, monkeypatch):
    """Physical view (organization on, test mode off) must ignore them too."""
    monkeypatch.setenv("MAILOPS_TEST_MODE", "0")
    for mid, box in LEGACY_BOX.items():
        set_message_location("info", mid, box, subject="old", sender="o@x.example", priority=2)
    fake_him.add_message("info", "INBOX", "9", subject="whatever", sender="z@ext.example", uid=9)
    set_message_location("info", "9", "INBOX.P2-Normal", subject="whatever", sender="z@ext.example", priority=2)
    counts = {c.account_id: c for c in collect_counts(force=True)}["info"]
    assert (counts.p0, counts.p1, counts.p2, counts.p3) == (0, 0, 1, 0)
    assert [it["msg_id"] for it in collect_bucket(2, "info")] == ["9"]


def test_legacy_rows_are_left_in_place_by_a_full_pass(gmail_profile, fake_him):
    """Nothing in the pipeline deletes or rewrites the historical rows."""
    _seed_legacy_rows()
    snapshot = {k: dict(v) for k, v in get_message_locations(GMAIL).items() if k in LEGACY_IDS}
    _live(fake_him)
    _run(process_account(fake_him, GMAIL, "growthpilotforyou@gmail.com", None))
    collect_counts(force=True)
    after = get_message_locations(GMAIL)
    for mid in LEGACY_IDS:
        assert after[mid] == snapshot[mid]
    with db() as conn:
        n = conn.execute("select count(*) from notified where key like 'gmail:%'").fetchone()[0]
    assert n >= 8
