"""Read-only inspection of the persisted dashboard snapshot."""
import json
import sqlite3
import sys
import time

db = sys.argv[1] if len(sys.argv) > 1 else "data/ops.db"
conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
row = conn.execute(
    "SELECT value, updated_at FROM digest_state WHERE key='cache_counts_v1'"
).fetchone()
if row is None:
    print("NO SNAPSHOT PERSISTED")
    raise SystemExit(1)

blob = json.loads(row["value"])
rows = blob["counts"]
age = time.time() - float(blob.get("updated_at") or 0)
print(f"mailboxes in snapshot : {len(rows)}")
print(f"snapshot age          : {age:.0f}s")
print(f"total P0 / P1         : {sum(r['p0'] for r in rows)} / {sum(r['p1'] for r in rows)}")
nonzero = [r for r in rows if r["p0"] or r["p1"] or r["p2"] or r["p3"]]
print(f"mailboxes with mail   : {len(nonzero)}")
for r in nonzero[:8]:
    print(
        f"   {r['account_id']:<22} p0={r['p0']} p1={r['p1']} p2={r['p2']} p3={r['p3']}"
    )
