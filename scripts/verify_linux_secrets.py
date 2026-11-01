"""Linux-side verification: every registered mailbox's IMAP+SMTP secret must
decrypt through the real secrets_vault, and the registry must be internally
consistent. Prints ONLY counts, ids and byte lengths - never plaintext.

Run as the mailops service user with the service environment.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/opt/mailops")

from mailops import paths, secrets_vault  # noqa: E402


def main() -> int:
    accounts_file = Path(paths.HIMALAYA_DIR) / "accounts.json"
    settings_file = Path(paths.DATA_DIR) / "settings.json"

    accounts = json.loads(accounts_file.read_text(encoding="utf-8"))
    ids = sorted(accounts.keys()) if isinstance(accounts, dict) else sorted(
        a.get("id") or a.get("account_id") for a in accounts
    )
    settings = json.loads(settings_file.read_text(encoding="utf-8"))
    active = sorted(settings.get("active_accounts", []))

    print(f"vault status        : {json.dumps(secrets_vault.vault_status())}")
    print(f"accounts.json ids   : {len(ids)}")
    print(f"active_accounts     : {len(active)}")
    print(f"registries match    : {ids == active}")
    print(f".enc files on disk  : {len(list(Path(paths.SECRETS_DIR).glob('*.enc')))}")
    print()

    ok, fail = [], []
    for aid in ids:
        for kind in ("imap", "smtp"):
            try:
                sec = secrets_vault.load_secret(aid, kind)
            except Exception as exc:  # noqa: BLE001
                fail.append((aid, kind, type(exc).__name__ + ": " + str(exc)[:80]))
                continue
            n = len(sec)
            sec = "\x00" * n
            del sec
            if n == 0:
                fail.append((aid, kind, "EMPTY"))
            else:
                ok.append((aid, kind, n))

    print(f"decrypted OK        : {len(ok)} / {len(ids) * 2}")
    print(f"failures            : {len(fail)}")
    for aid, kind, why in fail:
        print(f"   FAIL {aid}.{kind}: {why}")

    if ok:
        lens = sorted(n for _, _, n in ok)
        print(f"plaintext lengths   : min={lens[0]} max={lens[-1]} (lengths only)")

    accts_ok = sorted({a for a, _, _ in ok})
    print(f"accounts fully OK   : {len(accts_ok)} / {len(ids)}")
    print()
    hard = bool(fail) or ids != active or len(ok) != len(ids) * 2
    print("RESULT:", "FAIL" if hard else f"PASS - {len(accts_ok)}/{len(ids)} accounts, {len(ok)} secrets decrypt on Linux")
    return 1 if hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
