"""Render the dashboard exactly as the live bot would, right now.

Read-only probe: loads the real account registry and the REAL persisted
snapshot (read-only SQLite connection), then renders the group dashboard
text + keyboard. No IMAP, no Telegram, no writes of any kind.

This answers the only question that matters mid-incident: "if I typed
/dashboard this second, what would come back?"
"""
from __future__ import annotations

import os
import tempfile
import time

os.environ["MAILOPS_LOG_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="dash-render-"), "probe.log"
)

from mailops import cache as cache_mod  # noqa: E402
from mailops.bot import keyboards as kb  # noqa: E402
from mailops.config import load_accounts  # noqa: E402
from mailops.dashboard import format_dashboard  # noqa: E402


def main() -> int:
    accounts = load_accounts()
    print(f"accounts registered : {len(accounts)}")

    hydrated = cache_mod.hydrate()
    counts, cold = cache_mod.counts_now()
    print(f"snapshot on disk    : {'yes' if hydrated else 'no'}")
    print(f"counts available    : {len(counts)}")
    print(f"cold                : {cold}")
    age = cache_mod.age_seconds()
    print(f"snapshot age        : {'n/a' if age is None else f'{age:.0f}s'}")

    markup = kb.group_dashboard_keyboard(counts, is_admin=True)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    print(f"\nkeyboard rows       : {len(markup.inline_keyboard)}")
    print(f"controls            : {labels}")

    print("\n--- dashboard text the group would receive ---")
    print(format_dashboard(counts, group=True)[:1400])

    total = sum(c.p0 + c.p1 for c in counts)
    print(f"\nactionable (P0+P1)  : {total}")
    ok = bool(counts) and len(markup.inline_keyboard) >= 3
    print("RESULT:", "dashboard would render WITH controls" if ok else "NOT READY")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
