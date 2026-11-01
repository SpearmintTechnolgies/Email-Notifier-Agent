"""Encrypted-at-rest storage for mailbox credentials.

Design (follows spec §4)
------------------------
* The decryption key is NEVER stored next to the ciphertext files.
    - Windows (production): the ciphertext is wrapped by DPAPI (CurrentUser
      scope) via ``crypt32.dll``. The DPAPI master key lives in the Windows
      user profile under ``%APPDATA%\\Microsoft\\Protect\\<SID>\\`` and is
      itself protected by the user's logon credentials. We never see the key
      material; the OS only unwraps for the same user on the same machine.
    - Non-Windows (CI, Linux dev, tests): the ciphertext is wrapped by a
      Fernet key stored under ``$MAILOPS_KEY_DIR`` (default
      ``~/.config/mailops/keys/`` outside the project tree). The key file is
      0600 and lives in a different directory than ``data/`` so a database
      backup cannot accidentally include it.
* Encrypted blobs are written to ``paths.SECRETS_DIR`` with the ``.enc``
  suffix. The old plaintext ``.secret`` files are still read as a fallback,
  so the live Altcoin Informer account keeps working (forward-only
  migration, per the user's explicit choice).
* ``load_secret`` is called by the ``mailops get-secret`` CLI, which is
  what Himalaya's ``passwd.command`` invokes. The call chain is:
  Himalaya -> get-secret CLI -> ``vault.load_secret`` -> decrypted bytes
  to Himalaya over stdout. The decrypted plaintext never touches disk.
* Backups: the ``.enc`` files are safe to copy; the KEY is not. The runbook
  tells the operator to back up the key separately or re-wrap after a
  disaster.

Threat model
------------
* Attacker with read access to ``secrets\\<id>.imap.enc`` CANNOT decrypt
  without the DPAPI master key / Fernet key file. On Windows the master
  key cannot be exported to another user or machine without the logon
  password; on Linux the attacker also needs the keyfile.
* Attacker with full access to the service user's session can decrypt
  anyway — that's by design, same as any at-rest encryption. The point is
  that stolen backups, misconfigured shares, or weak file ACLs on the
  ciphertext alone are not enough.
* We NEVER log the plaintext, the ciphertext, or the key path with the key.
"""
from __future__ import annotations

import logging
import os
import secrets
import sys
from pathlib import Path

from . import paths

log = logging.getLogger("mailops.vault")

# Marker byte at the start of every blob we wrote. Lets us distinguish
# a Windows DPAPI envelope (``\x01``) from a Fernet envelope (``\x02``)
# from legacy plaintext (anything else) without pulling in a MIME header.
_DPAPI_MARK = b"\x01DPAPI\x00"
_FERNET_MARK = b"\x02FERN0\x00"

# --------------------------------------------------------------------------- backend selection
#
# ``MAILOPS_VAULT_BACKEND`` picks where the ciphertext (not the key) lives:
#   * "file" (default) -- today's behaviour: DPAPI/Fernet-wrapped blobs under
#     ``paths.SECRETS_DIR``. Unchanged; this is what Windows production uses.
#   * "ssm" -- AWS SSM Parameter Store SecureString, intended for the Linux
#     deployment. Each (account_id, kind) becomes one parameter encrypted by
#     a caller-chosen KMS CMK. Nothing in this file talks to AWS unless an
#     operator explicitly sets this env var; it does not affect the current
#     Windows process at all. See docs/AWS_CREDENTIAL_MIGRATION_PLAN.md.
def _backend() -> str:
    return (os.environ.get("MAILOPS_VAULT_BACKEND") or "file").strip().lower()


class VaultError(Exception):
    """A credential could not be sealed or opened."""


# --------------------------------------------------------------------------- key material

def _key_dir() -> Path:
    """Where the local-fallback Fernet key lives (NOT alongside ciphertext)."""
    override = os.environ.get("MAILOPS_KEY_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
        if base:
            return Path(base) / "Mailops" / "keys"
    return Path.home() / ".config" / "mailops" / "keys"


def _restrict_user_only(path: Path) -> None:
    """Best-effort: only the current user may read this file."""
    # Avoid firing icacls subprocesses from the test suite (slow and noisy).
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME") or ""
            if user:
                import subprocess

                subprocess.run(
                    ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                    capture_output=True, timeout=15, check=False,
                )
        else:
            os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001
        log.warning("could not tighten ACL on %s", path.name)


def _load_or_create_fernet_key() -> bytes:
    """Load the Fernet master key, creating one on first use."""
    from cryptography.fernet import Fernet

    kdir = _key_dir()
    kdir.mkdir(parents=True, exist_ok=True)
    key_path = kdir / "master.fernet"
    if key_path.exists():
        data = key_path.read_bytes().strip()
        # Minimal shape check; Fernet.__init__ will reject garbage.
        if len(data) == 44 and data.endswith(b"="):
            return data
        log.warning("master.fernet looks malformed; refusing to overwrite it")
        raise VaultError("master key file is unreadable; refusing to replace")
    key = Fernet.generate_key()
    tmp = key_path.with_suffix(".tmp")
    tmp.write_bytes(key)
    os.replace(tmp, key_path)
    _restrict_user_only(key_path)
    log.info("generated new Mailops master key at %s (back this up separately)", kdir)
    return key


# --------------------------------------------------------------------------- DPAPI (Windows)

def _dpapi_available() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _dpapi_protect(plaintext: bytes) -> bytes:
    """Call CryptProtectData(CurrentUser, no additional entropy)."""
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    in_blob = DATA_BLOB(len(plaintext), ctypes.cast(ctypes.c_char_p(plaintext),
                                                   ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        err = ctypes.GetLastError()
        raise VaultError(f"DPAPI protect failed (err={err})")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(ciphertext: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    in_blob = DATA_BLOB(len(ciphertext), ctypes.cast(ctypes.c_char_p(ciphertext),
                                                    ctypes.POINTER(ctypes.c_char)))
    out_blob = DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    )
    if not ok:
        err = ctypes.GetLastError()
        raise VaultError(f"DPAPI unprotect failed (err={err})")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


# --------------------------------------------------------------------------- SSM backend (Linux/AWS)

def _ssm_prefix() -> str:
    return (os.environ.get("MAILOPS_SSM_PREFIX") or "/mailops/secrets").rstrip("/")


def _ssm_param_name(account_id: str, kind: str) -> str:
    from .webmail_onboarding import _ID_RE, _KINDS
    if not _ID_RE.match(account_id) or kind not in _KINDS:
        raise VaultError("invalid secret reference")
    return f"{_ssm_prefix()}/{account_id}/{kind}"


def _ssm_client():  # pragma: no cover - thin wrapper, exercised via monkeypatch in tests
    """Build a boto3 SSM client. Overridable in tests without installing boto3."""
    try:
        import boto3  # noqa: F401
    except ImportError as exc:
        raise VaultError(
            "MAILOPS_VAULT_BACKEND=ssm requires boto3 (pip install boto3); "
            "not needed for the default file/DPAPI backend"
        ) from exc
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or None
    return boto3.client("ssm", region_name=region) if region else boto3.client("ssm")


def _ssm_store_secret(account_id: str, kind: str, secret: str) -> None:
    client = _ssm_client()
    name = _ssm_param_name(account_id, kind)
    kms_key = os.environ.get("MAILOPS_SSM_KMS_KEY_ID", "").strip()
    kwargs = {
        "Name": name,
        "Value": secret,
        "Type": "SecureString",
        "Overwrite": True,
        "Tier": "Standard",
    }
    if kms_key:
        kwargs["KeyId"] = kms_key
    client.put_parameter(**kwargs)


def _ssm_load_secret(account_id: str, kind: str) -> str:
    client = _ssm_client()
    name = _ssm_param_name(account_id, kind)
    try:
        resp = client.get_parameter(Name=name, WithDecryption=True)
    except Exception as exc:  # noqa: BLE001 - boto3 raises a dynamic ParameterNotFound class
        if type(exc).__name__ == "ParameterNotFound":
            return ""
        raise VaultError(f"SSM get_parameter failed for {name}: {exc}") from exc
    return resp["Parameter"]["Value"]


def _ssm_delete_secrets(account_id: str) -> None:
    from .webmail_onboarding import _KINDS
    client = _ssm_client()
    for kind in _KINDS:
        name = _ssm_param_name(account_id, kind)
        try:
            client.delete_parameter(Name=name)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ != "ParameterNotFound":
                log.debug("could not delete SSM parameter %s: %s", name, exc)


# --------------------------------------------------------------------------- public API

def _enc_path(account_id: str, kind: str) -> Path:
    from .webmail_onboarding import _ID_RE, _KINDS  # local import avoids cycle at module load
    if not _ID_RE.match(account_id) or kind not in _KINDS:
        raise VaultError("invalid secret reference")
    return paths.SECRETS_DIR / f"{account_id}.{kind}.enc"


def _plain_path(account_id: str, kind: str) -> Path:
    from .webmail_onboarding import _ID_RE, _KINDS
    if not _ID_RE.match(account_id) or kind not in _KINDS:
        raise VaultError("invalid secret reference")
    return paths.SECRETS_DIR / f"{account_id}.{kind}.secret"


def seal(plaintext: bytes) -> bytes:
    """Return a self-describing encrypted envelope for ``plaintext``.

    Uses DPAPI on Windows, Fernet elsewhere (and in tests). We force the
    Fernet path when ``MAILOPS_FORCE_SOFT_VAULT=1`` is set, which the test
    suite does to keep CI identical across platforms.
    """
    if not plaintext:
        raise VaultError("empty plaintext refused")
    if _dpapi_available() and os.environ.get("MAILOPS_FORCE_SOFT_VAULT") != "1":
        return _DPAPI_MARK + _dpapi_protect(plaintext)
    from cryptography.fernet import Fernet
    key = _load_or_create_fernet_key()
    token = Fernet(key).encrypt(plaintext)
    return _FERNET_MARK + token


def open_envelope(blob: bytes) -> bytes:
    if not blob:
        raise VaultError("empty envelope")
    if blob.startswith(_DPAPI_MARK):
        if not _dpapi_available():
            raise VaultError("DPAPI envelope but DPAPI not available on this platform")
        return _dpapi_unprotect(blob[len(_DPAPI_MARK):])
    if blob.startswith(_FERNET_MARK):
        from cryptography.fernet import Fernet, InvalidToken
        key = _load_or_create_fernet_key()
        try:
            return Fernet(key).decrypt(blob[len(_FERNET_MARK):])
        except InvalidToken as exc:
            raise VaultError("fernet envelope could not be decrypted") from exc
    # Legacy plaintext (byte-identical to what webmail_onboarding wrote before).
    return blob


def store_secret(account_id: str, kind: str, secret: str) -> Path:
    """Write ``secret`` encrypted at rest, atomically.

    Returns the path of the ``.enc`` file. Deletes any legacy ``.secret``
    plaintext for the same account/kind so that two files cannot drift.

    When ``MAILOPS_VAULT_BACKEND=ssm``, writes a SecureString to AWS SSM
    Parameter Store instead and returns a sentinel ``Path`` describing the
    parameter name (no local file is written in that mode).
    """
    if not secret:
        raise VaultError("empty password refused")
    if _backend() == "ssm":
        _ssm_store_secret(account_id, kind, secret)
        return Path(_ssm_param_name(account_id, kind))
    enc_path = _enc_path(account_id, kind)
    enc_path.parent.mkdir(parents=True, exist_ok=True)
    blob = seal(secret.encode("utf-8"))
    tmp = enc_path.with_suffix(enc_path.suffix + ".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, enc_path)
    _restrict_user_only(enc_path)
    # Drop any older plaintext for the same id so we never leave two
    # copies around after a rotation.
    legacy = _plain_path(account_id, kind)
    try:
        legacy.unlink(missing_ok=True)
    except OSError:
        log.debug("could not unlink legacy plaintext %s", legacy.name)
    return enc_path


def load_secret(account_id: str, kind: str) -> str:
    """Return the plaintext secret (empty string if none is stored).

    Lookup order (file backend): encrypted ``.enc`` first (new onboarding),
    then the legacy plaintext ``.secret`` (unmigrated Altcoin Informer and
    anyone else existing before this change). With
    ``MAILOPS_VAULT_BACKEND=ssm``, reads from AWS SSM Parameter Store
    instead and never touches local files.
    """
    if _backend() == "ssm":
        return _ssm_load_secret(account_id, kind)
    enc = _enc_path(account_id, kind)
    if enc.exists():
        return open_envelope(enc.read_bytes()).decode("utf-8", errors="strict")
    plain = _plain_path(account_id, kind)
    if plain.exists():
        return plain.read_text(encoding="utf-8")
    return ""


def delete_secrets(account_id: str) -> None:
    """Erase every stored credential for ``account_id`` (both formats)."""
    if _backend() == "ssm":
        _ssm_delete_secrets(account_id)
        return
    from .webmail_onboarding import _KINDS
    for kind in _KINDS:
        for path in (_enc_path(account_id, kind), _plain_path(account_id, kind)):
            try:
                if path.exists():
                    _overwrite_then_unlink(path)
            except OSError:
                log.debug("could not delete %s", path.name)


def _overwrite_then_unlink(path: Path) -> None:
    """Best-effort scrub: overwrite the file bytes before unlinking.

    This is defence in depth. SSDs may remap and ignore the overwrite, so
    the real guarantee comes from the encryption, not from this step.
    """
    try:
        size = path.stat().st_size
        if size > 0:
            with open(path, "r+b") as fh:
                fh.write(secrets.token_bytes(size))
                fh.flush()
                os.fsync(fh.fileno())
    except OSError:
        pass
    path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- diagnostics


def vault_status() -> dict[str, object]:
    """Non-sensitive status dict for the admin UI / runbook."""
    if _backend() == "ssm":
        return {
            "platform": sys.platform,
            "mode": "ssm",
            "key_dir": "aws:ssm-parameter-store+kms",
            "secrets_dir": _ssm_prefix(),
        }
    return {
        "platform": sys.platform,
        "mode": "dpapi" if (_dpapi_available() and os.environ.get("MAILOPS_FORCE_SOFT_VAULT") != "1") else "fernet",
        "key_dir": str(_key_dir()) if not _dpapi_available() or os.environ.get("MAILOPS_FORCE_SOFT_VAULT") == "1" else "windows:dpapi-currentuser",
        "secrets_dir": str(paths.SECRETS_DIR),
    }
