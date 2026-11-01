from __future__ import annotations

import json
import urllib.parse

import pytest

from mailops.gmail_oauth import (
    MOCK_ACCESS_TOKEN,
    MOCK_REFRESH_TOKEN,
    GmailAccountSpec,
    _redact,
    build_authorization_url,
    clean_client_id,
    clean_client_secret,
    ensure_config,
    get_access_token,
    load_tokens,
    oauth_credentials_present,
    render_account_block,
    run_login,
    save_tokens,
)


def test_render_block_v2_xoauth2_and_no_password():
    block = render_account_block(
        GmailAccountSpec("gmail", "me@gmail.com"), "cid", "csec"
    )
    assert "[accounts.gmail]" in block
    assert "[accounts.gmail.imap]" in block
    assert "[accounts.gmail.imap.sasl.xoauth2]" in block
    assert 'username = "me@gmail.com"' in block
    assert "token.command" in block
    assert "get-token" in block
    assert "[accounts.gmail.smtp.sasl.xoauth2]" in block
    assert "imap.gmail.com" in block
    assert "smtp.gmail.com" in block
    # v1 obsolete fields must not be present
    assert "backend.auth" not in block
    assert "password" not in block.lower()


def test_clean_client_id_regression():
    raw_malformed = (
        'PLACEHOLDER-client-id-value-not-a-real-credential'
        '","project_id":"mailops-test","auth_uri":"https://accounts.google.com/o/oauth2/auth",'
        '"token_uri":"https://oauth2.googleapis.com/token'
    )
    cleaned = clean_client_id(raw_malformed)
    assert cleaned == "PLACEHOLDER-client-id-value-not-a-real-credential"
    assert clean_client_id('"my-client-id"') == "my-client-id"
    assert clean_client_id("simple-id") == "simple-id"


def test_clean_client_secret_regression():
    raw_secret = 'PLACEHOLDER-client-secret-not-a-real-credential","other":"field'
    cleaned = clean_client_secret(raw_secret)
    assert cleaned == "PLACEHOLDER-client-secret-not-a-real-credential"


def test_build_authorization_url_no_malformed_characters():
    raw_client_id = (
        'PLACEHOLDER-client-id-value-not-a-real-credential'
        '","project_id":"mailops-test","auth_uri":"https://accounts.google.com/o/oauth2/auth",'
        '"token_uri":"https://oauth2.googleapis.com/token'
    )
    redirect_uri = "http://127.0.0.1:8080/"
    url = build_authorization_url(raw_client_id, redirect_uri)

    # Must NOT have %22 (escaped quote) or token_uri or quotes
    assert "%22" not in url
    assert '"' not in url
    assert "token_uri" not in url
    assert "project_id" not in url

    parsed = urllib.parse.urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs["client_id"] == ["PLACEHOLDER-client-id-value-not-a-real-credential"]
    assert qs["redirect_uri"] == ["http://127.0.0.1:8080/"]
    assert qs["response_type"] == ["code"]
    assert qs["access_type"] == ["offline"]


def test_token_save_and_retrieve_in_test_mode(tmp_profile, monkeypatch):
    monkeypatch.setenv("MAILOPS_TEST_MODE", "1")
    save_tokens("acct1", {"access_token": "tok123", "refresh_token": "ref123", "expires_at": 9999999999})
    loaded = load_tokens("acct1")
    assert loaded is not None
    assert loaded["access_token"] == "tok123"

    tok = get_access_token("acct1")
    assert tok == "tok123"

    # Default fallback in test mode when no token file
    assert get_access_token("nonexistent") == "mock-access-token"


def test_ensure_config_requires_credentials(tmp_profile, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_CLIENT_ID"):
        ensure_config(GmailAccountSpec("gmail", "me@gmail.com"))


def test_ensure_config_writes_v2_config_and_accounts(tmp_profile, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csec")
    from mailops.paths import ACCOUNTS_JSON, CONFIG_TOML

    status = ensure_config(GmailAccountSpec("gmail", "me@gmail.com", default=True))
    assert "added account 'gmail'" in status
    text = CONFIG_TOML.read_text(encoding="utf-8")
    assert "[accounts.gmail]" in text
    assert "[accounts.gmail.imap.sasl.xoauth2]" in text
    assert "token.command" in text
    rows = json.loads(ACCOUNTS_JSON.read_text(encoding="utf-8"))
    ids = [r["id"] for r in rows]
    assert "gmail" in ids
    # Idempotent: second call does not duplicate
    status2 = ensure_config(GmailAccountSpec("gmail", "me@gmail.com"))
    assert "left unchanged" in status2
    assert CONFIG_TOML.read_text(encoding="utf-8").count("[accounts.gmail]") == 1


def test_oauth_credentials_present(tmp_profile, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    assert oauth_credentials_present() is False
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "y")
    assert oauth_credentials_present() is True


def test_redact_hides_tokens():
    out = _redact("access_token=abc123 refresh_token: xyz client_secret=shh")
    assert "abc123" not in out
    assert "xyz" not in out
    assert "shh" not in out


def test_expired_token_is_refreshed_even_in_test_mode(tmp_profile, monkeypatch, tmp_path):
    """Regression: the live poller runs with MAILOPS_TEST_MODE=1 (mutations
    blocked). get_access_token used to return the stored access token without
    checking expiry in test mode, so Gmail XOAUTH2 failed ('Invalid
    credentials') one hour after login. Test mode must still refresh."""
    import mailops.gmail_oauth as goa

    monkeypatch.setenv("MAILOPS_TEST_MODE", "1")
    monkeypatch.setattr(goa, "token_file", lambda aid: tmp_path / f"{aid}.json")
    save_tokens("live", {"access_token": "stale", "refresh_token": "r", "expires_at": 1})

    calls = []

    def fake_refresh(account_id, tokens):
        calls.append(account_id)
        return "fresh-token"

    monkeypatch.setattr(goa, "refresh_token", fake_refresh)
    assert get_access_token("live") == "fresh-token"
    assert calls == ["live"]

    # A still-valid token is returned as-is without a network refresh.
    save_tokens("live", {"access_token": "valid", "refresh_token": "r", "expires_at": 9999999999})
    assert get_access_token("live") == "valid"
    assert calls == ["live"]


def test_refresh_failure_falls_back_to_stored_token(tmp_profile, monkeypatch, tmp_path):
    import mailops.gmail_oauth as goa

    monkeypatch.setenv("MAILOPS_TEST_MODE", "1")
    monkeypatch.setattr(goa, "token_file", lambda aid: tmp_path / f"{aid}.json")
    save_tokens("live2", {"access_token": "old", "refresh_token": "r", "expires_at": 1})
    monkeypatch.setattr(goa, "refresh_token", lambda a, t: None)
    assert get_access_token("live2") == "old"


def test_save_tokens_refuses_to_overwrite_real_login_with_mock(tmp_profile):
    save_tokens("gmail", {"access_token": "real-access", "refresh_token": "real-refresh"})
    with pytest.raises(RuntimeError, match="mock credentials"):
        save_tokens(
            "gmail",
            {"access_token": MOCK_ACCESS_TOKEN, "refresh_token": MOCK_REFRESH_TOKEN},
        )
    loaded = load_tokens("gmail")
    assert loaded["access_token"] == "real-access"
    assert loaded["refresh_token"] == "real-refresh"


def test_run_login_does_nothing_while_gmail_is_disabled(tmp_profile):
    save_tokens("gmail", {"access_token": "real-access", "refresh_token": "real-refresh"})
    assert run_login("gmail") == 1
    assert load_tokens("gmail")["access_token"] == "real-access"


def test_run_login_in_test_mode_does_not_write_mock_tokens(tmp_profile, monkeypatch):
    import mailops.config as cfg

    monkeypatch.setattr(cfg, "GMAIL_ENABLED", True)
    monkeypatch.setenv("MAILOPS_TEST_MODE", "1")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "placeholder-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "placeholder-client-secret")
    assert run_login("gmail") == 0
    assert load_tokens("gmail") is None


def test_gmail_setup_cli_refuses_when_disabled(tmp_profile, capsys):
    from mailops.main import main

    assert main(["gmail-setup", "gmail", "me@gmail.com"]) == 1
    out = capsys.readouterr().out.lower()
    assert "disabled" in out
    assert "nothing was written" in out
