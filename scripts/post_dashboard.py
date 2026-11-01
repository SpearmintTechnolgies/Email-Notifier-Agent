"""One-shot: post a fresh Mailboxes dashboard into the whitelisted group.

Uses the already-running Mailops config (same token, same identity) so the
message is sent BY the Mailops bot and renders the usual inline keyboard.
Never writes to disk, never sends mail, never touches credentials.
"""
from __future__ import annotations

import asyncio

from telegram import Bot

from mailops import config
from mailops.bot import mailboxes as mb


GROUP_ID = -5531260744


async def main() -> None:
    r = config.load_runtime()
    bot = Bot(token=r.telegram_token)
    text, markup = mb.main_screen(in_group=True)
    header = (
        "Mailops ONLINE after controlled restart.\n"
        "Bulk CSV upload is now APPROVED for this group (admin-only).\n"
        "Fresh dashboard below — all buttons are live.\n\n"
    )
    body = (header + text)[:3900]
    msg = await bot.send_message(
        chat_id=GROUP_ID,
        text=body,
        reply_markup=markup,
    )
    print("posted message_id=", msg.message_id, "to chat=", msg.chat_id)


if __name__ == "__main__":
    asyncio.run(main())
