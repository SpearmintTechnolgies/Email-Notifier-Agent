"""Cancel stale onboarding draft sessions for a specific admin.

Only operates on sessions that are NOT in a credentials-captured state.
Used to clear residue left by probe runs. No mailbox is touched.
"""
from __future__ import annotations

import sys
from mailops.config import load_dotenv

load_dotenv()
from mailops.db import db  # noqa: E402

ADMIN = int(sys.argv[1]) if len(sys.argv) > 1 else 8925736576

with db() as c:
    tables = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%onboarding%'"
    ).fetchall()]
    print("tables:", tables)
    for t in tables:
        cols = [col[1] for col in c.execute(f"PRAGMA table_info({t})").fetchall()]
        print(t, "cols:", cols)
        if "user_id" in cols and "state" in cols:
            rows = c.execute(
                f"SELECT rowid, state FROM {t} WHERE user_id = ?", (ADMIN,)
            ).fetchall()
            print(f"  rows for admin {ADMIN}: {rows}")
            # Only purge rows that are clearly pre-credentials. We refuse
            # to touch anything whose state hints at completed/armed.
            SAFE_STATES = {"draft", "pending", "awaiting_fields", "collecting", "collect", "fields"}
            purged = 0
            for rid, state in rows:
                if (state or "").lower() in SAFE_STATES or state is None:
                    c.execute(f"DELETE FROM {t} WHERE rowid = ?", (rid,))
                    purged += 1
            print(f"  purged {purged} draft rows")
    c.commit()
