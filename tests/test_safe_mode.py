from __future__ import annotations

from mailops.himalaya_client import Himalaya
from mailops.safe_mode import guard_mutation
from mailops.safe_mode import test_mode_enabled as _mode_on


def test_env_var_enables_test_mode(monkeypatch):
    monkeypatch.setenv("MAILOPS_TEST_MODE", "1")
    assert _mode_on() is True
    monkeypatch.setenv("MAILOPS_TEST_MODE", "0")
    # falls back to settings (default False)
    assert _mode_on() is False


def test_settings_flag_enables_test_mode(tmp_profile, monkeypatch):
    monkeypatch.delenv("MAILOPS_TEST_MODE", raising=False)
    from mailops.config import load_settings, save_settings

    s = load_settings()
    s["test_mode"] = True
    save_settings(s)
    assert _mode_on() is True


def test_mutations_blocked_in_test_mode(tmp_profile, monkeypatch):
    monkeypatch.setenv("MAILOPS_TEST_MODE", "1")
    him = Himalaya()
    assert him.move("sales", "1", "INBOX.P0-Critical", "INBOX").ok
    assert him.copy("sales", "1", "INBOX.Tag.Support", "INBOX").ok
    assert him.flag_add("sales", "1").ok
    assert him.flag_remove("sales", "1").ok
    assert him.create_mailbox("sales", "INBOX.X").ok
    # Sends need explicit approval for every email; with it, test mode still
    # turns the send into a logged no-op.
    r = him.reply_send("sales", "1", "body", "INBOX", approved=True)
    assert r.ok and "test-mode" in r.stdout
    r2 = him.send_raw("sales", "raw", approved=True)
    assert r2.ok and "test-mode" in r2.stdout
    # Without approval the send is refused outright (not even simulated).
    assert not him.reply_send("sales", "1", "body", "INBOX").ok
    assert not him.send_raw("sales", "raw").ok


def test_guard_mutation_blocks_and_allows(monkeypatch):
    monkeypatch.setenv("MAILOPS_TEST_MODE", "1")
    assert guard_mutation("move", "x") is True
    monkeypatch.setenv("MAILOPS_TEST_MODE", "0")
    assert guard_mutation("move", "x") is False

