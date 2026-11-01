"""Credential-path regressions (mock credentials only): get-secret byte fidelity
through a real pipe, typo-protected prompts, and the independent auth probe."""
from __future__ import annotations

import base64
import importlib.util
import os
import subprocess
import sys

import pytest

import mailops.paths as paths
from mailops import webmail_onboarding as wm

TRICKY = "P@ss w0rd!#\"'\\%$&<>;=é☃ "  # punctuation, quotes, backslash, space, non-cp1252


def test_get_secret_is_byte_exact_utf8_through_a_pipe(tmp_path):
    """Himalaya reads passwd.command through a pipe. On Windows Python would
    encode with cp1252 there: crash on U+2603, wrong bytes for U+00E9."""
    home = tmp_path / "profile"
    (home / "secrets").mkdir(parents=True)
    (home / "secrets" / "acct.imap.secret").write_bytes(TRICKY.encode("utf-8"))
    env = {
        **os.environ,
        "MAILOPS_HOME": str(home),
        "PYTHONPATH": str(paths.MAILOPS_ROOT),
        "PYTHONIOENCODING": "cp1252",  # force the legacy Windows pipe encoding
    }
    cp = subprocess.run(
        [sys.executable, "-m", "mailops", "get-secret", "acct", "imap"],
        capture_output=True,
        env=env,
        cwd=str(tmp_path),
        timeout=60,
    )
    assert cp.returncode == 0
    assert cp.stdout == TRICKY.encode("utf-8")  # no newline, no trimming, no re-encoding
    assert cp.stderr == b""


def test_store_and_load_roundtrip_preserves_special_characters(tmp_path, monkeypatch):
    """Encrypted-at-rest round trip: special characters, non-cp1252 glyphs
    and raw backslashes must come back byte-identical, and the on-disk
    file must NOT contain the plaintext (defence in depth)."""
    monkeypatch.setenv("MAILOPS_FORCE_SOFT_VAULT", "1")
    monkeypatch.setenv("MAILOPS_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.setattr(paths, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.setattr(wm, "_restrict_to_current_user", lambda p: None)
    from mailops import secrets_vault

    monkeypatch.setattr(secrets_vault.paths, "SECRETS_DIR", tmp_path / "secrets")
    wm.store_secret("acct", "imap", TRICKY)
    # On disk, the plaintext must be absent and the encrypted envelope must exist.
    enc_file = tmp_path / "secrets" / "acct.imap.enc"
    plain_file = tmp_path / "secrets" / "acct.imap.secret"
    assert enc_file.exists(), "encrypted envelope must be written"
    assert not plain_file.exists(), "legacy plaintext must not be left behind"
    blob = enc_file.read_bytes()
    assert TRICKY.encode("utf-8") not in blob, "ciphertext must not reveal the password"
    # Round-trip through the public API returns the exact string.
    assert wm.load_secret("acct", "imap") == TRICKY


def test_password_prompt_requires_matching_confirmation(monkeypatch):
    answers = iter(["first-pass", "first-pass-typo"])
    monkeypatch.setattr(wm.getpass, "getpass", lambda prompt="": next(answers))
    with pytest.raises(wm.OnboardingError, match="did not match"):
        wm._ask_password("IMAP password")
    answers = iter(["same", "same"])
    monkeypatch.setattr(wm.getpass, "getpass", lambda prompt="": next(answers))
    assert wm._ask_password("IMAP password") == "same"
    monkeypatch.setattr(wm.getpass, "getpass", lambda prompt="": "")
    with pytest.raises(wm.OnboardingError, match="empty"):
        wm._ask_password("IMAP password")


# --- independent probe ------------------------------------------------------------
def _load_probe():
    path = paths.MAILOPS_ROOT / "scripts" / "webmail_auth_probe.py"
    spec = importlib.util.spec_from_file_location("webmail_auth_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_probe_smtp_sends_only_one_auth_plain_and_no_mail(monkeypatch):
    probe = _load_probe()
    sent: list[tuple[str, str]] = []

    class FakeSMTP:
        esmtp_features = {"auth": "PLAIN LOGIN"}

        def __init__(self, *a, **k):
            pass

        def ehlo(self):
            pass

        def docmd(self, cmd, arg=""):
            sent.append((cmd, arg))
            return 535, b"5.7.8 Error: authentication failed: " + TRICKY.encode()

        def quit(self):
            sent.append(("QUIT", ""))

        def __getattr__(self, name):  # any mail-sending method must never be touched
            raise AssertionError(f"probe must not call {name}")

    monkeypatch.setattr(probe.smtplib, "SMTP_SSL", FakeSMTP)
    ok, detail = probe.probe_smtp("smtp.example.invalid", 465, "u@example.invalid", TRICKY)
    assert not ok and detail.startswith("535")
    assert TRICKY not in detail  # server echo (if any) is scrubbed
    assert [c for c, _ in sent] == ["AUTH", "QUIT"]  # exactly one attempt, no MAIL/RCPT/DATA
    token = sent[0][1].removeprefix("PLAIN ")
    assert base64.b64decode(token) == f"\0u@example.invalid\0{TRICKY}".encode("utf-8")


def test_probe_imap_uses_plain_once_and_never_selects(monkeypatch):
    probe = _load_probe()
    calls: list[str] = []

    class FakeIMAP:
        capabilities = ("IMAP4REV1", "AUTH=PLAIN")

        def __init__(self, *a, **k):
            pass

        def authenticate(self, mech, authobject):
            calls.append(mech)
            assert authobject(b"") == f"\0u@example.invalid\0{TRICKY}"
            raise probe.imaplib.IMAP4.error("AUTHENTICATE failed.")

        def logout(self):
            calls.append("LOGOUT")

        def __getattr__(self, name):
            raise AssertionError(f"probe must not call {name}")

    monkeypatch.setattr(probe.imaplib, "IMAP4_SSL", FakeIMAP)
    ok, detail = probe.probe_imap("imap.example.invalid", 993, "u@example.invalid", TRICKY)
    assert not ok and "AUTHENTICATE failed" in detail
    assert calls == ["PLAIN", "LOGOUT"]


def test_probe_refuses_without_terminal_and_with_mismatched_entries(monkeypatch, capsys):
    probe = _load_probe()
    monkeypatch.setattr(sys, "argv", ["probe", "u@example.invalid"])
    assert probe.main() == 2  # pytest stdin is not a TTY
    assert "interactive terminal" in capsys.readouterr().out

    import io

    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr(sys, "stdout", Tty())
    answers = iter(["abc", "abd"])
    monkeypatch.setattr(probe.getpass, "getpass", lambda prompt="": next(answers))
    monkeypatch.setattr(probe, "probe_imap", lambda *a: pytest.fail("no network call on mismatch"))
    monkeypatch.setattr(probe, "probe_smtp", lambda *a: pytest.fail("no network call on mismatch"))
    assert probe.main() == 2
