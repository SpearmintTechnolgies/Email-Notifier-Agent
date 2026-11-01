"""Session-end isolation: tests must not mutate live credentials, KBs, or the live log."""
from __future__ import annotations

import os
from pathlib import Path

from tests.conftest import LIVE_LOG, PROTECTED_AT_START, TEST_LOG_DIR, snapshot_protected

MARKER = "UNIQUE-MAILOPS-TEST-LOG-ISOLATION-7e2a9c"


def test_protected_live_files_were_not_rewritten_by_tests():
    now = snapshot_protected()
    for path, before in PROTECTED_AT_START.items():
        assert path in now, f"protected file disappeared: {path}"
        if Path(path).name == "settings.json":
            # Hash-only: the live agent may save the file; tests must not change contents.
            assert now[path] == before, f"tests changed live settings.json ({path})"
        else:
            assert now[path] == before, f"tests touched {path}"


def test_session_log_is_not_the_live_log():
    target = Path(os.environ["MAILOPS_LOG_FILE"]).resolve()
    assert target.parent == TEST_LOG_DIR.resolve()
    assert target != LIVE_LOG.resolve()


def test_simulated_log_marker_never_lands_in_live_mailops_log():
    import logging

    logging.getLogger("mailops").warning(MARKER)
    live = LIVE_LOG.read_text(encoding="utf-8", errors="replace") if LIVE_LOG.exists() else ""
    assert MARKER not in live


def test_live_ops_db_cache_snapshot_was_not_written_by_tests():
    """Tests must not write the dashboard count snapshot to the live db.

    Added after a real leak: ``mailops.cache.save_counts`` persists counts
    through ``digest_state``, and tests that call ``refresh_all`` against the
    real account registry with an empty mail double wrote an all-zeros
    39-mailbox snapshot into the LIVE ops.db. The next restart restored it as
    though those zeros were real.

    Deliberately scoped to the snapshot row rather than hashing the whole
    file: the live agent may legitimately be running during a test session
    and writing its own rows, so a file hash would be flaky. What must never
    change is the value this session is capable of writing.
    """
    from tests.conftest import LIVE_CACHE_SNAPSHOT_AT_START, read_live_cache_snapshot

    assert read_live_cache_snapshot() == LIVE_CACHE_SNAPSHOT_AT_START, (
        "tests changed the dashboard count snapshot in the live data/ops.db"
    )


def test_cache_snapshot_is_never_persisted_to_the_live_db(tmp_profile):
    """``save_counts`` must refuse the live ops.db under pytest."""
    import mailops.db as db_mod
    import mailops.paths as paths_mod
    from mailops import cache as cache_mod
    from tests.conftest import read_live_cache_snapshot

    before = read_live_cache_snapshot()
    # Point the db module at the real file, as a leaking test would.
    original = db_mod.OPS_DB
    db_mod.OPS_DB = paths_mod.OPS_DB
    cache_mod._counts = [
        cache_mod.AccountCounts(
            account_id="probe", email="p@e.com", p0=1, p1=2, p2=3, p3=4
        )
    ]
    cache_mod._updated_at = 1.0
    try:
        cache_mod.save_counts()  # must be a no-op
    finally:
        db_mod.OPS_DB = original
        cache_mod._counts = []
        cache_mod._updated_at = 0.0
    after = read_live_cache_snapshot()
    assert after == before, "save_counts wrote to the live ops.db from a test"
    assert after is None or "probe" not in after, "probe counts leaked to production"
