from __future__ import annotations

import asyncio
import os
from pathlib import Path

from mailops import paths


def test_no_hardcoded_developer_paths():
    # MAILOPS_ROOT must be DERIVED from this file's location, never a literal.
    # Asserting a specific directory name here would itself be a hardcoded
    # developer path and would fail on the Linux deployment (/opt/mailops), so
    # assert the invariant instead: the root is the parent of the package.
    assert paths.MAILOPS_ROOT == Path(paths.__file__).resolve().parents[1]
    assert paths.MAILOPS_ROOT.is_absolute()
    assert (paths.MAILOPS_ROOT / "mailops" / "paths.py").is_file()
    assert paths.DATA_DIR == paths.MAILOPS_ROOT / "data"

    # No absolute path is baked into paths.py except the documented
    # Windows-only himalaya binary candidates inside himalaya_bin().
    source = Path(paths.__file__).read_text(encoding="utf-8")
    body = source.split("def himalaya_bin", 1)[0]
    assert "C:" not in body and "/home/" not in body and "/opt/" not in body
    assert "Users" not in body

    # PROFILE defaults to the repo root and is overridable via MAILOPS_HOME
    # (which the Linux deployment sets to /opt/mailops).
    if not os.environ.get("MAILOPS_HOME"):
        assert paths.PROFILE == paths.MAILOPS_ROOT


def test_himalaya_bin_returns_string():
    assert isinstance(paths.himalaya_bin(), str)


def test_notify_dedupe(tmp_profile):
    from mailops.db import already_notified, init_db, mark_notified

    init_db()
    assert not already_notified("k1")
    mark_notified("k1")
    assert already_notified("k1")


def test_handle_event_new_mail_sends_once(tmp_profile, monkeypatch):
    from mailops.db import init_db
    from mailops.notify import handle_event

    init_db()
    sent = []

    class FakeBot:
        async def send_message(self, chat_id=None, text=None, reply_markup=None):
            sent.append(text)

    event = {
        "type": "new_mail",
        "key": "k-notify",
        "account_id": "sales",
        "email": "sales@example.com",
        "msg_id": "1",
        "mailbox": "INBOX.P1-Important",
        "subject": "hi",
        "sender": "a@b.co",
        "priority": 1,
    }
    asyncio.run(handle_event(FakeBot(), event))
    asyncio.run(handle_event(FakeBot(), event))  # deduped
    assert len(sent) == 1
    assert "P1 Important" in sent[0]


def test_handle_event_sync_error(tmp_profile):
    from mailops.db import init_db
    from mailops.notify import handle_event

    init_db()
    sent = []

    class FakeBot:
        async def send_message(self, chat_id=None, text=None, reply_markup=None):
            sent.append(text)

    ev = {"type": "sync_error", "key": "e1", "account_id": "sales", "email": "s@x.co"}
    asyncio.run(handle_event(FakeBot(), ev))
    assert sent and "Sync issue" in sent[0]


def test_himalaya_bin_falls_back_to_env_file(tmp_profile, monkeypatch, tmp_path):
    """Regression: a launcher that does not inherit HIMALAYA_BIN from a shell
    (Task Scheduler, double-click) used to resolve a bare 'himalaya' that was
    not on PATH, so every mailbox read silently returned nothing."""
    fake_bin = tmp_path / "himalaya.exe"
    fake_bin.write_text("", encoding="utf-8")
    monkeypatch.delenv("HIMALAYA_BIN", raising=False)
    monkeypatch.setattr(paths.shutil, "which", lambda name: None)
    env = tmp_profile / ".env"
    env.write_text(env.read_text(encoding="utf-8") + f"HIMALAYA_BIN={fake_bin}\n", encoding="utf-8")
    assert paths.himalaya_bin() == str(fake_bin)
    # A commented-out line must not count.
    env.write_text(f"# HIMALAYA_BIN={fake_bin}\n", encoding="utf-8")
    assert paths.himalaya_bin() != str(fake_bin)
