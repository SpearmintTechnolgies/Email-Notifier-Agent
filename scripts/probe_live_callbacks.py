"""Live callback-handler probe.

Drives the SAME ``app._on_callback`` function the Telegram framework calls,
with a fully-formed group update, for every button the admin can see.
Dumps the next screen each tap would render. Nothing is sent to Telegram
and no mailbox is touched.

Both production admins (8925736576, 5337250772) are exercised.
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


GROUP = -5531260744
USER_A = 8925736576
USER_B = 5337250772


class FakeMessage:
    def __init__(self):
        self.sent: list = []

    async def reply_text(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.edits: list = []
        self.answers: list = []
        self.message = FakeMessage()

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edits.append(("", reply_markup))


def _upd(data, user_id):
    q = FakeQuery(data)
    return SimpleNamespace(
        callback_query=q,
        effective_user=SimpleNamespace(id=user_id, is_bot=False, username=f"u{user_id}"),
        effective_chat=SimpleNamespace(id=GROUP, type="group"),
        effective_message=q.message,
    ), q


def _ctx():
    return SimpleNamespace(user_data={}, bot=None)


def _dump(markup) -> list[str]:
    if markup is None:
        return []
    out = []
    for row in markup.inline_keyboard:
        for b in row:
            label = (b.text or "")[:30]
            tgt = b.callback_data or f"url:{b.url}" if b.url else b.callback_data
            out.append(f"[{label!r}]->{tgt!r}")
    return out


async def _tap(data, user_id, label):
    upd, q = _upd(data, user_id)
    await app_mod.on_callback(upd, _ctx())
    denials = [t for t, alert in q.answers if alert and t]
    private = any("private" in (t or "").lower() for t in denials)
    unauth = any("unauth" in (t or "").lower() for t in denials)
    rendered = bool(q.edits or q.message.sent)
    status = "DENIED(PRIVATE)" if private else ("DENIED(UNAUTH)" if unauth else ("OK" if rendered else "SILENT"))
    print(f"  user={user_id} tap {data!r:28} -> {status}")
    if q.edits:
        text, markup = q.edits[-1]
        print(f"      screen title : {text.splitlines()[0] if text else '(no title)'}")
        for btn in _dump(markup)[:12]:
            print(f"      next button  : {btn}")
    elif q.message.sent:
        text, markup = q.message.sent[-1]
        print(f"      sent reply   : {text.splitlines()[0] if text else '(no title)'}")
        for btn in _dump(markup)[:12]:
            print(f"      next button  : {btn}")
    return q


async def main():
    print("=" * 72)
    print(f"LIVE callback-handler probe in group {GROUP}")
    print("=" * 72)

    for admin in (USER_A, USER_B):
        print(f"\n--- admin {admin} ---")
        await _tap("mb:m",            admin, "Mailboxes main")
        await _tap("mb:add",          admin, "Add email picker")
        await _tap("mb:add:hostinger", admin, "Hostinger start")
        await _tap("mb:add:siteground",admin, "SiteGround start")
        await _tap("mb:add:google",   admin, "Google / Gmail")
        await _tap("mb:add:custom",   admin, "Custom IMAP+SMTP start")
        await _tap("mb:switch",       admin, "Switch Inbox")
        await _tap("mb:qadd",         admin, "Quick Add Email")
        await _tap("mb:bulk",         admin, "Bulk CSV")
        # NOTE: ``mb:bulk:tmpl`` is intentionally skipped by the probe
        # because its handler calls ``context.bot.send_document(...)``,
        # which requires a real Telegram ``Bot`` on the context. The
        # production framework provides one; the fake context here does
        # not. The CSV-template path is exercised by the main regression
        # suite instead (tests/test_bulk_onboarding.py).
        await _tap("nav:home",        admin, "Back to dashboard")
        # Negative probe: ac:m MUST still be refused (defence in depth).
        q = await _tap("ac:m",        admin, "ac:m (must be denied)")
        private = any("private" in (t or "").lower() for t, _ in q.answers if t)
        assert private, "ac:m should still be denied in groups"


if __name__ == "__main__":
    asyncio.run(main())
