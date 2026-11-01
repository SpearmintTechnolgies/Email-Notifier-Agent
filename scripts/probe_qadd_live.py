"""Live Quick Add verification.

Simulates the /qadd command fired from group -5531260744 by admin
8925736576 with the exact body the user tried to paste earlier. We
drive mailops.bot.mailboxes.handle_qadd_command the same way PTB does,
and dump the resulting confirmation-card screen plus the OnboardingSession
state. NO mailbox is registered: the flow stops at Review.
"""
from __future__ import annotations

import asyncio
import io
import sys
from types import SimpleNamespace

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from mailops.config import load_dotenv  # noqa: E402

load_dotenv()

from mailops.bot import mailboxes as mb  # noqa: E402
from mailops import onboarding_session as os_mod  # noqa: E402

GROUP = -5531260744
ADMIN = 8925736576

PASTE = """/qadd
Email: info@alien.fi
Name: Alien FI
IMAP: gcam1108.siteground.biz
IMAP Port: 993
SMTP: gcam1108.siteground.biz
SMTP Port: 465"""


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies: list = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


def _dump(markup) -> list[str]:
    if markup is None:
        return []
    out = []
    for row in markup.inline_keyboard:
        for b in row:
            tgt = b.callback_data or (f"url:{b.url}" if b.url else "")
            out.append(f"[{(b.text or '')[:35]!r}]->{tgt!r}")
    return out


async def main():
    msg = FakeMessage(PASTE)
    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=ADMIN, is_bot=False, username="admin"),
        effective_chat=SimpleNamespace(id=GROUP, type="group"),
        effective_message=msg,
        callback_query=None,
    )
    ctx = SimpleNamespace(user_data={}, bot=None)

    # Clear any stale slot from the earlier probe runs.
    try:
        from mailops.bot.mailboxes import _clear_awaiting
        _clear_awaiting(ADMIN)
    except Exception:
        pass

    print(f"-> /qadd fired by user={ADMIN} in group={GROUP}")
    await mb.handle_qadd_command(upd, ctx)

    print(f"\nreplies emitted: {len(msg.replies)}")
    for i, (text, markup) in enumerate(msg.replies, 1):
        head = text.splitlines()[0] if text else "(empty)"
        print(f"\n--- reply #{i} ---")
        print(f"  title: {head}")
        print(f"  body preview:")
        for line in text.splitlines()[:14]:
            print(f"    {line}")
        for btn in _dump(markup)[:12]:
            print(f"  button: {btn}")

    # Confirm an onboarding session was opened and the inbox is NOT yet
    # armed (confirmation card stage, as required).
    try:
        sessions = os_mod.list_user_sessions(ADMIN)
    except AttributeError:
        try:
            from mailops.db import db
            with db() as c:
                rows = c.execute(
                    "SELECT token, state, account_id FROM onboarding_sessions "
                    "WHERE user_id = ? ORDER BY created_at DESC LIMIT 3",
                    (ADMIN,),
                ).fetchall()
            sessions = [SimpleNamespace(token=r[0], state=r[1], account_id=r[2]) for r in rows]
        except Exception as e:
            sessions = []
            print(f"\n(cannot read sessions directly: {e})")

    print(f"\nrecent onboarding sessions for admin {ADMIN}:")
    for s in sessions[:3]:
        print(f"  token={getattr(s,'token','?')[:12]}... state={getattr(s,'state','?')} account_id={getattr(s,'account_id','?')}")


if __name__ == "__main__":
    asyncio.run(main())
