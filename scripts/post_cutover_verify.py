"""One-shot post-cutover verification message into the whitelisted group.

Proves, end to end on the AWS host: the bot token works, outbound Telegram
send works, and the real inline keyboard renders (so callbacks are tappable).

Sends exactly ONE message. Never sends email, never writes to disk, never
touches credentials.
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
        "Mailops has been migrated to AWS and is now LIVE.\n"
        "Host: dm-mini (Amazon Linux 2023) | service: mailops.service\n"
        "36 of 39 mailboxes are authenticating.\n"
        "Needs a password reset at the provider: altcoininformer, alien, marketing.\n"
        "Fresh dashboard below - please tap a button to confirm callbacks.\n\n"
    )
    body = (header + text)[:3900]
    msg = await bot.send_message(chat_id=GROUP_ID, text=body, reply_markup=markup)
    kb = msg.reply_markup.inline_keyboard if msg.reply_markup else []
    print("posted message_id =", msg.message_id, "chat =", msg.chat_id)
    print("inline rows       =", len(kb))
    print("inline buttons    =", sum(len(row) for row in kb))
    print("sample callbacks  =", [b.callback_data for row in kb[:3] for b in row][:6])


if __name__ == "__main__":
    asyncio.run(main())
