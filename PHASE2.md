# Phase 2 — Hermes agent (scaffold only; not active in Phase 1)

Phase 1 owns product logic: IMAP folder taxonomy, classifier, button Telegram UI, notifications.
Hermes stays offline for this profile’s Telegram token until Phase 2 is deliberately enabled.

## Integration points for Hermes later

1. **Same IMAP world model** — folders `INBOX.P0-Critical` … `INBOX.Handled` and `INBOX.Tag.*` are the shared substrate (Gmail labels via IMAP).
2. **Same Connect UI / Himalaya config** — `../home/.config/himalaya/`.
3. **Tiny ops DB** — `data/ops.db` for dedupe/settings only; Hermes should not invent a second mail store.
4. **Reuse mailops as library** — import `mailops.sync_worker`, `mailops.himalaya_client`, `mailops.classifier` instead of reimplementing folder ops.
5. **Bot token handoff** — either:
   - Hermes gateway resumes Telegram and mailops becomes a library Hermes calls, or
   - Hermes runs as a second profile/bot for NL chat while mailops keeps the button UI.

## Activation checklist (when starting Phase 2)

1. Stop mailops polling (`start.ps1` / kill `python -m mailops run`).
2. Point Hermes skills at the same Himalaya accounts + folder names above.
3. Prefer read/act on already-sorted folders; do not rebuild a local mail mirror.
4. Keep button UX or migrate callbacks carefully — users already rely on Critical / Needs reply / Inboxes.

## Suggested Phase 2 skills

- Natural-language triage over already-sorted folders
- Learning VIP/mute from feedback already in `data/settings.json`
- Multi-step ops (“prepare weekly sales follow-ups”) using folder queries
- **Reply intelligence on top of `mailops/data/kb/`** — Hermes can propose KB updates from successful sends / Master corrections, but Mailops remains the draft runtime that injects the KB into AI replies

## Reply KB (already live in Phase 1)

- Path: `mailops/data/kb/_shared.md` + `{account}.md`
- Consumed by `mailops.reply_kb` → Telegram **AI draft reply**
- Telegram: Dashboard → Reply knowledge base (view / append notes)

Do not enable Hermes gateway on the same bot token while mailops is polling.
