"""Controlled, rate-limited health check against the REAL registered
mailboxes, using the exact production code path (mailops.health +
mailops.himalaya_client). Read-only: lists 1 envelope from INBOX per
account. Never resets passwords, never retries aggressively (the
per-account cooldown in mailops.health still applies on repeat runs).
Never prints secrets.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mailops.db import init_db  # noqa: E402
from mailops.health import run_manual_health_check  # noqa: E402
from mailops.config import load_accounts  # noqa: E402

init_db()
accounts = load_accounts()
print(f"Registered active accounts: {[a.id for a in accounts]}")
results = run_manual_health_check([a.id for a in accounts])
for r in results:
    status = "RATE-LIMITED" if r.rate_limited else ("OK" if r.ok else f"FAIL ({r.category})")
    print(f"{r.account_id}: {status}" + (f" - {r.detail}" if r.detail else ""))
