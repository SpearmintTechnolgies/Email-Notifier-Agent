"""Gmail OAuth2 setup for Himalaya v2 backends.

Himalaya v2 requires external SASL XOAUTH2 token management.
This module:
  1. Opens browser to Google OAuth consent screen with a clean, valid URL.
  2. Receives authorization code via local loopback redirect.
  3. Exchanges code for access and refresh tokens.
  4. Stores machine-local tokens in TOKEN_DIR (gitignored).
  5. Provides token refresh on demand via `get_access_token` and `python -m mailops get-token <account>`.
  6. Configures Himalaya v2 accounts with SASL XOAUTH2 token commands.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from .config import load_dotenv
from .paths import ACCOUNTS_JSON, CONFIG_TOML, HIMALAYA_DIR, TOKEN_DIR, himalaya_bin

log = logging.getLogger("mailops.gmail_oauth")


MOCK_ACCESS_TOKEN = "mock-access-token"
MOCK_REFRESH_TOKEN = "mock-refresh-token"


def test_mode_enabled() -> bool:
    # Same resolution as every other command: env var, then .env, then settings.
    from .safe_mode import test_mode_enabled as _test_mode

    return _test_mode()


def is_mock_token(tokens: dict | None) -> bool:
    """True for placeholder credentials that never came from Google."""
    return bool(tokens) and (
        tokens.get("access_token") == MOCK_ACCESS_TOKEN
        or tokens.get("refresh_token") == MOCK_REFRESH_TOKEN
    )


GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_SMTP_HOST = "smtp.gmail.com"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://mail.google.com/"

_ACCOUNT_BLOCK_RE = re.compile(r"^\[accounts\.(?P<name>[^\]]+)\]\s*$", re.M)


@dataclass
class GmailAccountSpec:
    account_id: str
    email: str
    display_name: str = ""
    default: bool = False


def clean_client_id(raw: str) -> str:
    """Sanitize client ID from pasted JSON, escaped quotes, or trailing metadata."""
    if not raw:
        return ""
    val = raw.strip().strip('"').strip("'")
    m = re.search(r'["\']?client_id["\']?\s*:\s*["\']([^"\']+)["\']', val)
    if m:
        return m.group(1).strip()
    m = re.search(r'([0-9a-zA-Z_\-]+\.apps\.googleusercontent\.com)', val)
    if m:
        return m.group(1).strip()
    if '","' in val:
        val = val.split('","', 1)[0]
    return val.strip().strip('"').strip("'")


def clean_client_secret(raw: str) -> str:
    """Sanitize client secret from pasted JSON or trailing metadata."""
    if not raw:
        return ""
    val = raw.strip().strip('"').strip("'")
    m = re.search(r'["\']?client_secret["\']?\s*:\s*["\']([^"\']+)["\']', val)
    if m:
        return m.group(1).strip()
    m = re.search(r'(GOCSPX-[0-9a-zA-Z_\-]+)', val)
    if m:
        return m.group(1).strip()
    if '","' in val:
        val = val.split('","', 1)[0]
    return val.strip().strip('"').strip("'")


def build_authorization_url(client_id: str, redirect_uri: str, state: str = "") -> str:
    """Construct a clean, valid Google OAuth authorization URL without corruption."""
    cid = clean_client_id(client_id)
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    if state:
        params["state"] = state
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _oauth_credentials() -> tuple[str, str]:
    import os

    env = load_dotenv()
    raw_id = os.environ.get("GOOGLE_CLIENT_ID") or env.get("GOOGLE_CLIENT_ID") or ""
    raw_secret = os.environ.get("GOOGLE_CLIENT_SECRET") or env.get("GOOGLE_CLIENT_SECRET") or ""
    return clean_client_id(raw_id), clean_client_secret(raw_secret)


def oauth_credentials_present() -> bool:
    cid, csec = _oauth_credentials()
    return bool(cid and csec)


def render_account_block(spec: GmailAccountSpec, client_id: str = "", client_secret: str = "") -> str:
    """Himalaya v2 config block for a Gmail account using SASL XOAUTH2 token command."""
    display = spec.display_name or spec.email.split("@", 1)[0]
    return f"""
[accounts.{spec.account_id}]
default = {str(spec.default).lower()}
email = "{spec.email}"
display-name = "{display}"

[accounts.{spec.account_id}.imap]
server = "{GMAIL_IMAP_HOST}"

[accounts.{spec.account_id}.imap.sasl.xoauth2]
username = "{spec.email}"
token.command = ["python", "-m", "mailops", "get-token", "{spec.account_id}"]

[accounts.{spec.account_id}.smtp]
server = "{GMAIL_SMTP_HOST}"

[accounts.{spec.account_id}.smtp.sasl.xoauth2]
username = "{spec.email}"
token.command = ["python", "-m", "mailops", "get-token", "{spec.account_id}"]
""".lstrip("\n")


def token_file(account_id: str) -> Path:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    return TOKEN_DIR / f"{account_id}.json"


def save_tokens(account_id: str, tokens: dict) -> None:
    path = token_file(account_id)
    if is_mock_token(tokens):
        existing = load_tokens(account_id)
        if existing is not None and not is_mock_token(existing):
            # Never let placeholder credentials replace a real stored login.
            raise RuntimeError(
                f"refusing to overwrite the real stored token for '{account_id}' with mock credentials"
            )
    path.write_text(json.dumps(tokens, indent=2) + "\n", encoding="utf-8")


def load_tokens(account_id: str) -> dict | None:
    path = token_file(account_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def refresh_token(account_id: str, tokens: dict) -> str | None:
    refresh_token_val = tokens.get("refresh_token")
    if not refresh_token_val:
        return None
    client_id, client_secret = _oauth_credentials()
    if not client_id or not client_secret:
        return None

    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token_val,
        "grant_type": "refresh_token",
    }).encode("utf-8")
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            access_token = body.get("access_token")
            if access_token:
                tokens["access_token"] = access_token
                expires_in = body.get("expires_in", 3600)
                tokens["expires_at"] = time.time() + float(expires_in)
                save_tokens(account_id, tokens)
                return access_token
    except Exception as e:
        log.warning(f"Failed to refresh token for {account_id}: {e}")
        return None
    return None


def get_access_token(account_id: str) -> str:
    """Retrieve an active access token for account_id, refreshing if expired."""
    toks = load_tokens(account_id)
    if not toks:
        # Test mode with no stored login: placeholder so unit tests never hit
        # the network. Real accounts always have a token file.
        return "mock-access-token" if test_mode_enabled() else ""
    # NOTE: test mode must NOT short-circuit here. MAILOPS_TEST_MODE only
    # blocks mailbox *mutations*; the live poller runs in test mode and needs
    # a refreshed token once the 1-hour access token expires. Refreshing is a
    # read-only auth operation.
    expires_at = toks.get("expires_at", 0)
    if time.time() < (expires_at - 60) and toks.get("access_token"):
        return toks["access_token"]

    new_token = refresh_token(account_id, toks)
    if new_token:
        return new_token
    return toks.get("access_token", "")


def _existing_accounts(text: str) -> set[str]:
    return {m.group("name").strip() for m in _ACCOUNT_BLOCK_RE.finditer(text)}


def ensure_config(spec: GmailAccountSpec) -> str:
    """Create/extend himalaya config.toml + accounts.json for a Gmail account."""
    client_id, client_secret = _oauth_credentials()
    if not client_id or not client_secret:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET missing in .env. "
            "Create a Google Cloud OAuth 'Desktop app' client first (see README)."
        )

    HIMALAYA_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_TOML.exists():
        text = CONFIG_TOML.read_text(encoding="utf-8")
    else:
        text = ""
    existing = _existing_accounts(text)
    if spec.account_id in existing:
        status = f"config.toml already has account '{spec.account_id}' (left unchanged)"
    else:
        block = render_account_block(spec, client_id, client_secret)
        sep = "" if text.endswith("\n") or not text else "\n"
        CONFIG_TOML.write_text(text + sep + block, encoding="utf-8")
        status = f"added account '{spec.account_id}' to config.toml"

    rows: list[dict] = []
    if ACCOUNTS_JSON.exists():
        try:
            raw = json.loads(ACCOUNTS_JSON.read_text(encoding="utf-8-sig"))
            rows = raw if isinstance(raw, list) else [raw]
        except json.JSONDecodeError:
            rows = []
    if not any(str(r.get("id")) == spec.account_id for r in rows):
        rows.append(
            {
                "id": spec.account_id,
                "email": spec.email,
                "display_name": spec.display_name or spec.email,
                "default": spec.default,
            }
        )
        ACCOUNTS_JSON.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        status += f"; registered in accounts.json"
    return status


def run_login(account_id: str) -> int:
    """Trigger the browser OAuth consent flow for one account."""
    from .config import GMAIL_ENABLED

    if not GMAIL_ENABLED:
        print(
            "Gmail is disabled for this deployment: no login was started and no stored "
            f"token for '{account_id}' was touched."
        )
        return 1

    client_id, client_secret = _oauth_credentials()
    if not client_id or not client_secret:
        print("GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET not configured.")
        return 1

    if test_mode_enabled():
        # Test mode must never write mock credentials into the real token
        # store: a placeholder there would replace (or mask) a real login.
        # ``get_access_token`` already answers with a placeholder when no token
        # exists, so nothing needs to be stored.
        print(
            f"Test mode enabled: no login performed and no token written for '{account_id}'. "
            "Real stored tokens are left untouched."
        )
        return 0

    import http.server
    import socket

    # Bind to ephemeral port on localhost
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    redirect_uri = f"http://127.0.0.1:{port}/"
    auth_url = build_authorization_url(client_id, redirect_uri)

    auth_code: list[str] = []

    class OAuthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if "code" in qs:
                auth_code.append(qs["code"][0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h1>Authentication successful!</h1><p>You may return to the terminal.</p>")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"<h1>Authentication failed.</h1>")

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), OAuthHandler)
    server.timeout = 120

    print(f"Starting Gmail OAuth login for account '{account_id}'.")
    print(f"Opening consent URL in browser:\n{auth_url}\n")
    webbrowser.open(auth_url)

    while not auth_code:
        server.handle_request()
        if not auth_code:
            break

    if not auth_code:
        print("No authorization code received (request timed out or canceled).")
        return 1

    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": auth_code[0],
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }).encode("utf-8")
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            access_token = body.get("access_token")
            if not access_token:
                print("Failed to obtain access token from response.")
                return 1
            body["expires_at"] = time.time() + float(body.get("expires_in", 3600))
            save_tokens(account_id, body)
            print(f"Login OK for '{account_id}' - tokens acquired and saved.")
            return 0
    except Exception as e:
        print(f"Token exchange failed: {_redact(str(e))}")
        return 1


def _redact(text: str) -> str:
    text = re.sub(r"(client_secret=)[^&\s]+", r"\1***", text)
    text = re.sub(r"(refresh_token[\"'=:\s]+)[^&\s,\"}]+", r"\1***", text)
    text = re.sub(r"(access_token[\"'=:\s]+)[^&\s,\"}]+", r"\1***", text)
    return text
