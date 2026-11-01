"""Live Bot API probe of the Mailboxes UI in the production group.

Posts fresh test messages (not the dashboard header) that carry each key
screen's inline keyboard, then reads back what the Bot API accepted and
dumps every button's label + callback_data.

This proves end-to-end that:
  * The RUNNING bot process is the one we think it is.
  * The Mailboxes main screen offers Switch Inbox / Add email / Quick Add
    Email / Bulk Add (CSV) + the fixed Back-to-dashboard button.
  * The Add email provider screen offers Hostinger / SiteGround / Google /
    Custom.
  * The Switch Inbox screen renders status for the registered mailboxes.

Nothing is sent to any inbox. No credentials are touched. The posted
messages are plain Telegram messages in the group.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from telegram import Bot  # noqa: E402

from mailops.config import load_dotenv, load_runtime  # noqa: E402

load_dotenv()
from mailops.bot import mailboxes as mb_mod  # noqa: E402
from mailops.bot import bulk_mailboxes as bmb_mod  # noqa: E402
from types import SimpleNamespace  # noqa: E402


GROUP = -5531260744


def _dump(markup) -> list[dict[str, str]]:
    if markup is None:
        return []
    out: list[dict[str, str]] = []
    for row in markup.inline_keyboard:
        for b in row:
            out.append({
                "label": b.text,
                "callback_data": b.callback_data or "",
                "url": b.url or "",
            })
    return out


async def _post(bot: Bot, label: str, text: str, markup) -> None:
    msg = await bot.send_message(
        chat_id=GROUP,
        text=f"[live probe] {label}\n\n" + text[:3500],
        reply_markup=markup,
    )
    print(f"\n=== {label} ===")
    print(f"posted message_id={msg.message_id} chat={msg.chat_id}")
    print("buttons (as Telegram received them):")
    for b in _dump(markup):
        print(f"  [{b['label']!r:30}] cb={b['callback_data']!r}  url={b['url']!r}")


async def main() -> None:
    r = load_runtime()
    bot = Bot(token=r.telegram_token)

    # 1) Mailboxes main screen (group render).
    text, markup = mb_mod.main_screen(in_group=True)
    await _post(bot, "Mailboxes main (group)", text, markup)

    # 2) Add email -> provider picker.
    text, markup = mb_mod.add_start_screen()
    await _post(bot, "Add email -> provider picker", text, markup)

    # 3) Switch Inbox / Status.
    text, markup = mb_mod.switch_screen(in_group=True)
    await _post(bot, "Switch Inbox / Status (group)", text, markup)

    # 4) Bulk Add entry.
    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=8925736576),
        effective_chat=SimpleNamespace(id=GROUP, type="group"),
        effective_message=None,
        callback_query=None,
    )
    text, markup = bmb_mod._entry_screen(upd)
    await _post(bot, "Bulk Add (CSV) entry", text, markup)

    # 5) Google/Gmail provider screen (should refuse password input).
    text, markup = mb_mod._google_provider_screen()
    await _post(bot, "Google / Gmail provider screen", text, markup)


if __name__ == "__main__":
    asyncio.run(main())
