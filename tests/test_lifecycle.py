"""Regression: the sync-loop background task was created in post_init but
never cancelled/awaited on shutdown, so PTB closed the event loop with it
still pending -> 'Task was destroyed but it is pending!'. Also guards the
cache refresh single-pass rewrite."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import mailops.main as main_mod
from mailops.dashboard import collect_bucket, collect_counts
from mailops.db import init_db


def _run(coro):
    return asyncio.run(coro)


def test_post_shutdown_cancels_and_awaits_sync_task(tmp_profile):
    """A pending sync task must be cancelled and awaited, not destroyed."""
    init_db()

    async def scenario():
        started = asyncio.Event()

        async def fake_sync(app):
            started.set()
            await asyncio.sleep(3600)  # long sleep, like the real loop

        app = SimpleNamespace(bot_data={})
        task = asyncio.create_task(fake_sync(app), name=main_mod._SYNC_TASK_NAME)
        app.bot_data[main_mod._SYNC_TASK_NAME] = task
        await started.wait()

        await main_mod.post_shutdown(app)
        # Task finished (cancelled) and was awaited — nothing left pending.
        assert task.done()
        assert task.cancelled()

    _run(scenario())


def test_post_shutdown_handles_missing_task(tmp_profile):
    app = SimpleNamespace(bot_data={})
    _run(main_mod.post_shutdown(app))  # must not raise


def test_post_shutdown_swallows_task_exception(tmp_profile):
    async def scenario():
        async def boom(app):
            raise RuntimeError("sync exploded")

        app = SimpleNamespace(bot_data={})
        task = asyncio.create_task(boom(app), name=main_mod._SYNC_TASK_NAME)
        app.bot_data[main_mod._SYNC_TASK_NAME] = task
        await asyncio.sleep(0)  # let it run and fail
        await main_mod.post_shutdown(app)  # must not re-raise
        assert task.done()

    _run(scenario())


def test_sync_loop_survives_recoverable_error(tmp_profile, monkeypatch):
    """A sync error must be logged and the loop must continue, not terminate.
    Drives sync_loop with stubbed dependencies so no real bot/IMAP runs."""
    init_db()
    calls = {"sync": 0}

    async def flaky_sync_once(notify=None):
        calls["sync"] += 1
        if calls["sync"] == 1:
            raise RuntimeError("imap down")
        return []

    async def noop(*a, **k):
        return 0

    # sync_loop imports these lazily from their modules each iteration.
    monkeypatch.setattr("mailops.sync_worker.sync_once", flaky_sync_once)
    monkeypatch.setattr("mailops.sync_worker.retry_pending_notifications", noop)
    monkeypatch.setattr("mailops.notify.flush_p2_batch", noop)
    monkeypatch.setattr("mailops.notify.send_dashboard_digest", noop)
    monkeypatch.setattr("mailops.notify.make_notifier", lambda bot: None)
    monkeypatch.setattr("mailops.cache.refresh_all", lambda: {"accounts": 0})
    # Shorten the inter-check sleep so the test runs fast.
    from mailops.config import load_settings, save_settings

    s = load_settings()
    s["sync_interval_seconds"] = 60  # loop floor is 60; we cancel before it matters
    save_settings(s)

    async def scenario():
        app = SimpleNamespace(bot=None)
        task = asyncio.create_task(main_mod.sync_loop(app), name="t")
        # Let the first iteration hit the error, then cancel like shutdown.
        await asyncio.sleep(3.2)
        # The recoverable error was logged but the loop is STILL ALIVE.
        assert not task.done()
        assert calls["sync"] == 1  # errored once, now sleeping before retry
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return task

    task = _run(scenario())
    assert task.cancelled()


def test_refresh_all_single_pass_consistent_counts_and_buckets(tmp_profile, fake_him):
    """The single-pass refresh must produce counts and buckets that agree with
    each other and with the per-bucket fetch path."""
    init_db()
    fake_him.add_message("sales", "INBOX.P0-Critical", "1", subject="crit", sender="a@b.co")
    fake_him.add_message("sales", "INBOX.P1-Important", "2", subject="imp", sender="c@d.co")
    fake_him.add_message("sales", "INBOX.P1-Important", "3", subject="imp2", sender="e@f.co")
    fake_him.add_message("info", "INBOX.P2-Normal", "9", subject="norm", sender="g@h.co")

    counts = {c.account_id: c for c in collect_counts(force=True)}
    assert (counts["sales"].p0, counts["sales"].p1, counts["sales"].p2) == (1, 2, 0)
    assert (counts["info"].p0, counts["info"].p1, counts["info"].p2) == (0, 0, 1)

    # Buckets derived from the same pass match.
    sales_p1 = collect_bucket(1, "sales")
    assert {i["msg_id"] for i in sales_p1} == {"2", "3"}
    all_p1 = collect_bucket(1, None)
    assert {i["msg_id"] for i in all_p1} == {"2", "3"}
    info_p2 = collect_bucket(2, "info")
    assert [i["msg_id"] for i in info_p2] == ["9"]
