"""One-shot live verification: post a fresh dashboard + a [TEST] digest.

Posts into the whitelisted group ``-5531260744`` so the user can tap each
of the four digest buttons (Open Inbox, View Critical, Select Mailbox,
Refresh Summary) and the group dashboard controls directly from
Telegram against the running Mailops polling bot. No real emails are
sent, no mailboxes are mutated, no digest row is marked delivered: this
script talks to the Telegram HTTP API from a separate short-lived Bot
client using the token from ``.env``.

Specifically this script:

* loads the LIVE Mailops configuration (same token the running bot
  polls with);
* reads the LIVE ops.db to compute current digest counts (production
  data, NOT simulated) WITHOUT marking anything delivered;
* posts ONE dashboard message to the group (so the user can tap every
  group control);
* posts ONE ``[TEST]`` digest message prefixed with the test tag so the
  group audience knows it is not a scheduled production digest;
* exits without touching settings.json, ops.db or any secret file.

Running the real digest scheduler is NOT invoked here (would mark rows
delivered and perturb the next scheduled window).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from telegram import Bot  # noqa: E402

from mailops import config  # noqa: E402
from mailops.bot import keyboards as kb, mailboxes as mb  # noqa: E402
from mailops.dashboard import collect_counts, format_dashboard  # noqa: E402
from mailops.digest import collect_window, digest_keyboard, format_digest  # noqa: E402


GROUP_ID = -5531260744


async def _post_dashboard(bot: Bot, counts) -> None:
    """Post the LIVE group dashboard (same screen the dispatcher renders)."""
    text = format_dashboard(counts, force_note="", group=True)
    markup = kb.group_dashboard_keyboard(
        counts,
        is_admin=True,
        can_settings=True,
        can_draft=True,
        can_send=True,
    )
    header = (
        "[Live verify] fresh Mailops group dashboard\n"
        "Running code: current on-disk mailops/* (restarted after today's fixes).\n"
        "Tap any control to verify dispatcher routing.\n\n"
    )
    msg = await bot.send_message(
        chat_id=GROUP_ID,
        text=(header + text)[:3900],
        reply_markup=markup,
    )
    print(f"[live-verify] dashboard posted  message_id={msg.message_id}")


async def _post_mailboxes(bot: Bot) -> None:
    """Post the group-safe Mailboxes list (same screen mb:m renders)."""
    text, markup = mb.main_screen(in_group=True)
    msg = await bot.send_message(
        chat_id=GROUP_ID,
        text=("[Live verify] group Mailboxes screen\n\n" + text)[:3900],
        reply_markup=markup,
    )
    print(f"[live-verify] mailboxes posted  message_id={msg.message_id}")


async def _post_digest(bot: Bot) -> None:
    """Post ONE [TEST] digest message using live production counts.

    Does NOT call build_and_send_digest (that one marks rows delivered
    and advances last_run_at). This path renders + sends only.

    Posts TWO variants so the user can tap buttons under both windows:

    1. The REAL current window (whatever production counts are) with the
       actual digest_keyboard rendered from live data.
    2. A canned "demo" window built in memory (not persisted) so all
       four digest buttons appear even when the real window is empty.
       Open Inbox on the demo keyboard points at a REAL mailbox id so
       the tap-through still routes to the real priority view.
    """
    window = collect_window()
    text = format_digest(window, test_tag="LIVE VERIFY after dispatcher fix")
    markup = digest_keyboard(window)
    msg = await bot.send_message(
        chat_id=GROUP_ID,
        text=text[:3900],
        reply_markup=markup,
    )
    print(
        f"[live-verify] [TEST] digest posted  message_id={msg.message_id}  "
        f"mailboxes={len(window.buckets)}  actionable={window.total_actionable}"
    )

    # --- Demo variant: build an in-memory window so the four-button
    # keyboard is populated even on an empty production window. We do
    # NOT enqueue these rows into digest_pending so production state is
    # untouched.
    from mailops.digest import DigestWindow, MailboxBucket
    accounts = config.load_accounts()
    if not accounts:
        return
    demo_buckets = [
        MailboxBucket(
            account_id=accounts[0].id,
            display_name=accounts[0].display_name or accounts[0].id,
            email=accounts[0].email,
            p0=1,
            p1=2,
            top_subjects=[(0, "demo: critical sample"), (1, "demo: important sample")],
        )
    ]
    demo = DigestWindow(
        buckets=demo_buckets,
        total_p0=1,
        total_p1=2,
        total_actionable=3,
        all_keys=[],
        window_started_at=0.0,
        window_ended_at=0.0,
    )
    demo_text = format_digest(demo, test_tag="LIVE VERIFY (demo keyboard)")
    demo_markup = digest_keyboard(demo)
    msg2 = await bot.send_message(
        chat_id=GROUP_ID,
        text=demo_text[:3900],
        reply_markup=demo_markup,
    )
    print(
        f"[live-verify] [TEST] demo digest posted  message_id={msg2.message_id}"
    )


async def main() -> None:
    r = config.load_runtime()
    bot = Bot(token=r.telegram_token)
    try:
        await bot.initialize()
        counts = collect_counts(force=False)
        await _post_dashboard(bot, counts)
        await _post_mailboxes(bot)
        await _post_digest(bot)
    finally:
        try:
            await bot.shutdown()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    asyncio.run(main())
