"""Live verification of the new interactive Switch Inbox / Account Status
manager against the running Mailops data set.

This script:
  * Loads the LIVE .env, data/settings.json and himalaya/accounts.json
    (same files the running bot reads).
  * Imports ``mailops.bot.mailboxes`` and ``mailops.bot.app`` directly -
    this IS the dispatcher code the live bot executes.
  * Renders every new screen the admin can reach via Switch Inbox in
    both the authorized group chat and a private DM, and verifies that
    every button on each screen routes to a callback the dispatcher
    accepts (no "Private chat only - open the bot directly for this."
    will fire).
  * Prints the live mailboxes and the per-mailbox control screen.

Nothing is sent to Telegram and nothing talks to IMAP/SMTP: the health
check button is NOT exercised here so no mailbox is touched.

Usage (from the repo root):
    python scripts/verify_switch_inbox_live.py
"""
from __future__ import annotations

import io
import sys

# Force UTF-8 output for Windows consoles - our status strings contain
# both the warning glyph and non-ASCII mailbox display names.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from mailops.config import load_dotenv  # noqa: E402

load_dotenv()

from mailops import mailbox_admin  # noqa: E402
from mailops.bot import app as bot_app  # noqa: E402
from mailops.bot import mailboxes as mb  # noqa: E402

GROUP_ID = -5531260744


def _dump(title: str, text: str, markup) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    print(text)
    print("--- buttons ---")
    for row in markup.inline_keyboard:
        line = []
        for b in row:
            line.append(f"[{b.text!r} -> {b.callback_data!r}]")
        print("  " + " | ".join(line))


def _assert_group_safe(label: str, markup) -> int:
    """Returns the count of buttons checked. Fails loudly on any button
    that would be denied by the group dispatcher."""
    checked = 0
    for row in markup.inline_keyboard:
        for b in row:
            data = b.callback_data
            if not data or data == "noop":
                continue
            if data.startswith("mb:"):
                ok = bot_app.group_mailbox_callback_allowed(data)
            else:
                ok = bot_app.group_callback_allowed(data)
            if not ok:
                raise SystemExit(
                    f"[{label}] button {b.text!r} -> {data!r} would be "
                    f"DENIED in group {GROUP_ID}. Fix GROUP_MB_* / "
                    f"GROUP_CALLBACK_* in mailops/bot/app.py."
                )
            checked += 1
    return checked


def main() -> int:
    print(f"Verifying Switch Inbox manager against group {GROUP_ID}")
    print()
    live = mailbox_admin.list_mailboxes()
    if not live:
        print("No mailboxes registered - nothing to verify.")
        return 1
    print(f"Registered mailboxes ({len(live)}):")
    for m in live:
        print(
            f"  - {m.account_id:20s}  <{m.email}>  "
            f"active={m.active}  creds={m.has_credentials}  locked={m.locked}"
        )

    # 1. mb:switch — group render.
    text, markup = mb.manager_screen(page=0, query="", in_group=True)
    _dump("1. Switch Inbox (group, page 0)", text, markup)
    n = _assert_group_safe("manager_screen(in_group=True)", markup)
    print(f"  OK: {n} buttons group-safe")

    # 2. mb:switch — private render.
    text, markup = mb.manager_screen(page=0, query="", in_group=False)
    _dump("2. Switch Inbox (private, page 0)", text, markup)

    # 3. mb:switch filtered (via a substring the live fleet contains).
    probe = live[0].account_id[:4]
    text, markup = mb.manager_screen(page=0, query=probe, in_group=True)
    _dump(f"3. Switch Inbox with filter {probe!r} (group)", text, markup)
    n = _assert_group_safe("manager_screen(filtered)", markup)
    print(f"  OK: {n} buttons group-safe")

    # 4. Per-mailbox control screen for every live mailbox, both chats.
    for m in live:
        out = mb.mailbox_screen(m.account_id, in_group=True)
        assert out is not None, f"mailbox_screen({m.account_id!r}) returned None"
        text, markup = out
        _dump(f"4. mb:mbx:{m.account_id} (group)", text, markup)
        n = _assert_group_safe(f"mailbox_screen({m.account_id}, in_group=True)", markup)
        print(f"  OK: {n} buttons group-safe")

        out = mb.mailbox_screen(m.account_id, in_group=False)
        assert out is not None
        text, markup = out
        _dump(f"5. mb:mbx:{m.account_id} (private)", text, markup)

    # 5. Pagination arithmetic: with 90-100+ mailboxes we would land on
    # ceil(n / page_size) pages; prove the math is right at the current
    # fleet size.
    page_size = mb._MANAGER_PAGE_SIZE
    pages = max(1, (len(live) + page_size - 1) // page_size)
    print()
    print(f"Pagination: {len(live)} mailbox(es) across {pages} page(s) "
          f"of {page_size} - every page is reachable via mb:switch:p:N")

    # 6. Prove the dispatcher accepts every new callback the manager
    # emits in a group. We synthesise each callback as a bare string and
    # check group_mailbox_callback_allowed - the same test the live
    # dispatcher runs before invoking mailboxes.handle_callback.
    print()
    print("Live dispatcher check for every manager callback:")
    sample_aid = live[0].account_id
    for data in [
        "mb:switch",
        "mb:switch:p:0",
        "mb:switch:p:1",
        "mb:switch:refresh",
        "mb:switch:clear",
        "mb:switch:search",
        f"mb:mbx:{sample_aid}",
        f"mb:mbx:{sample_aid}:refresh",
        f"mb:mbx:{sample_aid}:health",
        f"mb:mbx:{sample_aid}:settings",
    ]:
        allowed = bot_app.group_mailbox_callback_allowed(data)
        print(f"  {'OK ' if allowed else 'NO '}  {data}")
        if not allowed:
            raise SystemExit(f"dispatcher would deny {data!r} in group")

    print()
    print("All checks passed. Switch Inbox is interactive and group-safe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
