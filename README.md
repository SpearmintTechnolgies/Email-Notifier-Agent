# Email Notifier Agent (Mailops)

Button-first Telegram email agent for **one** business webmail inbox:
`info@altcoininformer.com` (account id `altcoininformer`, Hostinger IMAP/SMTP).
It classifies mail into P0–P3, notifies on Important/Critical, and drafts
replies with a purpose-aware knowledge base. The code still supports several
IMAP inboxes, but this deployment runs exactly one.

## Active account (single-inbox deployment)

- Mailops manages only `altcoininformer`. `himalaya/accounts.json` and
  `himalaya/config.toml` contain only that account, and `data/settings.json`
  has `"active_accounts": ["altcoininformer"]` as an allowlist: an account that
  is not on it (or has `"enabled": false` in `accounts.json`) is never synced,
  cached, notified about, shown in Telegram, or reachable from an old button.
- The former Gmail account (`growthpilotforyou@gmail.com`) was retired from
  Mailops. Nothing was deleted: the database keeps its historical rows (they
  never reach any view or notification), the Gmail OAuth token/client files and
  `.env` Google settings are untouched, and the previous configs are in
  `backups/` (git-ignored, current-user-only ACL). The Sales-Agent keeps its own
  Gmail configuration independently.
- Telegram: private chat = role-based dashboard for authorized users; the
  configured group = read-only dashboard + metadata-only alerts.
- Safe by default: `MAILOPS_TEST_MODE=1`, mailbox organization off, sending
  off (and, once enabled, every email still needs its own approval).

To re-activate another inbox later: register it (`webmail-add` or restore from
`backups/`), add its id to `active_accounts`, and restart.

## What it does

- Polls the active Himalaya IMAP account(s)
- Classifies into `INBOX.P0-Critical` … `INBOX.P3-Noise` (keywords first, LLM fallback)
- Telegram dashboard: browse 10/page, open, reply, AI draft, send
- AI drafts use per-inbox **purpose + reply KB** (links, packages, signature)
- Confirm send → SMTP via Himalaya, copy saved to **Sent**, original moved to **Waiting**

## Launch (Windows)

```powershell
.\start.ps1
# or double-click Start Mailops.bat
```

The launcher uses the project `.venv` (`.\.venv\Scripts\python.exe`) and
writes logs to `data\mailops.log`.

Requires:

- Python 3.11+ with a venv at `.\.venv`
- [Himalaya](https://github.com/pimalaya/himalaya) v2 on PATH (or set
  `HIMALAYA_BIN`), with config in `.\himalaya\` (or `MAILOPS_HIMALAYA_DIR`)
- Telegram bot token + allowed user id in `.env` (see `.env.example`)
- Optional: any OpenAI-compatible `/v1` endpoint for classification + drafts

## Gmail OAuth setup (NOT used in this deployment)

Gmail is not part of the Altcoin-Informer-only deployment; this section is kept
only for recovery. A Gmail account added here stays inactive unless its id is
also listed in `active_accounts`.

Gmail app passwords are not needed. Mailops uses Himalaya's OAuth2 backend:
the first login opens your browser for Google sign-in/consent, and tokens are
stored in **Windows Credential Manager** (never in this repo, never logged).

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create
   a project (or reuse one).
2. Enable the **Gmail API** (APIs & Services → Library).
3. Configure the **OAuth consent screen** (APIs & Services → OAuth consent
   screen): user type *External*, fill app name + your email, add scope
   `https://mail.google.com/`, and add each Gmail address you will use as a
   **Test user**.
4. Create credentials (APIs & Services → Credentials → Create Credentials →
   **OAuth client ID** → application type **Desktop app**). Copy the client id
   and client secret.
5. Put them in `.env` (copy from `.env.example`):
   ```
   GOOGLE_CLIENT_ID=....apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=....
   ```
6. Register and log in each Gmail account:
   ```powershell
   .\.venv\Scripts\python.exe -m mailops gmail-setup gmail you@gmail.com "Your Name"
   .\.venv\Scripts\python.exe -m mailops gmail-login gmail
   ```
   `gmail-login` opens the browser; after you consent, IMAP/SMTP just work.
   Repeat with a different account id per additional Gmail account.

Access tokens last ~1 hour and are refreshed automatically (also in test
mode). If your OAuth app is still in Google's "Testing" state, Google expires
the refresh token after 7 days - re-run `gmail-login <id>` or publish the app.

## Telegram: private chats and groups

Access is stored in `data/ops.db` (survives restarts). `.env` is only the
first-launch seed plus private-chat notification destinations.

- `TELEGRAM_ALLOWED_USERS` - numeric ids seeded as authorized users on first launch.
  The first id (or `TELEGRAM_OWNER_ID` if set) becomes the bootstrap owner and
  cannot be removed. Later users/roles are managed from **Admin** in a private chat.
- `TELEGRAM_HOME_CHANNEL` - where push alerts go, comma-separated:
  `123456789,-5531260744` = that user's private chat **and** a group.
  Negative ids are also seeded as the group whitelist. (This deployment's group
  is `-5531260744`.)
- **Roles**
  - **Owner/Admin**: manage users and groups, view email, draft, approve sending, audit.
  - **Operator**: view email, draft; approve sending only if an owner granted it.
  - **Viewer**: dashboards and subject/sender summaries only.
- **Private chats**: actions follow the user's role. Owners see an Admin button
  (`/admin`) to add/remove numeric user ids and whitelist groups, list access,
  view effective permissions, and read the access audit. There is no
  self-registration. `/myid` prints a Telegram id so an owner can add it.
- **Groups**: must be explicitly whitelisted. Whitelisting a group does **not**
  authorize its members — the sender must still be an authorized user.
  `/start@<bot_username>` or `/dashboard` opens a read-only inline dashboard
  (counts + subject/sender lists, paging, refresh). Opening bodies, drafts,
  credentials, sending and settings never appear in groups. Strangers in a
  group are ignored silently. Group alerts carry metadata only.
- To add a group: add `@<bot_username>` to it, send `/chatid@<bot_username>` as
  an authorized user, then an owner pastes that id in Admin → Add group (or
  sends `/allowgroup` in the group). No `.env` edit is required.
  (If Telegram upgrades the group to a supergroup its id changes to `-100...`.)
- Mailbox passwords, OAuth tokens and API keys are never collected in Telegram.
  Hostinger/IMAP accounts are added locally with `webmail-add` (hidden prompt).
- Bot privacy mode is ON by default, so in groups the bot only sees `/commands`
  and replies to itself - that is why groups use commands + inline buttons.
- A user must send `/start` to the bot privately once before it can DM them.
- Only ONE process may poll a bot token. The log shows
  `Telegram Conflict: another getUpdates consumer...` if a second one exists.

## Adding business webmail accounts (IMAP/SMTP with a password)

### From Telegram (recommended)

An authorized admin can start onboarding without touching the terminal or
config files:

1. In the group dashboard tap **Mailboxes** → **Add email**.
2. Pick Hostinger (host/port pre-filled) or Custom IMAP+SMTP.
3. Answer the wizard questions in the private DM: account id, email,
   hostnames, ports, usernames. Passwords are NEVER requested here.
4. Mailops shows a one-shot server-side command, e.g.
   `python -m mailops webmail-run-pending <session-token>`. Run it on
   the Mailops host; the password is read at a hidden terminal prompt.
5. The Mailboxes screen flips to "validated and registered" when the
   IMAP + SMTP handshake succeeds; nothing was sent to the mailbox.

Opt-in only (`MAILOPS_ALLOW_DM_CREDENTIALS=1` in `.env`): the admin can
instead send the password directly in the bot DM. Mailops holds it in
RAM for the single validation call, then deletes the message and wipes
the secret. The CLI path is the default because it never puts the
password on Telegram servers.

A new mailbox always starts **read-only with sending off** regardless of
the capture mode. An admin flips the per-account policy explicitly from
the Settings screens before anything is organized or sent.

### Direct CLI (unchanged)

You need, from your hosting provider: IMAP host, SMTP host (implicit-TLS ports
993/465 by default), the login username (often the full email address) and
the mailbox password. Run in an interactive terminal:

```powershell
.\.venv\Scripts\python.exe -m mailops webmail-add <id> <email> --imap-host <host> --smtp-host <host>
.\.venv\Scripts\python.exe -m mailops webmail-check <id>     # re-validate later
```

The password is typed at a hidden prompt and stored only in `secrets/` (git-
ignored); `config.toml` references it through `get-secret`. Validation uses
`himalaya account check` - it logs in to IMAP and SMTP but sends no mail, moves
nothing and creates no folders. The account is registered in `accounts.json`
only if validation passes; on failure everything is rolled back. Existing
accounts are never modified and files are backed up as `*.bak-<timestamp>`.
STARTTLS-only servers are not supported yet (Himalaya v2.2.1 limitation).

## Safe test mode

Blocks every mutating mailbox operation (move/copy/flag/send/create-folder)
and every send, while reads, classification, the dashboard, and Telegram
buttons still work. Enable either way:

- `.env`: `MAILOPS_TEST_MODE=1`
- or `data/settings.json`: `"test_mode": true`

## Account policies (what each mailbox may do)

Every account is **deny-by-default** for anything that changes a mailbox or
sends mail. Reading, classifying, caching and notifying always work.

| Account | Organize (move/copy/flag/create folders) | Send |
|---|---|---|
| `altcoininformer` (the only active account) | Supported, **off until you approve production activation** | Off |
| `gmail` / any `@gmail.com` account (retired, inactive) | **Never** - locked read-only as a safety net if it is ever re-added (the inbox is shared with the Sales-Agent). | **Never** (locked) |
| any other account | Off | Off |

Settings live in `data/settings.json` (missing = off; only the JSON literal
`true` enables a capability; Gmail ignores overrides):

```json
"account_policies": {
  "altcoininformer": { "organize": false, "send": false }
}
```

- **Production activation of Altcoin Informer** = set `"organize": true` for it
  (and turn test mode off). Do this only after explicit approval. Priority
  folders are then created on the next sync/`bootstrap`.
- **Sending** needs `"send": true` for the account **and** an explicit approval
  of every single email (the Send button on a reviewed draft, private chat,
  authorized user). Code that calls `reply_send`/`send_raw` without
  `approved=True` is refused. Groups can never send.
- Enforced in three layers: the sync pipeline, the Telegram handlers, and the
  Himalaya client itself (including a backstop on raw himalaya commands).
- **Dashboards** show Mailops' own classification for read-only / not-yet-
  approved accounts and in test mode, so counts are correct even though no
  `INBOX.P*` folder exists. Each dashboard line shows the account's mode, and
  buttons open the real message in INBOX.
- `python -m mailops policy` prints the effective policy (never contacts a mailbox).

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

Roughly 350 tests cover classification (rules + LLM fallback), priority buckets,
reply KB + AI drafting, mailbox operations (fake Himalaya), sync worker,
Telegram button actions (send requires a draft; failures keep drafts),
Telegram group authorization/routing/keyboards/notifications, webmail
onboarding, safe mode, per-account policies (read-only Gmail shared with the
Sales-Agent, approval-gated sends), Gmail OAuth, and the Altcoin-Informer-only
guarantees (a retired Gmail account is never synced, cached, notified, shown or
reachable from stale buttons). No network or real mailbox is touched.

`scripts/smoke_phase1.py` is a **manual live** smoke check (moves real
messages, sends real Telegram pushes) — it refuses to run unless
`MAILOPS_LIVE_SMOKE=1`.

## Commands

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

python -m mailops bootstrap   # create P0–P3 / Tag folders
python -m mailops sync        # classify new INBOX mail
python -m mailops rescan      # full classify + sort (INBOX + P folders)
python -m mailops accounts    # list registered accounts
python -m mailops policy      # what each account may do (organize / send)
python -m mailops run         # Telegram bot + hourly sync
```

## Deployment / running continuously

1. Clone the repo, create the venv and install deps:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill it in (Telegram token, allowed
   user ids, Google OAuth client, optional LLM endpoint).
3. Do the Gmail OAuth login above (once per account).
4. Start now: `powershell -ExecutionPolicy Bypass -File .\start.ps1`
5. Start automatically at logon (optional):
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\register_startup.ps1
   ```
   Remove with `Unregister-ScheduledTask -TaskName "Mailops Agent"`.

Optional environment overrides: `MAILOPS_HOME` (where `.env` + himalaya
config live, default = repo root), `MAILOPS_HIMALAYA_DIR`, `HIMALAYA_BIN`.

## Reply knowledge base

Edit `data/kb/`:

| File | Role |
|------|------|
| `_shared.md` | Voice, always-include links, never-invent-prices |
| `sales.md` / `coinography.md` / `memecoinist.md` | Per-inbox links, packages, signature |

Telegram: **Dashboard → Reply knowledge base** (view / append notes).

## Phase 2

Hermes stays offline on the same bot token while Mailops is polling. See `PHASE2.md`.
