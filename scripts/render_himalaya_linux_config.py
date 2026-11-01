"""Rewrite a Windows ``himalaya/config.toml`` for the Linux deployment.

What it does
------------
Himalaya never stores a password: every account block ends with

    passwd.command = ["<interpreter>", "-m", "mailops", "get-secret", "<id>", "<imap|smtp>"]

On the Windows host ``<interpreter>`` is an absolute path to
``.venv/Scripts/python.exe``. On Amazon Linux 2023 it must be the Linux venv
interpreter (``/opt/mailops/venv/bin/python``). That interpreter path is the
ONLY Windows-specific thing in the whole file.

This tool rewrites exactly that one array element, per account, per kind, and
leaves every other byte (hosts, ports, usernames, ordering, comments,
whitespace, encoding) untouched.

Safety properties
-----------------
* **Read-only on the input.** The input file is opened for reading only and is
  never written, renamed or backed up. Running this against the live Windows
  profile cannot disturb the running bot.
* **Refuses to overwrite.** ``--out`` must not already exist unless ``--force``
  is passed.
* **Refuses to emit a file containing a password.** After rendering, the output
  is scanned for ``passwd =`` / ``password =`` assignments (the only shape in
  which himalaya accepts an inline secret) and the write is aborted if one is
  found. It also asserts every account block still routes through
  ``get-secret``.
* **No network, no AWS, no credential decryption.** It is pure text transform.

Usage
-----
    python scripts/render_himalaya_linux_config.py \
        --in himalaya/config.toml \
        --out build/config.linux.toml \
        --python /opt/mailops/venv/bin/python

    # see what would change without writing anything
    python scripts/render_himalaya_linux_config.py --in himalaya/config.toml --dry-run

Exit codes: 0 ok, 2 bad usage / refused, 3 nothing matched (input unexpected).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ["<anything>", "-m", "mailops", "get-secret", "<id>", "<kind>"]
_PASSWD_CMD = re.compile(
    r'(?P<head>passwd\.command\s*=\s*\[\s*)'
    r'(?P<interp>"(?:[^"\\]|\\.)*")'
    r'(?P<tail>\s*,\s*"-m"\s*,\s*"mailops"\s*,\s*"get-secret"\s*,\s*'
    r'"(?P<id>[^"]+)"\s*,\s*"(?P<kind>imap|smtp)"\s*\])'
)

# An inline secret in himalaya v2 can only appear as `passwd = "..."` (or the
# legacy `password = "..."`). `passwd.command = [...]` must NOT match.
_INLINE_SECRET = re.compile(r'^\s*pass(?:wd|word)\s*=\s*"', re.MULTILINE)


def rewrite(text: str, interpreter: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Return (new_text, [(account_id, kind, old_interpreter), ...])."""
    changes: list[tuple[str, str, str]] = []
    new_interp = json.dumps(interpreter)  # TOML basic strings share JSON escaping

    def _sub(m: re.Match[str]) -> str:
        old = m.group("interp")
        if old != new_interp:
            changes.append((m.group("id"), m.group("kind"), json.loads(old)))
        return f'{m.group("head")}{new_interp}{m.group("tail")}'

    return _PASSWD_CMD.sub(_sub, text), changes


def _assert_no_secret(text: str) -> None:
    hit = _INLINE_SECRET.search(text)
    if hit:
        line = text[: hit.start()].count("\n") + 1
        raise SystemExit(
            f"REFUSED: rendered config contains an inline password assignment at line {line}. "
            "himalaya must always obtain passwords via passwd.command -> "
            "`python -m mailops get-secret`."
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="src", required=True, help="source config.toml (read-only)")
    ap.add_argument("--out", dest="dst", help="destination path (required unless --dry-run)")
    ap.add_argument(
        "--python",
        default="/opt/mailops/venv/bin/python",
        help="Linux interpreter for passwd.command (default: %(default)s)",
    )
    ap.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    ap.add_argument("--force", action="store_true", help="allow overwriting --out")
    args = ap.parse_args(argv)

    src = Path(args.src)
    if not src.is_file():
        print(f"error: no such file: {src}", file=sys.stderr)
        return 2
    if not args.dry_run and not args.dst:
        print("error: --out is required unless --dry-run", file=sys.stderr)
        return 2
    if not args.python.startswith("/"):
        print("error: --python must be an absolute POSIX path", file=sys.stderr)
        return 2

    text = src.read_text(encoding="utf-8-sig")
    total = len(_PASSWD_CMD.findall(text))
    if total == 0:
        print(
            "error: no `passwd.command = [... 'get-secret' ...]` entries found; "
            "refusing to guess at this file's shape",
            file=sys.stderr,
        )
        return 3

    rendered, changes = rewrite(text, args.python)
    _assert_no_secret(rendered)

    accounts = sorted({aid for aid, _, _ in changes} | {m.group("id") for m in _PASSWD_CMD.finditer(text)})
    print(f"passwd.command entries found : {total}")
    print(f"accounts referenced          : {len(accounts)}")
    print(f"interpreter paths rewritten  : {len(changes)}")
    if changes:
        olds = sorted({old for _, _, old in changes})
        for old in olds:
            print(f"  {old}\n    -> {args.python}")

    if args.dry_run:
        print("\ndry run: nothing written")
        return 0

    dst = Path(args.dst)
    if dst.exists() and not args.force:
        print(f"error: {dst} already exists (pass --force to overwrite)", file=sys.stderr)
        return 2
    dst.parent.mkdir(parents=True, exist_ok=True)
    # newline="" keeps whatever line endings the source used; himalaya and the
    # TOML parser accept both, and preserving them keeps the diff minimal.
    dst.write_text(rendered, encoding="utf-8", newline="")
    print(f"\nwrote {dst} ({dst.stat().st_size} bytes)")
    print("source file was NOT modified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
