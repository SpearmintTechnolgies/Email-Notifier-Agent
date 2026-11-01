"""Secrets must never reach logs, exception text or diagnostics."""
from __future__ import annotations

import importlib.util
import io
import logging
from pathlib import Path

import pytest

from mailops import paths
from mailops.redact import REDACTED, RedactingFormatter, install, redact_text

# Obviously fake, but shaped exactly like real credentials.
TOKEN = "1234567890:AAFakeFakeFakeFakeFakeFakeFakeFake_-x9"
OTHER = "9876543210:BBdifferentDIFFERENTdifferentDIFF-_1"
ACCESS = "ya29.a0AfakeAccessTokenValue-_123"
REFRESH = "1//0gFakeRefreshTokenValue_abcdefghij-KLM"
CLIENT_SECRET = "GOCSPX-FakeClientSecret_12345"


@pytest.mark.parametrize(
    "text",
    [
        f"POST https://api.telegram.org/bot{TOKEN}/getUpdates \"HTTP/1.1 200 OK\"",
        f"HTTP Request: GET https://api.telegram.org/bot{OTHER}/getMe",
        f"Conflict while calling /bot{TOKEN}/getUpdates",
        f"token is {TOKEN} ok",
        f"Authorization failed for {ACCESS}",
        f"refresh_token: {REFRESH}",
        f"client_secret={CLIENT_SECRET}&grant_type=refresh_token",
        f"secret {CLIENT_SECRET}",
        f'{{"access_token": "{ACCESS}", "x": 1}}',
    ],
)
def test_known_secret_shapes_are_redacted(text):
    out = redact_text(text)
    for secret in (TOKEN, OTHER, ACCESS, REFRESH, CLIENT_SECRET):
        assert secret not in out, out
    assert REDACTED in out


def test_ordinary_text_untouched():
    msg = "mailops: home_channels=[8925736576, -5531260744] allowed=[8925736576, 5337250772]"
    assert redact_text(msg) == msg
    assert redact_text("notification to chat -5531260744 failed: Chat not found") != ""
    assert "Chat not found" in redact_text("notification to chat -5531260744 failed: Chat not found")


def test_literal_env_secret_redacted_even_in_unusual_format(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "odd-format-secret-value-123")
    assert "odd-format-secret-value-123" not in redact_text("boom odd-format-secret-value-123 boom")
    assert "pw-extra-secret-1" not in redact_text("x pw-extra-secret-1", extra=("pw-extra-secret-1",))


def _logger_with_buffer(name):
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    lg = logging.getLogger(name)
    lg.handlers = [handler]
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    return lg, buf


def test_installed_redaction_scrubs_messages_args_and_tracebacks():
    lg, buf = _logger_with_buffer("redact-test")
    install(lg)
    lg.info("plain %s", f"https://api.telegram.org/bot{TOKEN}/sendMessage")
    lg.warning(f"inline {TOKEN}")
    try:
        raise RuntimeError(f"Bad Request calling https://api.telegram.org/bot{TOKEN}/getMe")
    except RuntimeError:
        lg.exception("handler failed")  # traceback text carries the URL
    out = buf.getvalue()
    assert TOKEN not in out
    assert "RuntimeError" in out and "handler failed" in out  # still useful
    assert REDACTED in out


def test_exception_chain_is_redacted():
    lg, buf = _logger_with_buffer("redact-chain")
    install(lg)
    try:
        try:
            raise ValueError(f"inner {TOKEN}")
        except ValueError as inner:
            raise RuntimeError("outer failure") from inner
    except RuntimeError:
        lg.exception("chained")
    assert TOKEN not in buf.getvalue()


def test_http_client_loggers_quiet_after_install():
    lg, _ = _logger_with_buffer("redact-http")
    install(lg)
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING


def test_formatter_standalone():
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, f"x {TOKEN}", (), None)
    assert TOKEN not in RedactingFormatter("%(message)s").format(rec)


# --- scripts ------------------------------------------------------------------------
def _load_script(name):
    path = Path(paths.MAILOPS_ROOT) / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_replace_token_line_changes_only_that_line_and_keeps_crlf():
    mod = _load_script("set_telegram_token")
    original = (
        "# header\r\nTELEGRAM_BOT_TOKEN=old-token\r\nTELEGRAM_ALLOWED_USERS= 1,2\r\n"
        "TELEGRAM_HOME_CHANNEL=1,-5\r\nMAILOPS_TEST_MODE=1\r\n"
    )
    new, n = mod.replace_token_line(original, TOKEN)
    assert n == 1
    assert new == original.replace("old-token", TOKEN)
    assert new.count("\r\n") == original.count("\r\n")
    # LF-only files stay LF-only, last line without newline works too
    new2, n2 = mod.replace_token_line("A=1\nTELEGRAM_BOT_TOKEN=x", TOKEN)
    assert n2 == 1 and new2 == f"A=1\nTELEGRAM_BOT_TOKEN={TOKEN}"
    assert mod.replace_token_line("A=1\n", TOKEN)[1] == 0


def test_token_format_check():
    mod = _load_script("set_telegram_token")
    assert mod.looks_like_token(TOKEN)
    for bad in ("", "abc", "123:short", "notdigits:AAFakeFakeFakeFakeFakeFakeFakeFake_-x9", TOKEN + " "):
        assert not mod.looks_like_token(bad)


def test_set_token_script_refuses_without_tty(capsys, monkeypatch):
    mod = _load_script("set_telegram_token")
    monkeypatch.setattr(mod.getpass, "getpass", lambda *a, **k: pytest.fail("must not prompt without a TTY"))
    assert mod.main() == 2
    assert "interactive terminal" in capsys.readouterr().out


def test_get_me_error_text_is_redacted(monkeypatch):
    mod = _load_script("set_telegram_token")

    def boom(*a, **k):
        raise OSError(f"cannot reach https://api.telegram.org/bot{TOKEN}/getMe")

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    res = mod.get_me(TOKEN)
    assert not res["ok"] and TOKEN not in res["description"]
