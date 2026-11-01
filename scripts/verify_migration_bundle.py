"""Offline verification of the staged Fernet migration bundle.

Confirms every .enc blob in the staging dir is a Fernet envelope that the
provided master key can open, and that the set of (account, kind) pairs
matches the live registry. NEVER prints, logs or returns plaintext - only
byte lengths and pass/fail per account.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

STAGE = Path(r"C:\Users\Global\Downloads\mailops-secure-migration")
SECRETS = STAGE / "secrets"
KEYFILE = STAGE / "keys" / "master.fernet"
REPO = Path(r"C:\Users\Global\Downloads\Email-Notifier-Agent")

_FERNET_MARK = b"\x02FERN0\x00"


def main() -> int:
    from cryptography.fernet import Fernet, InvalidToken

    key = KEYFILE.read_bytes().strip()
    f = Fernet(key)

    reg = json.loads((REPO / "himalaya" / "accounts.json").read_text(encoding="utf-8"))
    if isinstance(reg, dict):
        live_ids = sorted(reg.keys())
    else:
        live_ids = sorted(a.get("id") or a.get("account_id") for a in reg)

    settings = json.loads((REPO / "data" / "settings.json").read_text(encoding="utf-8"))
    active = sorted(settings.get("active_accounts", []))

    files = sorted(SECRETS.glob("*.enc"))
    ok, bad, not_fernet = [], [], []
    lengths = {}

    for p in files:
        stem = p.name[: -len(".enc")]
        blob = p.read_bytes()
        if not blob.startswith(_FERNET_MARK):
            not_fernet.append((stem, blob[:7].hex()))
            continue
        try:
            pt = f.decrypt(blob[len(_FERNET_MARK):])
        except InvalidToken:
            bad.append(stem)
            continue
        n = len(pt)
        pt = b"\x00" * n
        del pt
        if n == 0:
            bad.append(stem + " (EMPTY)")
            continue
        lengths[stem] = n
        ok.append(stem)

    staged_ids = sorted({s.rsplit(".", 1)[0] for s in ok})
    expected = {f"{a}.{k}" for a in live_ids for k in ("imap", "smtp")}
    missing = sorted(expected - set(ok))
    extra = sorted(set(s for s in (p.name[:-4] for p in files)) - expected)

    print(f"staging dir            : {SECRETS}")
    print(f"blob files found       : {len(files)}")
    print(f"fernet envelopes OK    : {len(ok)}")
    print(f"distinct accounts OK   : {len(staged_ids)}")
    print(f"live accounts.json ids : {len(live_ids)}")
    print(f"live active_accounts   : {len(active)}")
    print(f"registry == staged     : {staged_ids == live_ids}")
    print(f"active == accounts.json: {active == live_ids}")
    print(f"decrypt failures       : {len(bad)} {bad if bad else ''}")
    print(f"non-fernet envelopes   : {len(not_fernet)} {not_fernet if not_fernet else ''}")
    print(f"missing (expected)     : {len(missing)} {missing if missing else ''}")
    print(f"unexpected extra       : {len(extra)} {extra if extra else ''}")
    if lengths:
        vals = sorted(lengths.values())
        print(f"plaintext byte lengths : min={vals[0]} max={vals[-1]} (values only, no content)")

    hard_fail = bool(bad or not_fernet or missing)
    print()
    print("RESULT:", "FAIL" if hard_fail else "PASS - all staged secrets open with the staged key")
    return 1 if hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
