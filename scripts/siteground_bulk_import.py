"""One-off orchestration for the SiteGround.xlsx bulk mailbox import.

NOT part of the Mailops package; NOT committed. Uses ONLY the existing
mailops.bulk.{csv_parse,presets,jobs,service,validator} backend - this file
does not implement its own IMAP/SMTP/registration logic.

Usage:
    .venv\\Scripts\\python.exe scripts\\siteground_bulk_import.py dryrun
    .venv\\Scripts\\python.exe scripts\\siteground_bulk_import.py run --batch 1
    .venv\\Scripts\\python.exe scripts\\siteground_bulk_import.py run --batch 2
    .venv\\Scripts\\python.exe scripts\\siteground_bulk_import.py run --batch 3
    .venv\\Scripts\\python.exe scripts\\siteground_bulk_import.py demote-monitoring
    .venv\\Scripts\\python.exe scripts\\siteground_bulk_import.py report

Never prints a password. Builds the CSV entirely in memory (no temp file
written to disk).
"""
from __future__ import annotations

import csv
import io
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

XLSX_PATH = r"C:\Users\Global\Downloads\SiteGround.xlsx"

# The admin/owner acting as actor for audit purposes (bootstrap owner id from
# TELEGRAM_HOME_CHANNEL / access_users in the live log).
ACTOR_ID = 8925736576
CHAT_ID = 8925736576  # private chat, not a group

# Explicit account-id overrides matching PRE-EXISTING settings.json
# account_purposes / data/kb/*.md bindings. Without this, the generic-word
# domain-fallback in derive_account_id would assign "coinography" to
# whichever generic-local-part row (sales/contact/info) appears first in
# file order, which does NOT match the already-written purpose text.
ACCOUNT_ID_OVERRIDES = {
    "sales@coinography.com": "sales",
    "info@coinography.com": "coinography",
    "sales@memecoinist.com": "memecoinist",
}

_INJECT_LEAD = ("=", "+", "-", "|", "\t", "\r")


def _load_rows():
    import openpyxl

    wb = openpyxl.load_workbook(XLSX_PATH, data_only=True)
    ws = wb["Sheet1"]
    raw = list(ws.iter_rows(min_row=2, values_only=True))

    groups: dict[str, dict] = {}
    order: list[str] = []
    cur_domain = None
    cur_imap_host = cur_smtp_host = None
    cur_imap_port = cur_smtp_port = None

    for i, r in enumerate(raw, start=2):
        domain, e1, p1, e2, p2, imap_host, imap_port, smtp_host, smtp_port = r
        domain = (domain or "").strip()
        if imap_host:
            cur_imap_host = str(imap_host).strip().lower()
            cur_imap_port = int(imap_port) if imap_port else 993
            cur_smtp_host = str(smtp_host).strip().lower() if smtp_host else cur_imap_host
            cur_smtp_port = int(smtp_port) if smtp_port else 465
        if domain:
            cur_domain = domain
        if not e1 and not e2:
            continue  # separator / blank row
        if cur_domain is None:
            continue
        if cur_domain not in groups:
            groups[cur_domain] = {
                "imap_host": cur_imap_host,
                "imap_port": cur_imap_port,
                "smtp_host": cur_smtp_host,
                "smtp_port": cur_smtp_port,
                "rows": [],
            }
            order.append(cur_domain)
        g = groups[cur_domain]
        for email, pw in ((e1, p1), (e2, p2)):
            if not email or not pw:
                continue
            email = str(email).strip()
            g["rows"].append({"row_no": i, "email": email, "password": pw})
    return order, groups


def _preflight_problem(email: str, password: str, imap_host: str, smtp_host: str) -> str:
    if not imap_host or not smtp_host:
        return "missing server host (never guessed)"
    if password and password[0] in _INJECT_LEAD:
        return "password starts with a character reserved for formula-injection defence"
    if len(password) > 1024:
        return "password column implausibly long"
    if password.lower() in ("password", "changeme", "123456", "test", "fixme"):
        return "password looks like a placeholder"
    return ""


def build_batches():
    """Return list of (label, csv_bytes, row_meta) in import order."""
    order, groups = _load_rows()
    batches = []

    # Batch 1: every single-pair / mixed-host domain except the two big ones.
    singles_domains = [d for d in order if d not in ("coinography.com", "memecoinist.com")]
    batches.append(("batch1_singles", singles_domains))
    batches.append(("batch2_coinography", ["coinography.com"]))
    batches.append(("batch3_memecoinist", ["memecoinist.com"]))

    out = []
    for label, domains in batches:
        ordered_rows = []
        for d in domains:
            g = groups[d]
            rows = list(g["rows"])
            # Pin override rows first within their domain so taken_ids is
            # seeded correctly before any generic-word fallback runs.
            rows.sort(key=lambda r: 0 if r["email"].lower() in ACCOUNT_ID_OVERRIDES else 1)
            for r in rows:
                ordered_rows.append(
                    {
                        **r,
                        "imap_host": g["imap_host"],
                        "imap_port": g["imap_port"],
                        "smtp_host": g["smtp_host"],
                        "smtp_port": g["smtp_port"],
                        "domain": d,
                    }
                )
        out.append((label, ordered_rows))
    return out


def rows_to_csv_bytes(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(
        ["email", "password", "account_id", "imap_host", "imap_port", "smtp_host", "smtp_port"]
    )
    for r in rows:
        account_id = ACCOUNT_ID_OVERRIDES.get(r["email"].lower(), "")
        w.writerow(
            [
                r["email"],
                r["password"],
                account_id,
                r["imap_host"],
                r["imap_port"],
                r["smtp_host"],
                r["smtp_port"],
            ]
        )
    return buf.getvalue().encode("utf-8")


# --------------------------------------------------------------------- dryrun


def cmd_dryrun():
    from mailops.bulk import csv_parse, presets
    from mailops import mailbox_admin

    taken_ids = {m.account_id.lower() for m in mailbox_admin.list_mailboxes() if m.account_id}
    reg_emails = {m.email.lower() for m in mailbox_admin.list_mailboxes() if m.email}

    total_emails = 0
    ready = []
    dup_registry = []
    dup_within_sheet = []
    invalid = []
    unresolved = []

    for label, rows in build_batches():
        data = rows_to_csv_bytes(rows)
        total_emails += len(rows)
        try:
            parsed = csv_parse.parse_bytes(data)
        except csv_parse.CsvError as exc:
            print(f"[{label}] HARD REJECT (whole batch): {exc}")
            continue
        for inv in parsed.invalid:
            invalid.append((label, inv.row_no, inv.email, "; ".join(inv.problems)))
        for row_no, kind, val in parsed.duplicates:
            dup_within_sheet.append((label, row_no, kind, val))
        for row in parsed.rows:
            spec = presets.resolve(row, taken_ids)
            if spec.account_id:
                taken_ids.add(spec.account_id.lower())
            if spec.email.lower() in reg_emails:
                dup_registry.append((label, spec.row_no, spec.email, spec.account_id))
                continue
            if spec.problems:
                unresolved.append((label, spec.row_no, spec.email, "; ".join(spec.problems)))
                continue
            ready.append((label, spec.row_no, spec.email, spec.account_id, spec.imap_host, spec.smtp_host))

    print("=" * 70)
    print("SANITIZED VALIDATION SUMMARY (no passwords shown or logged)")
    print("=" * 70)
    print(f"Total email rows in spreadsheet: {total_emails}")
    print(f"Ready to import (unambiguous server, not a duplicate): {len(ready)}")
    print(f"Already registered (skip, never overwritten): {len(dup_registry)}")
    print(f"Duplicate within the spreadsheet itself: {len(dup_within_sheet)}")
    print(f"Invalid (shape/injection/placeholder password etc.): {len(invalid)}")
    print(f"Unresolved (ambiguous/missing server config): {len(unresolved)}")
    print()
    print("-- Already registered / skipped --")
    for label, row_no, email, aid in dup_registry:
        print(f"  [{label}] row {row_no}: {email}  (matches existing account '{aid or '?'}')")
    print()
    print("-- Unresolved / excluded (would need manual server info) --")
    for label, row_no, email, problems in unresolved:
        print(f"  [{label}] row {row_no}: {email} -- {problems}")
    print()
    print("-- Invalid rows --")
    for label, row_no, email, problems in invalid:
        print(f"  [{label}] row {row_no}: {email or '(no email)'} -- {problems}")
    print()
    print("-- Ready rows (account_id <- email @ imap_host) --")
    for label, row_no, email, aid, imap_host, smtp_host in ready:
        print(f"  [{label}] row {row_no}: {aid:<20s} <- {email}  (imap={imap_host})")
    print()
    print(f"TOTAL READY FOR LIVE IMPORT: {len(ready)}")
    return ready


# --------------------------------------------------------------------- live run


def cmd_run(batch_idx: int, *, workers: int = 1, per_host: int = 1):
    """Serialized by default (workers=1): the shared config.toml /
    accounts.json read-modify-write in webmail_onboarding.add_account() is
    NOT safe under concurrent calls (confirmed: the default workers=6 run
    on batches 1-2 caused lost-update races). workers=1 makes each row's
    file mutation complete before the next one starts."""
    import asyncio

    from mailops.bulk import service, validator

    batches = build_batches()
    label, rows = batches[batch_idx]
    data = rows_to_csv_bytes(rows)
    print(f"=== {label} : ingesting {len(rows)} row(s) ===")
    result = service.ingest_document(ACTOR_ID, CHAT_ID, "private", data=data)
    print(result.preview_text)
    job_id = result.job.job_id
    service.confirm_import(ACTOR_ID, job_id, activate=False)
    print(f"=== {label} : running validator (job {job_id}, workers={workers}, per_host={per_host}) ===")
    cfg = validator.ValidatorConfig(workers=workers, per_host=per_host, host_rate=2.0)
    final = asyncio.run(validator.run_job(job_id, config=cfg))
    print(f"=== {label} : final state = {final.state} ===")
    print(service.format_final_report(job_id))
    print(f"JOB_ID:{job_id}")
    return job_id


# Rows whose password cannot pass the bulk CSV text-safety gate (leading
# '@') but whose server config is fully unambiguous. Registered through the
# SAME webmail_onboarding.add_account() the bulk validator itself calls -
# not a new importer, just skipping the irrelevant text-parsing gate.
SPECIAL_EMAILS = {
    "info@blockwire.org",
    "info@shekel.pro",
    "marketing@shekel.pro",
    "media@coinography.com",
}


def cmd_run_specials():
    from mailops import mailbox_admin, webmail_onboarding as wm
    from mailops.bot.mb_quick import derive_account_id

    order, groups = _load_rows()
    taken = {m.account_id.lower() for m in mailbox_admin.list_mailboxes() if m.account_id}
    reg_emails = {m.email.lower() for m in mailbox_admin.list_mailboxes() if m.email}

    results = []
    for d in order:
        g = groups[d]
        for r in g["rows"]:
            if r["email"].lower() not in SPECIAL_EMAILS:
                continue
            email = r["email"]
            if email.lower() in reg_emails:
                results.append((email, "skipped", "already registered"))
                continue
            account_id = ACCOUNT_ID_OVERRIDES.get(email.lower()) or derive_account_id(email, taken)
            taken.add(account_id.lower())
            spec = wm.WebmailSpec(
                account_id=account_id,
                email=email,
                imap_host=g["imap_host"],
                smtp_host=g["smtp_host"],
                imap_port=g["imap_port"],
                smtp_port=g["smtp_port"],
            )
            problems = wm.validate_spec(spec)
            if problems:
                results.append((email, "failed_validation", "; ".join(problems)))
                continue
            mailbox_admin.record(ACTOR_ID, "siteground_direct_onboard_start", account_id, email)
            try:
                res = wm.add_account(spec, r["password"], r["password"])
            except wm.OnboardingError as exc:
                results.append((email, "failed", str(exc)))
                mailbox_admin.record(ACTOR_ID, "siteground_direct_rolled_back", account_id, str(exc))
                continue
            if res.ok:
                results.append((email, "ok", account_id))
                mailbox_admin.record(ACTOR_ID, "siteground_direct_onboarded", account_id, email)
            else:
                detail = res.raw_error or ", ".join(f"{k}:{v}" for k, v in res.detail.items() if v)
                results.append((email, "failed", detail))
                mailbox_admin.record(ACTOR_ID, "siteground_direct_rolled_back", account_id, detail)

    print("=== special (@-password) accounts ===")
    for email, status, detail in results:
        print(f"  {email}: {status} -- {detail}")
    return results


# Rows that were valid, unambiguous and non-duplicate but got dropped by the
# workers=6 lost-update race during batch 2 (confirmed via post-hoc dryrun
# diff against the registry) and never retried after the repair. Same safe,
# serialized, single-row path as cmd_run_specials() - still
# webmail_onboarding.add_account(), still no new importer.
REMNANT_EMAILS = {
    "seo@coinography.com",
    "katiearcher@coinography.com",
    "jennadeleon@coinography.com",
}


def cmd_run_remnants():
    from mailops import mailbox_admin, webmail_onboarding as wm
    from mailops.bot.mb_quick import derive_account_id

    order, groups = _load_rows()
    taken = {m.account_id.lower() for m in mailbox_admin.list_mailboxes() if m.account_id}
    reg_emails = {m.email.lower() for m in mailbox_admin.list_mailboxes() if m.email}

    results = []
    for d in order:
        g = groups[d]
        for r in g["rows"]:
            if r["email"].lower() not in REMNANT_EMAILS:
                continue
            email = r["email"]
            if email.lower() in reg_emails:
                results.append((email, "skipped", "already registered"))
                continue
            account_id = ACCOUNT_ID_OVERRIDES.get(email.lower()) or derive_account_id(email, taken)
            taken.add(account_id.lower())
            spec = wm.WebmailSpec(
                account_id=account_id,
                email=email,
                imap_host=g["imap_host"],
                smtp_host=g["smtp_host"],
                imap_port=g["imap_port"],
                smtp_port=g["smtp_port"],
            )
            problems = wm.validate_spec(spec)
            if problems:
                results.append((email, "failed_validation", "; ".join(problems)))
                continue
            mailbox_admin.record(ACTOR_ID, "siteground_remnant_onboard_start", account_id, email)
            try:
                res = wm.add_account(spec, r["password"], r["password"])
            except wm.OnboardingError as exc:
                results.append((email, "failed", str(exc)))
                mailbox_admin.record(ACTOR_ID, "siteground_remnant_rolled_back", account_id, str(exc))
                continue
            if res.ok:
                results.append((email, "ok", account_id))
                mailbox_admin.record(ACTOR_ID, "siteground_remnant_onboarded", account_id, email)
            else:
                detail = res.raw_error or ", ".join(f"{k}:{v}" for k, v in res.detail.items() if v)
                results.append((email, "failed", detail))
                mailbox_admin.record(ACTOR_ID, "siteground_remnant_rolled_back", account_id, detail)

    print("=== remnant (batch-2-race-dropped) accounts ===")
    for email, status, detail in results:
        print(f"  {email}: {status} -- {detail}")
    return results


def cmd_demote_monitoring(job_ids: list[str]):
    """Strip every account this import run registered back OUT of
    active_accounts (monitoring OFF) while leaving organize/send OFF and
    all pre-existing accounts completely untouched."""
    from mailops.bulk import jobs
    from mailops.config import load_settings, save_settings
    from mailops import mailbox_admin

    new_ids: set[str] = set()
    for jid in job_ids:
        for r in jobs.list_rows(jid):
            if r.status == "ok":
                new_ids.add(r.account_id)

    settings = load_settings()
    allow = [str(x).strip() for x in (settings.get("active_accounts") or [])]
    before = set(allow)
    allow = [a for a in allow if a not in new_ids]
    removed = before - set(allow)
    settings["active_accounts"] = allow
    save_settings(settings)
    for aid in sorted(new_ids):
        mailbox_admin.record(ACTOR_ID, "siteground_monitoring_held_off", aid, "job batch import")
    print(f"Accounts newly registered this run: {sorted(new_ids)}")
    print(f"Removed from active_accounts (monitoring OFF): {sorted(removed)}")
    print(f"active_accounts now: {allow}")
    return new_ids


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dryrun"
    if cmd == "dryrun":
        cmd_dryrun()
    elif cmd == "run":
        cmd_run(int(sys.argv[2]))
    elif cmd == "run-specials":
        cmd_run_specials()
    elif cmd == "run-remnants":
        cmd_run_remnants()
    elif cmd == "demote":
        cmd_demote_monitoring(sys.argv[2:])
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(2)
