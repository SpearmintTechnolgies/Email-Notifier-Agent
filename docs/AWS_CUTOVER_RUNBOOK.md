# Mailops AWS Cutover & Rollback Runbook (dm-mini)

Generated during the live migration session, Oct 27 2026. Every command here
has either been executed (marked ✅) or is staged awaiting approval (marked ⏳).

## Current state (verified)

| Item | State |
|---|---|
| AWS auth | ✅ `arn:aws:iam::564882306593:user/ZOHA`, same-device `aws login`, profile `zoha` |
| SSH over SSM | ✅ `ssh dm-mini` works, no port 22 exposed |
| Source | ✅ `/opt/mailops` laid out from release `2026.10.26`, MANIFEST verified |
| Secrets | ✅ 78 Fernet blobs, `0600 mailops:mailops`, all decrypt on Linux |
| Master key | ✅ `/opt/mailops/keys/master.fernet`, `0400 mailops:mailops` |
| Registry | ✅ 39 accounts, `settings.json` == `accounts.json` |
| Database | ✅ `ops.db` snapshot via SQLite backup API, `integrity_check: ok` |
| himalaya | ✅ v2.2.1, reads live production mail end-to-end |
| Test suite | ✅ 1041 passed, 1 skipped (matches Windows baseline) |
| systemd unit | ✅ installed, `disabled` + `inactive` |
| Telegram | ✅ `MAILOPS_TEST_MODE=1`, no poller running anywhere |
| Mailbox auth | ⚠️ 36/39 (3 pre-existing credential failures, see below) |
| Windows bot | ⚠️ **was already stopped before this migration** — stale pidfile (PID 17144 dead), no scheduled task, last log line Oct 26 02:59 after a `MailboxUnavailable` crash. Left exactly as found, as the rollback target. |

## The 3 failing mailboxes (NOT migration faults)

Proven by `scripts/diagnose_failed_mailboxes.py`: the staged ciphertext is
**byte-identical** to the live Windows secret for all three, and `ops.db`
carries `status='open'` incidents that predate this migration.

| Account | Linux result | Windows result | `ops.db` incident |
|---|---|---|---|
| `altcoininformer` | auth rejected | auth rejected | `auth_failed` (open) |
| `alien` | auth rejected | network timeout | `network_timeout` (open) |
| `marketing` | auth rejected | network timeout | `network_timeout` (open) |

AWS actually improves the diagnosis: Windows could not reach
`gcam1108.siteground.biz` at all, whereas AWS connects fine and gets a clean
`535 Incorrect authentication data`. So all three need a **password reset at
the provider**, not a migration fix.

## ⏳→✅ Cutover: EXECUTED Oct 27 2026 04:52 UTC

Approved mode: **start without `enable`** (no auto-start on reboot).

```bash
# 1. Final consistent snapshot - DONE
#    /opt/mailops/data/ops.db.pre-cutover-20261027-045203 (integrity: ok)
# 2. MAILOPS_TEST_MODE 1 -> 0 - DONE
# 3. systemctl start mailops  (NOT enable) - DONE
```

Live verification results:

| Check | Result |
|---|---|
| Service | `active (running)`, PID 1184621, **0 restarts**, ~50 MiB |
| `is-enabled` | ✅ **`enabled`** — boot symlink created Oct 27 05:04 UTC (see below) |
| Startup log | `accounts=[39 ids]`, `sync loop started`, `Application started` |
| Telegram polling | no webhook, `pending_updates: 0`, **no `Conflict`** |
| Outbound send | dashboard posted to `-5531260744` (msg 372, 7 inline buttons) |
| **Inbound callbacks** | **3 real taps handled** from user `8925736576`: `nav:home`, `ab:memecoinist\|p1`, `dash` — zero errors |
| Scheduled digest | `digest sent mailboxes=5 actionable=0 covered=9 edited=2` |
| Cache refresh | `cache refreshed accounts=39, 3 unreachable` (127s full pass) |
| Health recovery | **30 accounts `recovered`** that Windows could not reach |
| Restart test | `systemctl restart` → clean re-start, no conflict |
| Singleton lock | second instance correctly refused |
| `/opt/videoagent` | untouched (mtime still Oct 19) |

### ✅ Boot persistence ENABLED (Oct 27 2026 05:04 UTC)

```bash
sudo systemctl enable mailops
# Created symlink /etc/systemd/system/multi-user.target.wants/mailops.service
#   -> /etc/systemd/system/mailops.service
```

`enable` only writes the boot symlink; it does not start a process. Verified
no second poller was created: **MainPID stayed 1184621** across the operation
and `pgrep -cf 'python -m mailops'` remained `1`.

Post-enable state: `enabled` + `active`, **0 restarts**, 1 poller,
`pending_updates: 0`, **0 Conflict errors in the entire journal**.

To undo (service keeps running, just won't auto-start on reboot):

```bash
sudo systemctl disable mailops
```

## Rollback

Linux side, immediate:

```bash
sudo systemctl disable --now mailops        # stops the only poller
sudo sed -i 's/^MAILOPS_TEST_MODE=0$/MAILOPS_TEST_MODE=1/' /opt/mailops/.env
```

Windows side, to resume there instead:

```powershell
cd C:\Users\Global\Downloads\Email-Notifier-Agent
Remove-Item data\mailops.pid -Force    # clear the stale lock
.\start.ps1
```

Nothing on Windows was modified during this migration — `secrets/`,
`himalaya/`, `data/` were read-only sources, so Windows remains immediately
runnable. If AWS has accumulated state before a rollback, copy
`/opt/mailops/data/ops.db` and `settings.json` back first.

**Single-poller invariant:** exactly one of {Windows `start.ps1`,
AWS `mailops.service`} may run at a time. Both are currently stopped.
