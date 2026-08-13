# Email Notifier Agent (Mailops)

Button-first Telegram remote control for multiple IMAP inboxes. Sorts mail on the live mailbox (P0–P3 folders), notifies on Important/Critical, and drafts/sends replies with a purpose-aware knowledge base.

## What it does

- Polls connected Himalaya IMAP accounts
- Classifies into `INBOX.P0-Critical` … `INBOX.P3-Noise` (keywords first, LLM fallback)
- Telegram dashboard: browse 10/page, open, reply, AI draft, send
- AI drafts use per-inbox **purpose + reply KB** (links, packages, signature)
- Confirm send → SMTP via Himalaya, copy saved to **Sent**, original moved to **Waiting**

## Launch (Windows)

```powershell
.\start.ps1
# or double-click Start Mailops.bat
```

Requires:

- Python 3.11+
- [Himalaya](https://github.com/pimalaya/himalaya) with accounts in `%LOCALAPPDATA%\hermes\profiles\inbox\home\.config\himalaya\`
- Telegram bot token + allowed user id in the inbox `.env` (see `.env.example`)
- Optional: FreeLLMAPI / OpenAI-compatible `/v1` for classification + drafts

## Commands

```powershell
pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location)

python -m mailops bootstrap   # create P0–P3 / Tag folders
python -m mailops sync        # classify new INBOX mail
python -m mailops rescan      # full classify + sort (INBOX + P folders)
python -m mailops run         # Telegram bot + hourly sync
```

## Reply knowledge base

Edit `data/kb/`:

| File | Role |
|------|------|
| `_shared.md` | Voice, always-include links, never-invent-prices |
| `sales.md` / `coinography.md` / `memecoinist.md` | Per-inbox links, packages, signature |

Telegram: **Dashboard → Reply knowledge base** (view / append notes).

## Phase 2

Hermes stays offline on the same bot token while Mailops is polling. See `PHASE2.md`.
