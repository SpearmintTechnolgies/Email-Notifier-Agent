"""Build the Mailops runtime transfer bundle for dm-mini.

Contents (nothing else - no logs, no .bak files, no pid, no OAuth tokens):
    secrets/<id>.<kind>.enc  x78   Fernet-sealed mailbox credentials
    keys/master.fernet              the matching Fernet master key
    himalaya/config.toml            LINUX-RENDERED (passwd.command rewritten)
    himalaya/accounts.json          account metadata registry
    data/settings.json              live registry / policies / monitoring
    data/ops.db                     CONSISTENT snapshot (sqlite backup API)

Never prints secret content. Prints only names, sizes and digests.
"""
from __future__ import annotations

import hashlib
import json
import tarfile
from io import BytesIO
from pathlib import Path

REPO = Path(r"C:\Users\Global\Downloads\Email-Notifier-Agent")
STAGE = Path(r"C:\Users\Global\Downloads\mailops-secure-migration")
OUT = REPO / "build" / "mailops-runtime.tar.gz"

# (archive_name, source_path, mode)
def build() -> int:
    members: list[tuple[str, Path, int]] = []

    enc = sorted((STAGE / "secrets").glob("*.enc"))
    for p in enc:
        members.append((f"secrets/{p.name}", p, 0o600))

    members.append(("keys/master.fernet", STAGE / "keys" / "master.fernet", 0o400))
    members.append(("himalaya/config.toml", REPO / "build" / "config.linux.toml", 0o640))
    members.append(("himalaya/accounts.json", REPO / "himalaya" / "accounts.json", 0o640))
    members.append(("data/settings.json", REPO / "data" / "settings.json", 0o640))
    members.append(("data/ops.db", REPO / "build" / "ops.db.snapshot", 0o600))

    missing = [str(p) for _, p, _ in members if not p.is_file()]
    if missing:
        print("MISSING SOURCES:")
        for m in missing:
            print("  ", m)
        return 1

    # sanity: the rendered config must not contain a Windows path or a password
    cfg = (REPO / "build" / "config.linux.toml").read_text(encoding="utf-8")
    if "C:/" in cfg or "C:\\" in cfg or ".venv" in cfg:
        print("REFUSED: rendered config still contains a Windows interpreter path")
        return 1
    if cfg.count("/opt/mailops/venv/bin/python") != 78:
        print(f"REFUSED: expected 78 linux interpreter paths, found {cfg.count('/opt/mailops/venv/bin/python')}")
        return 1

    # sanity: settings.json and accounts.json agree at 39
    settings = json.loads((REPO / "data" / "settings.json").read_text(encoding="utf-8"))
    accounts = json.loads((REPO / "himalaya" / "accounts.json").read_text(encoding="utf-8"))
    acct_ids = sorted(accounts.keys()) if isinstance(accounts, dict) else sorted(
        a.get("id") or a.get("account_id") for a in accounts
    )
    active = sorted(settings.get("active_accounts", []))
    if not (len(acct_ids) == len(active) == 39 and acct_ids == active):
        print(f"REFUSED: registry mismatch accounts={len(acct_ids)} active={len(active)}")
        return 1
    if len(enc) != 78:
        print(f"REFUSED: expected 78 .enc blobs, found {len(enc)}")
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()

    with tarfile.open(OUT, "w:gz") as tf:
        for name, src, mode in members:
            data = src.read_bytes()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = mode
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mtime = int(src.stat().st_mtime)
            tf.addfile(ti, BytesIO(data))

    digest = hashlib.sha256(OUT.read_bytes()).hexdigest()
    print(f"members          : {len(members)}  (78 secrets + key + 4 config/state)")
    print(f"registry         : 39 accounts, active==accounts.json OK")
    print(f"linux interp paths: 78 OK, zero Windows paths")
    print(f"archive          : {OUT}")
    print(f"bytes            : {OUT.stat().st_size}")
    print(f"sha256           : {digest}")
    (OUT.parent / "mailops-runtime.tar.gz.sha256").write_text(
        f"{digest}  mailops-runtime.tar.gz\n", encoding="ascii"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
