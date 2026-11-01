"""LIVE UAT verification for the exact failing screen:

    Dashboard -> Important -> coinography2

Renders that screen through the SAME ``show_bucket_page`` code path the
running bot executes, against the REAL production settings/registry, using
the authorized group chat id and a real authorized owner id - then asserts
the user's acceptance criteria:

  * tappable per-email Open buttons exist (gr:n:<N>)
  * the "Open an email in a private chat with the bot" text is GONE
  * tapping Open renders the body with Back / Reply / Home, in-group
  * Back returns to a list that still has Open buttons

This never calls the Telegram API (no sends, no getUpdates conflict with
the live poller) and performs no mailbox mutation - it is a pure render +
read probe, in the same spirit as scripts/verify_live_ui.py.
"""
from __future__ import annotations

import asyncio
import io
import sys
from types import SimpleNamespace

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from mailops.config import load_dotenv  # noqa: E402

load_dotenv()

from mailops import access  # noqa: E402
from mailops.bot import app as bot_app  # noqa: E402

GROUP_ID = -5531260744
ACCOUNT = "coinography2"


class CapturingQuery:
    def __init__(self, data: str):
        self.data = data
        self.edits: list[tuple] = []
        self.answers: list[tuple] = []
        self.message = CapturingMessage()

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edits.append(("", reply_markup))


class CapturingMessage:
    def __init__(self):
        self.sent: list[tuple] = []

    async def reply_text(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return SimpleNamespace(message_id=1)


def make_update(data: str, user_id: int):
    q = CapturingQuery(data)
    return SimpleNamespace(
        callback_query=q,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=GROUP_ID, type="supergroup"),
        effective_message=q.message,
    ), q


def datas(markup) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def labels(markup) -> list[str]:
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


def dump(title: str, text: str, markup) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)
    print(text[:1200])
    print("--- buttons ---")
    for row in (markup.inline_keyboard if markup else []):
        print("  " + " | ".join(f"[{b.text}] -> {b.callback_data or b.url}" for b in row))


async def run() -> int:
    owner = sorted(access.owner_ids())[0]
    print(f"authorized group: {GROUP_ID}")
    print(f"acting owner    : {owner}")
    print(f"group allowed   : {access.group_allowed(GROUP_ID)}")
    print(f"owner can READ  : {access.can(owner, access.P_READ)}")

    failures: list[str] = []
    ctx = SimpleNamespace(user_data={}, bot=SimpleNamespace(username="ourmailagent_bot"))

    # Scope the IMAP work to the ONE account under test. The dashboard cache
    # is per-process and in-memory, so a cold probe process would otherwise
    # trigger refresh_all() across the whole 39-mailbox fleet and compete
    # with the live poller for the same mailboxes. Marking the cache as
    # "already stamped" sends get_bucket down its single-account
    # ``_fetch_bucket`` path instead (see mailops/cache.py::get_bucket).
    import time as _time

    from mailops import cache as cache_mod

    with cache_mod._lock:
        if not cache_mod._updated_at:
            cache_mod._updated_at = _time.time()

    # --- the exact failing tap: Important -> coinography2 -----------------
    # Served from the dashboard cache path (collect_bucket(force=False)),
    # i.e. exactly what the live button does.
    upd, q = make_update(f"ab:{ACCOUNT}|p1", owner)
    await bot_app.on_callback(upd, ctx)
    if not q.edits:
        print(f"\nFAIL: ab:{ACCOUNT}|p1 rendered nothing (answers={q.answers})")
        return 1
    text, markup = q.edits[-1]
    dump(f"1. ab:{ACCOUNT}|p1  (Dashboard -> Important -> {ACCOUNT})", text, markup)
    d = datas(markup)

    if "private chat with the bot" in text:
        failures.append("list text STILL says 'private chat with the bot'")
    open_btns = [x for x in d if x.startswith("gr:n:")]
    if not open_btns:
        failures.append(f"no tappable gr:n:<N> Open buttons (datas={d})")
    if any(x.startswith("n:") for x in d):
        failures.append("leaked private-only n:<N> buttons into the group")
    for x in d:
        if not bot_app.group_callback_allowed(x):
            failures.append(f"button {x} is not group-allowed")
    print(f"\n  -> Open buttons: {open_btns}")
    print(f"  -> 'Open N' labels: {[l for l in labels(markup) if l.startswith('Open ')]}")
    print(f"  -> page_refs resolved: {len(ctx.user_data.get('page_refs') or [])}")

    if not open_btns:
        print("\nRESULT: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1

    # --- prove the Open tap resolves and is permitted, WITHOUT fetching --
    # ``open_thread`` does a real IMAP body fetch; running that here would
    # compete with the live poller for the same mailbox (and is slow across
    # a 39-mailbox fleet). The routing decision - which ref an Open button
    # maps to, whether this group may see it, and which keyboard it gets -
    # is fully determined without the network, so assert that instead and
    # leave the single body fetch to the human tap.
    page_refs = list(ctx.user_data.get("page_refs") or [])
    n = int(open_btns[0].split(":")[2])
    ref = page_refs[n - 1]
    account_id, mailbox, msg_id = bot_app._parse_ref(ref)
    from mailops.policy import group_shareable

    shareable = group_shareable(account_id)
    print()
    print("=" * 72)
    print(f"2. {open_btns[0]} routing (no IMAP fetch performed here)")
    print("=" * 72)
    print(f"  resolves to   : account={account_id} mailbox={mailbox} msg_id={msg_id}")
    print(f"  group_shareable({account_id}) = {shareable}")
    print(f"  required perm : {bot_app.admin.required_perm(open_btns[0])}")
    print(f"  owner has it  : {access.can(owner, bot_app.admin.required_perm(open_btns[0]))}")
    print(f"  group-allowed : {bot_app.group_callback_allowed(open_btns[0])}")

    if account_id != ACCOUNT:
        failures.append(f"Open 1 resolves to {account_id}, not {ACCOUNT} (cross-mailbox bug)")
    if not shareable:
        failures.append(
            f"{account_id} is group-restricted, so it would deep-link to DM "
            "(set group_restricted_accounts to [] to read it in-group)"
        )
    if not access.can(owner, bot_app.admin.required_perm(open_btns[0])):
        failures.append("owner lacks the permission the Open button requires")

    # The in-group reading keyboard this ref will receive (pure function).
    from mailops.bot import keyboards as kb

    body_markup = kb.thread_keyboard(
        account_id, mailbox, msg_id, has_prev=False, has_next=True,
        group=True, body_page=0, total_body_pages=1,
    )
    bd = datas(body_markup)
    print("--- reading keyboard it will render ---")
    for row in body_markup.inline_keyboard:
        print("  " + " | ".join(f"[{b.text}] -> {b.callback_data}" for b in row))
    if not any(x.startswith("gr:reply:") for x in bd):
        failures.append("reading keyboard has no Reply button")
    if "gr:back" not in bd:
        failures.append("reading keyboard has no Back button")
    if "dash" not in bd:
        failures.append("reading keyboard has no Home button")

    # --- Back must return to a still-tappable list -----------------------
    upd3, q3 = make_update("gr:back", owner)
    await bot_app.on_callback(upd3, ctx)
    if not q3.edits:
        failures.append("gr:back rendered nothing")
    else:
        back_text, back_markup = q3.edits[-1]
        back_d = datas(back_markup)
        print()
        print("=" * 72)
        print("3. gr:back  (Back to list)")
        print("=" * 72)
        print(back_text[:400])
        print(f"\n  -> Open buttons after Back: {[x for x in back_d if x.startswith('gr:n:')]}")
        if not any(x.startswith("gr:n:") for x in back_d):
            failures.append("Back returned a list with no Open buttons")
        if "private chat with the bot" in back_text:
            failures.append("Back list still says 'private chat with the bot'")

    # --- Reply menu routing (no draft generated, nothing sent) -----------
    reply_cb = f"gr:reply:{ref}"
    reply_markup = kb.reply_keyboard(account_id, mailbox, msg_id, group=True)
    rd = datas(reply_markup)
    print()
    print("=" * 72)
    print(f"4. {reply_cb}  (Reply menu - nothing drafted, nothing sent)")
    print("=" * 72)
    for row in reply_markup.inline_keyboard:
        print("  " + " | ".join(f"[{b.text}] -> {b.callback_data}" for b in row))
    if not any(x.startswith("gr:aidraft:") for x in rd):
        failures.append("Reply menu has no AI draft option")
    if not any(x.startswith("gr:writeme:") for x in rd):
        failures.append("Reply menu has no 'Type my reply' option")
    for x in rd + bd:
        if not bot_app.group_callback_allowed(x):
            failures.append(f"{x} is not group-allowed")

    print()
    print("=" * 72)
    if failures:
        print("RESULT: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS - group-first reading is live for the exact UAT screen")
    print(f"  Dashboard -> Important -> {ACCOUNT} now renders tappable Open buttons,")
    print("  routed in-group to a reading view with Back / Reply / Home.")
    print("  (no private chat required; no email fetched or sent by this probe)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
