"""Independent, Himalaya-free login probe for a password-based IMAP/SMTP mailbox.

Purpose: tell apart "the server rejects these credentials" from "Himalaya or
Mailops mis-sent them". It performs exactly ONE IMAP ``AUTHENTICATE PLAIN`` and
ONE SMTP ``AUTH PLAIN`` (the same mechanism Himalaya uses), over implicit TLS
with full certificate verification. It never issues MAIL FROM / RCPT / DATA,
never selects or lists a mailbox, and never retries a rejected login.

The password is typed at a hidden prompt (twice, so a typo is caught *before*
any network call - failed logins can trigger provider lockouts). It is never
accepted from arguments, files or pipes, never printed, and never stored.

    python scripts/webmail_auth_probe.py info@altcoininformer.com
    python scripts/webmail_auth_probe.py USER --imap-host H --smtp-host H
"""
from __future__ import annotations

import argparse
import base64
import getpass
import imaplib
import smtplib
import ssl
import sys

DEFAULT_IMAP = ("imap.hostinger.com", 993)
DEFAULT_SMTP = ("smtp.hostinger.com", 465)


def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def probe_imap(host: str, port: int, user: str, password: str) -> tuple[bool, str]:
    ctx = ssl.create_default_context()  # verification stays ON
    try:
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return False, f"connect/TLS failed: {type(exc).__name__}"
    try:
        caps = " ".join(conn.capabilities)
        if "AUTH=PLAIN" not in caps.upper():
            return False, "server does not advertise AUTH=PLAIN"
        try:
            conn.authenticate("PLAIN", lambda _challenge: f"\0{user}\0{password}")
        except imaplib.IMAP4.error as exc:
            return False, _scrub(str(exc), password)[:200]
        return True, "authenticated"
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass


def probe_smtp(host: str, port: int, user: str, password: str) -> tuple[bool, str]:
    ctx = ssl.create_default_context()
    try:
        conn = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
    except Exception as exc:  # noqa: BLE001
        return False, f"connect/TLS failed: {type(exc).__name__}"
    try:
        conn.ehlo()
        if "plain" not in conn.esmtp_features.get("auth", "").lower():
            return False, "server does not advertise AUTH PLAIN"
        # Build the initial response ourselves (UTF-8, like Himalaya) instead of
        # smtplib.login(), which would also try other mechanisms on failure.
        token = base64.b64encode(f"\0{user}\0{password}".encode("utf-8")).decode("ascii")
        code, msg = conn.docmd("AUTH", f"PLAIN {token}")
        text = _scrub(msg.decode("utf-8", "replace"), password)[:200]
        return code == 235, f"{code} {text}".strip()
    finally:
        try:
            conn.quit()  # QUIT only; nothing is ever sent
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("user", help="full login (the e-mail address)")
    p.add_argument("--imap-host", default=DEFAULT_IMAP[0])
    p.add_argument("--imap-port", type=int, default=DEFAULT_IMAP[1])
    p.add_argument("--smtp-host", default=DEFAULT_SMTP[0])
    p.add_argument("--smtp-port", type=int, default=DEFAULT_SMTP[1])
    args = p.parse_args()

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("Needs an interactive terminal: the password is typed at a hidden prompt only.")
        return 2

    password = getpass.getpass(f"Password (or app password) for {args.user} (hidden): ")
    again = getpass.getpass("Type it again to confirm (hidden): ")
    if not password or password != again:
        print("Entries are empty or do not match - nothing was sent to any server. Run again.")
        return 2
    del again

    print(f"\nLogin name used for both protocols: {args.user}")
    ok_imap, d_imap = probe_imap(args.imap_host, args.imap_port, args.user, password)
    print(f"IMAP {args.imap_host}:{args.imap_port}  AUTHENTICATE PLAIN: {'OK' if ok_imap else 'FAIL'}  ({d_imap})")
    ok_smtp, d_smtp = probe_smtp(args.smtp_host, args.smtp_port, args.user, password)
    print(f"SMTP {args.smtp_host}:{args.smtp_port}  AUTH PLAIN:         {'OK' if ok_smtp else 'FAIL'}  ({d_smtp})")
    print("No message was sent, no mailbox was opened, and nothing was saved.")
    if ok_imap and ok_smtp:
        print("\nThe server accepts these credentials. Next: run `webmail-add` and type the SAME password.")
        return 0
    print(
        "\nThe server rejected them. Do NOT keep retrying (lockout risk). Create an app password in hPanel "
        "(Emails > domain > Mailboxes > ⋮ > App passwords) and run this probe once with it."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
