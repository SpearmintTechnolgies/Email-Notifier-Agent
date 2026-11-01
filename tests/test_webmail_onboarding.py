"""Webmail (password IMAP/SMTP) onboarding: validation, secure storage,
rollback, preservation of existing accounts, and no-send validation."""
from __future__ import annotations

import json
import tomllib

import pytest

import mailops.paths as paths
from mailops import webmail_onboarding as wm
from mailops.himalaya_client import RunResult

GMAIL_BLOCK = """[accounts.gmail]
default = true
email = "growthpilotforyou@gmail.com"

[accounts.gmail.imap]
server = "imap.gmail.com"

[accounts.gmail.imap.sasl.xoauth2]
username = "growthpilotforyou@gmail.com"
token.command = ["C:/py/python.exe", "-m", "mailops", "get-token", "gmail"]
"""
PASSWORD = "S3cret-pass-do-not-leak!"


class FakeCheckHim:
    """Stands in for Himalaya: records args, returns a canned check report."""

    def __init__(self, imap="OK", smtp="OK", code=0):
        self.calls: list[list[str]] = []
        self.imap, self.smtp, self.code = imap, smtp, code

    def run(self, args, timeout=60, input_text=None):
        self.calls.append(list(args))
        return RunResult(
            self.code,
            f"Account: {args[1]}\n  imap: {self.imap}\n  smtp: {self.smtp}\n",
            "",
        )


@pytest.fixture()
def profile(tmp_profile, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "SECRETS_DIR", tmp_path / "secrets")
    monkeypatch.setattr(wm, "_restrict_to_current_user", lambda p: None)
    paths.CONFIG_TOML.write_text(GMAIL_BLOCK, encoding="utf-8")
    paths.ACCOUNTS_JSON.write_text(
        json.dumps(
            [{"id": "gmail", "email": "growthpilotforyou@gmail.com", "display_name": "g", "default": True}]
        ),
        encoding="utf-8",
    )
    return tmp_profile


def spec(**kw):
    base = dict(
        account_id="coinography",
        email="ads@coinography.example",
        imap_host="mail.coinography.example",
        smtp_host="mail.coinography.example",
    )
    base.update(kw)
    return wm.WebmailSpec(**base)


# --- validation ---------------------------------------------------------------
def test_valid_spec_has_no_problems():
    assert wm.validate_spec(spec()) == []


def test_hosts_are_required_not_guessed():
    s = spec(imap_host="", smtp_host="")
    problems = " ".join(wm.validate_spec(s))
    assert "IMAP host is required" in problems and "SMTP host is required" in problems


@pytest.mark.parametrize(
    "kw",
    [
        {"imap_host": "imaps://mail.example.com"},
        {"imap_host": "mail.example.com:993"},
        {"smtp_host": "mail example.com"},
        {"smtp_host": 'x"; evil = "1'},
        {"imap_port": 0},
        {"smtp_port": 70000},
        {"account_id": "../etc"},
        {"account_id": "Has Space"},
        {"account_id": ""},
        {"email": "not-an-email"},
        {"email": 'a"b@example.com'},
        {"display_name": 'Bad "name"'},
        {"imap_user": 'u"ser'},
        {"security": "starttls"},
    ],
)
def test_invalid_specs_are_rejected(kw):
    assert wm.validate_spec(spec(**kw)), kw


def test_username_defaults_to_email_and_smtp_follows_imap():
    s = spec(imap_user="login@x.example")
    assert s.imap_user == "login@x.example" and s.smtp_user == "login@x.example"
    assert spec().imap_user == "ads@coinography.example"


# --- rendering ------------------------------------------------------------------
def test_rendered_block_is_valid_toml_without_password():
    block = wm.render_account_block(spec())
    data = tomllib.loads(block)["accounts"]["coinography"]
    assert data["imap"]["server"] == "mail.coinography.example:993"
    assert data["smtp"]["server"] == "mail.coinography.example:465"
    plain = data["imap"]["sasl"]["plain"]
    assert plain["authcid"] == "ads@coinography.example"
    assert plain["passwd"]["command"][-3:] == ["get-secret", "coinography", "imap"]
    assert data["smtp"]["sasl"]["plain"]["passwd"]["command"][-1] == "smtp"
    assert "raw" not in plain["passwd"]
    assert data["default"] is False


# --- add / rollback ---------------------------------------------------------------
def test_add_account_success_preserves_existing_and_registers(profile):
    him = FakeCheckHim()
    res = wm.add_account(spec(), PASSWORD, him=him, now=1_800_000_000)
    assert res.ok and res.imap_ok and res.smtp_ok

    cfg = paths.CONFIG_TOML.read_text(encoding="utf-8")
    assert cfg.startswith(GMAIL_BLOCK)  # existing Gmail block untouched, byte for byte
    parsed = tomllib.loads(cfg)["accounts"]
    assert set(parsed) == {"gmail", "coinography"}
    assert parsed["gmail"]["default"] is True and parsed["coinography"]["default"] is False

    rows = json.loads(paths.ACCOUNTS_JSON.read_text(encoding="utf-8"))
    assert [r["id"] for r in rows] == ["gmail", "coinography"]
    assert rows[0]["default"] is True and rows[1]["default"] is False

    # validated with the handshake-only command, never a send
    assert him.calls == [["-a", "coinography", "account", "check"]]
    # password only in the local secrets files
    assert wm.load_secret("coinography", "imap") == PASSWORD
    assert wm.load_secret("coinography", "smtp") == PASSWORD
    assert PASSWORD not in cfg
    assert PASSWORD not in paths.ACCOUNTS_JSON.read_text(encoding="utf-8")
    # timestamped backups of both files, with the pre-change content
    backups = sorted(p.name for p in paths.HIMALAYA_DIR.glob("*.bak-*"))
    assert len(backups) == 2
    cfg_bak = next(paths.HIMALAYA_DIR.glob("config.toml.bak-*"))
    assert cfg_bak.read_text(encoding="utf-8") == GMAIL_BLOCK
    assert PASSWORD not in "".join(p.read_text(encoding="utf-8") for p in paths.HIMALAYA_DIR.iterdir())


def test_failed_validation_rolls_everything_back(profile):
    before_cfg = paths.CONFIG_TOML.read_text(encoding="utf-8")
    before_acc = paths.ACCOUNTS_JSON.read_text(encoding="utf-8")
    him = FakeCheckHim(imap="FAIL (authentication failed)", smtp="OK", code=1)
    res = wm.add_account(spec(), PASSWORD, him=him)
    assert not res.ok and not res.imap_ok and res.smtp_ok
    assert res.detail["imap"] == "authentication failed"
    assert paths.CONFIG_TOML.read_text(encoding="utf-8") == before_cfg
    assert paths.ACCOUNTS_JSON.read_text(encoding="utf-8") == before_acc
    assert wm.load_secret("coinography", "imap") == ""
    assert wm.load_secret("coinography", "smtp") == ""


def test_exception_during_validation_rolls_back(profile):
    class Boom:
        def run(self, *a, **k):
            raise RuntimeError("himalaya exploded")

    before_cfg = paths.CONFIG_TOML.read_text(encoding="utf-8")
    with pytest.raises(RuntimeError):
        wm.add_account(spec(), PASSWORD, him=Boom())
    assert paths.CONFIG_TOML.read_text(encoding="utf-8") == before_cfg
    assert wm.load_secret("coinography", "imap") == ""
    assert "coinography" not in paths.ACCOUNTS_JSON.read_text(encoding="utf-8")


def test_unregistered_until_validated(profile):
    """The sync loop reads accounts.json; it must not see the account while
    validation is pending or after it failed."""
    seen = {}

    class Spy(FakeCheckHim):
        def run(self, args, timeout=60, input_text=None):
            seen["accounts_during_check"] = paths.ACCOUNTS_JSON.read_text(encoding="utf-8")
            return super().run(args, timeout, input_text)

    wm.add_account(spec(), PASSWORD, him=Spy())
    assert "coinography" not in seen["accounts_during_check"]
    assert "coinography" in paths.ACCOUNTS_JSON.read_text(encoding="utf-8")


def test_duplicate_id_or_email_refused_and_existing_untouched(profile):
    wm.add_account(spec(), PASSWORD, him=FakeCheckHim())
    after = paths.CONFIG_TOML.read_text(encoding="utf-8")
    with pytest.raises(wm.OnboardingError, match="already exists"):
        wm.add_account(spec(), PASSWORD, him=FakeCheckHim())
    with pytest.raises(wm.OnboardingError, match="already registered"):
        wm.add_account(spec(account_id="other", email="ads@coinography.example"), PASSWORD, him=FakeCheckHim())
    with pytest.raises(wm.OnboardingError, match="Gmail is disabled"):
        wm.add_account(spec(account_id="gmail", email="me@gmail.com"), PASSWORD, him=FakeCheckHim())
    assert paths.CONFIG_TOML.read_text(encoding="utf-8") == after
    assert after.startswith(GMAIL_BLOCK)


def test_second_account_after_first_keeps_both(profile):
    wm.add_account(spec(), PASSWORD, him=FakeCheckHim())
    wm.add_account(
        spec(account_id="memecoinist", email="ads@memecoinist.example", imap_host="mail.memecoinist.example", smtp_host="mail.memecoinist.example"),
        "another-pass",
        him=FakeCheckHim(),
    )
    ids = [r["id"] for r in json.loads(paths.ACCOUNTS_JSON.read_text(encoding="utf-8"))]
    assert ids == ["gmail", "coinography", "memecoinist"]
    assert set(tomllib.loads(paths.CONFIG_TOML.read_text(encoding="utf-8"))["accounts"]) == {
        "gmail", "coinography", "memecoinist"
    }
    assert wm.load_secret("memecoinist", "imap") == "another-pass"


def test_empty_password_refused(profile):
    with pytest.raises(wm.OnboardingError):
        wm.add_account(spec(), "", him=FakeCheckHim())
    assert paths.CONFIG_TOML.read_text(encoding="utf-8") == GMAIL_BLOCK


def test_invalid_accounts_json_is_not_overwritten(profile):
    paths.ACCOUNTS_JSON.write_text("{ not json", encoding="utf-8")
    with pytest.raises(wm.OnboardingError, match="not valid JSON"):
        wm.add_account(spec(), PASSWORD, him=FakeCheckHim())
    assert paths.ACCOUNTS_JSON.read_text(encoding="utf-8") == "{ not json"


# --- check parsing / redaction ---------------------------------------------------
def test_parse_check_output_variants():
    ok = wm.parse_check_output("Account: x\n  imap: OK\n  smtp: OK\n")
    assert ok.ok
    half = wm.parse_check_output("Account: x\n  imap: OK\n  smtp: FAIL (535 auth)\n", code=1)
    assert not half.ok and half.imap_ok and not half.smtp_ok and half.detail["smtp"] == "535 auth"
    junk = wm.parse_check_output("", "Error: no configuration found", 1)
    assert not junk.ok and "no configuration" in junk.raw_error
    # exit code non-zero with OK lines is still not OK
    assert not wm.parse_check_output("  imap: OK\n  smtp: OK\n", code=1).ok


def test_check_account_redacts_password_and_never_sends():
    class Leaky(FakeCheckHim):
        def run(self, args, timeout=60, input_text=None):
            self.calls.append(list(args))
            return RunResult(1, f"  imap: FAIL (bad login {PASSWORD})\n  smtp: OK\n", "")

    him = Leaky()
    res = wm.check_account("coinography", him, secrets=(PASSWORD,))
    assert PASSWORD not in json.dumps(res.detail)
    assert all("send" not in a and "message" not in a for a in him.calls[0])


# --- secrets ---------------------------------------------------------------------
def test_secret_path_rejects_traversal(profile):
    for bad in ("../x", "a/b", "", "UP", "x y"):
        with pytest.raises(wm.OnboardingError):
            wm.secret_path(bad, "imap")
    with pytest.raises(wm.OnboardingError):
        wm.secret_path("ok", "../imap")


def test_get_secret_command_outputs_only_the_secret(profile, capsys):
    wm.store_secret("coinography", "imap", PASSWORD)
    assert wm.cli_get_secret("coinography", "imap") == 0
    assert capsys.readouterr().out == PASSWORD  # no newline, no decoration
    assert wm.cli_get_secret("coinography", "smtp") == 1  # missing
    assert wm.cli_get_secret("../x", "imap") == 2  # rejected


def test_gitignore_covers_secrets_and_backups():
    text = (paths.MAILOPS_ROOT / ".gitignore").read_text(encoding="utf-8")
    for needle in ("secrets/", "backups/", "tokens/", ".env"):
        assert needle in text


# --- CLI -------------------------------------------------------------------------
def test_cli_add_refuses_without_interactive_terminal(profile, capsys):
    # pytest stdin is not a TTY: passwords must never come from pipes/args.
    assert wm.cli_add(["x", "a@b.example", "--imap-host", "mail.b.example", "--smtp-host", "mail.b.example"]) == 2
    assert "interactive terminal" in capsys.readouterr().out
    assert PASSWORD not in paths.CONFIG_TOML.read_text(encoding="utf-8")


def test_main_dispatches_webmail_commands(profile, monkeypatch):
    import mailops.main as main_mod

    calls = []
    monkeypatch.setattr(wm, "cli_add", lambda argv: calls.append(("add", argv)) or 0)
    monkeypatch.setattr(wm, "cli_check", lambda a: calls.append(("check", a)) or 0)
    assert main_mod.main(["webmail-add", "id", "e@x.example", "--imap-host", "h.example"]) == 0
    assert main_mod.main(["webmail-check", "id"]) == 0
    assert main_mod.main(["webmail-check"]) == 2
    assert calls == [("add", ["id", "e@x.example", "--imap-host", "h.example"]), ("check", "id")]


# --- byte-exact preservation (CRLF Gmail config) -----------------------------------
def _crlf_profile(profile):
    raw = GMAIL_BLOCK.replace("\n", "\r\n").encode("utf-8")
    paths.CONFIG_TOML.write_bytes(raw)
    return raw


def test_failed_validation_restores_config_byte_for_byte_with_crlf(profile):
    raw = _crlf_profile(profile)
    res = wm.add_account(spec(), PASSWORD, him=FakeCheckHim(imap="FAIL", smtp="FAIL", code=1))
    assert not res.ok
    assert paths.CONFIG_TOML.read_bytes() == raw


def test_success_keeps_existing_crlf_blocks_byte_for_byte(profile):
    raw = _crlf_profile(profile)
    assert wm.add_account(spec(), PASSWORD, him=FakeCheckHim()).ok
    assert paths.CONFIG_TOML.read_bytes().startswith(raw)


# --- password rotation ---------------------------------------------------------------
def _onboard(profile):
    wm.add_account(spec(), PASSWORD, him=FakeCheckHim())
    return paths.CONFIG_TOML.read_bytes(), paths.ACCOUNTS_JSON.read_bytes()


def test_update_credentials_rotates_secret_without_touching_config(profile):
    cfg, acc = _onboard(profile)
    him = FakeCheckHim()
    res = wm.update_credentials("coinography", "N3w-pass", him=him)
    assert res.ok
    assert wm.load_secret("coinography", "imap") == "N3w-pass"
    assert wm.load_secret("coinography", "smtp") == "N3w-pass"
    assert paths.CONFIG_TOML.read_bytes() == cfg and paths.ACCOUNTS_JSON.read_bytes() == acc
    assert him.calls == [["-a", "coinography", "account", "check"]]  # handshake only


def test_update_credentials_failure_restores_previous_password(profile):
    cfg, acc = _onboard(profile)
    res = wm.update_credentials(
        "coinography", "typo-pass", him=FakeCheckHim(imap="FAIL (auth)", smtp="FAIL (auth)", code=1)
    )
    assert not res.ok
    assert wm.load_secret("coinography", "imap") == PASSWORD
    assert wm.load_secret("coinography", "smtp") == PASSWORD
    assert paths.CONFIG_TOML.read_bytes() == cfg and paths.ACCOUNTS_JSON.read_bytes() == acc


def test_update_credentials_exception_restores_previous_password(profile):
    _onboard(profile)

    class Boom:
        def run(self, *a, **k):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        wm.update_credentials("coinography", "x-pass", him=Boom())
    assert wm.load_secret("coinography", "imap") == PASSWORD


def test_update_credentials_separate_smtp_password(profile):
    _onboard(profile)
    assert wm.update_credentials("coinography", "imap-pw", "smtp-pw", him=FakeCheckHim()).ok
    assert wm.load_secret("coinography", "imap") == "imap-pw"
    assert wm.load_secret("coinography", "smtp") == "smtp-pw"


def test_update_credentials_refuses_gmail_and_unknown_and_empty(profile):
    _onboard(profile)
    for bad in ("gmail", "nope", "../x"):
        with pytest.raises(wm.OnboardingError):
            wm.update_credentials(bad, "pw", him=FakeCheckHim())
    with pytest.raises(wm.OnboardingError):
        wm.update_credentials("coinography", "", him=FakeCheckHim())
    assert wm.load_secret("coinography", "imap") == PASSWORD
    assert not (paths.SECRETS_DIR / "gmail.imap.secret").exists()


def test_cli_set_password_refuses_without_interactive_terminal(profile, capsys):
    _onboard(profile)
    assert wm.cli_set_password(["coinography"]) == 2
    assert "interactive terminal" in capsys.readouterr().out
    assert wm.load_secret("coinography", "imap") == PASSWORD


def test_main_dispatches_set_password(profile, monkeypatch):
    import mailops.main as main_mod

    calls = []
    monkeypatch.setattr(wm, "cli_set_password", lambda argv: calls.append(argv) or 0)
    assert main_mod.main(["webmail-set-password", "coinography"]) == 0
    assert calls == [["coinography"]]


def test_gmail_cannot_be_onboarded():
    problems = " ".join(wm.validate_spec(spec(account_id="gmail", email="me@gmail.com")))
    assert "Gmail is disabled" in problems
    problems = " ".join(
        wm.validate_spec(spec(imap_host="imap.gmail.com", smtp_host="smtp.gmail.com"))
    )
    assert "Gmail is disabled" in problems


def test_add_account_appends_to_existing_allowlist_and_keeps_other_settings(profile):
    from mailops import config as cfg

    cfg.SETTINGS_JSON.write_text(
        json.dumps(
            {
                "active_accounts": ["altcoininformer"],
                "account_policies": {"altcoininformer": {"organize": False, "send": False}},
                "quiet_hours_start": 23,
                "inbox_context": "keep me",
            }
        ),
        encoding="utf-8",
    )
    assert wm.add_account(spec(), PASSWORD, him=FakeCheckHim()).ok
    data = json.loads(cfg.SETTINGS_JSON.read_text(encoding="utf-8"))
    assert data["active_accounts"] == ["altcoininformer", "coinography"]
    assert data["account_policies"]["coinography"] == {"organize": False, "send": False}
    assert data["account_policies"]["altcoininformer"] == {"organize": False, "send": False}
    assert data["quiet_hours_start"] == 23
    assert data["inbox_context"] == "keep me"


def test_failed_add_rolls_back_allowlist(profile):
    from mailops import config as cfg

    original = json.dumps(
        {"active_accounts": ["altcoininformer"], "account_policies": {"altcoininformer": {"organize": False}}}
    )
    cfg.SETTINGS_JSON.write_text(original, encoding="utf-8")
    res = wm.add_account(spec(), PASSWORD, him=FakeCheckHim(imap="FAIL", smtp="FAIL", code=1))
    assert not res.ok
    assert cfg.SETTINGS_JSON.read_text(encoding="utf-8") == original
    assert "coinography" not in paths.ACCOUNTS_JSON.read_text(encoding="utf-8")


def test_activate_account_refuses_gmail(profile):
    with pytest.raises(wm.OnboardingError, match="Gmail cannot be activated"):
        wm._activate_account("gmail")
