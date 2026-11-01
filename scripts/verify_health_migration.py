"""One-shot verification: apply the new sync_incidents schema to the live
ops.db and confirm it exists and is empty (no incidents yet). Read-only
otherwise — never touches mailbox credentials or sends anything."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mailops.db import init_db  # noqa: E402

init_db()
conn = sqlite3.connect("data/ops.db")
tables = [
    r[0]
    for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sync_incidents'"
    )
]
print("sync_incidents table present:", bool(tables))
count = conn.execute("SELECT COUNT(*) FROM sync_incidents").fetchone()[0]
print("sync_incidents row count:", count)
conn.close()
