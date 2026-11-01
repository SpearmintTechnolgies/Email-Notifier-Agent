"""Live IMAP + SMTP authentication test for every registered mailbox.

Uses Python's stdlib imaplib/smtplib directly (not himalaya) so we get a
precise, per-protocol pass/fail and a real TLS handshake, and so SMTP can be
proven WITHOUT composing or sending any message: we authenticate, then issue
QUIT. No RCPT TO, no DATA, no mail is ever sent.

Prints only account id, host, port and outcome. Never prints a password.
"""
from __future__ import annotations

import imaplib
import json
import smtplib
import socket
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, "/opt/mailops")

from mailops import paths, secrets_vault  # noqa: E402

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

TIMEOUT = 30


def _split_server(val: str | None) -> tuple[str | None, int | None]:
    """himalaya v2 schema: server = "host:port"."""
    if not val:
        return (None, None)
    if ":" in val:
        host, _, port = val.rpartition(":")
        try:
            return (host, int(port))
        except ValueError:
            return (val, None)
    return (val, None)


def parse_config() -> dict[str, dict]:
    cfg = tomllib.loads((Path(paths.HIMALAYA_DIR) / "config.toml").read_text(encoding="utf-8"))
    accounts = cfg.get("accounts", cfg)
    out: dict[str, dict] = {}
    for aid, blk in accounts.items():
        if not isinstance(blk, dict):
            continue
        imap = blk.get("imap", {}) or {}
        smtp = blk.get("smtp", {}) or {}
        email = blk.get("email")
        ihost, iport = _split_server(imap.get("server"))
        shost, sport = _split_server(smtp.get("server"))
        iuser = (imap.get("sasl", {}).get("plain", {}) or {}).get("authcid") or email
        suser = (smtp.get("sasl", {}).get("plain", {}) or {}).get("authcid") or email
        out[aid] = {
            "imap_host": ihost,
            "imap_port": iport or 993,
            "imap_user": iuser,
            "imap_enc": "tls",
            "smtp_host": shost,
            "smtp_port": sport or 465,
            "smtp_user": suser,
            "smtp_enc": "tls" if (sport or 465) == 465 else "starttls",
        }
    return out


def test_imap(host, port, user, pwd, enc) -> tuple[bool, str]:
    try:
        ctx = ssl.create_default_context()
        if str(enc or "").lower() in ("tls", "ssl", "none") or int(port) == 993:
            m = imaplib.IMAP4_SSL(host, int(port), ssl_context=ctx, timeout=TIMEOUT)
        else:
            m = imaplib.IMAP4(host, int(port), timeout=TIMEOUT)
            m.starttls(ssl_context=ctx)
        try:
            m.login(user, pwd)
            typ, _ = m.select("INBOX", readonly=True)
            ok = typ == "OK"
            try:
                m.logout()
            except Exception:
                pass
            return (ok, "AUTH OK, INBOX selectable" if ok else "auth ok, INBOX not selectable")
        except imaplib.IMAP4.error as e:
            try:
                m.logout()
            except Exception:
                pass
            return (False, f"AUTH FAILED: {str(e)[:90]}")
    except (socket.timeout, TimeoutError):
        return (False, "TIMEOUT connecting")
    except ssl.SSLError as e:
        return (False, f"TLS ERROR: {str(e)[:90]}")
    except Exception as e:  # noqa: BLE001
        return (False, f"{type(e).__name__}: {str(e)[:90]}")


def test_smtp(host, port, user, pwd, enc) -> tuple[bool, str]:
    """Authenticate only. Never issues MAIL FROM/RCPT TO/DATA."""
    try:
        ctx = ssl.create_default_context()
        p = int(port)
        if p == 465 or str(enc or "").lower() in ("tls", "ssl"):
            s = smtplib.SMTP_SSL(host, p, timeout=TIMEOUT, context=ctx)
        else:
            s = smtplib.SMTP(host, p, timeout=TIMEOUT)
            s.ehlo()
            if s.has_extn("starttls"):
                s.starttls(context=ctx)
                s.ehlo()
        try:
            s.login(user, pwd)
            s.quit()
            return (True, "AUTH OK (no mail sent)")
        except smtplib.SMTPAuthenticationError as e:
            try:
                s.quit()
            except Exception:
                pass
            return (False, f"AUTH FAILED: {str(e.smtp_code)} {str(e.smtp_error)[:70]}")
    except (socket.timeout, TimeoutError):
        return (False, "TIMEOUT connecting")
    except ssl.SSLError as e:
        return (False, f"TLS ERROR: {str(e)[:90]}")
    except Exception as e:  # noqa: BLE001
        return (False, f"{type(e).__name__}: {str(e)[:90]}")


def check(aid: str, c: dict) -> dict:
    res = {"id": aid}
    try:
        ipw = secrets_vault.load_secret(aid, "imap")
        spw = secrets_vault.load_secret(aid, "smtp")
    except Exception as e:  # noqa: BLE001
        res["imap"] = (False, f"secret load failed: {type(e).__name__}")
        res["smtp"] = (False, "skipped")
        return res

    res["imap"] = test_imap(c["imap_host"], c["imap_port"], c["imap_user"], ipw, c["imap_enc"])
    res["smtp"] = test_smtp(c["smtp_host"], c["smtp_port"], c["smtp_user"], spw, c["smtp_enc"])
    res["imap_ep"] = f"{c['imap_host']}:{c['imap_port']}"
    res["smtp_ep"] = f"{c['smtp_host']}:{c['smtp_port']}"
    ipw = "\x00" * len(ipw); spw = "\x00" * len(spw)
    del ipw, spw
    return res


def main() -> int:
    cfg = parse_config()
    accounts = json.loads((Path(paths.HIMALAYA_DIR) / "accounts.json").read_text(encoding="utf-8"))
    ids = sorted(accounts.keys()) if isinstance(accounts, dict) else sorted(
        a.get("id") or a.get("account_id") for a in accounts
    )
    missing_cfg = [i for i in ids if i not in cfg]
    if missing_cfg:
        print(f"WARNING: no config block for: {missing_cfg}")

    todo = [(i, cfg[i]) for i in ids if i in cfg]
    print(f"testing {len(todo)} mailboxes (IMAP login+SELECT, SMTP AUTH only, no mail sent)")
    print("=" * 78)

    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(lambda t: check(*t), todo):
            results.append(r)
            io, im = r["imap"]
            so, sm = r["smtp"]
            print(f"{r['id']:<22} IMAP {'OK  ' if io else 'FAIL'}  SMTP {'OK  ' if so else 'FAIL'}")
            if not io:
                print(f"    imap {r.get('imap_ep','?')}  {im}")
            if not so:
                print(f"    smtp {r.get('smtp_ep','?')}  {sm}")

    print("=" * 78)
    iok = sum(1 for r in results if r["imap"][0])
    sok = sum(1 for r in results if r["smtp"][0])
    both = sum(1 for r in results if r["imap"][0] and r["smtp"][0])
    print(f"elapsed            : {time.time()-t0:.0f}s")
    print(f"IMAP authenticated : {iok}/{len(results)}")
    print(f"SMTP authenticated : {sok}/{len(results)}")
    print(f"fully OK (both)    : {both}/{len(results)}")

    bad = [r["id"] for r in results if not (r["imap"][0] and r["smtp"][0])]
    if bad:
        print(f"NEEDS ATTENTION    : {bad}")
    print()
    print("RESULT:", "PASS - all mailboxes authenticate" if both == len(results) and both == 39
          else f"PARTIAL - {both}/{len(results)} fully OK")
    return 0 if both == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
