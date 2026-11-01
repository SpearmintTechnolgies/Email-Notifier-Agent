"""AWS credential-migration utility (PREPARATION ONLY -- see docs/AWS_CREDENTIAL_MIGRATION_PLAN.md).

What this does
---------------
For every (account_id, kind) pair in the live ``himalaya/accounts.json``
registry, this tool:

  1. Decrypts the existing secret in memory using the SAME
     ``mailops.secrets_vault.load_secret`` call the running bot uses
     (transparently handles today's mix of DPAPI ``.enc`` blobs and the one
     legacy ``altcoininformer`` plaintext ``.secret`` file).
  2. Immediately re-protects it -- either:
       a. ``--apply`` not given (default): DRY RUN. Nothing is written
          anywhere. Only a per-account OK/FAIL table is printed, with byte
          lengths, never secret values.
       b. ``--apply --target ssm``: writes a SecureString to AWS SSM
          Parameter Store via ``mailops.secrets_vault`` with
          ``MAILOPS_VAULT_BACKEND=ssm`` forced for the call, using
          credentials from the normal boto3 chain (profile/env/role) --
          this script never asks for or stores AWS credentials itself.
       c. ``--apply --target local-fernet --out DIR``: writes a *new*,
          separate Fernet-wrapped ``secrets/`` directory plus a fresh
          ``master.fernet`` key under ``DIR`` (NOT the live profile), for
          operators who prefer to hand-carry files over SSH/SCP instead of
          using SSM. The live ``secrets/`` directory used by the running
          Windows bot is never touched by this script in any mode.
  3. Scrubs the plaintext local variable immediately after use.

Safety
------
* Never prints, logs, or writes plaintext secret VALUES. Byte lengths and
  envelope types are fine (they reveal nothing about the password).
* Never touches ``himalaya/config.toml``, ``himalaya/accounts.json``,
  ``data/settings.json`` or ``data/ops.db`` -- those are plain config/state,
  handled by a separate, simpler copy step (see the migration plan doc).
* Refuses to run with ``--apply`` unless ``--i-understand-this-writes-to``
  is also passed with the matching target name, as a deliberate
  speed-bump against an accidental real write.
* Default mode (no flags) is always safe to run on the live Windows profile.

Usage
-----
    python scripts/migrate_secrets_to_aws.py                       # dry run, all accounts
    python scripts/migrate_secrets_to_aws.py --only altcoininformer # dry run, one account
    python scripts/migrate_secrets_to_aws.py --apply --target ssm \
        --i-understand-this-writes-to ssm
    python scripts/migrate_secrets_to_aws.py --apply --target local-fernet \
        --out C:\\migration-staging --i-understand-this-writes-to local-fernet
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mailops import paths  # noqa: E402
from mailops.secrets_vault import VaultError, load_secret, open_envelope  # noqa: E402

KINDS = ("imap", "smtp")


def _envelope_kind(blob: bytes) -> str:
    if blob.startswith(b"\x01DPAPI\x00"):
        return "dpapi"
    if blob.startswith(b"\x02FERN0\x00"):
        return "fernet"
    return "legacy-plaintext"


def _account_ids() -> list[str]:
    accounts = json.loads(paths.ACCOUNTS_JSON.read_text(encoding="utf-8"))
    return sorted(a["id"] for a in accounts)


def _dry_run(ids: list[str]) -> int:
    print(f"DRY RUN -- no secrets will be written anywhere. {len(ids)} account(s).\n")
    ok = 0
    rows = []
    for aid in ids:
        row = {"id": aid}
        for kind in KINDS:
            enc = paths.SECRETS_DIR / f"{aid}.{kind}.enc"
            plain = paths.SECRETS_DIR / f"{aid}.{kind}.secret"
            path = enc if enc.exists() else (plain if plain.exists() else None)
            if path is None:
                row[kind] = "MISSING"
                continue
            blob = path.read_bytes()
            kindname = _envelope_kind(blob)
            try:
                secret = load_secret(aid, kind)
                length = len(secret.encode("utf-8"))
                secret = "\x00" * len(secret)  # best-effort scrub of the local ref
                del secret
                row[kind] = f"OK:{kindname}:{length}b"
            except VaultError as exc:
                row[kind] = f"FAIL:{kindname}:{exc}"
        rows.append(row)
    print(f"{'account_id':20} {'imap':30} {'smtp':30}")
    for r in rows:
        imap_s = str(r.get("imap", "MISSING"))
        smtp_s = str(r.get("smtp", "MISSING"))
        if imap_s.startswith("OK") and smtp_s.startswith("OK"):
            ok += 1
        print(f"{r['id']:20} {imap_s:30} {smtp_s:30}")
    print(f"\n{ok}/{len(rows)} accounts fully recoverable (both imap+smtp).")
    if ok != len(rows):
        print("ACTION REQUIRED: the accounts above need credential re-entry before migrating.")
        return 1
    print("All accounts are ready for the real migration step (not yet performed).")
    return 0


def _apply_ssm(ids: list[str], only: str | None) -> int:
    """Push each secret to AWS SSM Parameter Store.

    Reads every source secret via the normal file-backed ``load_secret``
    FIRST (this never changes), then writes it out through the vault's
    internal SSM writer directly -- it deliberately does NOT set
    ``MAILOPS_VAULT_BACKEND=ssm`` as a process-wide env var, because that
    would also redirect the *read* side (``load_secret``) to SSM and break
    the very migration it's trying to perform.
    """
    from mailops import secrets_vault as vault

    targets = [only] if only else ids
    failed = []
    for aid in targets:
        for kind in KINDS:
            try:
                secret = load_secret(aid, kind)  # always file-backed: env var untouched
            except VaultError as exc:
                print(f"SKIP {aid}.{kind}: could not decrypt source secret ({exc})")
                failed.append(aid)
                continue
            if not secret:
                print(f"SKIP {aid}.{kind}: empty/missing source secret")
                failed.append(aid)
                continue
            vault._ssm_store_secret(aid, kind, secret)
            name = vault._ssm_param_name(aid, kind)
            secret = "\x00" * len(secret)
            del secret
            print(f"OK   {aid}.{kind}: written to SSM ({name})")
    if failed:
        print(f"\n{len(set(failed))} account(s) had problems; see SKIP lines above.")
        return 1
    print(f"\nWrote {len(targets)} account(s) x {len(KINDS)} kinds to SSM.")
    return 0


def _apply_local_fernet(ids: list[str], only: str | None, out_dir: Path) -> int:
    """Stage a NEW, separate Fernet-wrapped secrets dir + key under ``out_dir``.

    Reads each source secret via the normal ``load_secret`` (which still
    points at the live ``paths.SECRETS_DIR``) BEFORE touching any staging
    path, then seals it with a staging-only Fernet key and writes the blob
    directly -- this function never mutates the shared ``paths.SECRETS_DIR``
    global, so the live profile used by the running bot is never at risk of
    being misread or miswritten.
    """
    import os
    from cryptography.fernet import Fernet

    secrets_out = out_dir / "secrets"
    keys_out = out_dir / "keys"
    secrets_out.mkdir(parents=True, exist_ok=True)
    keys_out.mkdir(parents=True, exist_ok=True)

    key_path = keys_out / "master.fernet"
    if key_path.exists():
        key = key_path.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
    fernet = Fernet(key)

    targets = [only] if only else ids
    failed = []
    for aid in targets:
        for kind in KINDS:
            try:
                secret = load_secret(aid, kind)  # reads from the LIVE SECRETS_DIR, untouched
            except VaultError as exc:
                print(f"SKIP {aid}.{kind}: could not decrypt source secret ({exc})")
                failed.append(aid)
                continue
            if not secret:
                print(f"SKIP {aid}.{kind}: empty/missing source secret")
                failed.append(aid)
                continue
            blob = b"\x02FERN0\x00" + fernet.encrypt(secret.encode("utf-8"))
            secret = "\x00" * len(secret)
            del secret
            dest = secrets_out / f"{aid}.{kind}.enc"
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(blob)
            os.replace(tmp, dest)
            try:
                os.chmod(dest, 0o600)
            except OSError:
                pass
            print(f"OK   {aid}.{kind}: written to {dest}")
    print(f"\nStaged output: {secrets_out}")
    print(f"Staged key:    {key_path}")
    print("Transfer BOTH directories to the Linux host over an encrypted channel (scp/sftp).")
    print("NEVER commit, email, or paste master.fernet or the .enc files anywhere.")
    if failed:
        print(f"\n{len(set(failed))} account(s) had problems; see SKIP lines above.")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", help="limit to a single account id")
    p.add_argument("--apply", action="store_true", help="actually write secrets (default: dry run)")
    p.add_argument("--target", choices=("ssm", "local-fernet"), help="where --apply writes to")
    p.add_argument("--out", type=Path, help="staging directory for --target local-fernet")
    p.add_argument(
        "--i-understand-this-writes-to",
        dest="confirm",
        help="must equal --target exactly, to confirm you intend a real write",
    )
    args = p.parse_args(argv)

    ids = [args.only] if args.only else _account_ids()

    if not args.apply:
        return _dry_run(ids)

    if not args.target or args.confirm != args.target:
        print(
            "Refusing to --apply: pass --target {ssm|local-fernet} AND "
            "--i-understand-this-writes-to <same target> to confirm.",
            file=sys.stderr,
        )
        return 2

    if args.target == "ssm":
        return _apply_ssm(ids, args.only)
    out_dir = args.out or (Path.cwd() / "migration_out")
    return _apply_local_fernet(ids, args.only, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
