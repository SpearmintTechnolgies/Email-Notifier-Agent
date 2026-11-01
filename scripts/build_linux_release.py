"""Build the clean, Linux-only Mailops release archive.

Produces, outside the repository tree:

    <outdir>/mailops-linux-<version>.tar.gz
    <outdir>/mailops-linux-<version>.tar.gz.sha256
    <outdir>/mailops-linux-<version>.INCLUDED.txt
    <outdir>/mailops-linux-<version>.EXCLUDED.txt

Guarantees enforced by this script (it aborts rather than ship a bad archive):

1. **Allow-list first.** Only paths matched by ``INCLUDE`` are considered at
   all; ``DENY_*`` then removes anything dangerous that slipped through a glob.
2. **No secret material.** Three independent gates:
   a. filename/extension deny-list (``.env``, ``*.enc``, ``*.secret``, ``*.key``,
      ``*.crt``, ``*.db``, ``*.log``, ``*.pid``, ``*.bak*``, token/credential json);
   b. a content scan for credential-shaped strings (Telegram bot tokens, AWS
      access key ids, private-key PEM headers, OpenAI/Slack keys);
   c. a **live-value scan**: every non-trivial value in the real ``.env`` is
      read (never printed) and the build aborts if any staged file contains it.
3. **No Windows artifacts.** ``.exe/.dll/.pyd/.pyc/.ps1/.bat/.cmd/.vbs/.lnk``
   are denied by extension, and ``.venv``/``venv``/``__pycache__``/``.git`` by
   directory.
4. **Deterministic, POSIX-correct tar.** Fixed mtime, ``uid=gid=0``,
   ``root:root`` names, ``0644`` for files / ``0755`` for directories and
   ``*.sh``, sorted member order, gzip ``mtime=0`` - so the same input tree
   always yields the same SHA-256.
5. **Self-verifying.** A ``MANIFEST.sha256`` of every member is written inside
   the archive, and ``RELEASE.txt`` records the build inputs.

Usage:
    python scripts/build_linux_release.py                       # default outdir
    python scripts/build_linux_release.py --outdir D:\\releases
    python scripts/build_linux_release.py --version 2026.10.26
"""
from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import io
import os
import re
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTDIR = ROOT.parent / "mailops-release"

# --------------------------------------------------------------------------- policy

#: Everything the Linux deployment needs, and nothing else. Globs are relative
#: to the repo root and use POSIX separators.
INCLUDE: tuple[str, ...] = (
    # --- application source
    "mailops/**/*.py",
    "mailops/web/static/*",
    # --- test suite (the Linux acceptance run executes it)
    "tests/**/*.py",
    # --- operational / migration / verification tooling (POSIX-portable only)
    "scripts/**/*.py",
    # --- dependency manifests
    "requirements.txt",
    "pytest.ini",
    "deploy/requirements-linux.txt",
    "deploy/requirements-linux.lock.txt",
    # --- safe configuration templates (no secrets, no live registry)
    ".env.example",
    "deploy/env.linux.template",
    "deploy/settings.linux.template.json",
    "deploy/accounts.linux.template.json",
    "deploy/himalaya-config.linux.template.toml",
    "deploy/mailops.service.template",
    "deploy/mailops.service.dm-mini.template",
    "deploy/cloudflared-config.template.yml",
    # --- AI drafting knowledge bases (business content, no credentials)
    "data/kb/*.md",
    # The archived original of _shared.md. Required:
    # tests/test_knowledge_isolation.py asserts it exists and is non-empty as
    # proof the brand-neutralisation of _shared.md was a rewrite, not a
    # deletion.
    "data/kb/_archive/*.md",
    # --- sample input for the bulk-onboarding CSV wizard
    "data/samples/*.csv",
    # --- documentation / runbooks
    "README.md",
    "PHASE2.md",
    "docs/*.md",
    # Not git HISTORY (.git/ is pruned) - just the ignore policy, so the
    # deployed tree keeps the same "never commit a secret" rules and
    # tests/test_webmail_onboarding.py::test_gitignore_covers_secrets_and_backups
    # can assert them on the target host.
    ".gitignore",
)

#: Directory names that are pruned anywhere in the tree.
DENY_DIRS: frozenset[str] = frozenset({
    ".git", ".github", ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "backups", "secrets", "tokens", "web-tls",
    "node_modules", "build", "dist",
})

#: Glob patterns (matched against the POSIX relative path AND the bare name).
DENY_PATTERNS: tuple[str, ...] = (
    # secrets / credentials / tokens
    ".env", ".env.*", "*.enc", "*.secret", "*.pem", "*.key", "*.crt", "*.p12",
    "*.pfx", "*token*.json", "*oauth*client*.json",
    # credential-bearing DATA files only. Deliberately NOT a bare
    # ``*credential*``: that also matched legitimate SOURCE whose name merely
    # describes the subject it handles (mailops/group_credentials.py,
    # tests/test_webmail_credentials.py, docs/AWS_CREDENTIAL_MIGRATION_PLAN.md),
    # and dropping group_credentials.py broke `from .. import
    # group_credentials` in mailops/bot/mailboxes.py -- caught by the
    # extract-and-run-the-suite verification step, not by the name scan.
    "*credential*.json", "*credential*.csv", "*credential*.txt",
    "*credential*.yml", "*credential*.yaml", "*credential*.ini",
    "accounts.json", "config.toml", "himalaya/*",
    # private state
    "*.db", "*.db-wal", "*.db-shm", "*.sqlite*", "*.log", "*.pid",
    "data/settings.json", "data/settings.json.*",
    # editor / backup / scratch
    "*.bak", "*.bak-*", "*.bak.*", "*.orig", "*.rej", "*.tmp", "*.swp", "*~",
    "*.pre", "*.pre-*", "*.original",
    # Windows-only binaries, launchers and scripts
    "*.exe", "*.dll", "*.pyd", "*.pyc", "*.pyo", "*.msi", "*.lnk",
    "*.ps1", "*.bat", "*.cmd", "*.vbs",
    # macOS / misc noise
    ".DS_Store", "Thumbs.db",
)
# ``.env.example`` is an explicitly reviewed, value-free template.
DENY_EXCEPTIONS: frozenset[str] = frozenset({".env.example"})

#: Credential-shaped content. Any hit aborts the build.
SECRET_CONTENT_RULES: tuple[tuple[str, str], ...] = (
    ("telegram bot token", r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    ("AWS access key id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("private key PEM", r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    ("OpenAI api key", r"\bsk-[A-Za-z0-9]{32,}\b"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    ("Google oauth client secret", r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b"),
)
#: Files whose job is to *document* those shapes, so a match there is expected.
SECRET_SCAN_SKIP: frozenset[str] = frozenset({
    "mailops/redact.py",
    "tests/test_redaction.py",
    "scripts/build_linux_release.py",
})

TEXT_SUFFIXES = {
    ".py", ".md", ".txt", ".json", ".toml", ".yml", ".yaml", ".cfg", ".ini",
    ".csv", ".html", ".css", ".js", ".sh", ".template", ".example", ".service",
}

FIXED_MTIME = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())


# --------------------------------------------------------------------------- helpers

def _denied(rel: str) -> str | None:
    """Return the deny rule that rejects ``rel``, or None."""
    name = PurePosixPath(rel).name
    if name in DENY_EXCEPTIONS:
        return None
    parts = PurePosixPath(rel).parts
    for part in parts[:-1]:
        if part in DENY_DIRS:
            return f"dir:{part}"
    if name in DENY_DIRS:
        return f"dir:{name}"
    for pat in DENY_PATTERNS:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat):
            return f"glob:{pat}"
    return None


def _candidates() -> list[str]:
    out: set[str] = set()
    for pattern in INCLUDE:
        for p in ROOT.glob(pattern):
            if p.is_file():
                out.add(p.relative_to(ROOT).as_posix())
    return sorted(out)


def _all_repo_files() -> list[str]:
    """Every file in the working tree, for the exclusion audit.

    Walks the real tree rather than the include globs, so the EXCLUDED report
    accounts for *everything* present on disk - which is the only way to prove
    a secret or a Windows binary was considered and rejected, instead of merely
    never looked at.
    """
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = Path(dirpath).relative_to(ROOT).as_posix()
        # Prune denied directories in place so os.walk never descends into
        # .git / .venv / backups / secrets (fast, and avoids reading them).
        dirnames[:] = [d for d in sorted(dirnames) if d not in DENY_DIRS]
        if rel_dir != ".":
            parts = PurePosixPath(rel_dir).parts
            if any(p in DENY_DIRS for p in parts):
                continue
        for name in sorted(filenames):
            out.append(name if rel_dir == "." else f"{rel_dir}/{name}")
    return out


def _pruned_dir_summary() -> list[tuple[str, str, int]]:
    """(top-level pruned dir, reason, file count) for directories os.walk skipped."""
    rows: list[tuple[str, str, int]] = []
    for d in sorted(DENY_DIRS):
        base = ROOT / d
        if not base.is_dir():
            continue
        n = sum(1 for p in base.rglob("*") if p.is_file())
        rows.append((f"{d}/", f"dir:{d}", n))
    return rows


#: .env keys whose VALUE is credential material. Matched case-insensitively as
#: substrings of the key name, so a future KEY/TOKEN/SECRET/PASSWORD variable
#: is covered automatically without editing this list.
SECRET_KEY_HINTS: tuple[str, ...] = (
    "token", "secret", "password", "passwd", "api_key", "apikey", "client_id",
    "credential", "private",
)


def _env_live_values() -> list[tuple[str, str]]:
    """(key, value) pairs from the real .env whose value is credential material.

    Only keys whose NAME marks them as secret-bearing are returned. Values such
    as ``TELEGRAM_ALLOWED_USERS`` (numeric Telegram user ids, already published
    in docs/) or ``OPENAI_MODEL`` (a public model name) are identifiers, not
    credentials, and scanning for them produces only false positives.

    Values are used solely for substring comparison. They are never printed,
    logged, written to disk, or included in any report - only the KEY NAME is
    ever surfaced in an error message.
    """
    env = ROOT / ".env"
    vals: list[tuple[str, str]] = []
    if not env.is_file():
        return vals
    for line in env.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if len(v) < 12:
            continue
        kl = k.lower()
        if not any(h in kl for h in SECRET_KEY_HINTS):
            continue
        # A filesystem path or URL in a secret-named var is config, not a secret.
        if v.startswith(("http://", "https://", "/", "C:", "c:")) or "\\" in v:
            continue
        vals.append((k, v))
    return vals


#: A match whose text contains one of these is an intentional placeholder in a
#: test fixture or a doc example, not a live credential.
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "example", "dummy", "fake", "placeholder", "redacted", "xxxx", "0000",
    "test", "sample", "your-", "<",
)


def _scan_secrets(staged: dict[str, bytes], env_values: list[tuple[str, str]]) -> list[str]:
    problems: list[str] = []
    rules = [(label, re.compile(rx)) for label, rx in SECRET_CONTENT_RULES]
    for rel, data in staged.items():
        if rel in SECRET_SCAN_SKIP:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{rel}: not valid UTF-8 (unexpected binary in a source release)")
            continue
        for label, rx in rules:
            for m in rx.finditer(text):
                hit = m.group(0).lower()
                if any(mark in hit for mark in PLACEHOLDER_MARKERS):
                    continue
                line = text[: m.start()].count("\n") + 1
                problems.append(f"{rel}:{line}: matches {label} pattern")
        for key, v in env_values:
            if v in text:
                problems.append(
                    f"{rel}: contains the LIVE value of .env key {key} "
                    "(value withheld) - remove it before shipping"
                )
    return problems


def _mode_for(rel: str) -> int:
    return 0o755 if rel.endswith(".sh") else 0o644


# --------------------------------------------------------------------------- build

def build(outdir: Path, version: str, *, strict: bool = True) -> int:
    rels = _candidates()
    included: list[str] = []
    excluded: list[tuple[str, str]] = []
    for rel in rels:
        why = _denied(rel)
        if why:
            excluded.append((rel, why))
        else:
            included.append(rel)

    # Full-tree audit: account for every file on disk, not just include-glob
    # hits, so the EXCLUDED report is a proof rather than a sample.
    inc_set = set(included)
    for rel in _all_repo_files():
        if rel in inc_set:
            continue
        why = _denied(rel) or "not-in-include-list"
        excluded.append((rel, why))
    excluded = sorted(set(excluded))

    if not included:
        print("error: nothing matched the include list", file=sys.stderr)
        return 2

    staged: dict[str, bytes] = {rel: (ROOT / rel).read_bytes() for rel in included}

    # ---- gate: secret scan
    problems = _scan_secrets(staged, _env_live_values())
    if problems:
        print("REFUSING TO BUILD - possible secret material in the staged tree:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        if strict:
            return 2

    # ---- gate: every third-party import must be declared
    missing = _check_dependency_manifest(staged)
    if missing:
        print("REFUSING TO BUILD - undeclared third-party imports:", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        if strict:
            return 2

    prefix = f"mailops-linux-{version}"
    outdir.mkdir(parents=True, exist_ok=True)

    # ---- in-archive manifest
    manifest_lines = [
        f"{hashlib.sha256(staged[rel]).hexdigest()}  {rel}" for rel in included
    ]
    manifest = ("\n".join(manifest_lines) + "\n").encode("utf-8")
    staged["MANIFEST.sha256"] = manifest

    release_txt = _release_notes(version, included, excluded, manifest_lines, stamped=False)
    staged["RELEASE.txt"] = release_txt.encode("utf-8")

    members = sorted(staged)

    # ---- deterministic tar.gz
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        seen_dirs: set[str] = set()
        for rel in members:
            # parent dirs, in order, so extraction works with any tar
            parts = PurePosixPath(rel).parts[:-1]
            for i in range(len(parts)):
                d = "/".join(parts[: i + 1])
                if d in seen_dirs:
                    continue
                seen_dirs.add(d)
                di = tarfile.TarInfo(f"{prefix}/{d}")
                di.type = tarfile.DIRTYPE
                di.mode = 0o755
                di.mtime = FIXED_MTIME
                di.uid = di.gid = 0
                di.uname = di.gname = "root"
                tf.addfile(di)
            data = staged[rel]
            ti = tarfile.TarInfo(f"{prefix}/{rel}")
            ti.size = len(data)
            ti.mode = _mode_for(rel)
            ti.mtime = FIXED_MTIME
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            tf.addfile(ti, io.BytesIO(data))

    raw = tar_buf.getvalue()
    gz_buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=gz_buf, compresslevel=9, mtime=0) as gz:
        gz.write(raw)
    blob = gz_buf.getvalue()

    archive = outdir / f"{prefix}.tar.gz"
    archive.write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()
    (outdir / f"{prefix}.tar.gz.sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8", newline="\n"
    )
    (outdir / f"{prefix}.INCLUDED.txt").write_text(
        "\n".join(manifest_lines) + "\n", encoding="utf-8", newline="\n"
    )
    excl_report = [
        "# Mailops Linux release - EXCLUSION AUDIT",
        f"# version {version}",
        "#",
        "# Part 1: directories pruned wholesale (os.walk never descended into them).",
        "# reason                 path                      files",
    ]
    pruned = _pruned_dir_summary()
    for path, why, n in pruned:
        excl_report.append(f"{why:<22} {path:<25} {n}")
    excl_report += [
        "#",
        "# Part 2: individual files present on disk and NOT shipped.",
        "# 'glob:<pat>' / 'dir:<name>' = actively rejected by a deny rule.",
        "# 'not-in-include-list'       = outside the allow-list (not needed on Linux).",
        "# reason\tpath",
    ]
    excl_report += [f"{why}\t{rel}" for rel, why in excluded]
    excl_report += [
        "#",
        f"# totals: {len(pruned)} pruned directories ({sum(n for _, _, n in pruned)} files), "
        f"{len(excluded)} individually excluded files, {len(included)} included.",
    ]
    (outdir / f"{prefix}.EXCLUDED.txt").write_text(
        "\n".join(excl_report) + "\n", encoding="utf-8", newline="\n",
    )
    (outdir / f"{prefix}.RELEASE.txt").write_text(
        _release_notes(version, included, excluded, manifest_lines, stamped=True),
        encoding="utf-8", newline="\n",
    )

    print(f"archive : {archive}")
    print(f"bytes   : {len(blob)}")
    print(f"members : {len(members)} ({len(included)} source + 2 generated)")
    print(f"excluded: {len(excluded)} files individually excluded; "
          f"{len(pruned)} directories pruned wholesale "
          f"({sum(n for _, _, n in pruned)} files)")
    print(f"sha256  : {digest}")
    return 0


def _check_dependency_manifest(staged: dict[str, bytes]) -> list[str]:
    """Every top-level third-party import in the staged tree must be declared.

    Declared = pinned in requirements.txt / deploy/requirements-linux*.txt, or
    listed in ``OPTIONAL`` below (lazily imported, feature-gated, documented in
    deploy/requirements-linux.txt).
    """
    import ast

    OPTIONAL = {"boto3", "botocore", "dns", "openpyxl", "tomli"}
    LOCAL = {"mailops", "tests", "scripts", "conftest"}
    DIST_TO_MODULES = {
        "python-telegram-bot": {"telegram"},
        "typing-extensions": {"typing_extensions"},
        "charset-normalizer": {"charset_normalizer"},
    }

    declared: set[str] = set()
    for rel in ("requirements.txt", "deploy/requirements-linux.txt",
                "deploy/requirements-linux.lock.txt"):
        data = staged.get(rel)
        if not data:
            continue
        for line in data.decode("utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            dist = re.split(r"[=<>!~\[;]", line, maxsplit=1)[0].strip().lower()
            declared |= DIST_TO_MODULES.get(dist, {dist.replace("-", "_")})

    stdlib = set(sys.stdlib_module_names)
    missing: list[str] = []
    for rel, data in sorted(staged.items()):
        if not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse(data.decode("utf-8-sig"), filename=rel)
        except SyntaxError as exc:
            missing.append(f"{rel}: SYNTAX ERROR {exc}")
            continue
        mods: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
        for m in sorted(mods):
            if m in stdlib or m in LOCAL or m in declared or m in OPTIONAL:
                continue
            missing.append(f"{rel}: imports undeclared module {m!r}")
    return missing


def _release_notes(version: str, included: list[str], excluded: list[tuple[str, str]],
                   manifest_lines: list[str], *, stamped: bool) -> str:
    # The in-archive copy carries NO wall-clock timestamp: it would make the
    # archive's SHA-256 differ on every rebuild from identical sources, which
    # defeats the point of publishing a checksum. The out-of-archive copy
    # beside the archive is stamped, for the operator's own records.
    built = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if stamped
        else "(omitted - see the stamped copy beside the archive; omitting it "
             "keeps the archive byte-reproducible)"
    )
    by_top: dict[str, int] = {}
    for rel in included:
        top = rel.split("/", 1)[0] if "/" in rel else "(root)"
        by_top[top] = by_top.get(top, 0) + 1
    tree = "\n".join(f"  {k:<16} {v:>4} file(s)" for k, v in sorted(by_top.items()))
    return f"""Mailops - Linux release package
================================
version        : {version}
built (UTC)    : {built}
source tree    : {ROOT}
members        : {len(included)} source files + MANIFEST.sha256 + RELEASE.txt
target         : Amazon Linux 2023, CPython 3.12.x, service user "mailops",
                 install root /opt/mailops, venv /opt/mailops/venv

Contents by top-level path
--------------------------
{tree}

What is deliberately NOT in this archive
----------------------------------------
* .env and any real credential, token, bot token or mailbox password
* secrets/ (DPAPI-sealed .enc blobs - undecryptable on Linux by design)
* tokens/ (OAuth token cache)
* data/ops.db (live operational SQLite database)
* data/settings.json (live 39-mailbox registry / policies / business context)
* himalaya/accounts.json and himalaya/config.toml (live mailbox registry;
  safe templates plus scripts/render_himalaya_linux_config.py are included
  instead)
* data/*.log, data/*.pid, data/web-tls/* (runtime state and self-signed certs)
* backups/ and every *.bak-* timestamped snapshot
* Windows binaries and launchers: *.exe, *.dll, *.pyd, *.pyc, *.ps1, *.bat,
  *.vbs ("Start Mailops.bat/.vbs", start.ps1, scripts/*.ps1)
* .venv/ and any virtual environment
* .git/ (no repository history, no remotes, no commit metadata)

Install
-------
Read docs/AWS_RELEASE_TRANSFER.md. Nothing in this archive starts the bot:
the systemd unit is a *.template that must be copied into place manually, and
deploy/env.linux.template ships with MAILOPS_TEST_MODE=1 and an empty
TELEGRAM_BOT_TOKEN so that an accidental launch cannot poll Telegram or touch
a mailbox.

Integrity
---------
MANIFEST.sha256 inside this archive lists the SHA-256 of every member.
Verify after extraction with:
    cd mailops-linux-{version} && sha256sum -c MANIFEST.sha256
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the Linux release archive")
    ap.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    ap.add_argument("--version", default=time.strftime("%Y.%m.%d", time.gmtime()))
    ap.add_argument("--allow-secrets", action="store_true",
                    help="report instead of aborting (NOT for real releases)")
    args = ap.parse_args(argv)
    return build(args.outdir.resolve(), args.version, strict=not args.allow_secrets)


if __name__ == "__main__":
    raise SystemExit(main())
