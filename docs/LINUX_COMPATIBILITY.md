# Mailops — Linux Compatibility Audit (Amazon Linux 2023 / CPython 3.12)

Findings from auditing the **current** local code (including the group-first
`gr:` Telegram namespace and the `MailboxUnavailable` cache/IMAP failure
handling) against the prepared `dm-mini` host. Verified by direct source read
plus an AST import scan of every shipped `.py` file.

Severity key: **BLOCKER** = must be resolved before the bot can run correctly
on Linux. **CONFIG** = works, but needs a specific setting. **OK** = portable
as-is, recorded so it isn't re-investigated.

---

## BLOCKER 1 — DPAPI-sealed credentials cannot be decrypted on Linux

`mailops/secrets_vault.py` writes a self-describing envelope. The 78 live
`secrets/*.enc` blobs all start with the `\x01DPAPI\x00` marker, and
`open_envelope()` is explicit:

```
if blob.startswith(_DPAPI_MARK):
    if not _dpapi_available():
        raise VaultError("DPAPI envelope but DPAPI not available on this platform")
```

`_dpapi_available()` returns `False` whenever `os.name != "nt"`. **Copying
`secrets/` to Linux therefore produces 78 unreadable files and every mailbox
fails to authenticate.** This is by design (the DPAPI master key is bound to
the Windows user+machine and cannot be exported).

Two exceptions worth knowing: `altcoininformer`'s two credentials are still
legacy plaintext `.secret` files, which *would* port — but they are excluded
from the archive anyway, correctly.

**Resolution (pick one, both already built and tested):**

- `MAILOPS_VAULT_BACKEND=ssm` + `scripts/migrate_secrets_to_aws.py --apply
  --target ssm` → AWS SSM SecureString + KMS. Needs `boto3` (not in the
  default manifest; see `deploy/requirements-linux.txt`), an instance-role
  policy and a CMK. **Requires AWS IAM changes, which are out of scope for
  this pass** — nothing here does it for you.
- `--target local-fernet` → stages a fresh Fernet-sealed `secrets/` plus a new
  `master.fernet`, hand-carried. No IAM change needed.
- Or simply re-onboard each mailbox through the bot on Linux (Smart Add /
  Quick Add / bulk CSV), which seals with Fernet natively.

---

## BLOCKER 2 — `MAILOPS_KEY_DIR` must be set explicitly on Linux

This one is subtle and is *not* in the existing planning docs.

`secrets_vault._key_dir()` defaults to `~/.config/mailops/keys` on POSIX. But
`himalaya_client.Himalaya._proc_env()` deliberately overrides `HOME` for the
himalaya child process:

```188:197:mailops/himalaya_client.py
    def _proc_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(paths.PROFILE)
        env["XDG_CONFIG_HOME"] = str(paths.HIMALAYA_DIR.parent)
```

Himalaya then executes `passwd.command` → `python -m mailops get-secret ...`
as a grandchild, which inherits that overridden `HOME`. So:

- the **bot** process resolves the Fernet key to `/home/mailops/.config/mailops/keys`
- the **get-secret** child resolves it to `$MAILOPS_HOME/.config/mailops/keys`

Two different keys. The child would silently *generate a second key*, then
fail to decrypt blobs sealed with the first — surfacing as
`fernet envelope could not be decrypted` on every IMAP check, with nothing
obviously wrong in the config.

On Windows this never bites, because `_key_dir()` uses `%LOCALAPPDATA%` and
the `HOME` override is irrelevant.

**Resolution:** set `MAILOPS_KEY_DIR=/opt/mailops/keys` (an absolute path) in
both `/opt/mailops/.env` **and** the systemd unit. Both shipped templates
already do this, with the reason inline. Only applies to the `file` backend;
the `ssm` backend has no local key.

---

## BLOCKER 3 — `himalaya/config.toml` hard-codes the Windows interpreter

All **78** `passwd.command` entries (39 accounts × imap+smtp) begin with
`C:/Users/Global/Downloads/Email-Notifier-Agent/.venv/Scripts/python.exe`.
On Linux every one fails to exec, so every mailbox fails to authenticate.

**Resolution:** `scripts/render_himalaya_linux_config.py` (new, shipped). It
is read-only on the input, rewrites only that one array element per entry,
refuses to emit a file containing an inline password, and refuses to
overwrite an existing output. Verified against the live file in dry-run mode:
78 entries found, 39 accounts, 78 rewrites, source unmodified.

Newly-onboarded mailboxes need no fix:
`webmail_onboarding.render_account_block` uses `sys.executable`.

---

## BLOCKER 4 — the venv path in the existing systemd template is wrong

`deploy/mailops.service.template` (the earlier planning artifact) specifies
`/opt/mailops/.venv/bin/python` and `-m mailops.main`. The prepared host has
`/opt/mailops/venv` (no dot). As written the unit fails with
`status=203/EXEC`.

**Resolution:** `deploy/mailops.service.dm-mini.template` (new, shipped)
targets the host as actually prepared: `/opt/mailops/venv/bin/python -m
mailops run`. It also keeps `MAILOPS_VAULT_BACKEND=ssm` commented out (the
old template forced it on, which would crash at first credential read because
`boto3` is not installed), and widens `ReadWritePaths=` to include
`keys/`, `tokens/` and `himalaya/`, which the bot writes to during onboarding
but the old template omitted — those would have failed under
`ProtectSystem=strict`.

---

## CONFIG 1 — `timezone` is `Asia/Kolkata`; AL2023 may lack tzdata

`data/settings.json` carries `"timezone": "Asia/Kolkata"`. Minimal Amazon
Linux 2023 images do not always include the full zoneinfo database, and
CPython's `zoneinfo` has no bundled fallback unless the `tzdata` wheel is
installed.

Mitigation: current code does arithmetic on the integer `quiet_hours_*` rather
than constructing a `ZoneInfo` (an audit for `zoneinfo`/`ZoneInfo` found no
usages), so this is latent rather than live. Still worth closing:

```bash
sudo dnf install -y tzdata     # or: pip install tzdata
```

---

## CONFIG 2 — optional dependencies are intentionally absent

Three imports are lazy and feature-gated, documented in
`deploy/requirements-linux.txt` and enforced by the release builder's
dependency gate:

| Module | Needed when | Behavior if absent |
|---|---|---|
| `boto3` | `MAILOPS_VAULT_BACKEND=ssm` | `VaultError` with an actionable message |
| `dns` (dnspython) | `MAILOPS_AUTOCONFIG_MX=1` | `_mx_hint()` returns `None`; autoconfig falls back to pattern matching |
| `openpyxl` | `scripts/siteground_bulk_import.py` with `.xlsx` input | that one script only; CSV input needs nothing |

---

## OK — verified portable

- **No Windows-only runtime dependency.** The full transitive closure
  (`deploy/requirements-linux.lock.txt`, 22 packages) was resolved with PEP 508
  markers evaluated for `sys_platform=linux`; `colorama` and `pywin32-ctypes`
  drop out by construction. Every member is pure Python or ships a manylinux
  x86_64 wheel for CPython 3.12 — no compiler needed on the instance.
- **`singleton.py`** branches on `os.name`; the POSIX path uses
  `os.kill(pid, 0)`. Correct, and strictly better than the Windows path.
- **`himalaya_bin()`** checks `HIMALAYA_BIN` env → `.env` → `PATH` → Windows
  candidate paths. On Linux it resolves via `PATH` and the Windows list is
  never reached. Zero changes needed.
- **`secrets_vault._restrict_user_only()`** uses `icacls` on `nt` and
  `os.chmod(0o600)` elsewhere. Correct.
- **`paths.py`** is `pathlib`-based throughout with `MAILOPS_HOME` /
  `MAILOPS_HIMALAYA_DIR` overrides; no hard-coded drive letters outside the
  himalaya-binary candidate list.
- **Subprocess calls** (`himalaya_client.run`) pass an argv list with
  `text=True, encoding="utf-8"` and no `shell=True`, no `creationflags`, no
  `STARTUPINFO`. Portable.
- **IMAP failure handling** (`MailboxUnavailable`, raised in
  `himalaya_client.envelopes_checked`, handled in `cache.py` at 6 call sites
  and surfaced via `health.py`) is pure exception plumbing over subprocess
  exit codes — no OS-specific error numbers are matched, so a Linux
  `ECONNREFUSED` is classified the same as the Windows `os error 10060`.
- **The group-first `gr:` namespace** adds only Telegram callback routing, an
  in-memory TTL'd `WAITING_TEXT` dict and the existing `group_links` SQLite
  table. No new OS dependency, no new scheduler, no new secret path.
- **Line endings / encoding.** Every file read uses explicit
  `encoding="utf-8"` or `utf-8-sig`; three files carry a UTF-8 BOM
  (`mailops/classifier.py`, `tests/test_cli_logging_and_legacy_ids.py`,
  `tests/test_telegram_group.py`) which `utf-8-sig` and CPython's own source
  loader both handle. Not a blocker, but the AST gate in the release builder
  decodes with `utf-8-sig` specifically because of them.

---

## Fixed during this audit

**Live Google OAuth client credentials were hard-coded in a test fixture.**
`tests/test_gmail_oauth.py` contained the real `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET` from `.env` as regression-test literals (3 occurrences
of the client id, 2 of the `GOCSPX-` secret). The release builder's negative
scan — which compares staged content against the live `.env` values — caught
them and refused to build.

Replaced with `000000000000-exampleexample…` / `GOCSPX-ExampleExample…`
placeholders of identical length and shape, so the parser-regression
assertions still prove exactly what they did before. These values were only
ever used as *input strings* to `clean_client_id()` / `clean_client_secret()`
— no test authenticated with them — so the fix is behavior-preserving.

> Note: these credentials are for the Gmail/OAuth path, which this deployment
> does not use (`GMAIL_ENABLED` is off, all 39 accounts are IMAP/SMTP). They
> are still worth rotating, since they were present in the working tree.
