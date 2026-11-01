"""Independent verification of an extracted Mailops Linux release.

Deliberately does NOT import the builder: it re-derives every claim from the
extracted tree alone, so a bug in build_linux_release.py cannot vouch for
itself. Run it against the extracted directory, on Windows or Linux.

    python scripts/verify_linux_release.py <extracted-dir> [--archive <tar.gz>]

Checks
------
 1. MANIFEST.sha256 covers every file and every hash matches.
 2. No secret/private-state file (by name or extension).
 3. No Windows binary, bytecode or launcher script.
 4. No .git / .venv / backups / secrets / tokens directory.
 5. No credential-shaped string in any file's content.
 6. Every third-party import is declared in a shipped manifest.
 7. Every .py file compiles under this interpreter.
 8. Every shipped manifest pin is resolvable syntax and Linux-compatible.
 9. Config templates carry no filled-in secret values.
10. Optional: the archive's SHA-256 matches its .sha256 sidecar.

Exit code 0 = every check passed; 1 = at least one failed.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import re
import sys
from pathlib import Path, PurePosixPath

FORBIDDEN_NAMES = {".env", "ops.db", "settings.json", "accounts.json",
                   "config.toml", "mailops.pid", "mailops.log",
                   "google-oauth-client.json", "gmail.json", "acct1.json"}
FORBIDDEN_SUFFIXES = {
    # secrets / private state
    ".enc", ".secret", ".pem", ".key", ".crt", ".p12", ".pfx",
    ".db", ".sqlite", ".sqlite3", ".log", ".pid",
    # Windows artifacts
    ".exe", ".dll", ".pyd", ".pyc", ".pyo", ".msi", ".lnk",
    ".ps1", ".bat", ".cmd", ".vbs",
    # backups / scratch
    ".bak", ".orig", ".rej", ".tmp", ".swp", ".pre", ".original",
}
FORBIDDEN_DIRS = {".git", ".venv", "venv", "backups", "secrets", "tokens",
                  "__pycache__", ".pytest_cache", "web-tls"}

SECRET_RULES = (
    ("telegram bot token", r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    ("AWS access key id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("private key PEM", r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    ("OpenAI api key", r"\bsk-[A-Za-z0-9]{32,}\b"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    ("Google oauth client secret", r"\bGOCSPX-[A-Za-z0-9_-]{20,}\b"),
)
PLACEHOLDERS = ("example", "dummy", "fake", "placeholder", "redacted",
                "xxxx", "0000", "test", "sample", "your-", "<")
# These files document the *shape* of a secret, so a pattern hit is expected.
SCAN_SKIP = {"mailops/redact.py", "tests/test_redaction.py",
             "scripts/build_linux_release.py", "scripts/verify_linux_release.py",
             "docs/LINUX_COMPATIBILITY.md"}

OPTIONAL_MODULES = {"boto3", "botocore", "dns", "openpyxl", "tomli"}
LOCAL_MODULES = {"mailops", "tests", "scripts", "conftest"}
DIST_TO_MODULES = {
    "python-telegram-bot": {"telegram"},
    "typing-extensions": {"typing_extensions"},
    "charset-normalizer": {"charset_normalizer"},
}
WINDOWS_ONLY_DISTS = {"pywin32", "pywin32-ctypes", "colorama", "winshell",
                      "wmi", "pyreadline", "pyreadline3"}
MANIFESTS = ("requirements.txt", "deploy/requirements-linux.txt",
             "deploy/requirements-linux.lock.txt")


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.n = 0

    def check(self, name: str, problems: list[str], detail: str = "") -> None:
        self.n += 1
        if problems:
            self.failures.append(name)
            print(f"[FAIL] {name}")
            for p in problems[:25]:
                print(f"         {p}")
            if len(problems) > 25:
                print(f"         ... and {len(problems) - 25} more")
        else:
            print(f"[ OK ] {name}" + (f"  ({detail})" if detail else ""))


def _files(root: Path) -> list[tuple[str, Path]]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out.append((p.relative_to(root).as_posix(), p))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tree", type=Path, help="extracted release directory")
    ap.add_argument("--archive", type=Path, help="the .tar.gz, to check its .sha256 sidecar")
    args = ap.parse_args(argv)

    root: Path = args.tree.resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 1

    print(f"verifying {root}\n")
    rep = Report()
    files = _files(root)
    rel_to_path = dict(files)

    # ---- 1. manifest
    problems: list[str] = []
    mf = root / "MANIFEST.sha256"
    if not mf.is_file():
        problems.append("MANIFEST.sha256 is missing")
    else:
        listed: dict[str, str] = {}
        for line in mf.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            digest, _, name = line.partition("  ")
            listed[name.strip()] = digest.strip()
        generated = {"MANIFEST.sha256", "RELEASE.txt"}
        on_disk = {r for r in rel_to_path if r not in generated}
        for rel in sorted(on_disk - set(listed)):
            problems.append(f"present but not in manifest: {rel}")
        for rel in sorted(set(listed) - on_disk):
            problems.append(f"in manifest but missing on disk: {rel}")
        for rel in sorted(set(listed) & on_disk):
            actual = hashlib.sha256(rel_to_path[rel].read_bytes()).hexdigest()
            if actual != listed[rel]:
                problems.append(f"hash mismatch: {rel}")
    rep.check("1. MANIFEST.sha256 complete and every hash matches", problems,
              f"{len(files)} files")

    # ---- 2/3. forbidden names & extensions
    secret_hits, win_hits = [], []
    for rel, p in files:
        name = PurePosixPath(rel).name
        suffix = PurePosixPath(rel).suffix.lower()
        if name in FORBIDDEN_NAMES and name != ".env.example":
            secret_hits.append(f"{rel} (forbidden name)")
        if suffix in {".exe", ".dll", ".pyd", ".pyc", ".pyo", ".msi", ".lnk",
                      ".ps1", ".bat", ".cmd", ".vbs"}:
            win_hits.append(f"{rel} (Windows artifact)")
        elif suffix in FORBIDDEN_SUFFIXES:
            secret_hits.append(f"{rel} (forbidden extension {suffix})")
        if re.search(r"\.bak-|\.bak\.|\.pre-", rel):
            secret_hits.append(f"{rel} (timestamped backup)")
    rep.check("2. no secret or private-state file", secret_hits)
    rep.check("3. no Windows binary, bytecode or launcher", win_hits)

    # ---- 4. forbidden directories
    problems = []
    seen_dirs = {part for rel in rel_to_path for part in PurePosixPath(rel).parts[:-1]}
    for d in sorted(seen_dirs & FORBIDDEN_DIRS):
        problems.append(f"directory present: {d}/")
    rep.check("4. no .git / .venv / backups / secrets / tokens directory", problems)

    # ---- 5. content secret scan
    problems = []
    rules = [(lbl, re.compile(rx)) for lbl, rx in SECRET_RULES]
    for rel, p in files:
        if rel in SCAN_SKIP:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"{rel}: not UTF-8 text (unexpected binary)")
            continue
        for lbl, rx in rules:
            for m in rx.finditer(text):
                if any(ph in m.group(0).lower() for ph in PLACEHOLDERS):
                    continue
                problems.append(f"{rel}:{text[:m.start()].count(chr(10)) + 1}: {lbl}")
    rep.check("5. no credential-shaped string in any file", problems)

    # ---- 6. dependency manifest completeness
    declared: set[str] = set()
    pins: list[tuple[str, str]] = []
    for rel in MANIFESTS:
        p = rel_to_path.get(rel)
        if p is None:
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            dist = re.split(r"[=<>!~\[;]", line, maxsplit=1)[0].strip().lower()
            declared |= DIST_TO_MODULES.get(dist, {dist.replace("-", "_")})
            pins.append((rel, line))

    problems = []
    stdlib = set(sys.stdlib_module_names)
    py_files = [(r, p) for r, p in files if r.endswith(".py")]
    for rel, p in py_files:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig"), filename=rel)
        except SyntaxError as exc:
            problems.append(f"{rel}: syntax error {exc}")
            continue
        mods: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
        for m in sorted(mods):
            if m in stdlib or m in LOCAL_MODULES or m in declared or m in OPTIONAL_MODULES:
                continue
            problems.append(f"{rel}: undeclared third-party import {m!r}")
    rep.check("6. every third-party import is declared", problems,
              f"{len(declared)} distributions declared")

    # ---- 7. all .py compile
    problems = []
    for rel, p in py_files:
        try:
            compile(p.read_text(encoding="utf-8-sig"), rel, "exec")
        except SyntaxError as exc:
            problems.append(f"{rel}: {exc}")
    rep.check("7. every .py file compiles", problems,
              f"{len(py_files)} files, python {sys.version_info.major}.{sys.version_info.minor}")

    # ---- 8. manifests present, parseable, Linux-safe
    problems = []
    for rel in MANIFESTS:
        if rel not in rel_to_path:
            problems.append(f"manifest missing from archive: {rel}")
    for rel, line in pins:
        dist = re.split(r"[=<>!~\[;]", line, maxsplit=1)[0].strip().lower()
        if dist in WINDOWS_ONLY_DISTS:
            problems.append(f"{rel}: Windows-only distribution pinned: {line}")
        if not re.match(r"^[A-Za-z0-9._-]+(\[[^\]]+\])?\s*(==|>=|<=|~=|!=|<|>).+$", line):
            problems.append(f"{rel}: unpinned or unparseable requirement: {line!r}")
    lock = rel_to_path.get("deploy/requirements-linux.lock.txt")
    if lock is not None:
        n = sum(1 for l in lock.read_text(encoding="utf-8").splitlines()
                if l.strip() and not l.strip().startswith("#"))
        if n < 10:
            problems.append(f"lock file has only {n} pins; expected the full closure")
    rep.check("8. dependency manifests complete, pinned and Linux-safe", problems,
              f"{len(pins)} pins across {len(MANIFESTS)} manifests")

    # ---- 9. config templates carry no filled-in secrets
    problems = []
    SECRET_ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "GOOGLE_CLIENT_ID",
                       "GOOGLE_CLIENT_SECRET", "TELEGRAM_ALLOWED_USERS",
                       "TELEGRAM_OWNER_ID", "TELEGRAM_HOME_CHANNEL")
    for rel in (".env.example", "deploy/env.linux.template"):
        p = rel_to_path.get(rel)
        if p is None:
            problems.append(f"template missing: {rel}")
            continue
        for i, raw in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1):
            line = raw.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() in SECRET_ENV_KEYS and v.strip():
                problems.append(f"{rel}:{i}: {k.strip()} has a non-empty value")
    tmpl = rel_to_path.get("deploy/settings.linux.template.json")
    if tmpl is None:
        problems.append("template missing: deploy/settings.linux.template.json")
    else:
        import json
        data = json.loads(tmpl.read_text(encoding="utf-8"))
        if data.get("active_accounts"):
            problems.append("settings template ships a non-empty active_accounts registry")
        if data.get("account_policies"):
            problems.append("settings template ships non-empty account_policies (must be deny-by-default)")
        if data.get("test_mode") is not True:
            problems.append("settings template does not default to test_mode=true")
    toml = rel_to_path.get("deploy/himalaya-config.linux.template.toml")
    if toml is None:
        problems.append("template missing: deploy/himalaya-config.linux.template.toml")
    else:
        # Strip full-line comments: the template's header intentionally
        # *documents* the Windows path it migrates away from.
        active = "\n".join(
            l for l in toml.read_text(encoding="utf-8").splitlines()
            if not l.lstrip().startswith("#")
        )
        if re.search(r'^\s*pass(?:wd|word)\s*=\s*"', active, re.MULTILINE):
            problems.append("himalaya template contains an inline password assignment")
        if "python.exe" in active or re.search(r"\b[A-Za-z]:/", active):
            problems.append("himalaya template still references a Windows interpreter path")
        if "passwd.command" not in active:
            problems.append("himalaya template has no passwd.command (password would be inline)")
        if active.count("get-secret") < 2:
            problems.append("himalaya template does not route both imap and smtp via get-secret")
    rep.check("9. config templates are secret-free and safe-by-default", problems)

    # ---- 10. internal module graph is complete
    problems = []
    pkg_modules: set[str] = set()
    for rel, _ in files:
        if rel.endswith(".py"):
            mod = rel[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            pkg_modules.add(mod)
    for rel, p in py_files:
        pkg_parts = rel[:-3].split("/")[:-1]  # package path of this file
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig"), filename=rel)
        except SyntaxError:
            continue  # already reported by check 7
        targets: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:  # relative: from .x / from ..x import y
                    base = pkg_parts[: len(pkg_parts) - (node.level - 1)] if node.level > 1 else pkg_parts
                    head = list(base) + ([node.module] if node.module else [])
                    for alias in node.names:
                        targets.add(".".join(head + [alias.name]))
                        targets.add(".".join(head))
                elif node.module and node.module.split(".")[0] in LOCAL_MODULES:
                    for alias in node.names:
                        targets.add(f"{node.module}.{alias.name}")
                    targets.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in LOCAL_MODULES:
                        targets.add(alias.name)
        for t in sorted(targets):
            if not t:
                continue
            # A target is satisfied if it is a shipped module/package, or if its
            # parent is shipped (then it is a NAME inside that module).
            parent = t.rsplit(".", 1)[0]
            if t in pkg_modules or parent in pkg_modules:
                continue
            problems.append(f"{rel}: imports missing internal module {t!r}")
    rep.check("10. internal module graph complete (no module dropped by a deny rule)",
              problems, f"{len(pkg_modules)} shipped modules")

    # ---- 11. archive checksum sidecar
    if args.archive:
        problems = []
        arc: Path = args.archive.resolve()
        side = arc.with_name(arc.name + ".sha256")
        if not arc.is_file():
            problems.append(f"archive not found: {arc}")
        elif not side.is_file():
            problems.append(f"sidecar not found: {side}")
        else:
            expected = side.read_text(encoding="utf-8").split()[0].strip().lower()
            actual = hashlib.sha256(arc.read_bytes()).hexdigest()
            if actual != expected:
                problems.append(f"archive sha256 {actual} != sidecar {expected}")
        rep.check("11. archive SHA-256 matches its .sha256 sidecar", problems)

    print()
    if rep.failures:
        print(f"RESULT: {len(rep.failures)}/{rep.n} CHECKS FAILED -> {', '.join(rep.failures)}")
        return 1
    print(f"RESULT: all {rep.n} checks passed. Archive is clean and complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
