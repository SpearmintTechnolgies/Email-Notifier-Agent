"""Telegram-facing orchestration for bulk mailbox onboarding.

This module is deliberately UI-agnostic: it does not import ``telegram.*``.
``mailops.bot.bulk_mailboxes`` is the Telegram glue; everything here is
plain functions that read/write SQLite, call ``csv_parse`` + ``presets`` +
``jobs``, and own the admin-safe text formatting.

Access rules
------------
* ``require_admin(uid)`` must have returned True before any entry point here
  is called. Every public function RE-CHECKS that the actor owns the job
  (``jobs._guard_actor``) so a stale callback cannot touch another admin's
  work.
* The CEO can run the whole workflow inside the whitelisted group when
  ``MAILOPS_BULK_ALLOW_GROUP_UPLOAD=1`` AND the chat matches
  ``MAILOPS_BULK_ALLOWED_GROUP`` (or, if that env var is empty, any
  whitelisted group). Group uploads surface a loud warning that group
  members may have seen the file before deletion.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Iterable

from .. import access, mailbox_admin
from ..bot import mb_quick
from ..redact import redact_text
from . import csv_parse, jobs, presets, validator

log = logging.getLogger("mailops.bulk.service")


# --------------------------------------------------------------------- env


def group_upload_allowed(chat_id: int) -> tuple[bool, str]:
    """Return (allowed, reason). ``reason`` is empty when allowed."""
    flag = (os.environ.get("MAILOPS_BULK_ALLOW_GROUP_UPLOAD") or "").strip() == "1"
    if not flag:
        return False, (
            "Group uploads are disabled. Open the bot in a private chat "
            "and resend the CSV there."
        )
    pinned = (os.environ.get("MAILOPS_BULK_ALLOWED_GROUP") or "").strip()
    if pinned:
        try:
            allowed_id = int(pinned)
        except ValueError:
            return False, "MAILOPS_BULK_ALLOWED_GROUP is not a numeric chat id."
        if int(chat_id) != allowed_id:
            return False, "This group is not the configured bulk-upload group."
        return True, ""
    # No pin configured: fall back to the general group whitelist.
    if not access.group_allowed(int(chat_id)):
        return False, "This group is not whitelisted."
    return True, ""


def default_activation() -> str:
    """Either 'inactive', 'active', or 'ask' (admin picks per import)."""
    raw = (os.environ.get("MAILOPS_BULK_DEFAULT_ACTIVATION") or "ask").strip().lower()
    return raw if raw in ("inactive", "active", "ask") else "ask"


def max_rows() -> int:
    raw = (os.environ.get("MAILOPS_BULK_MAX_ROWS") or "").strip()
    try:
        return max(1, min(10000, int(raw))) if raw else csv_parse.MAX_ROWS
    except ValueError:
        return csv_parse.MAX_ROWS


def max_file_bytes() -> int:
    raw = (os.environ.get("MAILOPS_BULK_MAX_FILE_BYTES") or "").strip()
    try:
        return max(1024, min(10 * 1024 * 1024, int(raw))) if raw else csv_parse.MAX_FILE_BYTES
    except ValueError:
        return csv_parse.MAX_FILE_BYTES


def max_line_bytes() -> int:
    raw = (os.environ.get("MAILOPS_BULK_MAX_LINE_BYTES") or "").strip()
    try:
        return max(256, min(64 * 1024, int(raw))) if raw else csv_parse.MAX_LINE_BYTES
    except ValueError:
        return csv_parse.MAX_LINE_BYTES


# --------------------------------------------------------------------- ingest


@dataclass
class IngestResult:
    job: jobs.BulkJob
    preview_text: str
    specs_count: int
    warn_group: bool


def ingest_document(
    actor_id: int,
    chat_id: int,
    origin: str,
    *,
    data: bytes,
    document_file_id: str = "",
    document_message_id: int | None = None,
) -> IngestResult:
    """Parse an uploaded CSV, resolve presets, open a job, store the preview.

    Raises ``csv_parse.CsvError`` or ``jobs.BulkJobError`` if the upload is
    refused. Never persists any raw CSV byte; only the sealed per-row
    credential blob plus password-free metadata lands on disk.
    """
    # Hard overlap check: one bulk job in flight per admin keeps things sane.
    existing = jobs.active_job_for(actor_id)
    if existing is not None:
        raise jobs.BulkJobError(
            f"Another bulk job is already in progress (state={existing.state}). "
            "Finish, pause or cancel it before starting a new import."
        )

    parsed = csv_parse.parse_bytes(
        data,
        max_rows=max_rows(),
        max_file_bytes=max_file_bytes(),
        max_line_bytes=max_line_bytes(),
    )

    taken_ids = {m.account_id.lower() for m in mailbox_admin.list_mailboxes() if m.account_id}
    specs: list[presets.ResolvedSpec] = []
    for row in parsed.rows:
        spec = presets.resolve(row, taken_ids)
        if spec.account_id:
            taken_ids.add(spec.account_id.lower())
        specs.append(spec)

    # Count registry duplicates separately so the preview is honest.
    reg_emails = _registered_emails_lower()
    registry_dupes = 0
    for spec in specs:
        if spec.email.lower() in reg_emails:
            spec.problems.insert(
                0, f"{spec.email} is already registered as a Mailops mailbox."
            )
            registry_dupes += 1

    preview = parsed.preview_summary()
    preview["registry_duplicates"] = registry_dupes
    preview["activation_default"] = default_activation()

    job = jobs.create_job(
        actor_id=actor_id,
        chat_id=chat_id,
        origin=origin,
        document_file_id=document_file_id,
        document_message_id=document_message_id,
    )
    job = jobs.record_preview(
        job.job_id,
        actor_id,
        rows_specs=specs,
        invalid_count=len(parsed.invalid),
        duplicate_count=len(parsed.duplicates) + registry_dupes,
        preview=preview,
    )
    # We never need the raw file again - the sealed per-row blobs are
    # everything the validator will touch.
    jobs.clear_document_handle(job.job_id)
    mailbox_admin.record(
        actor_id,
        "bulk_uploaded",
        "",
        f"job={job.job_id} rows={job.total_rows} valid={job.valid_rows} "
        f"invalid={job.invalid_rows} dupes={job.duplicate_rows}",
    )
    return IngestResult(
        job=job,
        preview_text=format_preview(job),
        specs_count=len(specs),
        warn_group=(origin == "group"),
    )


def _registered_emails_lower() -> set[str]:
    out: set[str] = set()
    for m in mailbox_admin.list_mailboxes():
        if m.email:
            out.add(m.email.lower())
    return out


# --------------------------------------------------------------------- preview + reports


def format_preview(job: jobs.BulkJob) -> str:
    p = job.preview or {}
    provider_lines = ", ".join(
        f"{name}: {count}" for name, count in sorted(p.get("providers", {}).items())
    ) or "(none detected)"
    lines = [
        "Bulk CSV preview",
        "",
        f"Job id:        {job.job_id}",
        f"Total rows:    {job.total_rows}",
        f"Valid rows:    {job.valid_rows}",
        f"Invalid rows:  {job.invalid_rows}",
        f"Duplicate rows (within CSV or registry): {job.duplicate_rows}",
        f"Providers:     {provider_lines}",
        f"Default activation: {p.get('activation_default', 'ask')}",
        "",
        "Sample valid rows (first five, passwords NEVER shown):",
    ]
    for s in p.get("sample_rows", [])[:5]:
        lines.append(
            f"  · row {s.get('row_no')}  {s.get('email')}  "
            f"[{s.get('provider') or 'custom'}]"
        )
    if p.get("invalid_sample"):
        lines.append("")
        lines.append("Sample invalid rows:")
        for inv in p["invalid_sample"][:5]:
            problems = "; ".join(str(x) for x in (inv.get("problems") or []))[:200]
            lines.append(
                f"  · row {inv.get('row_no')}  {inv.get('email') or '(no email)'}"
                f"  — {problems}"
            )
    if job.origin == "group":
        lines.append("")
        lines.append(
            "⚠ Credentials were uploaded in a GROUP. Group members may have "
            "seen the file before the bot could delete it. Rotate any "
            "accidentally-exposed passwords after onboarding."
        )
    lines.append("")
    lines.append(
        "No mailbox has been contacted yet. Confirm to begin validation; "
        "cancel to discard the sealed credential blobs."
    )
    return "\n".join(lines)


def format_progress(job_id: str) -> str:
    snap = validator.snapshot(job_id)
    counts = snap.get("counts", {})
    total = snap.get("total", 0)
    done_statuses = ("ok", "failed_auth", "failed_network",
                     "failed_duplicate", "failed_validation", "rolled_back", "skipped")
    done = sum(counts.get(s, 0) for s in done_statuses)
    pct = int(100 * done / total) if total else 0
    lines = [
        "Bulk CSV progress",
        "",
        f"Job id:   {snap['job_id']}",
        f"State:    {snap['state']}",
        f"Progress: {done}/{total} ({pct}%)",
        "",
        "Counts by status:",
    ]
    for k in ("ok", "pending", "running", "failed_auth", "failed_network",
              "failed_duplicate", "failed_validation", "rolled_back", "skipped"):
        if counts.get(k, 0):
            lines.append(f"  {k}: {counts[k]}")
    return "\n".join(lines)


def format_final_report(job_id: str) -> str:
    """Admin-safe final report. Password-free; CSV-injection-safe."""
    job = jobs.get_job(job_id)
    rows = jobs.list_rows(job_id)
    by_status: dict[str, list[jobs.BulkRow]] = {}
    for r in rows:
        by_status.setdefault(r.status, []).append(r)
    lines = [
        "Bulk CSV final report",
        "",
        f"Job id:       {job.job_id}",
        f"State:        {job.state}",
        f"Activation:   {job.activation}",
        f"Total rows:   {job.total_rows}",
        f"Succeeded:    {len(by_status.get('ok', []))}",
        f"Failed auth:  {len(by_status.get('failed_auth', []))}",
        f"Failed net:   {len(by_status.get('failed_network', []))}",
        f"Dup / exist:  {len(by_status.get('failed_duplicate', []))}",
        f"Validation:   {len(by_status.get('failed_validation', []))}",
        f"Rolled back:  {len(by_status.get('rolled_back', []))}",
        f"Skipped:      {len(by_status.get('skipped', []))}",
        "",
    ]
    for section_label, status_key in (
        ("Succeeded accounts", "ok"),
        ("Failed authentication", "failed_auth"),
        ("Network failures", "failed_network"),
        ("Duplicate / already registered", "failed_duplicate"),
        ("Validation failures", "failed_validation"),
        ("Rolled back", "rolled_back"),
    ):
        bucket = by_status.get(status_key, [])
        if not bucket:
            continue
        lines.append(section_label + ":")
        for r in bucket[:20]:
            email = csv_parse.report_escape(r.email)
            aid = csv_parse.report_escape(r.account_id)
            detail = ""
            if r.failure_detail:
                detail = f"  —  {csv_parse.report_escape(r.failure_detail[:180])}"
            lines.append(f"  · row {r.row_no}  {aid}  {email}{detail}")
        if len(bucket) > 20:
            lines.append(f"  ... and {len(bucket) - 20} more")
        lines.append("")
    if job.document_deleted == 2:
        lines.append("⚠ The uploaded Telegram document could NOT be deleted. "
                     "Delete the file message manually from the group.")
    return "\n".join(lines)


# --------------------------------------------------------------------- actions


def confirm_import(actor_id: int, job_id: str, *, activate: bool) -> jobs.BulkJob:
    activation = "active" if activate else "inactive"
    return jobs.confirm(job_id, actor_id, activation=activation)


def pause_import(actor_id: int, job_id: str) -> jobs.BulkJob:
    return jobs.pause(job_id, actor_id)


def resume_import(actor_id: int, job_id: str) -> jobs.BulkJob:
    return jobs.resume(job_id, actor_id)


def cancel_import(actor_id: int, job_id: str) -> jobs.BulkJob:
    job = jobs.cancel(job_id, actor_id, reason="cancelled by admin")
    jobs.purge_credentials(job_id)
    return job


def retry_failed(actor_id: int, job_id: str) -> int:
    return jobs.retry_failed(job_id, actor_id)


def retry_row(actor_id: int, job_id: str, row_no: int) -> None:
    jobs.requeue_row(job_id, row_no, actor_id)


def activate_registered_accounts(actor_id: int, job_id: str, *, enable_monitoring: bool) -> list[str]:
    """Flip ``active_accounts`` + policy for every mailbox this job onboarded.

    Called by the UI's 'Activate monitoring' button. The default is to leave
    accounts in the registered-but-inactive state so the sync loop does not
    immediately start polling N new mailboxes. Setting
    ``enable_monitoring=True`` adds them to the allowlist WITHOUT touching
    organize/send: those remain OFF and must be enabled per-account by the
    existing Settings screens.
    """
    job = jobs.get_job(job_id)
    jobs._guard_actor(job, actor_id)
    rows = [r for r in jobs.list_rows(job.job_id) if r.status == "ok"]
    activated: list[str] = []
    if not rows:
        return activated
    from ..config import load_settings, save_settings
    settings = load_settings()
    allow = list(settings.get("active_accounts") or [])
    allow_set = {str(x).strip() for x in allow if str(x).strip()}
    policies = dict(settings.get("account_policies") or {})
    changed = False
    for r in rows:
        aid = r.account_id
        if enable_monitoring:
            if aid not in allow_set:
                allow.append(aid)
                allow_set.add(aid)
                changed = True
        # Policy is ALWAYS deny-by-default. Even when the admin opts in to
        # 'enable monitoring' we leave organize/send OFF - the account is
        # read-only polling until explicitly promoted via the per-mailbox
        # Settings screen.
        policies.setdefault(aid, {"organize": False, "send": False})
        activated.append(aid)
    if changed:
        settings["active_accounts"] = allow
    settings["account_policies"] = policies
    save_settings(settings)
    for aid in activated:
        mailbox_admin.record(
            actor_id,
            "bulk_activate" if enable_monitoring else "bulk_register_only",
            aid,
            f"job={job_id}",
        )
    return activated
