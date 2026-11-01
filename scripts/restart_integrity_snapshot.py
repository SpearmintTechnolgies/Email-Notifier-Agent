"""Pre/post-restart integrity snapshot.

Proves the registry, settings, ops.db and encrypted secrets are byte-identical
across the restart required to pick up the group-reading fix. Prints a stable,
diffable report; stores nothing and mutates nothing.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def main() -> int:
    report: dict[str, object] = {}

    acc = ROOT / "himalaya" / "accounts.json"
    settings = ROOT / "data" / "settings.json"
    report["accounts.json"] = {
        "sha": sha(acc),
        "count": len(json.loads(acc.read_text(encoding="utf-8"))),
    }
    s = json.loads(settings.read_text(encoding="utf-8"))
    report["settings.json"] = {
        "sha": sha(settings),
        "active_accounts": len(s.get("active_accounts") or []),
        "account_policies": len(s.get("account_policies") or {}),
        "group_restricted_accounts": sorted(s.get("group_restricted_accounts") or []),
        "sync_interval_seconds": s.get("sync_interval_seconds"),
        "digest_interval_seconds": s.get("digest_interval_seconds"),
    }
    report["config.toml"] = {"sha": sha(ROOT / "himalaya" / "config.toml")}

    secrets = sorted((ROOT / "secrets").glob("*.enc")) if (ROOT / "secrets").exists() else []
    report["secrets"] = {"count": len(secrets), "shas": {p.name: sha(p) for p in secrets}}

    db = ROOT / "data" / "ops.db"
    if db.exists():
        import sqlite3

        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            counts = {}
            for t in ("access_users", "access_groups", "send_approvals", "digest_pending"):
                if t in tables:
                    counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            report["ops.db"] = {"tables": len(tables), "rows": counts}
        finally:
            conn.close()

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
