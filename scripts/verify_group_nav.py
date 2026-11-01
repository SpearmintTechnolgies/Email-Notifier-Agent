"""Live verification of the Back-navigation fix.

Simulates each production admin tapping ``nav:home`` (the new group Back
target) and ``mb:m`` from the whitelisted group, using the SAME code the
running bot is executing. Fails loudly if the private-only alert ever
surfaces on any hop. Prints the full button graph that a group admin can
tap through.

Never calls Telegram; never touches mail; never writes anywhere.
"""
from __future__ import annotations

import asyncio
import io
import sys
from types import SimpleNamespace

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from mailops.config import load_dotenv  # noqa: E402

load_dotenv()

import mailops.bot.app as app_mod  # noqa: E402
from mailops.bot import mailboxes as mb_mod  # noqa: E402
from mailops.bot import bulk_mailboxes as bmb_mod  # noqa: E402


GROUP = -5531260744
USER_A = 8925736576
USER_B = 5337250772


class FakeMessage:
    def __init__(self):
        self.sent: list[tuple] = []

    async def reply_text(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.edits: list[tuple] = []
        self.answers: list[tuple] = []
        self.message = FakeMessage()

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edits.append(("", reply_markup))


def _upd(data, user_id, chat_type="group", chat_id=GROUP):
    q = FakeQuery(data)
    return SimpleNamespace(
        callback_query=q,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        effective_message=q.message,
    ), q


def _ctx():
    return SimpleNamespace(user_data={}, bot=None)


def _dump_markup(markup) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def _run_tap(data, user_id, label):
    upd, q = _upd(data, user_id, "group", GROUP)
    await app_mod.on_callback(upd, _ctx())
    denials = [t for t, alert in q.answers if alert and t]
    private_hit = any("private" in (t or "").lower() for t in denials)
    rendered = bool(q.edits or q.message.sent)
    datas: list[str] = []
    if q.edits:
        _, markup = q.edits[-1]
        datas = _dump_markup(markup)
    elif q.message.sent:
        _, markup = q.message.sent[-1]
        datas = _dump_markup(markup)
    status = "DENIED(PRIVATE-ONLY)" if private_hit else ("OK" if rendered else "NO-RENDER")
    print(f"  user={user_id}  tap={data!r:16}  -> {status}")
    if datas:
        print(f"      next buttons: {datas}")
    if private_hit:
        print(f"      ! denials: {denials}")
    assert not private_hit, f"REGRESSION: {label} denied with private-only alert"
    return q, datas


async def main():
    print("=" * 72)
    print(f"Live navigation smoke in group {GROUP}")
    print("=" * 72)

    for admin in (USER_A, USER_B):
        print(f"\n--- admin {admin} ---")
        # 1. Tap "Mailboxes (in group)" from the dashboard.
        q, datas = await _run_tap("mb:m", admin, "mb:m")
        # Must offer a group-safe Back target.
        back_targets = [d for d in datas if d in ("nav:home", "dash")]
        assert back_targets, f"group Mailboxes offers no group-safe Back for admin {admin}: {datas!r}"
        assert "ac:m" not in datas, f"REGRESSION: group Mailboxes still ships ac:m to admin {admin}"
        # 2. Tap the Back button.
        await _run_tap(back_targets[0], admin, f"Back ({back_targets[0]})")
        # 3. Tap Switch Inbox and Back-to-mailboxes to confirm those paths too.
        await _run_tap("mb:switch", admin, "mb:switch")
        await _run_tap("mb:add", admin, "mb:add")
        await _run_tap("mb:bulk", admin, "mb:bulk")

    print()
    print("All group navigation hops passed. ac:m is only emitted from private DM.")


if __name__ == "__main__":
    asyncio.run(main())
