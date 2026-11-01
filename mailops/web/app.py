"""Loopback HTTPS onboarding server (aiohttp).

Design (follows spec §§1-7)
---------------------------
* Binds to ``127.0.0.1:MAILOPS_WEB_PORT`` ONLY. A phone reaches it via a
  reverse tunnel that terminates public TLS (see docs/REMOTE_ONBOARDING.md).
  We refuse to bind to a public interface, so even a misconfigured firewall
  cannot leak the service to the internet.
* Uses a locally-generated self-signed TLS certificate for the loopback
  socket. This is for defence in depth (anything on this host that reads
  the loopback traffic without being inside the Python process still sees
  TLS) and lets us always serve `https://`, which Telegram Mini Apps
  require. The public certificate at the tunnel edge is what the admin's
  browser actually validates.
* Every mutating request requires BOTH:
    - a server-side session cookie (opaque sid, HttpOnly / SameSite=Strict
      / Secure, 15 min TTL, bound to the Telegram user id)
    - a CSRF header carrying the matching csrf token for that session.
* Passwords are accepted only on POST /api/validate as part of a verified
  session, held in memory for the duration of the call, and immediately
  dropped. They are never logged, echoed, or persisted anywhere other
  than the encrypted vault inside the add_account path.
* No handler reads a client-supplied Telegram user id. The only user id
  we trust is the one from a verified ``initData`` on POST /api/session.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import os
import secrets as _secrets
import ssl
from pathlib import Path
from typing import Any

from aiohttp import web

from .. import access, onboarding_session as os_mod, paths, webmail_onboarding as wm
from ..config import load_runtime
from . import session as web_session
from . import ssrf as ssrf_mod
from . import telegram_auth

log = logging.getLogger("mailops.web.app")

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "mailops_sid"
CSRF_HEADER = "X-Mailops-CSRF"

# Server-side limits
MAX_BODY_BYTES = 32 * 1024          # 32 KiB request body ceiling
MAX_PASSWORD_BYTES = 1024           # mailbox passwords never exceed this
MAX_FIELD_BYTES = 254               # per onboarding field


# --------------------------------------------------------------------------- helpers


def _runtime_bot_token() -> str:
    return load_runtime().telegram_token


def _json(data: dict[str, Any] | list[Any], *, status: int = 200,
          cookies: dict[str, str] | None = None,
          clear_cookies: tuple[str, ...] = ()) -> web.Response:
    resp = web.json_response(data, status=status, headers={
        "Cache-Control": "no-store, max-age=0",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })
    if cookies:
        for k, v in cookies.items():
            resp.set_cookie(
                k, v,
                httponly=True,
                secure=True,
                samesite="Strict",
                path="/",
                max_age=web_session.SESSION_TTL_S,
            )
    for name in clear_cookies:
        resp.set_cookie(name, "", max_age=0, path="/")
    return resp


def _error(message: str, *, status: int = 400) -> web.Response:
    return _json({"error": message}, status=status)


async def _read_json(request: web.Request, max_bytes: int = MAX_BODY_BYTES) -> dict[str, Any]:
    raw = await request.content.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise web.HTTPRequestEntityTooLarge(max_size=max_bytes, actual_size=len(raw))
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise web.HTTPBadRequest(reason="invalid JSON") from exc
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(reason="JSON object expected")
    return data


def _bearer_session(request: web.Request) -> web_session.WebSession:
    """Resolve the current session from the cookie, or raise 401."""
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        raise web.HTTPUnauthorized(reason="no session cookie")
    try:
        return web_session.load_session(sid)
    except web_session.WebSessionError as exc:
        raise web.HTTPUnauthorized(reason=str(exc)) from exc


def _check_csrf(request: web.Request, session: web_session.WebSession) -> None:
    header = request.headers.get(CSRF_HEADER)
    try:
        web_session.require_csrf(session, header)
    except web_session.WebSessionError as exc:
        raise web.HTTPForbidden(reason=str(exc)) from exc


# --------------------------------------------------------------------------- handlers


async def handle_index(request: web.Request) -> web.StreamResponse:
    """Serve the mobile onboarding form. No auth here - the page itself
    cannot do anything without a server-verified initData POST."""
    index = STATIC_DIR / "onboard.html"
    return web.FileResponse(index, headers={
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        # Deny framing except from Telegram's Mini App origin (which runs
        # inside the Telegram app, not a public web page).
        "Content-Security-Policy": (
            "default-src 'self'; "
            "script-src 'self' https://telegram.org https://*.telegram.org; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "frame-ancestors 'none';"
        ),
        "Referrer-Policy": "no-referrer",
    })


async def handle_static(request: web.Request) -> web.StreamResponse:
    name = request.match_info.get("name", "")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise web.HTTPNotFound()
    path = STATIC_DIR / name
    if not path.is_file():
        raise web.HTTPNotFound()
    content_type = {
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
    }.get(path.suffix, "application/octet-stream")
    return web.FileResponse(path, headers={
        "Content-Type": content_type,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    })


async def handle_healthz(request: web.Request) -> web.Response:
    return _json({"ok": True})


async def handle_create_session(request: web.Request) -> web.Response:
    """Verify Telegram initData, open a server-side session, set the cookie.

    No cookie is required to call this endpoint (we're ESTABLISHING one),
    but ``initData`` must verify AND the user must currently be an admin.
    """
    data = await _read_json(request)
    init_data = data.get("init_data")
    if not isinstance(init_data, str) or not init_data:
        return _error("init_data is required", status=400)
    try:
        tg = telegram_auth.verify_initdata(init_data, _runtime_bot_token())
    except telegram_auth.TelegramAuthError as exc:
        return _error(str(exc), status=401)
    if not access.can(tg.user_id, access.P_ADMIN):
        # Pattern: never leak whether the id is unknown vs. a demoted admin;
        # both get the same message.
        return _error("not an authorized administrator", status=403)
    try:
        session = web_session.start_session(tg.user_id)
    except web_session.WebSessionError as exc:
        return _error(str(exc), status=429)
    return _json(
        {
            "ok": True,
            "csrf": session.csrf,
            "user": {
                "id": tg.user_id,
                "first_name": tg.first_name,
                "username": tg.username,
            },
            "expires_in_s": web_session.SESSION_TTL_S,
            "onboarding_token": session.onboarding_token,
        },
        cookies={COOKIE_NAME: session.sid},
    )


# ---- field validation that mirrors what webmail_onboarding does -------------

def _coerce_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Clamp and whitelist non-secret fields before storing them."""
    out: dict[str, Any] = {}
    textish = ("account_id", "email", "display_name", "imap_host", "imap_user",
               "smtp_host", "smtp_user")
    for key in textish:
        if key in payload:
            value = payload[key]
            if not isinstance(value, str):
                raise web.HTTPBadRequest(reason=f"{key} must be a string")
            if len(value) > MAX_FIELD_BYTES:
                raise web.HTTPBadRequest(reason=f"{key} is too long")
            # Reject NUL and control bytes to prevent injection into logs
            # and the Himalaya TOML config.
            if "\x00" in value or any(ord(c) < 0x20 and c not in "\t " for c in value):
                raise web.HTTPBadRequest(reason=f"{key} contains control characters")
            out[key] = value.strip()
    for key in ("imap_port", "smtp_port"):
        if key in payload:
            value = payload[key]
            if isinstance(value, bool) or not isinstance(value, int):
                try:
                    value = int(str(value))
                except (TypeError, ValueError):
                    raise web.HTTPBadRequest(reason=f"{key} must be an integer")
            if not 1 <= value <= 65535:
                raise web.HTTPBadRequest(reason=f"{key} out of range")
            out[key] = value
    for forbidden in ("password", "imap_password", "smtp_password", "pass", "token"):
        if forbidden in payload:
            raise web.HTTPBadRequest(reason=f"{forbidden} field is not accepted here")
    return out


async def handle_set_fields(request: web.Request) -> web.Response:
    session = _bearer_session(request)
    _check_csrf(request, session)
    body = await _read_json(request)
    patch = _coerce_fields(body)
    try:
        current = os_mod.get_session(session.onboarding_token, session.user_id)
        updated = os_mod.set_fields(
            session.onboarding_token, session.user_id, nonce=current.nonce, patch=patch,
        )
    except os_mod.SessionError as exc:
        return _error(str(exc), status=409)
    return _json({"ok": True, "fields": updated.fields, "state": updated.state})


async def handle_get_fields(request: web.Request) -> web.Response:
    session = _bearer_session(request)
    try:
        current = os_mod.get_session(session.onboarding_token, session.user_id)
    except os_mod.SessionError as exc:
        return _error(str(exc), status=409)
    return _json({
        "ok": True,
        "fields": current.fields,
        "state": current.state,
        "last_error": current.last_error,
    })


async def handle_validate(request: web.Request) -> web.Response:
    """Pin DNS, then run the IMAP+SMTP handshake. No email is sent.

    This handler consumes the password from the POST body and NEVER logs
    it. It returns the connection result; the admin must still POST to
    /api/activate to actually register the account.
    """
    session = _bearer_session(request)
    _check_csrf(request, session)
    body = await _read_json(request, max_bytes=MAX_BODY_BYTES)
    imap_pw = body.get("imap_password")
    smtp_pw = body.get("smtp_password") or imap_pw
    if not isinstance(imap_pw, str) or not imap_pw:
        return _error("imap_password is required", status=400)
    if not isinstance(smtp_pw, str):
        return _error("smtp_password must be a string", status=400)
    if len(imap_pw) > MAX_PASSWORD_BYTES or len(smtp_pw) > MAX_PASSWORD_BYTES:
        return _error("password exceeds the allowed length", status=400)

    try:
        onboarding = os_mod.get_session(session.onboarding_token, session.user_id)
    except os_mod.SessionError as exc:
        return _error(str(exc), status=409)

    # SSRF guard - evaluate BOTH endpoints before touching any socket.
    try:
        imap_target = ssrf_mod.validate_target(
            onboarding.fields.get("imap_host", ""),
            int(onboarding.fields.get("imap_port") or 0),
            kind="imap",
        )
        smtp_target = ssrf_mod.validate_target(
            onboarding.fields.get("smtp_host", ""),
            int(onboarding.fields.get("smtp_port") or 0),
            kind="smtp",
        )
    except ssrf_mod.SSRFBlocked as exc:
        return _error(f"refused: {exc}", status=400)

    # Advance the mailbox-level state machine, then run add_account.
    try:
        os_mod.arm_for_credentials(session.onboarding_token, session.user_id, nonce=onboarding.nonce)
    except os_mod.SessionError as exc:
        # Already armed is fine - retrying the step must not double-advance.
        if "state" not in str(exc).lower():
            return _error(str(exc), status=409)

    def _run_pending() -> wm.CheckResult:
        return wm.run_pending(
            session.onboarding_token,
            imap_password=imap_pw,
            smtp_password=smtp_pw,
        )

    try:
        result = await asyncio.to_thread(_run_pending)
    except wm.OnboardingError as exc:
        return _json({"ok": False, "detail": str(exc)[:400]}, status=400)
    finally:
        # Defence in depth: drop the local reference. Python cannot
        # guarantee the string is wiped from memory, but we at least do
        # not keep it in a long-lived handler scope.
        imap_pw = ""
        smtp_pw = ""
        body = {}

    detail = (
        result.raw_error
        or ", ".join(f"{k}:{v}" for k, v in result.detail.items() if v)
        or ("OK" if result.ok else "handshake failed")
    )
    return _json({
        "ok": result.ok,
        "imap_ok": result.imap_ok,
        "smtp_ok": result.smtp_ok,
        "detail": detail[:400],
        "resolved": {
            "imap": imap_target.ip_addresses,
            "smtp": smtp_target.ip_addresses,
        },
    })


async def handle_activate(request: web.Request) -> web.Response:
    """Final confirmation step after a successful validate.

    Idempotency: the web session is single-use - a second POST returns
    409. The underlying ``onboarding_session`` state machine also refuses
    a re-activation.
    """
    # Peek at the raw session before raising 401 for a terminal state:
    # a repeated /activate on an already-activated session should 409,
    # not 401, so the caller can distinguish "no cookie" from "already done".
    sid = request.cookies.get(COOKIE_NAME)
    if sid:
        try:
            peek = web_session.load_session(sid)
        except web_session.WebSessionError as exc:
            msg = str(exc).lower()
            if "closed" in msg or "activated" in msg:
                return _error("session is already activated", status=409)
            raise web.HTTPUnauthorized(reason=str(exc)) from exc
    else:
        raise web.HTTPUnauthorized(reason="no session cookie")
    session = peek
    _check_csrf(request, session)
    try:
        onboarding = os_mod.get_session(session.onboarding_token, session.user_id)
    except os_mod.SessionError as exc:
        return _error(str(exc), status=409)
    if onboarding.state != os_mod.STATE_DONE:
        return _error(
            f"onboarding is in state '{onboarding.state}', not 'done'",
            status=409,
        )
    try:
        web_session.mark_activated(session.sid)
    except web_session.WebSessionError as exc:
        return _error(str(exc), status=409)
    return _json({"ok": True, "account_id": onboarding.fields.get("account_id")})


async def handle_cancel(request: web.Request) -> web.Response:
    session = _bearer_session(request)
    _check_csrf(request, session)
    try:
        onboarding = os_mod.get_session(session.onboarding_token, session.user_id)
        os_mod.cancel(session.onboarding_token, session.user_id, nonce=onboarding.nonce)
    except os_mod.SessionError:
        pass
    web_session.cancel_session(session.sid, reason="user_cancel")
    return _json({"ok": True}, clear_cookies=(COOKIE_NAME,))


# --------------------------------------------------------------------------- TLS / server


def _ensure_selfsigned_cert(cert_dir: Path) -> tuple[Path, Path]:
    """Create a localhost self-signed cert + key if missing.

    The real public TLS is terminated at the tunnel (Cloudflare etc.);
    this cert exists only so the loopback socket speaks HTTPS, which is a
    defence-in-depth measure against local interception.
    """
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "localhost.crt"
    key_path = cert_dir / "localhost.key"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Mailops local"),
    ])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=5))
        .not_valid_after(now + _dt.timedelta(days=825))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(__import__("ipaddress").IPv4Address("127.0.0.1")),
                x509.IPAddress(__import__("ipaddress").IPv6Address("::1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return cert_path, key_path


def _build_ssl_context(cert_dir: Path) -> ssl.SSLContext:
    cert_path, key_path = _ensure_selfsigned_cert(cert_dir)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def create_app() -> web.Application:
    """Build the aiohttp app (public: used by tests)."""
    app = web.Application(client_max_size=MAX_BODY_BYTES * 4)
    app.add_routes([
        web.get("/", handle_index),
        web.get("/healthz", handle_healthz),
        web.get("/static/{name}", handle_static),
        web.post("/api/session", handle_create_session),
        web.get("/api/fields", handle_get_fields),
        web.post("/api/fields", handle_set_fields),
        web.post("/api/validate", handle_validate),
        web.post("/api/activate", handle_activate),
        web.post("/api/cancel", handle_cancel),
    ])

    async def _security_headers(request: web.Request, handler):
        try:
            resp = await handler(request)
        except web.HTTPException as exc:
            exc.headers.setdefault("Cache-Control", "no-store")
            exc.headers.setdefault("X-Content-Type-Options", "nosniff")
            raise
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    app.middlewares.append(web.middleware(_security_headers))
    return app


async def start_web_server(*, host: str = "127.0.0.1",
                           port: int | None = None,
                           cert_dir: Path | None = None) -> web.AppRunner:
    """Launch the server on the loopback interface and return the runner.

    Caller is responsible for awaiting ``runner.cleanup()`` on shutdown.
    """
    if host not in ("127.0.0.1", "::1", "localhost"):
        # Hard safety rail: nobody should ever call this with a public bind.
        raise RuntimeError("web server may only bind to loopback addresses")
    port = int(port or int(os.environ.get("MAILOPS_WEB_PORT") or 8738))
    cert_dir = cert_dir or (paths.DATA_DIR / "web-tls")
    app = create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    ssl_ctx = _build_ssl_context(cert_dir)
    site = web.TCPSite(runner, host=host, port=port, ssl_context=ssl_ctx, reuse_address=False)
    await site.start()
    log.info("Mailops web onboarding listening on https://%s:%s (loopback only)", host, port)
    return runner
