"""Live UI snapshot — renders every screen the admin can reach via the
fresh dashboard, using the SAME module code the running bot is executing.
This script never calls Telegram; it only verifies the buttons + text so
we can be sure the deploy is correct before any admin tap.
"""
from __future__ import annotations

import io
import os
import sys

# Force UTF-8 output so the warning glyph ⚠ in the Gmail screen does not
# trip cp1252 on Windows consoles.
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Force the real production env (same .env the running bot read).
from mailops.config import load_dotenv  # noqa: E402

load_dotenv()  # mirror what mailops.main does at startup

from mailops import config  # noqa: E402
from mailops.bot import mailboxes as mb, app as bot_app  # noqa: E402
from mailops.bulk import csv_parse, service  # noqa: E402

GROUP_ID = -5531260744


def dump_screen(title: str, text: str, markup) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)
    print(text)
    print()
    print("--- buttons ---")
    for row in markup.inline_keyboard:
        for b in row:
            print(f"  [{b.text!r}] -> {b.callback_data!r}")


def main() -> None:
    # 1. Main Mailboxes dashboard in the whitelisted group.
    text, markup = mb.main_screen(in_group=True)
    dump_screen("1. mb.main_screen(in_group=True)", text, markup)

    # 2. Provider pick (Add email).
    text, markup = mb.add_start_screen()
    dump_screen("2. mb.add_start_screen() — provider pick", text, markup)

    # 3. Google / Gmail screen (should refuse password input).
    text, markup = mb._google_provider_screen()
    dump_screen("3. mb._google_provider_screen() — Gmail is OAuth-only", text, markup)

    # 4. Switch Inbox screen.
    text, markup = mb.switch_screen(in_group=True)
    dump_screen("4. mb.switch_screen(in_group=True)", text, markup)

    # 5. Bulk Add entry (same render the bot gives the admin after tapping Bulk Add).
    from mailops.bot import bulk_mailboxes as bulk_mb
    from types import SimpleNamespace

    upd = SimpleNamespace(
        effective_user=SimpleNamespace(id=8925736576),
        effective_chat=SimpleNamespace(id=GROUP_ID, type="group"),
        effective_message=None,
        callback_query=None,
    )
    text, markup = bulk_mb._entry_screen(upd)
    dump_screen("5. bulk_mb._entry_screen(in_group=True) — Bulk CSV", text, markup)

    # 6. Group-upload gate (what service.group_upload_allowed returns right now).
    ok, reason = service.group_upload_allowed(GROUP_ID)
    print()
    print("=" * 72)
    print("6. service.group_upload_allowed(-5531260744)")
    print("=" * 72)
    print(f"  allowed = {ok!r}")
    print(f"  reason  = {reason!r}")

    # 7. Confirm the group callback whitelist includes the new entries.
    print()
    print("=" * 72)
    print("7. app.GROUP_MB_EXACT / GROUP_MB_PREFIXES (group callback whitelist)")
    print("=" * 72)
    for k in sorted(bot_app.GROUP_MB_EXACT):
        print(f"  EXACT  {k}")
    for k in bot_app.GROUP_MB_PREFIXES:
        print(f"  PREFIX {k}")

    # 8. CSV template bytes — proves "Download CSV template" has content.
    body = csv_parse.render_template()
    print()
    print("=" * 72)
    print("8. csv_parse.render_template() — Bulk Add CSV template")
    print("=" * 72)
    print(f"  template bytes = {len(body)}")
    print(f"  first line     = {body.splitlines()[0].decode('utf-8', 'replace')!r}")

    # 9. Env sanity (live).
    print()
    print("=" * 72)
    print("9. Live env (bulk-upload tunables)")
    print("=" * 72)
    for k in (
        "MAILOPS_BULK_ALLOW_GROUP_UPLOAD",
        "MAILOPS_BULK_ALLOWED_GROUP",
        "MAILOPS_BULK_MAX_ROWS",
        "MAILOPS_BULK_MAX_FILE_BYTES",
        "MAILOPS_BULK_MAX_LINE_BYTES",
        "MAILOPS_BULK_WORKERS",
        "MAILOPS_BULK_PER_HOST",
        "MAILOPS_BULK_HOST_RATE_PER_SEC",
        "MAILOPS_BULK_DEFAULT_ACTIVATION",
        "MAILOPS_TEST_MODE",
    ):
        print(f"  {k} = {os.environ.get(k)!r}")

    r = config.load_runtime()
    print()
    print(f"  runtime.home_groups        = {r.home_groups}")
    print(f"  runtime.allowed_users      = {r.allowed_users}")
    print(f"  runtime.openai_base_url    = {r.openai_base_url}")
    print(f"  runtime.model              = {r.model}")


if __name__ == "__main__":
    sys.exit(main())
