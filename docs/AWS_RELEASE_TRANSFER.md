# Mailops — Linux Release Transfer & Install Runbook (AWS `dm-mini`)

**Scope of this document:** how to get the release archive onto the prepared
AWS host and lay it out under `/opt/mailops`, using **AWS Systems Manager
Session Manager only**. No SSH, no SCP, no inbound port, no key pair is
assumed to exist.

**What this runbook deliberately does NOT do:**

- It does **not** install, enable or start the Telegram bot. The systemd unit
  is copied nowhere; `systemctl start` appears in no step below.
- It does **not** transfer any credential, mailbox password, bot token, OAuth
  token, the live registry or the live database. Those are §6, a *separate,
  later* operation that happens only after the Windows bot is stopped.
- It does **not** change AWS IAM, security groups, or any AWS credential.
- The **Windows Mailops bot keeps running untouched** through every step here.

---

## 1. What you are transferring

| Artifact | Purpose |
|---|---|
| `mailops-linux-<version>.tar.gz` | The release archive: application source, test suite, dependency manifests, migration scripts, safe config templates, docs. |
| `mailops-linux-<version>.tar.gz.sha256` | SHA-256 of the archive, for end-to-end integrity verification on the Linux side. |

The archive contains **no secrets** — see `RELEASE.txt` and the
`*.EXCLUDED.txt` audit that was generated beside it.

---

## 2. Prerequisites on the host (verify, do not change)

Open a Session Manager shell (Console → EC2 → `dm-mini` → **Connect** →
**Session Manager** → Connect), then confirm the prepared environment:

```bash
cat /etc/os-release | head -2            # expect Amazon Linux 2023
id mailops                               # expect the dedicated service user
sudo -u mailops /opt/mailops/venv/bin/python -V   # expect Python 3.12.14
df -h /opt                               # need ~300 MB free for venv + wheels
```

Also confirm the tooling this runbook uses is present:

```bash
command -v sha256sum tar base64 python3
```

> **If `aws` CLI on the instance is needed for §3 Method A**, check
> `command -v aws`. AL2023 ships `aws` v2 by default. Method B needs nothing
> but the Session Manager shell itself.

---

## 3. Transfer the archive (choose ONE method)

### Method A — S3 relay (recommended for a ~0.5 MB archive and up)

This is the standard, auditable Session-Manager-compatible transfer path: the
file travels over AWS APIs, never over SSH, and the instance pulls it using
its own instance role.

**A1. On Windows**, upload from the release directory:

```powershell
cd C:\Users\Global\Downloads\mailops-release

# Requires the AWS CLI configured with the same identity you use for Session
# Manager. If `aws` is not installed on this Windows box, use Method B.
aws s3 cp .\mailops-linux-<version>.tar.gz        s3://<your-bucket>/mailops/ --sse AES256
aws s3 cp .\mailops-linux-<version>.tar.gz.sha256 s3://<your-bucket>/mailops/ --sse AES256
```

**A2. In the Session Manager shell**, pull and verify:

```bash
sudo install -d -o mailops -g mailops -m 755 /opt/mailops/_incoming
cd /opt/mailops/_incoming
sudo aws s3 cp s3://<your-bucket>/mailops/mailops-linux-<version>.tar.gz        .
sudo aws s3 cp s3://<your-bucket>/mailops/mailops-linux-<version>.tar.gz.sha256 .
sha256sum -c mailops-linux-<version>.tar.gz.sha256
#   expect: mailops-linux-<version>.tar.gz: OK
```

**A3. Delete the staged copy** once §4 succeeds:

```powershell
aws s3 rm s3://<your-bucket>/mailops/mailops-linux-<version>.tar.gz
aws s3 rm s3://<your-bucket>/mailops/mailops-linux-<version>.tar.gz.sha256
```

> The instance role needs `s3:GetObject` on that one prefix. **Do not add it
> as part of this runbook** — confirming/arranging IAM is explicitly outside
> the scope you authorized. If the role lacks it, use Method B.

### Method B — paste through the Session Manager shell (no S3, no IAM change)

Session Manager is a terminal, so a base64 text stream is a legitimate
transfer channel. For ~0.5 MB this is roughly 0.7 MB of base64; split it into
chunks the terminal accepts reliably.

**B1. On Windows**, produce base64 chunks next to the archive:

```powershell
cd C:\Users\Global\Downloads\mailops-release
$name = "mailops-linux-<version>.tar.gz"
$b64  = [Convert]::ToBase64String([IO.File]::ReadAllBytes($name))
# 40,000-char chunks: comfortably under the shell's input limits.
$size = 40000
$i = 0
for ($o = 0; $o -lt $b64.Length; $o += $size) {
    $len = [Math]::Min($size, $b64.Length - $o)
    Set-Content -Path ("chunk_{0:d3}.b64" -f $i) -Value $b64.Substring($o, $len) -NoNewline -Encoding ascii
    $i++
}
"chunks: $i"
(Get-FileHash $name -Algorithm SHA256).Hash.ToLower()
```

**B2. In the Session Manager shell**, prepare the landing spot:

```bash
sudo install -d -o mailops -g mailops -m 755 /opt/mailops/_incoming
cd /opt/mailops/_incoming
sudo rm -f parts.b64
```

**B3. For each chunk**, open `chunk_000.b64` in Notepad, copy its contents,
and paste into this command (one chunk per invocation, in order):

```bash
cat >> parts.b64 <<'MAILOPS_EOF'
<paste the chunk here>
MAILOPS_EOF
```

**B4. Reassemble and verify** against the hash from B1:

```bash
tr -d '\n\r ' < parts.b64 | base64 -d > mailops-linux-<version>.tar.gz
sha256sum mailops-linux-<version>.tar.gz
#  must equal the hash printed at the end of step B1
rm -f parts.b64
```

**If the two hashes differ, STOP.** The paste was truncated — delete the
`.tar.gz` and redo B2–B4. Never extract an archive whose hash does not match.

---

## 4. Extract and verify contents

```bash
cd /opt/mailops/_incoming
tar -tzf mailops-linux-<version>.tar.gz | head -20     # look before extracting
tar -xzf mailops-linux-<version>.tar.gz
cd mailops-linux-<version>

# Per-file integrity of every member:
sha256sum -c MANIFEST.sha256 | grep -v ': OK$' || echo "ALL MEMBERS OK"

# Prove the archive is secret-free and Windows-free, on the Linux side:
! find . -type f \( -name '*.exe' -o -name '*.dll' -o -name '*.pyc' \
      -o -name '*.ps1' -o -name '*.bat' -o -name '*.vbs' \) -print | grep . \
  && echo "no Windows artifacts"
! find . -type f \( -name '.env' -o -name '*.enc' -o -name '*.secret' \
      -o -name '*.db' -o -name '*.key' -o -name '*.log' \) -print | grep . \
  && echo "no secrets or private state"
test ! -e .git && echo "no git history"

cat RELEASE.txt
```

---

## 5. Lay out `/opt/mailops` (still nothing started)

```bash
SRC=/opt/mailops/_incoming/mailops-linux-<version>

# Application code + tooling + templates + docs.
sudo rsync -a --chown=mailops:mailops \
     "$SRC"/{mailops,tests,scripts,deploy,docs} /opt/mailops/
sudo install -o mailops -g mailops -m 644 \
     "$SRC"/{requirements.txt,pytest.ini,README.md,PHASE2.md,RELEASE.txt,MANIFEST.sha256} \
     /opt/mailops/

# Runtime directories the bot needs (empty, correct ownership and modes).
sudo install -d -o mailops -g mailops -m 750 /opt/mailops/data
sudo install -d -o mailops -g mailops -m 750 /opt/mailops/himalaya
sudo install -d -o mailops -g mailops -m 700 /opt/mailops/secrets
sudo install -d -o mailops -g mailops -m 700 /opt/mailops/tokens
sudo install -d -o mailops -g mailops -m 700 /opt/mailops/keys

# AI drafting knowledge bases and the bulk-import CSV sample.
sudo install -d -o mailops -g mailops -m 750 /opt/mailops/data/kb
sudo install -d -o mailops -g mailops -m 750 /opt/mailops/data/samples
sudo install -o mailops -g mailops -m 640 "$SRC"/data/kb/*.md       /opt/mailops/data/kb/
sudo install -o mailops -g mailops -m 640 "$SRC"/data/samples/*.csv /opt/mailops/data/samples/

# Python dependencies into the EXISTING venv, as the service user.
sudo -u mailops /opt/mailops/venv/bin/python -m pip install --upgrade pip
sudo -u mailops /opt/mailops/venv/bin/pip install \
     -r /opt/mailops/deploy/requirements-linux.lock.txt

# Configuration from the SAFE templates. .env ships with
# MAILOPS_TEST_MODE=1 and an EMPTY bot token, so even an accidental launch
# cannot poll Telegram or mutate a mailbox.
sudo install -o mailops -g mailops -m 600 \
     /opt/mailops/deploy/env.linux.template /opt/mailops/.env
sudo install -o mailops -g mailops -m 640 \
     /opt/mailops/deploy/settings.linux.template.json /opt/mailops/data/settings.json

# Offline sanity checks (no Telegram, no IMAP, no credentials):
sudo -u mailops env MAILOPS_HOME=/opt/mailops \
     /opt/mailops/venv/bin/python -c "import mailops, mailops.main; print('import ok')"
sudo -u mailops env MAILOPS_HOME=/opt/mailops MAILOPS_KEY_DIR=/opt/mailops/keys \
     /opt/mailops/venv/bin/python -m pytest -q /opt/mailops/tests
```

Expected: the suite passes. `tests/conftest.py` forces
`MAILOPS_TEST_MODE=1` and `MAILOPS_FORCE_SOFT_VAULT=1`, blocks the real
himalaya binary, and redirects every path into a temp profile — so running it
cannot touch a mailbox or a credential.

**Install `himalaya` for Linux** (needed before any real mailbox check, not
before the test suite):

```bash
# x86_64-unknown-linux-gnu build of the SAME version the Windows bot uses (2.2.1)
# from the upstream GitHub releases; AL2023 is glibc, so use the gnu (not musl) build.
himalaya --version   # after install; expect 2.2.1 with +imap +smtp
```

Then stop. **Do not** copy the systemd unit, and **do not** run
`systemctl enable`/`start`.

---

## 6. LATER — the live data cutover (not part of this transfer)

Listed so the boundary is unambiguous. Each item is live operational data,
deliberately excluded from the archive, and must move **only after the
Windows bot process has fully exited** (otherwise `ops.db` is copied from
under an open SQLite handle and the registry/config can drift apart).

These five move together, as one consistent snapshot:

1. `data/settings.json` — the 39-mailbox registry, policies, business context.
2. `himalaya/accounts.json` — the matching account metadata registry.
3. `himalaya/config.toml` — hosts/ports/usernames. Must be rewritten first:
   ```powershell
   python scripts\render_himalaya_linux_config.py `
      --in himalaya\config.toml `
      --out build\config.linux.toml `
      --python /opt/mailops/venv/bin/python
   ```
   (read-only on the live file; rewrites only the 78 `passwd.command`
   interpreter paths, refuses to emit a file containing an inline password).
4. `data/ops.db` — operational SQLite state (dedup ledgers, approvals, access).
5. The **39 × 2 mailbox credentials**. These cannot be copied: the `.enc`
   blobs are Windows-DPAPI-sealed and are undecryptable on Linux by design.
   Use `scripts/migrate_secrets_to_aws.py` (dry-run first) per
   `docs/AWS_CREDENTIAL_MIGRATION_PLAN.md`, or re-onboard each mailbox.

Plus `.env`'s real values (bot token, allowlist, home channel), typed in on
the Linux host — never transferred as a file.

**Hard sequencing rule:** one Telegram bot token may be polled by exactly one
process. Stop Windows → confirm it exited → *then* start Linux. Never both.

---

## 7. Rollback

Nothing in §1–§5 touches the Windows deployment, so rollback is simply:

```bash
sudo rm -rf /opt/mailops/_incoming
# and, to undo the layout:
sudo rm -rf /opt/mailops/{mailops,tests,scripts,deploy,docs,data,himalaya}
sudo rm -f  /opt/mailops/{.env,requirements.txt,pytest.ini,README.md,PHASE2.md,RELEASE.txt,MANIFEST.sha256}
```

The venv at `/opt/mailops/venv` and the `mailops` user are left in place.
