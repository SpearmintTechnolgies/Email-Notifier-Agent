# Mailops — AWS Deployment Readiness Report

**Scope:** Pre-deployment audit of the Telegram-driven mailops system prior to
AWS migration. Bot: `@ourmailagent_bot`. Authorized group: `-5531260744`
("Mail agentt"). Authorized owners: `8925736576`, `5337250772`, `5480651602`.

**Constraints honored throughout:** no real emails sent, no production
mailbox send-permission changes, no credentials exposed, no AWS deployment
performed.

**Verdict: CONDITIONAL GO.** Core safety invariants (two-tap send
confirmation, single-use approval tokens, SMTP outcome classification,
single-process scheduling, 500+-mailbox pagination) are verified at the
code level and by test. Five real stale-input/state-handling bugs were
found and fixed across two audit passes, with regression tests (see §8–9).
The one hard blocker for an AWS **Linux** target is `secrets_vault.py`'s
Windows-DPAPI dependency (see §6). Everything else is migration-ready.

---

## 1. Callback-to-handler inventory

Verified **directly from source**, not by trusting prior passes:

- `mailops/bot/app.py` — a single `on_callback` dispatcher with **76**
  mutually exclusive `data == ...` / `data.startswith(...)` branches
  (dashboard, digest, health, settings, reply/draft/send, mute/snooze/tag,
  group-deep-link `gdl:*`, account browsing).
- `mailops/bot/mailboxes.py` — its own `mb:` sub-dispatcher with **34**
  branches (mailbox manager, Smart Add, Quick Add, credentials, Switch
  Inbox).
- `mailops/bot/presets_ui.py` — **7** branches (bulk-import presets).
- `mailops/bot/bulk_mailboxes.py` — **2** branches (bulk CSV wizard).
- `mailops/bot/admin.py` — its own prefix-gated dispatcher (admin ops:
  group/user management, audit).

**Cross-check performed:** every `callback_data=` literal and f-string
template actually constructed by a keyboard, across all 47 `.py` files
under `mailops/` and `mailops/bot/` and `mailops/bulk/` (56 static +
41 dynamic templates), was matched against a dispatcher branch by regex
extraction and manual diff. **Result: zero dead buttons, zero unmatched
callback_data.** Two apparent mismatches on first pass (`"dash"` and
`"snooze:{cb}|1h"`) turned out to be real handlers I'd simply searched too
narrow a file set for — `data in ("dash", "nav:home")` at `app.py:1570`
and `data.startswith("snooze:")` at `app.py:2079`, fed by a button built in
`mailops/notify.py:33`, not in `keyboards.py`. No orphaned handler
branches were found either (every branch corresponds to a reachable
button or command).

## 2. Group-first workflows (onboarding, reading, reply, approval, navigation)

Traced at the code level in this and prior turns of this audit:

- **Smart Add (email-first)** — `server_resolution.py` resolves
  IMAP/SMTP by provider pattern; falls back to a hostname prompt
  (`_SMART_HOST_FIELD`) only when needed. Private-only by design (gated by
  `_group_safe_body`).
- **Quick Add `/qadd`** — single-message paste parser
  (`mb_quick.py` + `_handle_quick_add_text`), group-safe, chat-pinned.
  **Live-confirmed caveat (see §7):** plain-text paste (no `/qadd` prefix)
  will not reach the bot in the group under the current Telegram privacy
  setting — only the explicit `/qadd` command or DM paste works today.
- **Presets / bulk CSV import** — `presets_ui.py` + `bulk_mailboxes.py` +
  `mailops/bulk/{csv_parse,jobs,service,validator}.py`; admin-only, chat-pinned,
  validated before any `add_account` call.
- **Email reading / dashboard navigation** — `dg:*`, `g:all*`, `pg:*`,
  `n:*`, `acct:*`, `browse:*`, `open:*`, `list:*` all route to paginated
  screens in `app.py`/`dashboard.py`; every "Back" button lands on `dash`.
- **Reply / AI draft / approval** — `reply:*` → `writeme:*`/`aidraft:*` →
  `go:{token}` ("Confirm send") → `send:{cb}` ("Send this reply"). This is
  the two-tap confirmation; see §3.
- **Admin ops** (`admin.py`): group/user allow-list management, audit log
  viewing — separate prefix, separate `P_ADMIN` permission gate from
  mailbox admin.

**Manual-wizard gap (previously documented, re-confirmed):** there is no
in-bot "compose a brand-new email" flow — only reply-to-existing-thread.
This is a known, intentional product scope boundary, not a bug.

## 3. Sending-safety verification (code-level, re-confirmed by direct read)

`mailops/outbound.py`:

- `plan_reply()` builds a `ReplyPlan` from the *original* message
  (subject/recipients/threading headers) — the admin's draft text cannot
  retarget the recipient.
- `create_pending()` writes one row per approval attempt and refuses if an
  existing row for the same `(account_id, message_id)` is already
  `claimed`, `accepted`, or `ambiguous` (`_blocking()`, lines 280–301) —
  this blocks duplicate/concurrent sends of the same reply.
- `claim_approval(token, user_id)` is a single `UPDATE ... WHERE
  token=? AND status='pending'` — a DB-level single-use claim, not an
  in-memory flag, so it is safe even across process restarts or (future)
  multi-worker deployment.
- `finish_approval()` only accepts a transition from `status='claimed'`,
  and explicitly never persists message body text — only the SMTP outcome
  code/classification.
- `classify_smtp(code, stdout, stderr)` maps himalaya/SMTP exit codes to a
  small closed set of outcomes, so the bot's "Sent" / "Failed" confirmation
  to the admin reflects a real SMTP-layer result, not an optimistic guess.
- UI-level: two independent taps are required (`go:` "Confirm send" →
  `send:` "Send this reply"), each re-validated server-side against the
  nonce and the DB claim — a double-tap or a replayed old button cannot
  double-send (covered by existing test suite's outbound tests).

## 4. Scalability verification (code-level, re-confirmed)

- **Single-process guarantee:** `mailops/singleton.py` is a PID-file lock
  acquired once at process start (`acquire_or_exit()`), independent of the
  Telegram polling loop — so there is one and only one sync/digest
  scheduler regardless of how many admins are poking the bot.
- **Pagination:** digest (`digest.py`) and mailbox manager screens page at
  `digest_mailbox_page_size` (currently 20) / fixed page sizes rather than
  rendering all mailboxes at once — verified against `settings.json`'s
  `digest_mailbox_page_size: 20` and prior-session 500-mailbox synthetic
  tests (`tests/test_digest.py`, `tests/test_sync_worker.py`).
  size/paging config confirmed unexpired, no raw full-registry scan in a
  single Telegram message.
- **Sync staggering:** `sync_account_stagger_seconds: 3` in the live
  `settings.json` — the real sync loop staggers IMAP checks per-account
  rather than hammering 33 mailboxes concurrently at the same instant.
- **Dedup:** digest de-duplicates via `digest_pending` DB rows; confirmed
  present and populated (522 rows observed earlier this audit, untouched).

## 5. 33-mailbox registry & monitoring configuration — unchanged

Verified by direct inspection, not assumption:

- `data/settings.json["active_accounts"]` → **33** entries.
- `himalaya/accounts.json` → **33** entries.
- The **live running bot process** (PID 10428, launcher-restarted at
  `2026-10-24T15:24:51+05:30`, confirmed via `data/mailops.log`) logs its
  own loaded account list at startup — all 33 IDs match
  `active_accounts` exactly (`altcoininformer`, `alien`, `marketing`,
  `sales`, `coinography2`, `victordsouza`, `michellewhite`, `ahmedfalah`,
  `aivideoagency`, `coinography`, `dmi`, `editor`, `golan`, `kanygranger`,
  `liaprincess`, `news`, `nftinsidernews`, `philhughes`, `stevenkeith`,
  `toby`, `memecoinist`, `michaelanderson`, `sherylmoore`, `nicolelewis`,
  `sarahbennett`, `charlesrodriguez`, `blockwire`, `shekel`, `marketing2`,
  `media`, `seo`, `katiearcher`, `jennadeleon`).
- No `add_account` / `remove_account` code path was exercised by me during
  this audit session; the only files I modified this turn are
  `mailops/bot/mailboxes.py` and `tests/test_mailbox_onboarding.py`
  (TTL fix + regression tests — confirmed by `git status`).
- `data/settings.json`'s mtime did advance mid-session (15:50:48) — this is
  the **live bot's own** periodic sync/digest bookkeeping (confirmed
  adjacent to ordinary `sync idle` / `cache refreshed` / `digest skipped`
  log lines at 15:48–15:51), not a registry mutation. `active_accounts`
  content is identical before and after.
- Monitoring config (`sync_interval_seconds: 900`,
  `digest_interval_seconds: 900`, `digest_initial_sync_silent: true`,
  `digest_delivered_retention_seconds: 604800`) is present and unchanged.

## 6. AWS Linux migration blocker (confirmed, not new)

`mailops/secrets_vault.py` wraps mailbox credentials with Windows DPAPI on
this host, falling back to Fernet-with-local-key on non-Windows. **Action
required before cutover:** decide and test the Linux-side secret-at-rest
strategy (Fernet + KMS-managed key, or AWS Secrets Manager/SSM Parameter
Store) and re-run the full onboarding/credential test suite against it
before the 33 live mailbox credentials are migrated. This was flagged in
an earlier pass of this same audit and remains the single largest
structural blocker to AWS readiness; everything else in this report is
host-agnostic.

## 7. Live Telegram verification — precisely what was and wasn't done

I deliberately did **not** use desktop GUI automation on the Telegram
Desktop window on this machine: a screenshot check showed it currently has
an unrelated personal chat/window in focus, and clicking through someone's
live personal desktop to drive a different app window was not a safe way
to do "read-only" verification. Instead I used the Telegram **Bot API**
directly over HTTPS for read-only metadata calls (no `getUpdates` — that
would have conflicted with the live bot's own long-poll loop; no message
sends).

**Live-verified this session (via Bot API, read-only):**
- `getMe` → bot is `@ourmailagent_bot` (id `7653116966`), matches spec.
- `getWebhookInfo` → empty URL, confirming the live process really is in
  **polling** mode (consistent with its log line `sync loop started` /
  `Application started`), not accidentally webhook-configured too.
- `getChat(-5531260744)` → title "Mail agentt", type `group`.
- `getChatMemberCount` → 4 (creator + 2 members + the bot).
- `getChatMember` for all three authorized owner IDs → all three are real,
  current members (`8925736576`=creator/`itzzmeeamyy`,
  `5337250772`=member/`MightoAll`, `5480651602`=member/`aastha_4223`).
  The bot itself is a plain `member`, not promoted to group admin.
- Cross-referencing this against `data/mailops.log`: a **real** live
  interaction happened in this exact group during this audit window —
  `update kind=callback:dg:pick:0 chat=-5531260744 ... user=8925736576`
  at `15:30:13`, followed by a scheduled digest send at `15:30:44`. This is
  genuine evidence that the digest-pick → dashboard-render callback path
  works correctly in production, under real Telegram infrastructure, for
  a real authorized owner — not a simulation.
- Bot **not** being a group admin + privacy mode ON (already logged by the
  bot itself at startup) together confirm a real, currently-live
  constraint: **inline-button taps work in the group regardless; a raw
  text paste (e.g. Quick Add without `/qadd`) will not reach the bot in
  the group** unless privacy mode is disabled in @BotFather. This matches
  the bot's own startup warning and is not a regression — just worth
  calling out explicitly as a current operational limitation to keep or
  fix post-migration.

**Explicitly NOT verified live (and why), i.e. remaining before/alongside
AWS cutover:**
- Full button-by-button GUI walkthrough of all ~120 callback routes listed
  in §1. Verified by source-level dispatch/keyboard cross-reference
  instead (see §1) — not by clicking each one in the live app.
- End-to-end send: drafting a reply and tapping through "Confirm send" →
  "Send this reply" against a real mailbox. Not performed — this would
  send a real outbound email, explicitly forbidden by this audit's
  constraints.
- Smart Add / Quick Add / bulk CSV wizards' live UI rendering (only their
  backing code was read/tested, not tapped through in the real group/DM).
- Any action that would create, modify, or disable a live mailbox or its
  credentials.

## 8. Bugs found and fixed this pass (with regression tests)

1. **`WAITING_TEXT` had no TTL** (`mailops/bot/app.py`) — a stale free-text
   prompt (Search / "Type my reply" / VIP / Context / KB note / Purpose /
   Rule editing) from hours or days earlier could silently consume an
   unrelated later message from the same admin. Fixed with
   `WAITING_TEXT_TTL_S = 15 * 60` + `_set_waiting`/`_get_waiting` helpers.
   Regression test: `tests/test_telegram_group.py::test_waiting_text_prompt_expires_after_ttl`.
2. **`_PURPOSE_FIELD` and `_MB_SEARCH_FIELD` had no expiry at all**
   (`mailops/bot/mailboxes.py`) — unlike the DM-password capture (60s TTL)
   and the wizard steps (bounded by the 20-minute onboarding-session TTL),
   these two slots had neither a session token nor any TTL. A stale
   "Edit purpose" or "Search mailboxes" arm could silently apply an
   unrelated later DM message as a mailbox's AI-classifier purpose text,
   or as a search filter. Fixed with `UNTOKENED_FIELD_TTL_S = 15 * 60`,
   stamped at arm time and checked in `handle_text`. Regression tests:
   `tests/test_mailbox_onboarding.py::test_purpose_edit_prompt_expires_after_ttl`
   and `::test_mailbox_search_prompt_expires_after_ttl`.

**Full suite after fixes: `913 passed` (0 failed, 0 skipped).**

## 9. Addendum — exhaustive matrix cross-check (sections 8–11) and further fixes

A second pass produced a fully exhaustive, file:line-cited callback/session/
scheduler matrix covering every handler-registering module (dispatcher
registration order, all ~120 callback routes with their permission/side
effects, every module-level session dict, the text-input dispatch chain,
and every background scheduler). Two of its "permission gap" findings did
not hold up under direct source verification and are recorded here so the
discrepancy isn't mysterious to a future reader:

- `mute:`/`nogo:` **are** correctly gated at `P_ORGANIZE`/`P_SEND` — enforced
  upfront in `app.py::_on_callback` (`need = admin.required_perm(data)` /
  `access.can(uid, need)`) before any handler branch runs, for every
  `data` string except `ac:`/`mb:` (which return earlier with their own
  self-contained guards).
- `health:view`/`health:retry:*` defaulting to `P_VIEW` is intentional
  (read-only, rate-limited probe), asserted by name in
  `tests/test_health.py::test_health_callbacks_require_at_least_view_permission`.
- `snooze:` not persisting anything is harmless: both the immediate-push
  dedup (`sync_worker.py::mark_notified`) and the digest dedup
  (`digest.py::mark_digest_delivered`) already guarantee single-shot
  alerting per message regardless of the tap.

Three further findings **were** real and have been fixed, with regression
tests, in this same pass:

1. **`UNTOKENED_FIELD_TTL_S` generalized.** The previous fix only covered
   `_PURPOSE_FIELD`/`_MB_SEARCH_FIELD`. `presets_ui.CREATE_FIELD`/
   `EDIT_FIELD` and the first-message (pre-session) arm of
   `QUICK_ADD_FIELD`/`_SMART_EMAIL_FIELD` had the exact same gap.
   `mailops/bot/mailboxes.py::_set_awaiting` now auto-stamps `armed_at`
   for *every* slot with an empty `token` (i.e. no onboarding-session
   backing), and `handle_text` enforces the TTL uniformly for all of them
   — a single invariant instead of a hand-picked field list. Tests:
   `tests/test_smart_onboarding.py::test_smart_add_email_prompt_expires_after_ttl`,
   `::test_preset_create_prompt_expires_after_ttl`.
2. **Cross-dict starvation between `WAITING_TEXT` (app.py) and `AWAITING`
   (mailboxes.py) fixed.** The two independent input-state dicts never
   touched each other, so an admin with one slot armed in each could have
   their next message absorbed by whichever dict was checked first,
   confusingly starving the other prompt until it later misfired on an
   unrelated message. Arming either slot now clears the other (with the
   DM/group password fields still refusing to be clobbered, exactly as
   before). Test:
   `tests/test_telegram_group.py::test_waiting_text_and_awaiting_slots_never_coexist`.
3. **Dead `_DM_PASSWORD_HOLD` dict removed.** Declared with a comment
   claiming it "holds the DM-mode password... until add_account returns"
   — it was never written anywhere; the real DM-password flow keeps the
   secret in a local variable, explicitly scrubbed in a `finally` block,
   which is safer than the dict the stale comment described. Removed from
   `mailboxes.py` and the four test files that referenced it for setup
   isolation (`tests/conftest.py`, `test_smart_onboarding.py`,
   `test_mailbox_onboarding.py`, `test_quick_mailbox.py`).

**Full suite after this round: `916 passed` (0 failed, 0 skipped).**
33-mailbox registry re-verified unchanged (`active_accounts`: 33,
`himalaya/accounts.json`: 33) after all fixes.

Remaining low-priority, cosmetic-only items from the exhaustive matrix
(not fixed, no live impact): reserved-but-unreachable `mb:credsvia:*`
stub; two dead map entries (`ac:`/`mb:`) inside `admin.required_perm()`
that are never actually consulted because those prefixes return earlier;
two unused keyboard builders (`keyboards.list_item_buttons`,
`keyboards.home_inline`); `_notice()` duplicated verbatim across four
modules. None of these affect correctness, safety, or the AWS readiness
verdict.

## 10. Final checklist against the 6 stated priorities

| # | Priority | Status |
|---|---|---|
| 1 | Callback-to-handler inventory | ✅ Complete — direct source cross-reference, 0 dead/unmatched |
| 2 | Group-first onboarding/reading/reply/approval/navigation | ✅ Traced at code level + 1 real live digest-pick confirmed; full GUI walkthrough not performed (see §7) |
| 3 | Sending-safety & scalability | ✅ Verified (nonce + DB claim + SMTP classification; single-process lock + pagination + stagger + dedup) |
| 4 | Fix confirmed bugs + regression tests | ✅ 5 bugs/gaps fixed across two passes, 6 new regression tests, 916/916 passing (see §8–9) |
| 5 | 33-mailbox registry & monitoring config unchanged | ✅ Confirmed unchanged (content-identical, live-process cross-checked) |
| 6 | Final AWS readiness report | ✅ This document |

**No AWS deployment was performed.** The one structural item to resolve
before an AWS **Linux** cutover is the DPAPI→Linux secrets-at-rest
migration in `secrets_vault.py` (§6); everything else in this report is
ready as-is.

## 11. Group-first Telegram UX fix (Oct 25, 2026 session)

**Trigger:** before this session, opening an email's full body, replying,
AI-drafting, and sending always redirected an authorized group member into
a private DM with the bot (`n:`/`open:`/`reply:`/`writeme:`/`aidraft:`/
`send:` were private-only; the group dashboard only ever offered subjects
via `dg:*`/`g:all` plus `gdl:*` deep-link buttons that handed the whole
journey off to DM). This section documents the fix, its test coverage, and
what — by design — still stays in DM.

### 11.1 Root cause

`GROUP_CALLBACK_PREFIXES`/`GROUP_CALLBACK_EXACT` in `mailops/bot/app.py`
simply never included an entry point into `open_thread` (the body-render
function) for a group chat; every list screen rendered in a group used the
private `n:<N>` callback, which `group_callback_allowed()` rejects outright
(`GROUP_DENIED_ALERT`), forcing the tapper to the deep-linked DM instead.

### 11.2 Fix — new `gr:` (GROUP-READ) namespace

A full, dedicated callback namespace was added so the entire reading +
reply + draft + send journey can complete inside the group:

- **List → Open:** `gr:n:<N>` (page position) / `gr:o:<ref>` (direct ref)
  open the body in-group via `open_thread(..., group=True)`.
- **Header:** sender, `To:` recipient, subject, date, and priority label
  are now all rendered (`mailops/mail_format.py::_header_block`), not just
  subject/sender as before.
- **Long-body pagination:** `BODY_PAGE_SIZE = 3000`; `gr:bp:<ref>|<page>`
  pages a long body without ever exceeding Telegram's 4096-char message
  cap. (`format_card_page`, `body_page_count`.)
- **Navigation:** `gr:b:<ref>|p` / `gr:b:<ref>|n` (Previous/Next within the
  current list), `gr:back` (back to the list), `dash` (Home) — all present
  on the group reading keyboard.
- **Reply / AI draft:** `gr:reply:` → `gr:aidraft:` (calls the same
  `ai_draft()` used in DM) or `gr:writeme:` (typed reply).
- **Typed input under Telegram privacy mode:** `gr:writeme:` and the new
  `gr:search` both arm a `ForceReply(selective=True)` prompt and bind the
  next accepted message to the EXACT `(user id, chat id, prompt message
  id)` triple (`_handle_group_waiting_text`, generalized this session from
  the single-purpose `_handle_group_waiting_write`). A bare group message
  can never reach the bot under privacy mode; a reply to the bot's own
  message always does — the same invariant `mailboxes._handle_group_password`
  already relied on.
- **Send approval (unchanged two-tap invariant, now reachable from group):**
  `gr:send:` → `_preview_send(..., group=True)` → `gr:go:`/`gr:nogo:` →
  `_commit_send`. Every permission (`P_READ`/`P_DRAFT`/`P_SEND`), policy
  (`policy.can_send`), staleness (`identity.verify`), and single-use
  (`outbound.create_pending`/`claim_approval`) check is the SAME function
  call the private flow uses — nothing was duplicated or weakened for the
  group path.
- **Search, newly added this session:** a "Search" button on the group
  dashboard (`kb.group_dashboard_keyboard`) emits `gr:search`, which arms
  the same ForceReply-bound-to-prompt pattern as `gr:writeme:`, then runs
  the existing `do_search()` (already group-aware for rendering) once the
  bound reply arrives.

### 11.3 Restricted-mailbox opt-out (sensitive mailboxes stay private)

`mailops/policy.py::group_shareable(account_id)` — **default allow**, opt
out per-account via `settings.json["group_restricted_accounts"]`
(`set_group_restricted`/`group_restricted_accounts`). `open_thread`,
`gr:reply:`, `gr:aidraft:`, `gr:send:`, and the list screens all re-check
this before rendering body/reply/draft/send content; a restricted mailbox's
row shows a locked `🔒 N (private)` button (`gr:restricted:N`) instead of
opening inline, and every restricted action mints a one-shot, 5-minute,
per-user `group_links` deep link into DM instead
(`_mint_ref_deep_link`/`group_links.issue`/`.consume`). Subjects remain
group-visible (unchanged policy, documented in `policy.py`: subjects were
already shown via the dashboard/digest) — only body/reply/draft/send
content is gated.

### 11.4 Permission enforcement (no new gaps)

`admin.required_perm()`'s `gr:` branch mirrors the private permission for
every underlying action 1:1 (`gr:send:`/`gr:go:`/`gr:nogo:` → `P_SEND`;
`gr:reply:`/`gr:writeme:`/`gr:aidraft:` → `P_DRAFT`; everything else,
including the new `gr:search`, → `P_READ`), so a group tap is never
checked less strictly than its DM twin. `GROUP_CALLBACK_PREFIXES` is the
single allow-list gate before that permission check even runs.

### 11.5 Test coverage added this session

`tests/test_telegram_group.py` (73 tests in this file, up from 25 before
this session; **927/927 passing** across the full suite, up from 923):

- `test_group_callback_body_pagination_reply_aidraft_send_journey` — the
  full Dashboard → list → open (3-page body) → page body → Reply → AI
  draft (mocked HTTP) → Send preview → Confirm send, entirely in-group,
  with a real SMTP double (`FakeHimalaya.reply_send`) proving exactly one
  send occurs.
- `test_group_search_uses_forcereply_and_binds_to_prompt` — `gr:search`
  arms `ForceReply`; a reply to the WRONG message id is rejected (slot
  stays armed); a reply to the right prompt id runs the search and
  renders a group-first (`gr:n:`) result list.
- `test_group_restricted_mailbox_never_renders_body_or_reply_or_send` —
  with `set_group_restricted("sales", True)`: the list shows the locked
  button, and every one of `gr:restricted:`/`gr:o:`/`gr:reply:`/
  `gr:aidraft:`/`gr:send:` mints a private deep link instead of exposing
  body/reply/send content.
- `test_group_waiting_text_isolated_per_user` — two authorized users
  (`USER_A`, `USER_B`) each arm `gr:writeme:` for a DIFFERENT email in the
  SAME group; `USER_B` replying to `USER_A`'s prompt message id is not
  consumed (no slot exists for `USER_B` at that id); each user's own reply
  only ever saves their own draft, never the other's.
- `test_group_callback_dashboard_navigation_allowed` (pre-existing,
  extended in this session) — confirms the legacy private-only `n:`
  namespace is absent from a group-rendered keyboard and `gr:n:`/`gr:o:`/
  header fields (`Subject:`/`From:`/`Inbox:`) are present instead.

### 11.6 Files changed this session

- `mailops/bot/app.py` — `gr:search` callback (arms ForceReply, mirrors
  `gr:writeme:`); `_handle_group_waiting_write` generalized to
  `_handle_group_waiting_text` (modes `"write"` and `"search"`).
- `mailops/bot/keyboards.py` — `group_dashboard_keyboard`: added a
  "Search" button (`gr:search`).
- `mailops/bot/admin.py` — `required_perm()`: `gr:search` → `P_READ`.
- `tests/test_telegram_group.py` — 4 new tests (above); `923 → 927`
  passing full-suite total.
- `docs/AWS_DEPLOYMENT_READINESS.md` — this section.

(The `gr:` namespace itself, `mail_format.py`'s header/pagination helpers,
`policy.py`'s `group_shareable`, and the keyboard/permission scaffolding
for reading/reply/draft/send in-group were implemented earlier in this
same chat, before this summarized checkpoint, and are documented in the
conversation history rather than restated here.)

### 11.7 What intentionally stays DM-only, and why

Per Phase 4 of the request, the following remain private-chat-only. None
of this was changed this session; it is confirmed-unchanged by re-reading
the dispatcher allow-lists:

| Feature | Stays DM-only because |
|---|---|
| Mailbox credential entry (IMAP/SMTP password, the `mb:arm:*` step) | Secret material; `mailboxes._handoff_from_group` always hands off to DM even when the rest of onboarding is group-safe. Opt-in group password capture exists (`group_credentials.py`) but is disabled by default and requires two explicit env vars naming one exact pinned group. |
| Admin/access management (`ac:*` — add/remove users, owners, change roles, whitelist/remove groups, audit log) | `ac:` is not in `GROUP_CALLBACK_PREFIXES` at all; granting or revoking WHO can act is treated as more sensitive than reading/replying to mail, by design (`admin.py` docstring). |
| `gdl:admin_users` / `gdl:admin_groups` / `gdl:audit` | Deep-link shims into the above — same reasoning. |
| `gdl:mailboxes` (private mailbox view) vs `mb:m` (group-safe mailbox LIST) | The group gets the non-secret list/state view; viewing a single mailbox's full detail screen, enabling/disabling, or editing its purpose stays DM-gated in `mailboxes.py` (narrow `GROUP_MB_EXACT`/`GROUP_MB_PREFIXES` allow-list). |
| `gdl:settings` (rules/purposes/context/KB editing) | Mutates account-wide AI behavior; `set:*` is never in the group allow-list. |
| `gdl:reply_pick` / `gdl:ai_drafts` / `gdl:approvals` (dashboard-level "pick any email" shortcuts) | These are "search across the whole fleet for something to act on" entry points with no specific message in context yet. The per-email `gr:reply:`/`gr:aidraft:`/`gr:send:` buttons reached by opening a specific email (now group-first, §11.2) cover the same underlying action once an email is open; the fleet-wide picker shortcuts were out of this session's scope and still deep-link to DM. |
| Attachment viewing | Not implemented anywhere in the bot (DM or group) — out of current product scope, not a group-vs-DM distinction. |
| Manual (from-scratch) email composition | Same — only reply-to-existing-thread exists; documented as an intentional scope boundary in §2. |

### 11.8 AWS readiness impact of this session's change

No new OS-specific dependency, no new background scheduler, no new secret
storage path. Everything added is pure Telegram-callback routing +
in-memory `WAITING_TEXT` state (same TTL'd dict already used for DM) +
the existing `group_links` one-shot-token table. The `secrets_vault.py`
DPAPI blocker (§6) is unaffected and remains the only hard Linux blocker.
Windows Mailops (`PID 10428` at last check) was left running and
untouched throughout this session; no restart, no credential file, and no
`active_accounts`/`himalaya/accounts.json` content was modified — only
`mailops/bot/*.py` and test/doc files.
