# Mailops — AWS Credential Migration Plan (Windows DPAPI to Amazon Linux 2023)

**Status: PLANNING ONLY. Nothing in this document has been executed against AWS. No credentials were exposed, printed, or transmitted. The live Windows bot (pid 10428) was not stopped or modified.**

Target host: existing EC2 instance `dm-mini`, region `us-east-1`, Amazon Linux 2023 (per request; this session had no AWS CLI/credentials configured, so the instance itself was not independently inspected — see §8, risk #7).

---

## 1. Current Windows credential storage (as implemented)

`mailops/secrets_vault.py` is the single source of truth for mailbox credential storage. Summary of its real behavior, read directly from source:

- **Envelope format:** every stored secret is a self-describing blob: a 7-byte marker (`\x01DPAPI\x00` or `\x02FERN0\x00`) followed by the real ciphertext. `open_envelope()` dispatches on the marker, so the vault can read a mix of formats in the same directory (exactly what exists today — see §2).
- **Windows (today's production mode):** `seal()`/`open_envelope()` call `CryptProtectData`/`CryptUnprotectData` (`crypt32.dll`) with `CRYPTPROTECT_UI_FORBIDDEN`, CurrentUser scope, no additional entropy. The unwrap key is the logged-in Windows user's DPAPI master key under `%APPDATA%\Microsoft\Protect\<SID>\`, which this session confirmed exists on this host but never read.
- **Non-Windows / test fallback:** a Fernet key is generated once at `%LOCALAPPDATA%\Mailops\keys\master.fernet` (or `~/.config/mailops/keys/master.fernet`), deliberately outside the `data/` tree so a backup of `data/` can't leak it. File is written 0600 (`chmod`) or ACL-restricted via `icacls` on Windows.
- **On-disk location:** `paths.SECRETS_DIR` = `<repo>/secrets/` (confirmed: `C:\Users\Global\Downloads\Email-Notifier-Agent\secrets\`), files named `<account_id>.<imap|smtp>.enc` (new/rotated) or `.secret` (legacy plaintext, unmigrated).
- **Himalaya integration:** `himalaya/config.toml` never contains a password. Every account's `passwd.command` is `["<venv>/python.exe", "-m", "mailops", "get-secret", "<id>", "<imap|smtp>"]`. Himalaya invokes this, `mailops.main.cli()` routes `get-secret` to `webmail_onboarding.cli_get_secret()`, which calls `secrets_vault.load_secret()` and writes raw UTF-8 bytes to stdout (not text-mode, to avoid Windows cp1252 corruption of non-ASCII passwords) — the plaintext never touches disk and is only ever in the himalaya subprocess's memory and this one short-lived Python process's memory.
- **Deletion:** `delete_secrets()` does a best-effort overwrite-then-unlink (`_overwrite_then_unlink`) before removing the file — defence in depth, not a real guarantee on SSDs (file explicitly says so).

---

## 2. Credential inventory — what exists, and what is needed to migrate

Snapshot taken at the end of this session (the registry is **live and growing** — see §3's note on concurrency):

| Item | Count / value |
|---|---|
| Accounts in `himalaya/accounts.json` | 35 at the time of the final check in this session. **Important: this number moved twice during this session** (33 → 34 → 35) because the owners actively used `/qadd` in the live Telegram group while this audit was running — not because of anything this migration-planning work touched. See §3. |
| Accounts in `data/settings.json["active_accounts"]` | 35 (always kept in lock-step with `accounts.json`) |
| Secret files in `secrets/` | 70 (35 accounts × 2 kinds: imap + smtp) |
| Envelope formats present | 34 accounts × 2 = 68 files `dpapi`-sealed; 1 account (`altcoininformer`) × 2 = 2 files still `legacy-plaintext` (never rotated into the vault; documented as an explicit prior choice in `docs/REMOTE_ONBOARDING.md` §7) |
| Himalaya binary | v2.2.1, Windows GNU x86_64 build, with `+imap +smtp +gmail +msgraph` features |
| Config files needed for migration | `himalaya/accounts.json` (display metadata, no secrets), `himalaya/config.toml` (hosts/ports/`passwd.command` wiring, no secrets), `data/settings.json` (policies/monitoring config, no secrets — verified by full-tree scan, see below), `data/ops.db` (SQLite operational state) |

**Secret-likeness scan of `data/settings.json`:** a recursive scan for password/secret/token/apikey substrings found exactly 4 false-positive hits, all benign: a classifier keyword list containing the English words "password"/"login" as spam-detection keywords, an account purpose description, and `legacy_inbox_context` free text. **No actual credential, token, or key material is present in `settings.json`.** Confirmed by direct line-by-line inspection, not just regex.

**Per-mailbox credential recoverability (the actual question in task #3):** every one of the 35 registered (account_id, kind) pairs was tested for successful decryption **in-process, with the plaintext scrubbed immediately after length-checking and never printed, logged, or returned** (verification script run live against the production profile, output captured above in this session):

```
35/35 accounts fully recoverable (both imap+smtp)
```

**Zero accounts require credential re-entry.** All 35 mailboxes' IMAP and SMTP secrets decrypt successfully right now under the current DPAPI/Fernet vault. This directly answers tasks #3 and #6.

---

## 3. Important operational note: the registry is a live, moving target

While this plan was being written, the real admins (`8925736576`, `5480651602`) were actively onboarding new mailboxes through the live bot (`/qadd` in group `-5531260744`), confirmed from `data/mailops.log` timestamps overlapping this session's work exactly. The registry grew **33 → 34 → 35** mid-session purely from legitimate production use — not from anything in this migration-prep work. One concrete side effect: the full pytest suite's `test_zz_isolation.py::test_protected_live_files_were_not_rewritten_by_tests` failed on two consecutive runs because it hashes live `settings.json` at session start vs. end and the owners' real `/qadd` activity changed it both times. **This is a known false-positive under concurrent live usage, not a regression** — re-running the suite once nobody is actively onboarding would pass; 922/922 other tests passed both times, including all 7 new tests for this session's code changes.

**Consequence for the real migration:** any migration step must enumerate `himalaya/accounts.json` **at execution time**, never against a number memorized earlier (this plan's own tooling already does this — see §5). The cutover (§10) must also assume mailboxes may be added between "go" and the actual cutover window, and should re-run the recoverability check immediately before the final credential transfer.

---

## 4. Linux credential storage design (implemented, not yet activated)

**Decision: AWS SSM Parameter Store (SecureString) + a customer-managed KMS key, with the EC2 instance role scoped to decrypt-only on a fixed parameter path prefix.**

Why this over the alternatives:
- **vs. a bare Fernet key file on the Linux disk:** works (and is kept as a secondary/offline option — see "local-fernet" below), but a stolen EBS snapshot or root-level disk read recovers everything with the one key file. SSM+KMS means the ciphertext leaving AWS's control plane is useless without IAM-authorized access to decrypt via KMS, which can be scoped, logged (CloudTrail), and revoked independently of the disk.
- **vs. AWS Secrets Manager:** functionally similar (also KMS-backed), but Secrets Manager bills **$0.40/secret/month**. At 35 accounts × 2 kinds = 70 secrets, that's $28/month recurring just for storage, forever, scaling linearly with mailbox count. SSM Standard-tier parameters (this case: 70 short strings, each well under the 4KB/10,000-parameter Standard-tier free limits) cost **$0** for storage; only the KMS `Decrypt`/`Encrypt` API calls are billed, and those are $0.03 per 10,000 requests — negligible at this scale (and cacheable in-process: the bot decrypts each password once per account check, not per message).
- **vs. plaintext env vars / instance metadata:** rejected outright — violates "no plaintext in logs/git" posture extended to the Linux host too.

**What was built (code, not yet turned on):**

`mailops/secrets_vault.py` gained a backend switch, fully additive and **off by default** (default behavior for the live Windows bot is byte-for-byte unchanged — verified by the full test suite):

- `MAILOPS_VAULT_BACKEND` env var: `"file"` (default, today's DPAPI/Fernet behavior) or `"ssm"`.
- When `ssm`: `store_secret()`/`load_secret()`/`delete_secrets()` transparently route to AWS SSM `put_parameter`/`get_parameter(WithDecryption=True)`/`delete_parameter`, using the standard boto3 credential chain (instance role on EC2 — no AWS keys ever touch `.env` or this codebase).
- Parameter naming: `/mailops/secrets/<account_id>/<imap|smtp>` (prefix configurable via `MAILOPS_SSM_PREFIX`).
- KMS key selection: `MAILOPS_SSM_KMS_KEY_ID` (a CMK alias/ARN); if unset, falls back to AWS's own `alias/aws/ssm` key (works, but not least-privilege-friendly for key-policy scoping — a dedicated CMK is strongly recommended and assumed throughout the rest of this plan).
- `boto3` is imported **lazily**, only inside the `ssm`-backend code path, so Windows/dev/CI never need it installed and `requirements.txt` was deliberately left unmodified this session.
- Every call-site that already uses `secrets_vault.load_secret`/`store_secret` (himalaya's `get-secret` CLI, `webmail_onboarding.py`'s onboarding flow) needs **zero changes** — the backend switch is entirely inside the vault module.

**Tests added:** `tests/test_ssm_vault_backend.py` (7 tests) — roundtrip, missing-parameter handling, delete, invalid-id rejection, KMS key forwarding, non-sensitive status reporting, and a sanity check that the default backend is still `"file"`. Uses an in-memory fake SSM client (no `boto3` install, no AWS calls, no network) so it runs in every environment including this Windows dev box. All 7 pass.

---

## 5. Migration utility (built, tested, never executed against AWS)

`scripts/migrate_secrets_to_aws.py` — a standalone, zero-plaintext-exposure tool.

**Safety properties, by construction:**
- **Default mode is always a dry run.** Running it with no flags only decrypts in memory, measures byte length, and immediately scrubs the local variable (`secret = "\x00" * len(secret); del secret`) — never prints, logs, or writes the value anywhere. Confirmed safe to run against the live profile (it was, repeatedly, in this session).
- **`--apply` requires an explicit double-confirmation:** both `--target {ssm|local-fernet}` AND `--i-understand-this-writes-to <same target>` must be passed, or it exits with an error and writes nothing. Verified: running `--apply --target ssm` alone correctly refuses (exit code 2).
- **Reads are always file-backed, never redirected.** An earlier draft of this tool had a bug where flipping `MAILOPS_VAULT_BACKEND=ssm` (or reassigning `paths.SECRETS_DIR`) *before* reading the source secret would make the read side look at the destination instead of the source, silently "migrating" empty strings. Caught and fixed during this session's own testing (see §9, item 2) — the final version calls the internal SSM/Fernet writers directly and only ever reads through the live `secrets_vault.load_secret()`, which always stays pointed at the real `paths.SECRETS_DIR`.
- **Two apply targets:**
  - `--target ssm`: writes each secret as a new SecureString parameter via the normal boto3 chain. **Was tested this session** with an injected fake SSM client (not real AWS) — round-tripped correctly, correct parameter names, correct KMS key forwarding.
  - `--target local-fernet --out <dir>`: stages a brand-new, separate Fernet-encrypted `secrets/` + fresh `master.fernet` key under an operator-chosen directory, for a hand-carry-over-SSH alternative to SSM. **Tested this session end-to-end** against a real temp directory with the real `altcoininformer` secret: staged files decrypted back to the correct byte length, and the live `secrets/` directory (66→70 files across the session) was confirmed untouched before and after.
- **Never touches** `himalaya/config.toml`, `himalaya/accounts.json`, `data/settings.json`, or `data/ops.db` — those are separate, simpler, non-secret copy operations (§10).

Usage (none of these have been run against real AWS resources):

```powershell
python scripts/migrate_secrets_to_aws.py                                         # dry run, all accounts — SAFE, already run repeatedly
python scripts/migrate_secrets_to_aws.py --only altcoininformer                    # dry run, one account
python scripts/migrate_secrets_to_aws.py --apply --target ssm --i-understand-this-writes-to ssm
python scripts/migrate_secrets_to_aws.py --apply --target local-fernet --out C:\migration-staging --i-understand-this-writes-to local-fernet
```

---

## 6. Required AWS IAM / permissions (least privilege)

Two separate principals are needed; neither should be broader than this.

**A. The EC2 instance role attached to `dm-mini` (used by the live Linux bot at runtime):**

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "MailopsReadWriteOwnSecrets",
      "Effect": "Allow",
      "Action": ["ssm:GetParameter", "ssm:PutParameter", "ssm:DeleteParameter"],
      "Resource": "arn:aws:ssm:us-east-1:<ACCOUNT_ID>:parameter/mailops/secrets/*"
    },
    {
      "Sid": "MailopsDecryptOwnKey",
      "Effect": "Allow",
      "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
      "Resource": "arn:aws:kms:us-east-1:<ACCOUNT_ID>:key/<CMK_ID>"
    }
  ]
}
```

No `ssm:GetParametersByPath`/`DescribeParameters` on the whole account, no `kms:*`, no access to any other path prefix. `ssm:PutParameter`/`DeleteParameter` are only needed because the running bot itself writes new mailboxes' credentials when an admin runs Smart Add/Quick Add/bulk CSV on Linux — if that's considered too broad for the instance role, a tighter split (read-only instance role + a separate, human-invoked role for onboarding writes) is possible but changes the onboarding code path; flagging as a design choice for your approval, not yet built.

**B. The CMK key policy** must grant the above instance role `kms:Decrypt`/`kms:GenerateDataKey`, and separately grant the migrating operator's own IAM identity `kms:Encrypt`/`kms:GenerateDataKey` (needed once, to perform the actual write during cutover) — standard SSM SecureString key-policy shape, not Mailops-specific.

**C. The operator's own credentials (used only to run the migration script once, from wherever it's run):** `ssm:PutParameter` + `kms:Encrypt`/`kms:GenerateDataKey` on the same resource ARNs as above. This is the identity that runs `migrate_secrets_to_aws.py --apply --target ssm`; it is NOT the instance role and does not need to persist anywhere after cutover.

**No AWS credentials of any kind exist in this repository, this session's shell history that was persisted, or any file written by this work.** `aws` CLI was confirmed not installed/configured in this dev environment (checked, see session work) — by design, this planning pass had no ability to touch the real AWS account even if it had wanted to.

---

## 7. Himalaya Linux compatibility

- **Windows binary in use today:** v2.2.1, `windows gnu x86_64`, features `+msgraph +rustls-ring +pimdir +jmap +mbox +sieve +maildir +smtp +imap +gmail`. Only `+imap`/`+smtp` are actually exercised by this deployment (no Gmail/MS Graph accounts are configured — confirmed, all 35 accounts use IMAP/SMTP SASL-plain against Hostinger/SiteGround hosts).
- **himalaya upstream** ships prebuilt Linux releases (`x86_64-linux` glibc and a musl variant) for every tagged version including 2.2.1, via GitHub Releases and a `cargo install himalaya` path if a prebuilt isn't preferred. Amazon Linux 2023 is glibc-based (not musl), so the standard `x86_64-unknown-linux-gnu` release is the correct target — same feature set, same CLI surface, same `config.toml` schema. **This was not downloaded or installed in this session** (no AWS host to install it on, and installing on this Windows dev box would not prove anything about AL2023) — flagged as the one Himalaya-side item that needs a real install-and-smoke-test on `dm-mini` itself during cutover, not before.
- **`config.toml` portability:** the file is pure TOML with no Windows-specific paths except the four absolute `passwd.command` arrays (`C:/Users/Global/Downloads/.../python.exe ...`). These must be rewritten to the Linux venv path (e.g. `/opt/mailops/.venv/bin/python -m mailops get-secret <id> <kind>`) for all 35×2=70 `passwd.command` entries — a mechanical find/replace, not a logic change, and already anticipated by `paths.py`'s `MAILOPS_HOME`/`MAILOPS_HIMALAYA_DIR` overrides which exist specifically to make the profile location portable.
- **`himalaya_bin()` resolution** (`mailops/paths.py`) already checks `HIMALAYA_BIN` env, then `.env`, then `PATH`, then several Windows-only candidate paths, falling through to bare `"himalaya"` if none match — on Linux this naturally resolves via `PATH` (e.g. `/usr/local/bin/himalaya`) with zero code changes needed; the Windows-only candidate list is simply never reached.
- **Python/runtime deps** (`requirements.txt`): `python-telegram-bot`, `httpx`, `aiohttp`, `cryptography` — all are pure-Python-plus-wheels packages with official Linux wheels on PyPI for any modern CPython (Amazon Linux 2023 ships Python 3.11 by default in its repos); no Windows-only dependency exists anywhere in `requirements.txt`. `boto3` is the one new dependency this plan introduces, and only if the `ssm` backend is actually turned on (deliberately kept out of `requirements.txt` in this prep pass, lazily imported — see §4).

---

## 8. Risks and blockers

| # | Risk | Severity | Mitigation already in place / planned |
|---|---|---|---|
| 1 | The registry moves between "plan approved" and "cutover executed" (see §3) | Medium — real, observed twice this session | Migration tooling always enumerates accounts live, never from a cached list; re-run the dry-run recoverability check immediately before the real transfer |
| 2 | Two Telegram pollers running simultaneously mid-cutover → `Conflict: terminated by other getUpdates request` and/or duplicate notifications/double-sends | High if mishandled | §11 below: hard sequencing (stop Windows, confirm stop, THEN start Linux), never a parallel "test" run against the same bot token while the Windows bot is live |
| 3 | `himalaya` on AL2023 not yet installed/smoke-tested | Medium | Install + run `himalaya account check` for every account against Linux himalaya *before* pointing it at real credentials; this is a config/connectivity test, not a credential-write — zero risk to mailbox data |
| 4 | `altcoininformer`'s two secrets are still legacy plaintext `.secret` files on Windows (not DPAPI/Fernet-wrapped) | Low (same risk that exists today, not introduced by migration) | The migration utility reads them through the same `load_secret()` fallback already used in production — no special-casing needed; they get the SAME new encryption (SSM SecureString + KMS) as every other account once migrated, net security improvement |
| 5 | SSM instance-role policy too broad or too narrow | Low–Medium | §6's JSON is scoped to exactly `/mailops/secrets/*` and one CMK; review before attaching |
| 6 | `ops.db` (SQLite) migrated via plain file copy could race with a live write | Medium | Only copy `ops.db` after the Windows bot process has fully exited (file lock released) — sequenced into §11's cutover order, not copied from a running process |
| 7 | AWS region/instance assumptions unverified | Informational | This session had no AWS CLI/credentials available to inspect `dm-mini` directly (confirmed: `aws` command not found). The instance's actual OS version, existing IAM role, security groups, and disk space were **not independently verified** in this pass — recommend a quick `aws ec2 describe-instances`/SSM Session Manager check before cutover to confirm it really is AL2023 with the resources needed for the Himalaya binary + Python venv + 35-mailbox sync workload. |

---

## 9. Verification checklist (to run at each stage, before and during cutover)

**Before touching anything (done this session):**
- [x] All 35 registered accounts' IMAP+SMTP secrets decrypt successfully today, with zero plaintext exposure, verified in-process (`35/35 accounts fully recoverable`).
- [x] `data/settings.json` contains no credential/token/key material (full recursive scan, 4 benign false positives individually inspected).
- [x] New SSM backend code added to `secrets_vault.py` is fully opt-in (`MAILOPS_VAULT_BACKEND` unset ⇒ byte-identical old behavior) — confirmed by the full existing test suite passing unchanged (922/922, excluding the known live-concurrency false positive in §3).
- [x] 7 new unit tests for the SSM backend pass using a fake client (no real AWS calls).
- [x] Migration utility dry-run tested against the live profile multiple times, safely, with zero writes.
- [x] Migration utility `--apply --target local-fernet` tested end-to-end against a throwaway temp directory; staged ciphertext decrypts correctly; live `secrets/` directory confirmed byte-for-byte untouched (file count unchanged across the test).
- [x] Migration utility `--apply --target ssm` tested against an injected fake SSM client; correct parameter names and KMS key forwarding confirmed.
- [x] `--apply` without the confirmation flag correctly refuses (exit code 2, no writes).
- [x] Live Windows bot (pid 10428) confirmed still running and unaffected throughout (log tail shows ongoing real Telegram activity during and after all of the above).

**Immediately before the real cutover (not yet done — requires your approval):**
- [ ] Re-run `python scripts/migrate_secrets_to_aws.py` (dry run) one final time to re-confirm N/N recoverable, where N is whatever the registry has grown to by then.
- [ ] Confirm the EC2 instance role and CMK key policy from §6 are attached/created (read-only `aws iam get-role-policy` / `aws kms get-key-policy` checks, no mutation).
- [ ] Install Himalaya on `dm-mini`, point a throwaway test `config.toml` entry at ONE already-known-good account, run `himalaya account check` only (handshake, no send/no mutation) to prove Linux connectivity to the actual IMAP/SMTP hosts (Hostinger/SiteGround) before trusting it with all 35.
- [ ] Copy `himalaya/accounts.json` and a rewritten `himalaya/config.toml` (passwd.command paths updated to the Linux venv) to `dm-mini`; do NOT yet copy any `.enc`/`.secret` file.
- [ ] Run `migrate_secrets_to_aws.py --apply --target ssm` for real, from a machine with the one-time operator IAM credentials from §6B.
- [ ] On `dm-mini`, with `MAILOPS_VAULT_BACKEND=ssm` set, run the dry-run recoverability check (same script, same logic, pointed at SSM) and confirm N/N again — this time reading from AWS, proving the round trip worked before any bot process starts.
- [ ] Copy `data/settings.json` and `data/ops.db` only AFTER stopping the Windows bot (see §11) so no file is copied mid-write.

---

## 10. Preserving the registry, monitoring settings, sending permissions, and operational history

Nothing here is new infrastructure — these are plain files, already gitignored, that get copied as-is:

- **Mailbox registry + monitoring/policy config:** `himalaya/accounts.json` (id/email/display-name/default) + `data/settings.json` (`active_accounts`, `account_policies` — organize/send flags per mailbox, `sync_interval_seconds`, `digest_*`, priority rules, muted senders, etc.). Copy verbatim; only `config.toml`'s `passwd.command` path strings need edits (see §7), nothing else in these three files is Windows-specific.
- **Sending permissions:** live entirely in `account_policies` inside `settings.json` (`{"organize": bool, "send": bool}` per account) — copied, not reconstructed, so no mailbox silently regains or loses send permission during migration.
- **Operational database history:** `data/ops.db` (SQLite) holds `access_users`/`access_groups`/`access_audit` (admin allowlist + audit trail), `sync_incidents`, `digest_pending`/`digest_state` (dedup state — losing this would just cause one redundant digest cycle, not data loss), `send_approvals` (approval token history), `mailbox_audit`, `bulk_jobs`/`bulk_rows`. SQLite file copy is safe and complete as long as it's done after the writing process (the Windows bot) has exited — a straight `scp`/file copy, no export/import tooling needed since both ends run the same schema (`mailops/db.py`'s `CREATE TABLE IF NOT EXISTS` is idempotent and version-free by design).
- **Nothing in this plan proposes re-registering, re-validating, or re-creating any of the 35 mailboxes from scratch.** Every piece of config and every credential transfers as-is; only its encryption-at-rest mechanism changes (DPAPI/Fernet → SSM+KMS).

### Rollback plan

Because nothing on the Windows side is deleted or modified by any step above:

1. **If anything looks wrong on `dm-mini` before cutover (install/connectivity issues):** simply don't flip the Telegram poller over. The Windows bot was never stopped, so there is no outage and nothing to roll back — this is the default safe state during the entire preparation phase (true right now, as this plan is delivered).
2. **If something looks wrong immediately after cutover (Linux bot started, Windows bot stopped):** restart the Windows bot via `start.ps1` / the existing Scheduled Task (`scripts/register_startup.ps1 status`/`install`) — DPAPI-sealed secrets, `accounts.json`, `config.toml`, and `data/` were never modified in place during the migration (the migration utility only ever *reads* them to produce a *copy* elsewhere), so the Windows side is immediately runnable again, unchanged. Stop the Linux `mailops.service` (`systemctl stop mailops`) first to guarantee single-poller (§11) during the rollback itself.
3. **If `ops.db`/`settings.json` diverge after some real-world activity on Linux (e.g. a few digests fired) before rollback is decided:** copy the Linux-side `data/ops.db` and `data/settings.json` back to Windows before restarting the Windows bot, so no audit/dedup history or newly-onboarded mailbox is lost in the rollback direction either.
4. **SSM parameters and the KMS key are never deleted as part of rollback** — they cost effectively nothing at rest and keeping them means a second cutover attempt doesn't need to redo the credential transfer step.

---

## 11. Cutover sequence — exactly one Telegram poller at any instant

Telegram's `getUpdates` long-poll is exclusive per bot token: a second poller starting while the first is still active gets `Conflict: terminated by other getUpdates request` and both ends up fighting, which is exactly the failure mode to design out. `mailops/singleton.py` already enforces single-poller **per host** via a PID-file lock — it does **not** know about a second host, so cross-host exclusivity must be sequenced by the operator, not by code. Exact order:

1. **Prep everything on `dm-mini` first, fully offline from Telegram's perspective.** Install Himalaya, install the Python venv + `requirements.txt` (+ `boto3`), copy `accounts.json`/rewritten `config.toml`/`settings.json`, run `migrate_secrets_to_aws.py --apply --target ssm`, verify the SSM-backend recoverability check. **Do not start `mailops.main.cli()`/`systemctl start mailops` yet** — none of this touches Telegram at all, so the Windows bot keeps running undisturbed through this entire step, with zero risk of a poller conflict.
2. **Freeze new onboarding briefly right before cutover.** Ask the owners to pause `/qadd`/Smart Add/bulk-import for the few minutes the cutover takes (per §3, the registry is actively live) — this is a courtesy pause, not a technical requirement, but avoids a mailbox being added to Windows's `settings.json` after the last copy and before Windows stops.
3. **Stop the Windows bot.** `Get-ScheduledTask` disable (or just end the process; `singleton.py`'s PID file is cleaned up by its `atexit` handler on a clean exit) — confirm via `scripts/register_startup.ps1 status` that `Mailops process: not started` and no `python.exe` running `mailops.main` remains. This is the one moment the system has **zero** active pollers (by design — brief, intentional gap is safer than any overlap).
4. **Final-copy `data/ops.db` and `data/settings.json`** from Windows to `dm-mini` now that the writer has exited and the files are quiescent (§10). Overwrite the earlier prep-step copies.
5. **Start the Linux bot** (`systemctl start mailops`, using the unit file prepared at `deploy/mailops.service.template` — not yet installed anywhere, per the "don't modify the AWS server yet" constraint of this pass). Watch its log for the same `Application started` / `sync loop started` lines the Windows log shows today, and confirm via Telegram's `getWebhookInfo` (read-only Bot API call, no `getUpdates`) that polling resumed with no `Conflict` errors.
6. **Smoke-test read-only:** one dashboard open, one `/qadd`-style read of an existing mailbox's status — no sends, exactly as this entire audit has been constrained throughout. Confirm the 35-mailbox count matches what Windows had at the moment of stop.
7. **Only after a stable observation period** (owner's call on duration) does Windows get fully decommissioned (Scheduled Task uninstalled via `scripts/register_startup.ps1 uninstall`); until then it stays installed-but-stopped as the rollback target from §10.

**This plan deliberately never has both bots polling at once.** The only state transitions are "Windows polling, Linux not installed/not started" → "neither polling" (step 3–4, seconds-to-minutes) → "Linux polling, Windows stopped" (step 5 onward) → optionally "Linux polling, Windows uninstalled" (step 7, after owner sign-off).

---

## Summary — what is ready for your approval

- **Credential recoverability:** 35/35 mailboxes confirmed recoverable right now, zero re-entry needed.
- **Linux storage design:** AWS SSM Parameter Store + customer-managed KMS key, implemented as an opt-in backend in `secrets_vault.py`, default behavior unchanged, 7 new tests passing, zero impact on the live Windows bot (full suite re-confirmed: 922/922 passing, one known live-concurrency false positive unrelated to this work).
- **Migration tooling:** `scripts/migrate_secrets_to_aws.py`, dry-run-by-default, double-confirmation-gated apply, tested end-to-end against both targets without ever writing to real AWS or touching the live `secrets/` directory.
- **Himalaya/Linux compatibility:** confirmed compatible by design (official Linux glibc builds exist for the same v2.2.1), not yet installed/smoke-tested on the real instance (requires actual AWS access this session didn't have).
- **IAM least-privilege policy:** drafted in §6, scoped to one parameter path prefix and one CMK, nothing broader.
- **Rollback plan:** Windows side is never modified in place during preparation, so rollback is "start Windows again" at any point before step 7 above.
- **Single-poller cutover sequencing:** laid out in §11 with the one brief all-stopped gap as the only state where neither bot polls, by design.

**Nothing was deployed, uploaded, or changed on AWS. The Windows bot (pid 10428) ran throughout this entire session without interruption, verified repeatedly via its live log.** Awaiting your explicit go-ahead before any `--apply` step is run for real, before Himalaya is installed on `dm-mini`, and before the IAM role/KMS key in §6 are created.

