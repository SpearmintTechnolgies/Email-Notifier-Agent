"""Restart + retry semantics for the digest redesign.

Pre-October-2026 Mailops queued one row per undelivered P0/P1 alert in
``pending_notifications`` and retried individual Telegram sends on a
timer. That was precisely the architecture that produced the 12-13
individual Alien FI alerts and would not scale to 90-100 mailboxes, so
the redesign replaced it with a 15-minute consolidated digest.

The invariants this file protects are the same as before (every new
actionable email must survive a crash and must never fire twice), but
the mechanism is now the ``digest_pending`` + ``digest_state`` tables:

* on each sync pass, ``process_account`` writes one ``digest_pending``
  row per actionable discovery (``seeded=1`` on a mailbox's first sync,
  ``seeded=0`` thereafter);
* the sync loop calls ``build_and_send_digest`` which marks the covered
  rows ``delivered_at`` only AFTER Telegram confirms, so a crash between
  discovery and send leaves the row intact for the next tick;
* legacy ``pending_notifications`` rows written by an older process
  version are migrated into the digest table by
  ``retry_pending_notifications`` without ever producing a per-email
  Telegram message, so a bot restart cannot replay the pre-redesign
  flood.
"""
from __future__ import annotations

import asyncio
import json

from mailops.db import (
    enqueue_digest_pending,
    init_db,
    is_message_processed,
    list_pending_notifications,
    list_undelivered_digest_rows,
    mark_digest_delivered,
    queue_notification,
)
from mailops.sync_worker import process_account, retry_pending_notifications


def _run(coro):
    return asyncio.run(coro)


def _collector(events):
    async def notify(ev):
        events.append(ev)

    return notify


def _disable_first_sync_silence():
    from mailops.config import load_settings, save_settings

    s = load_settings()
    s["digest_initial_sync_silent"] = False
    save_settings(s)


def test_actionable_mail_enqueues_digest_row_even_when_notify_fails(
    tmp_profile, fake_him
):
    """A Telegram handoff failure must not block classification, moves, or
    the digest row that lets the next 15-minute tick retry."""
    init_db()
    _disable_first_sync_silence()
    fake_him.add_message(
        "sales", "INBOX", "a1", subject="security alert breach", sender="a@b.co"
    )

    async def bad_notify(ev):
        raise RuntimeError("telegram down")

    _run(process_account(fake_him, "sales", "sales@example.com", bad_notify))

    # Message classified + moved + processed exactly once.
    assert is_message_processed("sales", "a1")
    assert fake_him.envelopes("sales", "INBOX") == []
    assert len(fake_him.envelopes("sales", "INBOX.P0-Critical")) == 1

    # The digest table (not pending_notifications) is the restart-recovery
    # source now. Legacy table is quiet.
    rows = list_undelivered_digest_rows(max_priority=1)
    assert [r["msg_id"] for r in rows] == ["a1"]
    assert list_pending_notifications() == []

    # Next sync must not duplicate or re-move anything.
    _run(process_account(fake_him, "sales", "sales@example.com", bad_notify))
    rows2 = list_undelivered_digest_rows(max_priority=1)
    assert [r["msg_id"] for r in rows2] == ["a1"]


def test_digest_send_marks_rows_delivered(tmp_profile, fake_him):
    """A successful digest send flips every covered row to delivered so
    the same mail is never counted in two consecutive digests."""
    from mailops.digest import build_and_send_digest

    init_db()
    _disable_first_sync_silence()
    fake_him.add_message(
        "sales", "INBOX", "b2", subject="how much to advertise", sender="c@d.co"
    )
    _run(process_account(fake_him, "sales", "sales@example.com", None))

    class _OkBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id=None, text=None, reply_markup=None):
            self.sent.append((chat_id, text))

    # Point the bot at a real home target so send_to_home has somewhere
    # to deliver (the fixture profile seeds TELEGRAM_ALLOWED_USERS=111).
    bot = _OkBot()
    diag = _run(build_and_send_digest(bot, force=True, reason="test"))
    assert diag["sent"] is True
    assert diag["actionable"] == 1
    assert diag["covered_keys"] == 1
    assert list_undelivered_digest_rows(max_priority=1) == []


def test_legacy_pending_notifications_are_migrated_not_resent(tmp_profile, fake_him):
    """A row written by the pre-redesign Mailops survives a restart but
    must NEVER fire an individual Telegram message. It is instead folded
    into the next digest window.
    """
    init_db()
    payload = {
        "type": "new_mail",
        "key": "sales:INBOX.P0-Critical:zz9",
        "account_id": "sales",
        "email": "sales@example.com",
        "msg_id": "zz9",
        "mailbox": "INBOX.P0-Critical",
        "subject": "queued before crash",
        "sender": "x@y.co",
        "priority": 0,
        "tags": [],
        "needs_reply": False,
        "reason": "test",
    }
    queue_notification(payload["key"], "sales", "INBOX.P0-Critical", "zz9", payload)
    assert len(list_pending_notifications()) == 1

    events = []
    migrated = _run(retry_pending_notifications(_collector(events)))
    # Legacy row drained but zero per-mail Telegram sends.
    assert migrated == 1
    assert events == []
    assert list_pending_notifications() == []
    rows = list_undelivered_digest_rows(max_priority=1)
    assert [r["msg_id"] for r in rows] == ["zz9"]
    assert rows[0]["seeded"] == 0


def test_enqueue_digest_pending_is_idempotent(tmp_profile):
    """Re-syncing the same message must not create duplicate digest rows."""
    init_db()
    assert enqueue_digest_pending(
        "sales", "INBOX.P0-Critical", "e5", 0, subject="x", sender="y"
    ) is True
    assert enqueue_digest_pending(
        "sales", "INBOX.P0-Critical", "e5", 0, subject="x", sender="y"
    ) is False
    assert len(list_undelivered_digest_rows(max_priority=1)) == 1


def test_mark_digest_delivered_is_idempotent(tmp_profile):
    """A retry that re-asks for delivery confirmation on already-delivered
    rows must be a no-op (second-mark returns 0)."""
    init_db()
    enqueue_digest_pending("sales", "INBOX.P0-Critical", "zz1", 0)
    key = "sales:INBOX.P0-Critical:zz1"
    first = mark_digest_delivered([key])
    second = mark_digest_delivered([key])
    assert first == 1
    assert second == 0
    assert list_undelivered_digest_rows(max_priority=1) == []


def test_retry_with_none_notifier_still_migrates(tmp_profile, fake_him):
    """A CLI one-shot (no notifier wired) must still drain legacy rows into
    the digest table without raising."""
    init_db()
    payload = {"type": "new_mail", "key": "sales:INBOX.P0-Critical:f6", "msg_id": "f6",
               "mailbox": "INBOX.P0-Critical", "account_id": "sales", "priority": 0,
               "subject": "x", "sender": "y"}
    queue_notification(payload["key"], "sales", "INBOX.P0-Critical", "f6", payload)
    assert _run(retry_pending_notifications(None)) == 1
    assert list_pending_notifications() == []
    assert len(list_undelivered_digest_rows(max_priority=1)) == 1
