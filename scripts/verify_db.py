from __future__ import annotations

from mailops.db import db, init_db


def main() -> None:
    init_db()
    with db() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = [r[0] for r in rows]
        print("tables:", names)
        for t in ("bulk_jobs", "bulk_rows"):
            if t in names:
                cols = conn.execute(f"PRAGMA table_info({t})").fetchall()
                print(f"{t} columns:", [c[1] for c in cols])
                n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                print(f"{t} row count: {n}")
            else:
                print(f"{t}: MISSING")


if __name__ == "__main__":
    main()
