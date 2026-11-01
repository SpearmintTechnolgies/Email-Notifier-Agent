"""Secure onboarding of password-based IMAP/SMTP (business webmail) accounts.

Design rules
------------
* Nothing is guessed: the caller must supply IMAP and SMTP hostnames. Ports
  default only to the standard implicit-TLS ports (993 / 465).
* Passwords are entered locally via a hidden prompt (never argv, never chat,
  never logged) and stored in ``secrets/<account>.<imap|smtp>.secret`` -
  a gitignored, user-only-ACL file. ``himalaya`` reads them through
  ``python -m mailops get-secret`` (same pattern as the Gmail token command),
  so config.toml never contains a password.
* Validation uses ``himalaya account check``: it performs the IMAP and SMTP
  connect + TLS + authentication handshakes and sends NO email, moves nothing
  and creates no folders.
* The account is only registered in accounts.json (i.e. only picked up by the
  sync loop) after validation passes; on failure everything is rolled back.
* Existing accounts (Gmail, ...) are never modified; duplicate ids/emails are
  refused. Both files are backed up (timestamped .bak-*) before any write.

STARTTLS-only providers are not supported yet: Himalaya v2.2.1 rejects
``starttls = true`` while TLS is on and exposes no switch to turn TLS off, so
we cannot emit a config we are able to verify.
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, paths
from .config import is_gmail_account

log = logging.getLogger("mailops.webmail")

DEFAULT_IMAP_PORT = 993  # implicit TLS (IMAPS)
DEFAULT_SMTP_PORT = 465  # implicit TLS (SMTPS)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$")
_EMAIL_RE = re.compile(r"^[^@\s\"'\\<>]+@[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$")
_USER_RE = re.compile(r"^[^\s\"'\\<>]{1,254}$")
_TEXT_RE = re.compile(r"^[^\"\\\x00-\x1f]{0,100}$")
_BLOCK_RE = re.compile(r"^\[accounts\.(?P<name>[^\].]+)(?:\.[^\]]*)?\]\s*$", re.M)
_KINDS = ("imap", "smtp")
_STATUS_RE = re.compile(r"^\s*(imap|smtp)\s*:\s*(OK|FAIL)\b\s*(?:\((.*)\))?\s*$", re.I | re.M)


@dataclass
class WebmailSpec:
    account_id: str
    email: str
    imap_host: str
    smtp_host: str
    imap_port: int = DEFAULT_IMAP_PORT
    smtp_port: int = DEFAULT_SMTP_PORT
    imap_user: str = ""  # defaults to the email address
    smtp_user: str = ""  # defaults to imap_user
    display_name: str = ""
    default: bool = False
    security: str = "tls"

    def __post_init__(self) -> None:
        self.account_id = (self.account_id or "").strip()
        self.email = (self.email or "").strip()
        self.imap_host = (self.imap_host or "").strip().lower()
        self.smtp_host = (self.smtp_host or "").strip().lower()
        self.imap_user = (self.imap_user or self.email).strip()
        self.smtp_user = (self.smtp_user or self.imap_user).strip()
        self.display_name = (self.display_name or self.email.split("@", 1)[0]).strip()


class OnboardingError(Exception):
    pass


# --------------------------------------------------------------------------- validation


def reject_webmail_portal_url(raw: str, label: str = "host") -> str | None:
    """Return a human-readable problem string when ``raw`` looks like a web
    URL / webmail portal link rather than a plain IMAP/SMTP hostname.

    ``None`` means no problem was detected here (the caller should still run
    the normal ``_HOST_RE`` shape check afterwards). This exists as its own
    function - rather than relying solely on ``_HOST_RE`` rejecting the
    scheme/path characters - so every onboarding entry point (single wizard,
    Quick Add, bulk CSV, Smart Add, saved presets) gives the SAME clear
    explanation instead of a generic "not a valid hostname" message, and so
    tests can assert on this specific guard directly.
    """
    s = (raw or "").strip()
    if not s:
        return None
    low = s.lower()
    if "://" in low or low.startswith("http:") or low.startswith("https:"):
        return (
            f"{label} looks like a web URL ('{s[:60]}'). Enter only the mail "
            "server hostname (e.g. imap.hostinger.com) - not a webmail "
            "portal link, login page or any URL with a scheme."
        )
    if any(ch in s for ch in ("/", "?", " ", "\\")):
        return (
            f"{label} must be a plain hostname with no path, query string "
            "or spaces (e.g. imap.hostinger.com)."
        )
    return None


def validate_spec(spec: WebmailSpec) -> list[str]:
    """Return a list of human-readable problems (empty = OK). Offline only."""
    problems: list[str] = []
    if not _ID_RE.match(spec.account_id):
        problems.append("account id must be 1-32 chars of a-z, 0-9, '-' or '_' (start alphanumeric)")
    if not _EMAIL_RE.match(spec.email):
        problems.append("email address looks invalid")
    for label, host in (("IMAP", spec.imap_host), ("SMTP", spec.smtp_host)):
        if not host:
            problems.append(f"{label} host is required (not guessed - get it from your hosting provider)")
            continue
        portal_problem = reject_webmail_portal_url(host, label=f"{label} host")
        if portal_problem:
            problems.append(portal_problem)
        elif not _HOST_RE.match(host):
            problems.append(f"{label} host must be a plain hostname like mail.example.com (no scheme/port/path)")
    for label, port in (("IMAP", spec.imap_port), ("SMTP", spec.smtp_port)):
        if not isinstance(port, int) or not (1 <= port <= 65535):
            problems.append(f"{label} port must be 1-65535")
    for label, user in (("IMAP", spec.imap_user), ("SMTP", spec.smtp_user)):
        if not _USER_RE.match(user):
            problems.append(f"{label} username has unsupported characters (spaces, quotes, backslash, <>)")
    if not _TEXT_RE.match(spec.display_name):
        problems.append("display name must not contain quotes, backslashes or control characters")
    if is_gmail_account(spec.account_id, spec.email) or any(
        h == d or h.endswith("." + d)
        for h in (spec.imap_host, spec.smtp_host)
        for d in ("gmail.com", "googlemail.com")
    ):
        problems.append(
            "Gmail is disabled for this deployment: only approved business webmail "
            "accounts can be onboarded"
        )
    if spec.security != "tls":
        problems.append(
            "only implicit TLS (IMAPS/SMTPS, ports 993/465) is supported; STARTTLS-only "
            "servers cannot be configured and verified with Himalaya v2.2.1"
        )
    return problems


def _existing_ids_and_emails() -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    emails: set[str] = set()
    cfg = paths.CONFIG_TOML
    if cfg.exists():
        ids |= {m.group("name") for m in _BLOCK_RE.finditer(cfg.read_text(encoding="utf-8-sig"))}
    acc = paths.ACCOUNTS_JSON
    if acc.exists():
        try:
            raw = json.loads(acc.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            raw = []
        for row in raw if isinstance(raw, list) else [raw]:
            if isinstance(row, dict):
                ids.add(str(row.get("id")))
                emails.add(str(row.get("email", "")).lower())
    return ids, emails


# --------------------------------------------------------------------------- secrets
def secret_path(account_id: str, kind: str) -> Path:
    if not _ID_RE.match(account_id) or kind not in _KINDS:
        raise OnboardingError("invalid secret reference")
    return paths.SECRETS_DIR / f"{account_id}.{kind}.secret"


def _restrict_to_current_user(path: Path) -> None:
    """Best effort: remove inherited ACLs so only the current user can read."""
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME", "")
            if user:
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
        else:
            os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001
        log.warning("could not restrict permissions on %s", path.name)


def store_secret(account_id: str, kind: str, secret: str) -> Path:
    """Write ``secret`` to disk encrypted at rest (DPAPI on Windows,
    Fernet elsewhere); return the path of the ``.enc`` file.

    This is a thin forwarder onto ``mailops.secrets_vault``: all new
    mailboxes onboarded through the web form or CLI get an encrypted
    envelope. Previously existing plaintext ``.secret`` files are left
    as-is on disk and are still read by ``load_secret`` (forward-only
    migration — the live Altcoin Informer account must keep working).
    """
    if not secret:
        raise OnboardingError("empty password refused")
    from . import secrets_vault

    try:
        return secrets_vault.store_secret(account_id, kind, secret)
    except secrets_vault.VaultError as exc:
        # Never surface the ciphertext/key path in a user-facing error.
        raise OnboardingError(f"could not seal credential: {type(exc).__name__}") from exc


def load_secret(account_id: str, kind: str) -> str:
    """Return the plaintext secret (empty string if none is stored).

    Reads the encrypted ``.enc`` envelope first, falls back to the legacy
    plaintext ``.secret`` file. The CLI entry point ``mailops get-secret``
    (used by Himalaya's ``passwd.command``) ultimately calls this.
    """
    from . import secrets_vault

    try:
        return secrets_vault.load_secret(account_id, kind)
    except secrets_vault.VaultError:
        # Falling back to empty is correct: Himalaya will fail cleanly and
        # the operator sees a connection error rather than a stacktrace.
        log.warning("could not open credential envelope for %s/%s", account_id, kind)
        return ""


def delete_secrets(account_id: str) -> None:
    """Erase every stored credential for ``account_id`` (both formats)."""
    from . import secrets_vault

    secrets_vault.delete_secrets(account_id)


# --------------------------------------------------------------------------- config rendering
def _py_cmd() -> str:
    return Path(sys.executable).as_posix()


def render_account_block(spec: WebmailSpec) -> str:
    """Himalaya v2 block: implicit TLS, SASL PLAIN, password via command."""
    q = json.dumps  # TOML basic strings share JSON string escaping
    cmd = lambda kind: json.dumps([_py_cmd(), "-m", "mailops", "get-secret", spec.account_id, kind])  # noqa: E731
    return f"""
[accounts.{spec.account_id}]
default = {str(spec.default).lower()}
email = {q(spec.email)}
display-name = {q(spec.display_name)}

[accounts.{spec.account_id}.imap]
server = {q(f"{spec.imap_host}:{spec.imap_port}")}

[accounts.{spec.account_id}.imap.sasl.plain]
authcid = {q(spec.imap_user)}
passwd.command = {cmd("imap")}

[accounts.{spec.account_id}.smtp]
server = {q(f"{spec.smtp_host}:{spec.smtp_port}")}

[accounts.{spec.account_id}.smtp.sasl.plain]
authcid = {q(spec.smtp_user)}
passwd.command = {cmd("smtp")}
""".lstrip("\n")


# --------------------------------------------------------------------------- file helpers
def _backup(path: Path, stamp: str) -> Path | None:
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, dest)
    return dest


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _read_accounts_rows() -> list[dict]:
    if not paths.ACCOUNTS_JSON.exists():
        return []
    try:
        raw = json.loads(paths.ACCOUNTS_JSON.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise OnboardingError(f"accounts.json is not valid JSON ({exc}); refusing to modify it") from exc
    return raw if isinstance(raw, list) else [raw]


# --------------------------------------------------------------------------- core flow
@dataclass
class CheckResult:
    ok: bool
    imap_ok: bool
    smtp_ok: bool
    detail: dict[str, str]
    raw_error: str = ""


def parse_check_output(stdout: str, stderr: str = "", code: int = 0) -> CheckResult:
    status: dict[str, tuple[bool, str]] = {}
    for m in _STATUS_RE.finditer(stdout or ""):
        status[m.group(1).lower()] = (m.group(2).upper() == "OK", (m.group(3) or "").strip())
    imap_ok = status.get("imap", (False, ""))[0]
    smtp_ok = status.get("smtp", (False, ""))[0]
    detail = {k: v[1] for k, v in status.items()}
    err = "" if status else ((stderr or stdout or "").strip()[:500])
    return CheckResult(
        ok=bool(status) and imap_ok and smtp_ok and code == 0,
        imap_ok=imap_ok,
        smtp_ok=smtp_ok,
        detail=detail,
        raw_error=err,
    )


def check_account(account_id: str, him=None, *, secrets: tuple[str, ...] = ()) -> CheckResult:
    """Validate IMAP + SMTP login for a configured account WITHOUT sending
    mail: ``himalaya account check`` only connects and authenticates."""
    if him is None:
        from .himalaya_client import Himalaya

        him = Himalaya()
    r = him.run(["-a", account_id, "account", "check"], timeout=90)
    res = parse_check_output(r.stdout, r.stderr, r.code)
    for s in secrets:  # defence in depth: never echo a password back
        if s:
            res.detail = {k: v.replace(s, "***") for k, v in res.detail.items()}
            res.raw_error = res.raw_error.replace(s, "***")
    return res


def add_account(
    spec: WebmailSpec,
    imap_password: str,
    smtp_password: str | None = None,
    *,
    him=None,
    now: float | None = None,
) -> CheckResult:
    """Register a webmail account, validate it, roll back on failure.

    Raises OnboardingError for invalid input / duplicates. Returns the
    CheckResult (``.ok`` False means it was rolled back and NOT registered).
    """
    problems = validate_spec(spec)
    if problems:
        raise OnboardingError("; ".join(problems))
    ids, emails = _existing_ids_and_emails()
    if spec.account_id in ids:
        raise OnboardingError(f"account id '{spec.account_id}' already exists - choose another id")
    if spec.email.lower() in emails:
        raise OnboardingError(f"{spec.email} is already registered")
    smtp_password = smtp_password or imap_password

    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    paths.HIMALAYA_DIR.mkdir(parents=True, exist_ok=True)
    # Raw bytes, not text: universal-newline decoding would silently turn CRLF
    # into LF and rewrite the existing (Gmail) blocks on rollback or success.
    original_cfg = paths.CONFIG_TOML.read_bytes() if paths.CONFIG_TOML.exists() else None
    original_accounts = paths.ACCOUNTS_JSON.read_bytes() if paths.ACCOUNTS_JSON.exists() else None
    original_settings = config.SETTINGS_JSON.read_bytes() if config.SETTINGS_JSON.exists() else None
    rows = _read_accounts_rows()
    _backup(paths.CONFIG_TOML, stamp)
    _backup(paths.ACCOUNTS_JSON, stamp)
    _backup(config.SETTINGS_JSON, stamp)

    def rollback() -> None:
        """Undo EVERYTHING this call changed, byte for byte."""
        if original_cfg is None:
            paths.CONFIG_TOML.unlink(missing_ok=True)
        else:
            _atomic_write_bytes(paths.CONFIG_TOML, original_cfg)
        if original_accounts is None:
            paths.ACCOUNTS_JSON.unlink(missing_ok=True)
        elif not paths.ACCOUNTS_JSON.exists() or paths.ACCOUNTS_JSON.read_bytes() != original_accounts:
            _atomic_write_bytes(paths.ACCOUNTS_JSON, original_accounts)
        if original_settings is not None and (
            not config.SETTINGS_JSON.exists() or config.SETTINGS_JSON.read_bytes() != original_settings
        ):
            _atomic_write_bytes(config.SETTINGS_JSON, original_settings)
        delete_secrets(spec.account_id)

    try:
        store_secret(spec.account_id, "imap", imap_password)
        store_secret(spec.account_id, "smtp", smtp_password)
        base = original_cfg or b""
        sep = b"" if base.endswith(b"\n") or not base else b"\n"
        _atomic_write_bytes(
            paths.CONFIG_TOML,
            base + sep + (b"\n" if base else b"") + render_account_block(spec).encode("utf-8"),
        )
        result = check_account(spec.account_id, him, secrets=(imap_password, smtp_password))
    except Exception:
        rollback()
        raise
    if not result.ok:
        rollback()
        return result

    # Validated: only now make the sync loop aware of the account.
    rows.append(
        {
            "id": spec.account_id,
            "email": spec.email,
            "display_name": spec.display_name,
            "default": False,
        }
    )
    try:
        _atomic_write(paths.ACCOUNTS_JSON, json.dumps(rows, indent=2) + "\n")
        # ...and only now add it to the active-account allowlist, so the
        # dashboard picks it up. The account starts read-only with sending off.
        _activate_account(spec.account_id)
    except Exception:
        rollback()
        raise
    # Best-effort, non-fatal: remember this mailbox's verified server so a
    # LATER mailbox on the same email domain (same hosting tenant) can be
    # offered a VERIFIED reuse suggestion instead of re-asking for the
    # hostname. Never raises; a failure here must not undo a successful
    # onboarding.
    try:
        from . import server_resolution

        server_resolution.record_verified_server(spec)
    except Exception:  # noqa: BLE001
        log.warning("could not record server-resolution hint for %s", spec.account_id)
    return result


def _activate_account(account_id: str) -> None:
    """Add ``account_id`` to ``active_accounts`` in settings.json (when an
    allowlist is in use) and record its deny-by-default policy.

    Only those keys change; every other setting, key order and value is kept.
    No allowlist (missing/empty) already means "every registered account is
    active", so none is invented here. Gmail can never be activated.
    """
    if is_gmail_account(account_id):
        raise OnboardingError("Gmail cannot be activated in this deployment")
    path = config.SETTINGS_JSON
    if not path.exists():
        return  # defaults apply: no allowlist, organize/send off
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise OnboardingError(f"settings.json is not valid JSON ({exc}); refusing to modify it") from exc
    if not isinstance(data, dict):
        raise OnboardingError("settings.json is not a JSON object; refusing to modify it")
    changed = False
    allow = data.get("active_accounts")
    if isinstance(allow, list) and any(str(x).strip() for x in allow):
        if account_id not in [str(x).strip() for x in allow]:
            allow.append(account_id)
            changed = True
    policies = data.get("account_policies")
    if policies is None:
        policies = data["account_policies"] = {}
        changed = True
    if isinstance(policies, dict) and account_id not in policies:
        policies[account_id] = {"organize": False, "send": False}
        changed = True
    if changed:
        _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def update_credentials(
    account_id: str,
    imap_password: str,
    smtp_password: str | None = None,
    *,
    him=None,
) -> CheckResult:
    """Rotate the saved password(s) of a registered webmail account and
    re-validate (handshake only, nothing sent) without removing/re-adding it.

    Only the secret files change - config.toml and accounts.json are never
    touched. If the new password fails validation the previous secrets are
    restored, so a typo cannot break a working account.
    """
    if not imap_password:
        raise OnboardingError("empty password refused")
    smtp_password = smtp_password or imap_password
    # Only password accounts managed by webmail-add have secret files; this
    # also keeps OAuth accounts (Gmail) out of reach of this command.
    old = {kind: load_secret(account_id, kind) for kind in _KINDS}
    if not all(old.values()):
        raise OnboardingError(
            f"'{account_id}' is not a registered webmail account (no stored password); "
            "use webmail-add to onboard it"
        )
    ids, _ = _existing_ids_and_emails()
    if account_id not in ids:
        raise OnboardingError(f"account '{account_id}' is not configured")

    def restore() -> None:
        for kind, value in old.items():
            store_secret(account_id, kind, value)

    try:
        store_secret(account_id, "imap", imap_password)
        store_secret(account_id, "smtp", smtp_password)
        result = check_account(account_id, him, secrets=(imap_password, smtp_password))
    except Exception:
        restore()
        raise
    if not result.ok:
        restore()
    return result


# --------------------------------------------------------------------------- CLI
def _ask_password(label: str) -> str:
    """Hidden prompt, entered twice: a typo is caught before any login is
    attempted (repeated failed logins can lock a mailbox at the provider)."""
    first = getpass.getpass(f"{label} (hidden): ")
    if not first:
        raise OnboardingError("empty password refused")
    if getpass.getpass("Type it again to confirm (hidden): ") != first:
        raise OnboardingError("the two entries did not match - nothing was sent or changed")
    return first


def _require_tty() -> bool:
    if sys.stdin.isatty() and sys.stdout.isatty():
        return True
    print(
        "This command needs an interactive terminal: the password is typed at a hidden prompt "
        "and is never accepted via arguments, files or pipes."
    )
    return False


def cli_set_password(argv: list[str]) -> int:
    """Rotate a webmail account's saved password and re-validate it."""
    p = argparse.ArgumentParser(
        prog="python -m mailops webmail-set-password",
        description="Update the saved password of a webmail account and re-validate it "
        "(login handshake only - nothing is sent). The account is not removed or re-added.",
    )
    p.add_argument("account_id")
    p.add_argument("--separate-smtp", action="store_true", help="prompt for a different SMTP password")
    args = p.parse_args(argv)
    if not _require_tty():
        return 2
    try:
        imap_pw = _ask_password("New IMAP password")
        smtp_pw = _ask_password("New SMTP password") if args.separate_smtp else imap_pw
        print("Validating with the new password (login handshake only - no email is sent) ...")
        res = update_credentials(args.account_id, imap_pw, smtp_pw)
    except OnboardingError as exc:
        print(f"Refused: {exc}")
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled - the saved password was not changed.")
        return 130
    _print_result(res)
    if res.ok:
        print(f"Password for '{args.account_id}' updated and verified. Configuration was not modified.")
        return 0
    print("Validation failed - the previous saved password was restored.")
    return 1


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{label}{suffix}: ").strip()
    return val or default


def _print_result(res: CheckResult) -> None:
    for kind in _KINDS:
        ok = res.imap_ok if kind == "imap" else res.smtp_ok
        extra = res.detail.get(kind, "")
        print(f"  {kind.upper()}: {'OK' if ok else 'FAIL'}" + (f" ({extra})" if extra and not ok else ""))
    if res.raw_error:
        print(f"  himalaya said: {res.raw_error}")


def _print_context_status(account_id: str) -> None:
    """Tell the operator whether this inbox already has its OWN context. Nothing
    is ever copied from another brand."""
    try:
        from .reply_kb import has_account_kb

        purposes = config.load_settings().get("account_purposes") or {}
        has_purpose = bool(str(purposes.get(account_id) or "").strip())
        print(
            f"Context for '{account_id}': purpose {'found' if has_purpose else 'MISSING'}, "
            f"reply KB data/kb/{account_id}.md {'found' if has_account_kb(account_id) else 'MISSING'}."
        )
        if has_purpose or has_account_kb(account_id):
            print("  Stored brand context is carried over from the earlier setup and is NOT verified - review it.")
        if not has_purpose or not has_account_kb(account_id):
            print("  Add the missing context before relying on rankings or drafts (Telegram: Set inbox purposes / Reply knowledge base).")
    except Exception:  # noqa: BLE001 - advice only, never fail onboarding over it
        pass


def cli_add(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="python -m mailops webmail-add",
        description="Add a password-based IMAP/SMTP webmail account (implicit TLS).",
    )
    p.add_argument("account_id", nargs="?")
    p.add_argument("email", nargs="?")
    p.add_argument("--display-name", default="")
    p.add_argument("--imap-host", default="")
    p.add_argument("--imap-port", type=int, default=DEFAULT_IMAP_PORT)
    p.add_argument("--smtp-host", default="")
    p.add_argument("--smtp-port", type=int, default=DEFAULT_SMTP_PORT)
    p.add_argument("--imap-user", default="")
    p.add_argument("--smtp-user", default="")
    args = p.parse_args(argv)

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(
            "webmail-add needs an interactive terminal: the password is typed at a hidden prompt "
            "and is never accepted via arguments, files or pipes."
        )
        return 2
    try:
        account_id = args.account_id or _prompt("Account id (e.g. coinography)")
        email = args.email or _prompt("Email address")
        imap_host = args.imap_host or _prompt("IMAP host (from your hosting provider)")
        smtp_host = args.smtp_host or _prompt("SMTP host (from your hosting provider)")
        imap_user = args.imap_user or _prompt("IMAP login username", email)
        smtp_user = args.smtp_user or _prompt("SMTP login username", imap_user)
        spec = WebmailSpec(
            account_id=account_id,
            email=email,
            imap_host=imap_host,
            smtp_host=smtp_host,
            imap_port=args.imap_port,
            smtp_port=args.smtp_port,
            imap_user=imap_user,
            smtp_user=smtp_user,
            display_name=args.display_name,
        )
        problems = validate_spec(spec)
        if problems:
            print("Cannot continue:\n - " + "\n - ".join(problems))
            return 2
        imap_pw = _ask_password("IMAP password")
        same = _prompt("Use the same password for SMTP? (Y/n)", "Y").lower().startswith("y")
        smtp_pw = imap_pw if same else _ask_password("SMTP password")
        print(f"Validating {spec.imap_host}:{spec.imap_port} and {spec.smtp_host}:{spec.smtp_port} "
              "(login handshake only - no email is sent) ...")
        res = add_account(spec, imap_pw, smtp_pw)
    except OnboardingError as exc:
        print(f"Refused: {exc}")
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled - nothing was changed.")
        return 130
    _print_result(res)
    if res.ok:
        print(
            f"Account '{spec.account_id}' registered and activated. Other accounts were not modified.\n"
            "It is read-only with sending off; the dashboard shows it on the next refresh.\n"
            "With MAILOPS_TEST_MODE=1 the bot will read it but cannot move/create/send."
        )
        _print_context_status(spec.account_id)
        return 0
    print("Validation failed - configuration and stored password were rolled back; nothing registered.")
    return 1


def cli_check(account_id: str) -> int:
    """Read-only re-validation of an already registered account."""
    res = check_account(account_id)
    print(f"Account {account_id}:")
    _print_result(res)
    return 0 if res.ok else 1


# --------------------------------------------------------------------------- Telegram-initiated flow
# These helpers are called from the Telegram "Mailboxes" private screens
# (through mailops/onboarding_session.py). They never print or log a password:
# the password lives only as an argument on the stack during a single
# add_account call, and the stored secret files are the sole place it lands
# on disk.

def spec_from_session_fields(fields: dict) -> "WebmailSpec":
    """Build a WebmailSpec from the non-secret draft fields of an
    onboarding session. Validation is done afterwards by validate_spec /
    add_account. Unknown keys are ignored; port fallbacks use the
    implicit-TLS defaults."""
    def _int(key: str, default: int) -> int:
        raw = fields.get(key)
        if isinstance(raw, int):
            return raw
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            return default

    return WebmailSpec(
        account_id=str(fields.get("account_id") or "").strip(),
        email=str(fields.get("email") or "").strip(),
        imap_host=str(fields.get("imap_host") or "").strip(),
        smtp_host=str(fields.get("smtp_host") or "").strip(),
        imap_port=_int("imap_port", DEFAULT_IMAP_PORT),
        smtp_port=_int("smtp_port", DEFAULT_SMTP_PORT),
        imap_user=str(fields.get("imap_user") or "").strip(),
        smtp_user=str(fields.get("smtp_user") or "").strip(),
        display_name=str(fields.get("display_name") or "").strip(),
    )


def run_pending(
    session_token: str,
    *,
    imap_password: str,
    smtp_password: str | None = None,
    him=None,
) -> CheckResult:
    """Consume an ``awaiting_credentials`` session and run add_account.

    The caller supplies the password: either from a server-side hidden
    terminal prompt (CLI handoff, the default) or from a DM capture that
    is held in memory for the duration of this call. On success the
    session row flips to ``done``; on failure it flips to ``failed`` and
    everything add_account touched has already been rolled back.

    This function RE-RAISES ``OnboardingError`` so the caller can show the
    reason to the admin; it NEVER re-raises with the password in the
    message (``add_account``'s rollback also wipes stored secrets).
    """
    from . import onboarding_session as os_mod
    from . import mailbox_admin

    if not session_token:
        raise OnboardingError("no onboarding session token supplied")
    if not imap_password:
        raise OnboardingError("empty password refused")

    # Load without binding to a specific user: on the server side we trust
    # whoever already proved admin via the Telegram ingress to call us.
    # We still refuse non-awaiting sessions.
    from .db import db as _db

    with _db() as conn:
        row = conn.execute(
            "SELECT user_id,state FROM onboarding_sessions WHERE token=?",
            (session_token,),
        ).fetchone()
    if row is None:
        raise OnboardingError("unknown onboarding session")
    user_id = int(row["user_id"])
    state = str(row["state"])
    if state != os_mod.STATE_AWAITING_CREDS:
        raise OnboardingError(
            f"onboarding session is in state '{state}', not awaiting credentials"
        )

    # Build + validate spec BEFORE we touch anything on disk.
    session = os_mod.get_session(session_token, user_id)
    spec = spec_from_session_fields(session.fields)
    problems = validate_spec(spec)
    if problems:
        os_mod.finish_failure(session_token, user_id, "; ".join(problems))
        raise OnboardingError("; ".join(problems))

    os_mod.begin_validation(session_token, user_id)
    mailbox_admin.record(
        user_id,
        "onboard_validate_start",
        spec.account_id,
        f"imap={spec.imap_host}:{spec.imap_port} smtp={spec.smtp_host}:{spec.smtp_port}",
    )
    try:
        result = add_account(spec, imap_password, smtp_password, him=him)
    except OnboardingError as exc:
        os_mod.finish_failure(session_token, user_id, str(exc)[:200])
        mailbox_admin.record(user_id, "onboard_rolled_back", spec.account_id, str(exc))
        raise
    except Exception as exc:  # noqa: BLE001
        os_mod.finish_failure(session_token, user_id, f"{type(exc).__name__}")
        mailbox_admin.record(
            user_id, "onboard_rolled_back", spec.account_id, type(exc).__name__
        )
        raise OnboardingError(
            f"onboarding was rolled back after an unexpected error: {type(exc).__name__}"
        ) from exc

    if result.ok:
        os_mod.finish_success(session_token, user_id)
        mailbox_admin.record(
            user_id, "onboard_validated", spec.account_id, "imap+smtp OK"
        )
    else:
        reason = (
            result.raw_error
            or ", ".join(f"{k}:{v}" for k, v in result.detail.items() if v)
            or "validation failed"
        )
        os_mod.finish_failure(session_token, user_id, reason[:200])
        mailbox_admin.record(
            user_id, "onboard_rolled_back", spec.account_id, reason
        )
    return result


def cli_run_pending(argv: list[str]) -> int:
    """Server-side CLI counterpart of the Telegram Mailboxes 'Open secure
    terminal' button. Reads an onboarding session token (the Telegram UI
    shows it to the admin on screen) and prompts for the password at a
    hidden terminal prompt - never argv, never a file, never a pipe."""
    p = argparse.ArgumentParser(
        prog="python -m mailops webmail-run-pending",
        description="Finish a Telegram-initiated onboarding by supplying the "
        "mailbox password at a hidden terminal prompt. Nothing is sent.",
    )
    p.add_argument("session_token")
    p.add_argument(
        "--separate-smtp",
        action="store_true",
        help="prompt for a different SMTP password",
    )
    args = p.parse_args(argv)

    if not _require_tty():
        return 2

    from . import onboarding_session as os_mod

    try:
        # Peek at the fields (not the user) so the operator can see what
        # they are about to validate before typing a password.
        from .db import db as _db

        with _db() as conn:
            row = conn.execute(
                "SELECT user_id,state,fields_json FROM onboarding_sessions WHERE token=?",
                (args.session_token,),
            ).fetchone()
        if row is None:
            print("Unknown onboarding session token.")
            return 2
        if str(row["state"]) != os_mod.STATE_AWAITING_CREDS:
            print(
                f"That onboarding session is in state '{row['state']}'. "
                "Reopen the Mailboxes menu to generate a new one."
            )
            return 2
        try:
            fields = json.loads(row["fields_json"] or "{}")
        except ValueError:
            fields = {}
        print("About to validate (login handshake only, nothing is sent):")
        print(f"  account id:   {fields.get('account_id', '?')}")
        print(f"  email:        {fields.get('email', '?')}")
        print(f"  IMAP server:  {fields.get('imap_host', '?')}:{fields.get('imap_port', DEFAULT_IMAP_PORT)}")
        print(f"  SMTP server:  {fields.get('smtp_host', '?')}:{fields.get('smtp_port', DEFAULT_SMTP_PORT)}")
        print(f"  IMAP user:    {fields.get('imap_user', fields.get('email', '?'))}")
        imap_pw = _ask_password("IMAP password")
        smtp_pw = _ask_password("SMTP password") if args.separate_smtp else imap_pw
        print("Validating ...")
        result = run_pending(
            args.session_token, imap_password=imap_pw, smtp_password=smtp_pw
        )
    except OnboardingError as exc:
        print(f"Refused: {exc}")
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled - nothing was changed.")
        return 130
    _print_result(result)
    if result.ok:
        account_id = fields.get("account_id", "?")
        print(
            f"Account '{account_id}' registered and activated. The Telegram "
            "Mailboxes screen will show it on the next refresh. It is "
            "read-only with sending off until an admin explicitly flips it."
        )
        return 0
    print(
        "Validation failed - configuration and stored password were rolled back; "
        "nothing registered."
    )
    return 1


def cli_check_cli(account_id: str) -> int:
    return cli_check(account_id)


def cli_get_secret(account_id: str, kind: str) -> int:
    """Used by himalaya's passwd.command. Prints the secret, nothing else."""
    # Validate up front so a malformed id (e.g. ``../x``) returns the
    # dedicated "rejected" code, not the generic "missing" code.
    if not _ID_RE.match(account_id or "") or kind not in _KINDS:
        return 2
    try:
        secret = load_secret(account_id, kind)
    except OnboardingError:
        return 2
    if not secret:
        return 1
    # Raw UTF-8 bytes: when Himalaya captures stdout through a pipe, Windows
    # Python would otherwise encode with the ANSI code page (cp1252), crashing
    # on - or silently mangling - non-ASCII password characters.
    out = getattr(sys.stdout, "buffer", None)
    if out is not None:
        out.write(secret.encode("utf-8"))
        out.flush()
    else:  # pragma: no cover - exotic stdout replacements
        sys.stdout.write(secret)
    return 0
