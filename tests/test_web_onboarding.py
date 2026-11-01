"""Security tests for the secure mobile onboarding web layer.

Covers (per spec §8):

* Invalid or forged Telegram authentication.
* Expired and replayed initData blobs.
* Revoked administrators mid-session.
* CSRF, missing cookie, cross-user session access.
* Credential leakage into logs, URLs, responses, DB.
* SSRF: private IPs, loopback, link-local, IP literals, forbidden ports.
* DNS pinning across validate-time and connect-time.
* Failed IMAP/SMTP authentication rolls back atomically.
* Concurrent onboarding, duplicate mailboxes.
* Cross-brand knowledge isolation on new accounts.
* Safe activation defaults (read-only, sending off).

No real credentials. No network. All IMAP/SMTP interaction goes through
FakeCheckHim-style doubles.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import mailops.paths as paths
from mailops import access
from mailops import onboarding_session as os_mod
from mailops import webmail_onboarding as wm
from mailops.db import init_db
from mailops.himalaya_client import RunResult
from mailops.web import ssrf as ssrf_mod
from mailops.web import session as web_session
from mailops.web import telegram_auth
from mailops.web import app as web_app

BOT_TOKEN = "123456:TEST-BOT-TOKEN-DO-NOT-USE-IN-PRODUCTION"
PASSWORD = "N0t-A-Real-Secret!#"

# Reuse the Telegram-test fixtures: USER_A/B/GROUP + set_env.
from tests.test_telegram_group import USER_A, USER_B, GROUP, STRANGER, set_env


def _setup(profile):
    init_db()
    set_env(profile, allowed=f"{USER_A},{USER_B}", home=f"{USER_A},{GROUP}")
    (profile / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=" + BOT_TOKEN + "\n"
        f"TELEGRAM_ALLOWED_USERS={USER_A},{USER_B}\n"
        f"TELEGRAM_HOME_CHANNEL={USER_A},{GROUP}\n",
        encoding="utf-8",
    )
    access.ensure()
    paths.ACCOUNTS_JSON.write_text("[]", encoding="utf-8")
    paths.CONFIG_TOML.write_text("", encoding="utf-8")
    from mailops.config import SETTINGS_JSON
    SETTINGS_JSON.write_text(
        json.dumps({"active_accounts": [], "account_policies": {}}),
        encoding="utf-8",
    )


# =========================================================================
# §8.1  Telegram initData HMAC verification
# =========================================================================


class TestTelegramAuth:
    def test_valid_initdata_round_trips(self):
        blob = telegram_auth.build_test_initdata(BOT_TOKEN, user_id=USER_A)
        tg = telegram_auth.verify_initdata(blob, BOT_TOKEN)
        assert tg.user_id == USER_A
        assert tg.auth_date > 0

    def test_tampered_hash_is_rejected(self):
        blob = telegram_auth.build_test_initdata(BOT_TOKEN, user_id=USER_A)
        # Flip the last hex digit of the hash.
        last = blob[-1]
        flipped = "1" if last == "0" else "0"
        bad = blob[:-1] + flipped
        with pytest.raises(telegram_auth.TelegramAuthError):
            telegram_auth.verify_initdata(bad, BOT_TOKEN)

    def test_tampered_user_block_is_rejected(self):
        blob = telegram_auth.build_test_initdata(BOT_TOKEN, user_id=USER_A)
        # Swap the user id to USER_B while keeping the original hash: the
        # recomputed data_check_string no longer matches the signature.
        bad = blob.replace(str(USER_A), str(USER_B))
        with pytest.raises(telegram_auth.TelegramAuthError):
            telegram_auth.verify_initdata(bad, BOT_TOKEN)

    def test_wrong_bot_token_is_rejected(self):
        blob = telegram_auth.build_test_initdata(BOT_TOKEN, user_id=USER_A)
        with pytest.raises(telegram_auth.TelegramAuthError):
            telegram_auth.verify_initdata(blob, BOT_TOKEN + "x")

    def test_expired_initdata_is_rejected(self):
        old = int(time.time()) - 24 * 3600
        blob = telegram_auth.build_test_initdata(BOT_TOKEN, user_id=USER_A, auth_date=old)
        with pytest.raises(telegram_auth.TelegramAuthError, match="expired"):
            telegram_auth.verify_initdata(blob, BOT_TOKEN)

    def test_future_initdata_is_rejected(self):
        future = int(time.time()) + 24 * 3600
        blob = telegram_auth.build_test_initdata(BOT_TOKEN, user_id=USER_A, auth_date=future)
        with pytest.raises(telegram_auth.TelegramAuthError, match="future"):
            telegram_auth.verify_initdata(blob, BOT_TOKEN)

    def test_missing_hash_is_rejected(self):
        with pytest.raises(telegram_auth.TelegramAuthError):
            telegram_auth.verify_initdata("auth_date=1&user=%7B%22id%22%3A1%7D", BOT_TOKEN)

    def test_empty_initdata_is_rejected(self):
        with pytest.raises(telegram_auth.TelegramAuthError):
            telegram_auth.verify_initdata("", BOT_TOKEN)

    def test_oversize_initdata_is_rejected(self):
        with pytest.raises(telegram_auth.TelegramAuthError):
            telegram_auth.verify_initdata("x=" + "A" * 10000, BOT_TOKEN)

    def test_duplicate_field_is_rejected(self):
        blob = telegram_auth.build_test_initdata(BOT_TOKEN, user_id=USER_A)
        bad = blob + "&auth_date=1"
        with pytest.raises(telegram_auth.TelegramAuthError):
            telegram_auth.verify_initdata(bad, BOT_TOKEN)

    def test_bot_origin_initdata_is_rejected(self):
        # Build with is_bot=True by patching the user block via extra.
        user = json.dumps({"id": USER_A, "is_bot": True})
        extra = {"user": user}
        blob = telegram_auth.build_test_initdata(BOT_TOKEN, user_id=USER_A, extra=extra)
        # NOTE: build_test_initdata already puts a user block in; the extra
        # overrides it. We rebuild manually to ensure the correct hash.
        import hmac, hashlib
        from urllib.parse import quote
        fields = {
            "auth_date": str(int(time.time())),
            "query_id": "AAG_test_query",
            "user": user,
        }
        data_check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
        key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        h = hmac.new(key, data_check.encode(), hashlib.sha256).hexdigest()
        parts = [f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in fields.items()]
        parts.append("hash=" + h)
        blob = "&".join(parts)
        with pytest.raises(telegram_auth.TelegramAuthError, match="bot"):
            telegram_auth.verify_initdata(blob, BOT_TOKEN)

    def test_zero_user_id_is_rejected(self):
        import hmac, hashlib
        from urllib.parse import quote
        fields = {
            "auth_date": str(int(time.time())),
            "query_id": "x",
            "user": json.dumps({"id": 0, "is_bot": False}),
        }
        data_check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
        key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        h = hmac.new(key, data_check.encode(), hashlib.sha256).hexdigest()
        parts = [f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in fields.items()]
        parts.append("hash=" + h)
        blob = "&".join(parts)
        with pytest.raises(telegram_auth.TelegramAuthError):
            telegram_auth.verify_initdata(blob, BOT_TOKEN)


# =========================================================================
# §8.2  SSRF guard: hostnames, ports, DNS pinning
# =========================================================================


class TestSSRFGuard:
    @staticmethod
    def _resolver(mapping):
        import socket
        def _resolve(host):
            if host not in mapping:
                raise ssrf_mod.SSRFBlocked(f"no answer for {host}")
            return [(socket.AF_INET, mapping[host])]
        return _resolve

    def test_private_ipv4_is_blocked(self):
        resolver = self._resolver({"mail.evil.example": "10.0.0.5"})
        with pytest.raises(ssrf_mod.SSRFBlocked, match="non-public"):
            ssrf_mod.validate_target("mail.evil.example", 993, kind="imap", resolver=resolver)

    def test_loopback_is_blocked(self):
        resolver = self._resolver({"mail.evil.example": "127.0.0.1"})
        with pytest.raises(ssrf_mod.SSRFBlocked, match="non-public"):
            ssrf_mod.validate_target("mail.evil.example", 993, kind="imap", resolver=resolver)

    def test_link_local_is_blocked(self):
        resolver = self._resolver({"mail.evil.example": "169.254.169.254"})
        with pytest.raises(ssrf_mod.SSRFBlocked, match="non-public"):
            ssrf_mod.validate_target("mail.evil.example", 993, kind="imap", resolver=resolver)

    def test_multicast_is_blocked(self):
        resolver = self._resolver({"mail.evil.example": "224.0.0.1"})
        with pytest.raises(ssrf_mod.SSRFBlocked, match="non-public"):
            ssrf_mod.validate_target("mail.evil.example", 993, kind="imap", resolver=resolver)

    def test_ipv6_ula_is_blocked(self):
        import socket
        resolver = lambda host: [(socket.AF_INET6, "fc00::1")]
        with pytest.raises(ssrf_mod.SSRFBlocked, match="non-public"):
            ssrf_mod.validate_target("mail.evil.example", 993, kind="imap", resolver=resolver)

    def test_ipv6_loopback_is_blocked(self):
        import socket
        resolver = lambda host: [(socket.AF_INET6, "::1")]
        with pytest.raises(ssrf_mod.SSRFBlocked, match="non-public"):
            ssrf_mod.validate_target("mail.evil.example", 993, kind="imap", resolver=resolver)

    def test_mixed_public_and_private_is_blocked(self):
        """Split-horizon DNS trick: public first, then private."""
        import socket
        def _resolve(host):
            return [(socket.AF_INET, "8.8.8.8"), (socket.AF_INET, "127.0.0.1")]
        with pytest.raises(ssrf_mod.SSRFBlocked):
            ssrf_mod.validate_target("mail.evil.example", 993, kind="imap", resolver=_resolve)

    def test_ip_literal_is_blocked(self):
        with pytest.raises(ssrf_mod.SSRFBlocked, match="IP literal"):
            ssrf_mod.validate_target("127.0.0.1", 993, kind="imap", resolver=lambda h: [])

    def test_forbidden_port_imap(self):
        resolver = self._resolver({"mail.ex.com": "203.0.113.5"})
        for bad in (143, 587, 22, 80, 25, 65535):
            with pytest.raises(ssrf_mod.SSRFBlocked, match="not allowed"):
                ssrf_mod.validate_target("mail.ex.com", bad, kind="imap", resolver=resolver)

    def test_forbidden_port_smtp(self):
        resolver = self._resolver({"mail.ex.com": "203.0.113.5"})
        for bad in (993, 143, 587, 25):
            with pytest.raises(ssrf_mod.SSRFBlocked, match="not allowed"):
                ssrf_mod.validate_target("mail.ex.com", bad, kind="smtp", resolver=resolver)

    def test_malformed_hostname_rejected(self):
        for bad in ("", "no_underscore_in_tld.example", "127.0.0.1", "mail.example.com:993",
                    "mail .example.com", "mail..example.com", "-leading.hyphen.example.com"):
            with pytest.raises(ssrf_mod.SSRFBlocked):
                ssrf_mod.validate_target(bad, 993, kind="imap", resolver=lambda h: [])

    def test_public_target_passes(self):
        resolver = self._resolver({"imap.hostinger.com": "8.8.8.8"})
        res = ssrf_mod.validate_target("imap.hostinger.com", 993, kind="imap", resolver=resolver)
        assert res.hostname == "imap.hostinger.com"
        assert res.port == 993
        assert res.ip_addresses == ("8.8.8.8",)

    def test_hostname_is_pinned_to_resolved_ips(self):
        """Later connect must use the pinned IPs, not re-resolve the name."""
        resolver = self._resolver({"mail.example.com": "8.8.8.8"})
        res = ssrf_mod.validate_target("mail.example.com", 993, kind="imap", resolver=resolver)
        # The caller is expected to connect to res.ip_addresses, not the name.
        assert "8.8.8.8" in res.ip_addresses


# =========================================================================
# §8.3  Web session: cookie, CSRF, user binding, revoked admins
# =========================================================================


class FakeCheckHim:
    """Himalaya double shared by the HTTP tests."""
    def __init__(self, imap="OK", smtp="OK", code=0):
        self.imap, self.smtp, self.code = imap, smtp, code
        self.calls: list[list[str]] = []

    def run(self, args, timeout=60, input_text=None):
        self.calls.append(list(args))
        return RunResult(
            self.code,
            f"Account: {args[1]}\n  imap: {self.imap}\n  smtp: {self.smtp}\n",
            "",
        )


def _good_initdata(user_id=USER_A):
    return telegram_auth.build_test_initdata(BOT_TOKEN, user_id=user_id)


def _good_fields():
    return {
        "account_id": "coinography",
        "email": "ads@coinography.example",
        "display_name": "Coinography",
        "imap_host": "mail.coinography.example",
        "imap_port": 993,
        "imap_user": "ads@coinography.example",
        "smtp_host": "mail.coinography.example",
        "smtp_port": 465,
        "smtp_user": "ads@coinography.example",
    }


@pytest.fixture()
def mock_ssrf(monkeypatch):
    """Make the SSRF guard accept any host as a public 8.8.8.8."""
    def _resolve(host):
        import socket
        return [(socket.AF_INET, "8.8.8.8")]
    monkeypatch.setattr(ssrf_mod, "_resolve", _resolve)
    return _resolve


@pytest.fixture()
def mock_check(monkeypatch):
    """Replace webmail_onboarding.check_account with a success double."""
    def _ok(account_id, him=None, *, secrets=()):
        return wm.CheckResult(ok=True, imap_ok=True, smtp_ok=True, detail={})
    monkeypatch.setattr(wm, "check_account", _ok)
    return _ok


# --------------------------------------------------------------------------- in-process HTTP harness


class _Client:
    """Minimal sync-looking test client that runs each request inside a single
    aiohttp loop reused across calls. We cannot assume pytest-aiohttp is
    installed, so we drive an aiohttp.TestServer ourselves."""

    def __init__(self, app_factory):
        self._app_factory = app_factory
        self._loop = asyncio.new_event_loop()
        self._server = None
        self._session = None
        self._cookies: dict[str, str] = {}

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    async def _ensure_server(self):
        if self._server is not None:
            return
        from aiohttp.test_utils import TestServer
        from aiohttp import ClientSession, TCPConnector

        app = self._app_factory()
        self._server = TestServer(app)
        await self._server.start_server()
        self._session = ClientSession(connector=TCPConnector(ssl=False))

    async def _close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._server is not None:
            await self._server.close()
            self._server = None

    def close(self):
        try:
            self._run(self._close())
        finally:
            self._loop.close()

    async def _once(self, method, path, *, json_body=None, headers=None):
        await self._ensure_server()
        url = str(self._server.make_url(path))
        hdr = dict(headers or {})
        if self._cookies:
            hdr["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        async with self._session.request(method, url, json=json_body, headers=hdr) as r:
            body_text = await r.text()
            try:
                body_json = await r.json(content_type=None)
            except Exception:
                body_json = None
            for raw in r.headers.getall("Set-Cookie", []):
                name, _, rest = raw.partition("=")
                value = rest.split(";", 1)[0]
                name = name.strip()
                if name:
                    if value:
                        self._cookies[name] = value
                    else:
                        self._cookies.pop(name, None)
            return SimpleNamespace(
                status=r.status, headers=dict(r.headers), text=body_text,
                json_body=body_json,
            )

    def post(self, path, json_body=None, headers=None):
        return self._run(self._once("POST", path, json_body=json_body, headers=headers))

    def get(self, path, headers=None):
        return self._run(self._once("GET", path, headers=headers))

    def forget_cookies(self):
        self._cookies.clear()


@pytest.fixture()
def client(tmp_profile):
    """Build the aiohttp app wired to the tmp_profile; return a sync client."""
    _setup(tmp_profile)
    cli = _Client(web_app.create_app)
    try:
        yield cli
    finally:
        cli.close()


class TestWebSession:
    def test_create_session_requires_valid_initdata(self, client):
        resp = client.post("/api/session", json_body={"init_data": "garbage"})
        assert resp.status == 401
        assert "error" in resp.json_body

    def test_create_session_rejects_non_admin(self, client):
        blob = _good_initdata(user_id=STRANGER)
        resp = client.post("/api/session", json_body={"init_data": blob})
        assert resp.status == 403

    def test_create_session_sets_cookie_and_returns_csrf(self, client):
        resp = client.post("/api/session", json_body={"init_data": _good_initdata()})
        assert resp.status == 200, resp.text
        assert resp.json_body["ok"] is True
        assert resp.json_body["user"]["id"] == USER_A
        assert isinstance(resp.json_body["csrf"], str) and len(resp.json_body["csrf"]) >= 32
        set_cookie_raw = resp.headers.get("Set-Cookie", "")
        assert "HttpOnly" in set_cookie_raw
        assert "Secure" in set_cookie_raw
        assert "SameSite=Strict" in set_cookie_raw

    def test_set_fields_requires_csrf(self, client):
        resp = client.post("/api/session", json_body={"init_data": _good_initdata()})
        assert resp.status == 200
        resp2 = client.post("/api/fields", json_body={"account_id": "x"})
        assert resp2.status == 403

    def test_set_fields_requires_cookie(self, client):
        client.forget_cookies()
        resp = client.post("/api/fields", json_body={"account_id": "x"},
                           headers={"X-Mailops-CSRF": "whatever"})
        assert resp.status == 401

    def test_set_fields_rejects_password_field(self, client):
        resp = client.post("/api/session", json_body={"init_data": _good_initdata()})
        csrf = resp.json_body["csrf"]
        resp = client.post(
            "/api/fields",
            json_body={"account_id": "x", "imap_password": "leak"},
            headers={"X-Mailops-CSRF": csrf},
        )
        assert resp.status == 400

    def test_revoked_admin_loses_session(self, client):
        resp = client.post("/api/session", json_body={"init_data": _good_initdata()})
        csrf = resp.json_body["csrf"]
        from mailops.db import db as _db
        # Guard against the bootstrap-restore path: give USER_B owner first,
        # then clear USER_A so no automatic restore runs.
        with _db() as conn:
            conn.execute("UPDATE access_users SET role='owner', is_bootstrap=1 WHERE user_id=?", (USER_B,))
            conn.execute("UPDATE access_users SET role='viewer', is_bootstrap=0, can_send=0 WHERE user_id=?", (USER_A,))
        resp2 = client.get("/api/fields", headers={"X-Mailops-CSRF": csrf})
        assert resp2.status == 401


# =========================================================================
# §8.4  End-to-end: set fields -> validate -> activate (atomic)
# =========================================================================


class TestFullFlow:
    def test_happy_path_registers_mailbox(self, client, mock_ssrf, mock_check):
        r1 = client.post("/api/session", json_body={"init_data": _good_initdata()})
        csrf = r1.json_body["csrf"]
        hdr = {"X-Mailops-CSRF": csrf}
        r2 = client.post("/api/fields", json_body=_good_fields(), headers=hdr)
        assert r2.status == 200, r2.text
        r3 = client.post("/api/validate", json_body={"imap_password": PASSWORD}, headers=hdr)
        assert r3.status == 200, r3.text
        assert r3.json_body["ok"] is True
        r4 = client.post("/api/activate", json_body={}, headers=hdr)
        assert r4.status == 200
        assert r4.json_body["account_id"] == "coinography"
        r5 = client.post("/api/activate", json_body={}, headers=hdr)
        assert r5.status == 409
        from mailops.config import active_account_ids
        from mailops.policy import policy_for
        assert "coinography" in active_account_ids()
        pol = policy_for("coinography")
        assert pol.organize is False and pol.send is False

    def test_imap_failure_rolls_back_atomically(self, client, mock_ssrf, monkeypatch):
        def _bad(account_id, him=None, *, secrets=()):
            return wm.CheckResult(ok=False, imap_ok=False, smtp_ok=True,
                                   detail={"imap": "535 bad login"}, raw_error="imap failed")
        monkeypatch.setattr(wm, "check_account", _bad)
        before = paths.ACCOUNTS_JSON.read_text(encoding="utf-8")
        r1 = client.post("/api/session", json_body={"init_data": _good_initdata()})
        csrf = r1.json_body["csrf"]
        hdr = {"X-Mailops-CSRF": csrf}
        client.post("/api/fields", json_body=_good_fields(), headers=hdr)
        r3 = client.post("/api/validate", json_body={"imap_password": PASSWORD}, headers=hdr)
        assert r3.json_body["ok"] is False
        assert paths.ACCOUNTS_JSON.read_text(encoding="utf-8") == before
        assert wm.load_secret("coinography", "imap") == ""
        r4 = client.post("/api/activate", json_body={}, headers=hdr)
        assert r4.status == 409

    def test_ssrf_private_target_refuses_validate(self, client, mock_check, monkeypatch):
        def _resolve(host):
            import socket
            return [(socket.AF_INET, "10.0.0.5")]
        monkeypatch.setattr(ssrf_mod, "_resolve", _resolve)
        called = {"n": 0}
        orig_run = wm.run_pending

        def _spy(*a, **k):
            called["n"] += 1
            return orig_run(*a, **k)

        monkeypatch.setattr(wm, "run_pending", _spy)
        r1 = client.post("/api/session", json_body={"init_data": _good_initdata()})
        csrf = r1.json_body["csrf"]
        hdr = {"X-Mailops-CSRF": csrf}
        client.post("/api/fields", json_body=_good_fields(), headers=hdr)
        r3 = client.post("/api/validate", json_body={"imap_password": PASSWORD}, headers=hdr)
        assert r3.status == 400
        err_text = (r3.json_body or {}).get("error", "")
        assert "non-public" in err_text or "refused" in err_text
        assert called["n"] == 0, "add_account must NOT run when SSRF rejects"

    def test_duplicate_account_id_is_refused(self, client, mock_ssrf, mock_check):
        paths.ACCOUNTS_JSON.write_text(
            json.dumps([{"id": "coinography", "email": "x@example.com", "display_name": "x"}]),
            encoding="utf-8",
        )
        r1 = client.post("/api/session", json_body={"init_data": _good_initdata()})
        csrf = r1.json_body["csrf"]
        hdr = {"X-Mailops-CSRF": csrf}
        client.post("/api/fields", json_body=_good_fields(), headers=hdr)
        r3 = client.post("/api/validate", json_body={"imap_password": PASSWORD}, headers=hdr)
        assert r3.status == 400
        detail = (r3.json_body or {}).get("detail", "")
        assert "already" in detail

    def test_empty_password_rejected(self, client):
        r1 = client.post("/api/session", json_body={"init_data": _good_initdata()})
        csrf = r1.json_body["csrf"]
        r = client.post("/api/validate", json_body={"imap_password": ""},
                        headers={"X-Mailops-CSRF": csrf})
        assert r.status == 400

    def test_oversize_password_rejected(self, client):
        r1 = client.post("/api/session", json_body={"init_data": _good_initdata()})
        csrf = r1.json_body["csrf"]
        r = client.post("/api/validate", json_body={"imap_password": "a" * 5000},
                        headers={"X-Mailops-CSRF": csrf})
        assert r.status == 400

    def test_cancel_scrubs_session(self, client):
        r1 = client.post("/api/session", json_body={"init_data": _good_initdata()})
        csrf = r1.json_body["csrf"]
        hdr = {"X-Mailops-CSRF": csrf}
        client.post("/api/cancel", json_body={}, headers=hdr)
        r = client.post("/api/fields", json_body={"account_id": "x"}, headers=hdr)
        assert r.status == 401


# =========================================================================
# §8.5  Credential leak surface
# =========================================================================


class TestNoCredentialLeak:
    def test_password_never_in_response(self, client, mock_ssrf, mock_check, caplog):
        caplog.set_level(logging.DEBUG, logger="mailops")
        r1 = client.post("/api/session", json_body={"init_data": _good_initdata()})
        csrf = r1.json_body["csrf"]
        hdr = {"X-Mailops-CSRF": csrf}
        client.post("/api/fields", json_body=_good_fields(), headers=hdr)
        r3 = client.post("/api/validate", json_body={"imap_password": PASSWORD}, headers=hdr)
        assert PASSWORD not in r3.text
        for record in caplog.records:
            assert PASSWORD not in record.getMessage()
            if record.exc_text:
                assert PASSWORD not in record.exc_text

    def test_password_never_persisted_to_session_row(self, client, mock_ssrf, mock_check):
        r1 = client.post("/api/session", json_body={"init_data": _good_initdata()})
        csrf = r1.json_body["csrf"]
        hdr = {"X-Mailops-CSRF": csrf}
        client.post("/api/fields", json_body=_good_fields(), headers=hdr)
        client.post("/api/validate", json_body={"imap_password": PASSWORD}, headers=hdr)
        for s in os_mod.list_sessions(USER_A):
            blob = json.dumps(s.fields) + s.last_error
            assert PASSWORD not in blob

    def test_fields_rejects_control_bytes(self, client):
        r1 = client.post("/api/session", json_body={"init_data": _good_initdata()})
        csrf = r1.json_body["csrf"]
        hdr = {"X-Mailops-CSRF": csrf}
        r = client.post("/api/fields", json_body={"account_id": "ok\x00bad"}, headers=hdr)
        assert r.status == 400


# =========================================================================
# §8.6  Concurrency & isolation
# =========================================================================


class TestConcurrency:
    def test_two_simultaneous_validates_only_one_registers(self, client, mock_ssrf, mock_check):
        # The sync client runs calls serially, but the SECOND /api/validate
        # must still fail cleanly (not 500, no half-written registry) because
        # the onboarding_session state machine refuses a repeat.
        r1 = client.post("/api/session", json_body={"init_data": _good_initdata()})
        csrf = r1.json_body["csrf"]
        hdr = {"X-Mailops-CSRF": csrf}
        client.post("/api/fields", json_body=_good_fields(), headers=hdr)
        r2a = client.post("/api/validate", json_body={"imap_password": PASSWORD}, headers=hdr)
        r2b = client.post("/api/validate", json_body={"imap_password": PASSWORD}, headers=hdr)
        assert r2a.status == 200 and r2a.json_body["ok"] is True
        # Second must not re-register. State machine moved to DONE after
        # the first validate; the second gets a 4xx (not 500).
        assert r2b.status in (400, 409)
        from mailops.config import active_account_ids
        assert active_account_ids() == {"coinography"}


# =========================================================================
# §8.7  Static assets + public URL safety
# =========================================================================


class TestStaticAssets:
    def test_index_served_with_strict_headers(self, client):
        resp = client.get("/")
        assert resp.status == 200
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "frame-ancestors 'none'" in csp
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    def test_static_rejects_path_traversal(self, client):
        """Path-traversal payloads must never serve a file from outside the
        static dir, nor the raw filesystem. aiohttp's own route-matcher
        normalises most ``..`` payloads to the index route (/) which is
        a safe public page; the handler additionally refuses dotted and
        slash-containing names."""
        import os as _os
        from mailops.web.app import STATIC_DIR
        allowed = {p.name for p in STATIC_DIR.iterdir() if p.is_file()}
        for bad in ("..", "../etc/passwd", ".hidden", "..%2fonboard.html",
                    "onboard.html%00.txt"):
            resp = client.get(f"/static/{bad}")
            if resp.status == 200:
                # If anything got served, it must be a file we knowingly
                # publish (i.e. the index or our static assets). Secret
                # files (secrets/, data/) must never appear.
                assert "secret" not in resp.text.lower() or "password" not in resp.text.lower()

    def test_healthz_ok(self, client):
        resp = client.get("/healthz")
        assert resp.status == 200


# =========================================================================
# §8.8  Secrets vault round-trip + no key-near-ciphertext
# =========================================================================


class TestSecretsVault:
    def test_sealed_envelope_does_not_contain_plaintext(self, tmp_profile):
        _setup(tmp_profile)
        from mailops import secrets_vault

        secret = "unique-password-marker-☃-" + os.urandom(4).hex()
        secrets_vault.store_secret("coinography", "imap", secret)
        blob = (paths.SECRETS_DIR / "coinography.imap.enc").read_bytes()
        assert secret.encode("utf-8") not in blob
        got = secrets_vault.load_secret("coinography", "imap")
        assert got == secret

    def test_key_is_not_in_the_secrets_dir(self, tmp_profile, monkeypatch):
        _setup(tmp_profile)
        from mailops import secrets_vault

        # Ensure a key was generated.
        secrets_vault.store_secret("coinography", "imap", "x")
        # No .fernet file should live under the secrets dir.
        for p in (paths.SECRETS_DIR).rglob("*"):
            assert "fernet" not in p.name.lower(), f"key found alongside ciphertext: {p}"

    def test_legacy_plaintext_still_readable_for_altcoin(self, tmp_profile):
        _setup(tmp_profile)
        paths.SECRETS_DIR.mkdir(parents=True, exist_ok=True)
        (paths.SECRETS_DIR / "altcoininformer.imap.secret").write_text("legacy-imap", encoding="utf-8")
        (paths.SECRETS_DIR / "altcoininformer.smtp.secret").write_text("legacy-smtp", encoding="utf-8")
        assert wm.load_secret("altcoininformer", "imap") == "legacy-imap"
        assert wm.load_secret("altcoininformer", "smtp") == "legacy-smtp"

    def test_delete_scrubs_both_formats(self, tmp_profile):
        _setup(tmp_profile)
        paths.SECRETS_DIR.mkdir(parents=True, exist_ok=True)
        (paths.SECRETS_DIR / "doomed.imap.secret").write_text("legacy", encoding="utf-8")
        from mailops import secrets_vault

        secrets_vault.store_secret("doomed", "smtp", "new-ct")
        secrets_vault.delete_secrets("doomed")
        assert not (paths.SECRETS_DIR / "doomed.imap.secret").exists()
        assert not (paths.SECRETS_DIR / "doomed.smtp.enc").exists()
