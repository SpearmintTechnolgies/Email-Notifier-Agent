# Remote Mailbox Onboarding — Deployment Runbook

This document describes how to turn the loopback-only mobile onboarding
server (built into Mailops) into a secure HTTPS endpoint the CEO can reach
from a phone. The Mailops Windows host is **not** publicly reachable, so
you need to put a reverse tunnel in front of it. The tunnel owns the
public TLS certificate; the local server adds HTTPS on `127.0.0.1` as
defence in depth.

Do **not** publish the loopback server directly on a public interface and
do **not** forward a router port to it. Tests block that configuration
(`host must be a loopback address`) and so does the server itself.

> **Status after this change set:** the web server, the Telegram
> Mini App auth, the encrypted secrets vault, SSRF guard, atomic rollback
> and 50 security tests are all merged and passing. What remains is the
> hosting + Mini App registration — the two steps on this page.

---

## 0. Prerequisites

* Windows host running Mailops (where `python -m mailops run` already
  works for Altcoin Informer).
* Mailops `.env` has `TELEGRAM_BOT_TOKEN=…` set with the real
  `@ourmailagent_bot` token.
* The two admin IDs (`8925736576`, `5337250772`) are in the access store
  (`python -m mailops run` would have logged them at startup).

The CEO needs **only** the Telegram app on his phone. He does not need a
browser, VPN, PowerShell access or anything else.

---

## 1. Choose a tunnel (pick ONE)

### Option A — Cloudflare Tunnel (recommended when you own a domain)

Pros: free, no inbound port, your own hostname, auth available at edge.
Cons: requires a domain on Cloudflare DNS.

```powershell
# One-time install (as the user that runs Mailops)
winget install Cloudflare.cloudflared

# Log in and create a tunnel
cloudflared tunnel login
cloudflared tunnel create mailops-web

# Route a hostname to the tunnel (replace example)
cloudflared tunnel route dns mailops-web mailops.example.com

# Create C:\ProgramData\Cloudflare\cloudflared\config.yml:
#   tunnel: mailops-web
#   credentials-file: C:\Users\<you>\.cloudflared\<uuid>.json
#   originRequest:
#     noTLSVerify: true    # the loopback cert is self-signed; that is expected
#   ingress:
#     - hostname: mailops.example.com
#       service: https://127.0.0.1:8738
#     - service: http_status:404

# Install as a Windows service so it auto-starts
cloudflared service install
```

Public URL: `https://mailops.example.com`

### Option B — Cloudflare Quick Tunnel (no domain needed, DEMO ONLY)

```powershell
cloudflared tunnel --no-autoupdate --url https://127.0.0.1:8738
```

It will print a URL like `https://abc-123-xyz.trycloudflare.com`. The URL
changes on every restart and is not durable, but it's useful for a one-off
CEO walkthrough.

### Option C — Tailscale Funnel (own domain not needed)

```powershell
winget install Tailscale.Tailscale
tailscale up
tailscale funnel 8738
```

Public URL will be of the form `https://<machine>.<tailnet>.ts.net`.

Pick one option above and record the resulting HTTPS URL. We will call
it `PUBLIC_URL` from here on.

---

## 2. Register the Telegram Mini App

Open Telegram → `@BotFather` → `/mybots` → select `@ourmailagent_bot`
→ **Bot Settings** → **Configure Mini App** → **Enable Mini App**.

Set:

* **Mini App URL:** `PUBLIC_URL` from step 1 (must be HTTPS).
* **Mini App short name:** anything, e.g. `onboard`.
* Keep **Menu Button** → **Edit** optional; the Mailops bot already
  surfaces the WebApp through the **Mailboxes → Secure Setup (phone)**
  button so you do not need to add a global menu entry.

That's it on the Telegram side. Mini App `initData` signatures are keyed
by the bot token, which the Mailops server already has in `.env`. There
is no OAuth app and no secret to copy.

---

## 3. Configure Mailops to turn the web server on

Edit the Windows host's Mailops `.env` (same file where
`TELEGRAM_BOT_TOKEN` lives) and append:

```
MAILOPS_WEB_ENABLE=1
MAILOPS_WEB_PUBLIC_URL=https://mailops.example.com
# optional, defaults to 8738
MAILOPS_WEB_PORT=8738
```

The server is **off by default** (`MAILOPS_WEB_ENABLE=0`). It will not
start unless you set `=1`.

---

## 4. Restart Mailops

Stop the running `python -m mailops run` process. Start it again (your
usual launcher: `start.ps1`, `Start Mailops.vbs`, Task Scheduler…).

On startup you should see one new log line among the usual output:

```
Mailops web onboarding listening on https://127.0.0.1:8738 (loopback only)
```

If this line is missing, check the Mailops log for an error. The bot will
still run normally — the web server failing to start does not stop the
bot.

---

## 5. Smoke test from the CEO's phone

1. CEO opens Telegram → `@ourmailagent_bot` → `/start`.
2. In private chat with the bot: **Admin** → **Mailboxes**.
3. There should be a new button: **Secure Setup (phone)**. Tap it.
4. The Telegram app opens the Mini App form at `PUBLIC_URL`.
5. The server should show the CEO's first name at the top (that's proof
   initData was verified). If it says "could not verify your Telegram
   login", the `MAILOPS_WEB_PUBLIC_URL` and the Mini App URL at `@BotFather`
   do not match.
6. Fill in the form with the Coinography / Memecoinist details, choose
   "Hostinger" to prefill ports/hosts, hit **Continue**.
7. Enter the mailbox password. Mailops performs a login handshake only —
   no email is sent.
8. If the handshake succeeds the CEO taps **Activate mailbox**. The new
   account appears in the Telegram **Mailboxes** screen on next refresh,
   starts **read-only with sending off**, and is picked up by the sync
   loop on the next tick without a restart.

Nothing in Altcoin Informer's configuration is touched during any of
these steps.

---

## 6. If something goes wrong

| Symptom | Diagnosis |
| --- | --- |
| "Secure Setup (phone)" button missing | `MAILOPS_WEB_PUBLIC_URL` not set or not HTTPS. |
| Opening the Mini App shows a browser error | Tunnel is down. From the Windows host, run `curl -k https://127.0.0.1:8738/healthz` — must return `{"ok": true}`. |
| "initData could not be verified" | Bot token in `.env` differs from the one `@BotFather` holds (regenerated?). |
| "not an authorized administrator" | The admin's Telegram ID is not in the `access_users` table with the `admin` permission. Check via the private **Admin → Users** screen. |
| "refused: ... resolves to a non-public address" during validation | Mailbox hostname points at an internal/private IP. This is correct refusal — supply a real public mail server. |
| "duplicate" during validation | The account id is already registered. Pick another id or disable the previous one via Mailboxes first. |

Every mutation is logged with secrets scrubbed. If you need a deeper
view, inspect `data/mailops.log` on the host.

---

## 7. Key management

* **Windows (production default):** mailbox passwords are sealed with
  DPAPI (CurrentUser scope). The DPAPI master key lives under
  `%APPDATA%\Microsoft\Protect\<SID>\` and is itself protected by the
  Windows user's logon credentials. It is **not** stored next to
  the ciphertext.
* **Linux / CI:** a Fernet key is created at
  `%LOCALAPPDATA%\Mailops\keys\master.fernet` (or
  `~/.config/mailops/keys/master.fernet`). This path is deliberately
  outside the project tree so a database/data backup does not include it.

Back up these two things separately:

1. `secrets/*.enc` (the encrypted mailbox credentials)
2. The key material (DPAPI profile folder on Windows, or `master.fernet` on
   Linux)

If you lose the key, the encrypted secrets are unrecoverable — you will
have to re-onboard each mailbox.

To migrate the live Altcoin Informer account from legacy plaintext
`secrets/altcoininformer.imap.secret` to encrypted-at-rest you can run:

```powershell
python -c "from mailops import secrets_vault, webmail_onboarding as w; \
imap = w.load_secret('altcoininformer','imap'); \
smtp = w.load_secret('altcoininformer','smtp'); \
secrets_vault.store_secret('altcoininformer','imap', imap); \
secrets_vault.store_secret('altcoininformer','smtp', smtp)"
```

The vault's `store_secret` writes the new `.enc` file atomically and then
deletes the old `.secret` file. **Run this only when the Mailops process is
stopped**, so Himalaya does not read a half-rotated secret.
