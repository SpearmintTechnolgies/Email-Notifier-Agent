"""Compare the Windows live vault vs the staged migration bundle for specific
accounts, and test live IMAP auth from Windows.

Purpose: decide whether a Linux auth failure is a MIGRATION fault (the staged
blob differs from the live Windows secret) or a PRE-EXISTING credential fault
(the same password fails from Windows too).

Read-only. Never prints a password: only byte lengths, equality booleans and
auth outcomes.
"""
from __future__ import annotations

import imaplib
import smtplib
import ssl
import sys
from pathlib import Path

REPO = Path(r"C:\Users\Global\Downloads\Email-Notifier-Agent")
STAGE = Path(r"C:\Users\Global\Downloads\mailops-secure-migration")
sys.path.insert(0, str(REPO))

import tomllib  # noqa: E402

from mailops import secrets_vault  # noqa: E402

_FERNET_MARK = b"\x02FERN0\x00"
TARGETS = sys.argv[1:] or ["alien", "altcoininformer", "marketing"]


def staged(aid: str, kind: str) -> str:
    from cryptography.fernet import Fernet

    key = (STAGE / "keys" / "master.fernet").read_bytes().strip()
    blob = (STAGE / "secrets" / f"{aid}.{kind}.enc").read_bytes()
    assert blob.startswith(_FERNET_MARK)
    return Fernet(key).decrypt(blob[len(_FERNET_MARK):]).decode("utf-8")


def endpoints(aid: str) -> dict:
    cfg = tomllib.loads((REPO / "himalaya" / "config.toml").read_text(encoding="utf-8"))
    blk = cfg["accounts"][aid]
    def sp(v):
        h, _, p = v.rpartition(":")
        return h, int(p)
    ih, ip = sp(blk["imap"]["server"])
    sh, spt = sp(blk["smtp"]["server"])
    return {
        "imap_host": ih, "imap_port": ip,
        "imap_user": blk["imap"]["sasl"]["plain"]["authcid"],
        "smtp_host": sh, "smtp_port": spt,
        "smtp_user": blk["smtp"]["sasl"]["plain"]["authcid"],
        "email": blk.get("email"),
    }


def imap_auth(host, port, user, pwd) -> str:
    try:
        m = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=30)
        try:
            m.login(user, pwd)
            m.logout()
            return "OK"
        except imaplib.IMAP4.error as e:
            return f"AUTH FAILED: {str(e)[:70]}"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {str(e)[:70]}"


def smtp_auth(host, port, user, pwd) -> str:
    try:
        s = smtplib.SMTP_SSL(host, port, timeout=30, context=ssl.create_default_context())
        try:
            s.login(user, pwd)
            s.quit()
            return "OK"
        except smtplib.SMTPAuthenticationError as e:
            return f"AUTH FAILED: {e.smtp_code}"
    except Exception as e:  # noqa: BLE001
        return f"{type(e).__name__}: {str(e)[:70]}"


def main() -> int:
    for aid in TARGETS:
        print("=" * 72)
        print(f"ACCOUNT: {aid}")
        ep = endpoints(aid)
        print(f"  email      : {ep['email']}")
        print(f"  imap       : {ep['imap_host']}:{ep['imap_port']} as {ep['imap_user']}")
        print(f"  smtp       : {ep['smtp_host']}:{ep['smtp_port']} as {ep['smtp_user']}")

        for kind in ("imap", "smtp"):
            try:
                live = secrets_vault.load_secret(aid, kind)
            except Exception as e:  # noqa: BLE001
                print(f"  {kind}: LIVE WINDOWS LOAD FAILED: {type(e).__name__}: {e}")
                continue
            try:
                stg = staged(aid, kind)
            except Exception as e:  # noqa: BLE001
                print(f"  {kind}: STAGED LOAD FAILED: {type(e).__name__}: {e}")
                continue
            same = live == stg
            print(f"  {kind}: windows_len={len(live)} staged_len={len(stg)} identical={same}")
            if not same:
                print(f"       ^^ MIGRATION MISMATCH for {aid}.{kind}")

        # live auth from Windows with the WINDOWS secret
        try:
            ipw = secrets_vault.load_secret(aid, "imap")
            spw = secrets_vault.load_secret(aid, "smtp")
        except Exception as e:  # noqa: BLE001
            print(f"  cannot test auth: {e}")
            continue
        print(f"  WINDOWS imap auth : {imap_auth(ep['imap_host'], ep['imap_port'], ep['imap_user'], ipw)}")
        print(f"  WINDOWS smtp auth : {smtp_auth(ep['smtp_host'], ep['smtp_port'], ep['smtp_user'], spw)}")
        ipw = "\x00" * len(ipw); spw = "\x00" * len(spw)
        del ipw, spw
    print("=" * 72)
    print("If identical=True AND Windows auth also fails -> pre-existing credential issue,")
    print("not a migration fault.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
