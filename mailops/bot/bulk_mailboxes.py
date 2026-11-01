"""Telegram UI for bulk CSV mailbox onboarding.

Entry points
------------
* ``handle_callback(update, context, body)`` - owns every ``mb:bulk*`` button.
* ``handle_document(update, context)`` - called from ``app.on_document`` for
  any CSV upload sent to the whitelisted group or an authorized admin DM.

Access rules
------------
Every entry point re-checks:
  1. the actor is an authorized administrator (``access.can(uid, P_ADMIN)``);
  2. the chat is either private or the bulk-upload group (per
     ``service.group_upload_allowed``) + whitelisted in the general store;
  3. the actor owns the job (``jobs._guard_actor`` inside the service).

The uploaded CSV message is deleted best-effort as soon as ingest finishes.
Deletion failures are surfaced in the preview + final report so the admin
knows to delete manually. The preview NEVER echoes passwords, the raw
document URL, or raw CSV content.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from .callbacks import Ack

from .. import access
from ..redact import redact_text
from ..bulk import csv_parse, jobs, service, validator

log = logging.getLogger("mailops.bot.bulk")

PREFIX = "mb:bulk"


def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def _markup(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn(t, d) for t, d in row] for row in rows])


def _is_group(update: Update) -> bool:
    chat = getattr(update, "effective_chat", None)
    return getattr(chat, "type", None) in ("group", "supergroup")


def _is_private(update: Update) -> bool:
    chat = getattr(update, "effective_chat", None)
    return getattr(chat, "type", None) in (None, "private")


def _effective_chat_id(update: Update) -> int | None:
    chat = getattr(update, "effective_chat", None)
    cid = getattr(chat, "id", None)
    try:
        return int(cid) if cid is not None else None
    except (TypeError, ValueError):
        return None


async def _show(update: Update, text: str, markup: InlineKeyboardMarkup | None) -> None:
    if len(text) > 3900:
        text = text[:3900] + "\n..."
    q = getattr(update, "callback_query", None)
    if q is not None:
        try:
            await q.edit_message_text(text, reply_markup=markup)
            return
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return
    msg = update.effective_message
    if msg is not None:
        await msg.reply_text(text, reply_markup=markup)


async def _notice(q: Any, text: str) -> None:
    """Refusal/status text that never double-answers the callback query."""
    if q is None:
        return
    await Ack(q).notice(text[:180])


def _guard_admin(update: Update) -> int | None:
    uid = getattr(update.effective_user, "id", None)
    if uid is None or not access.can(uid, access.P_ADMIN):
        return None
    return int(uid)


# ---------------------------------------------------------- running jobs tracker

# In-process registry of validator tasks keyed by job_id. Pause/cancel use
# this to kill a running worker promptly; a bot restart falls back to the
# persisted job state (the worker will restart on its own).
_RUNNING_TASKS: dict[str, asyncio.Task] = {}


def _is_task_live(job_id: str) -> bool:
    t = _RUNNING_TASKS.get(job_id)
    return bool(t and not t.done())


def _spawn_validator(job_id: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_task_live(job_id):
        return
    loop = asyncio.get_running_loop()

    async def _run():
        try:
            await validator.run_job(job_id, progress=_progress_cb(context))
        except Exception:
            log.exception("bulk validator for %s crashed", job_id)
        finally:
            _RUNNING_TASKS.pop(job_id, None)

    task = loop.create_task(_run(), name=f"bulk-validator-{job_id}")
    _RUNNING_TASKS[job_id] = task


def _progress_cb(context: ContextTypes.DEFAULT_TYPE):
    async def _cb(job_id: str, snap: dict[str, Any]) -> None:
        # The validator calls back on every row completion. We don't
        # auto-edit the Telegram message (rate-limit risk); the admin
        # taps "Refresh" instead. This callback is retained so a future
        # ping / badge update can be plugged in without new wiring.
        return
    return _cb


# ---------------------------------------------------------- screens


def _entry_screen(update: Update) -> tuple[str, InlineKeyboardMarkup]:
    in_group = _is_group(update)
    chat_id = _effective_chat_id(update)
    group_ok = True
    group_reason = ""
    if in_group and chat_id is not None:
        group_ok, group_reason = service.group_upload_allowed(chat_id)
    lines = [
        "Bulk Add Email (CSV)",
        "",
        "Upload a CSV document to onboard many mailboxes in one go.",
        "",
        "Required columns: email, password.",
        "Optional columns: account_id, display_name, provider, imap_host, "
        "imap_port, imap_username, smtp_host, smtp_port, smtp_username.",
        "",
        "Supported provider presets:",
        "  · hostinger   - prefills imap.hostinger.com:993 + smtp:465",
        "  · siteground  - prefills ports 993/465; set imap_host/smtp_host",
        "                  per row because SiteGround assigns a different",
        "                  server per tenant.",
        "  · any saved custom preset (Mailboxes -> Quick Add -> Save)",
        "",
        "Google / Gmail is NOT accepted in bulk CSV (OAuth only; see",
        "Mailboxes -> Add email -> Google / Gmail).",
        "",
        f"Hard caps: {service.max_rows()} rows, "
        f"{service.max_file_bytes()} bytes, "
        f"{service.max_line_bytes()} bytes/line.",
    ]
    rows: list[list[tuple[str, str]]] = []
    if in_group:
        if not group_ok:
            lines.append("")
            lines.append(
                "⚠ This group is NOT allowed to receive the credential CSV: "
                + group_reason
                + " Open the bot in a DM instead."
            )
        else:
            lines.append("")
            lines.append(
                "⚠ Group upload is enabled. Every member of this group can "
                "see the CSV file in chat history before the bot deletes it. "
                "The bot deletes the upload message after ingest, but "
                "deletion is best-effort and devices that already received "
                "the message may keep a local copy. Rotate accidentally-"
                "exposed credentials after onboarding."
            )
    rows.append([("Download CSV template", f"{PREFIX}:tmpl")])
    jobs_rows = jobs.list_jobs_for(update.effective_user.id, limit=5)
    for j in jobs_rows:
        tail = " (active)" if not j.is_terminal else f" ({j.state})"
        rows.append([(f"Job {j.job_id}{tail}", f"{PREFIX}:status:{j.job_id}")])
    rows.append([("Back to mailboxes", "mb:m")])
    return "\n".join(lines), _markup(rows)


def _status_screen(job_id: str) -> tuple[str, InlineKeyboardMarkup]:
    job = jobs.get_job(job_id)
    rows: list[list[tuple[str, str]]] = []
    if job.state == "previewed":
        body = service.format_preview(job)
        rows = [
            [("Confirm & register (monitoring OFF)", f"{PREFIX}:confirm:{job_id}:inactive")],
            [("Confirm & activate monitoring", f"{PREFIX}:confirm:{job_id}:active")],
            [("Cancel import", f"{PREFIX}:cancel:{job_id}")],
            [("Back", f"{PREFIX}")],
        ]
    elif job.state in ("confirmed", "running"):
        body = service.format_progress(job_id)
        rows = [
            [("Refresh", f"{PREFIX}:status:{job_id}")],
            [("Pause", f"{PREFIX}:pause:{job_id}")],
            [("Cancel", f"{PREFIX}:cancel:{job_id}")],
            [("Back", f"{PREFIX}")],
        ]
    elif job.state == "paused":
        body = service.format_progress(job_id)
        rows = [
            [("Resume", f"{PREFIX}:resume:{job_id}")],
            [("Cancel", f"{PREFIX}:cancel:{job_id}")],
            [("Refresh", f"{PREFIX}:status:{job_id}")],
            [("Back", f"{PREFIX}")],
        ]
    else:  # done / cancelled / failed
        body = service.format_final_report(job_id)
        retryable_rows = [
            r for r in jobs.list_rows(job_id) if r.status in jobs.MANUALLY_RETRYABLE_STATUSES
        ]
        rows = []
        if retryable_rows:
            rows.append([("Retry failed rows", f"{PREFIX}:retry:{job_id}")])
            # Individual retry controls, bounded so the keyboard stays usable
            # even on a 90-100 mailbox import with many failures.
            for r in retryable_rows[:8]:
                rows.append(
                    [(f"Retry row {r.row_no} ({r.account_id or r.email})",
                      f"{PREFIX}:retryrow:{job_id}:{r.row_no}")]
                )
            if len(retryable_rows) > 8:
                body += f"\n\n({len(retryable_rows) - 8} more retryable row(s) - use 'Retry failed rows' for all.)"
        rows.append([("Activate monitoring now", f"{PREFIX}:activate:{job_id}:on")])
        rows.append([("Keep registered-only (no polling)", f"{PREFIX}:activate:{job_id}:off")])
        rows.append([("Back", f"{PREFIX}")])
    return body, _markup(rows)


async def _send_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    import io

    msg = update.effective_message
    if msg is None:
        return
    buf = io.BytesIO(csv_parse.render_template())
    buf.name = "mailops-bulk-template.csv"
    try:
        await context.bot.send_document(
            chat_id=msg.chat_id,
            document=buf,
            filename="mailops-bulk-template.csv",
            caption=(
                "Bulk mailbox CSV template. REPLACE the password column with "
                "a dedicated app password per mailbox before upload."
            ),
        )
    except TelegramError as exc:
        log.warning("could not send CSV template: %s", exc)
        if msg is not None:
            await msg.reply_text(
                "Could not attach the CSV template file (Telegram error). "
                "The columns are: " + ", ".join(sorted(csv_parse.ALLOWED_HEADERS))
            )


# ---------------------------------------------------------- public: callbacks


async def handle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, body: str
) -> None:
    """Dispatch a ``mb:bulk*`` callback. Admin + chat guards are already run
    by ``mailboxes.handle_callback``, but we re-check admin here as defence
    in depth."""
    uid = _guard_admin(update)
    if uid is None:
        q = getattr(update, "callback_query", None)
        if q is not None:
            await _notice(q, "Only administrators can run bulk imports.")
        return
    q = update.callback_query
    # Acknowledgment is owned by ``app.on_callback`` (exactly once per query).

    if body == "bulk":
        text, markup = _entry_screen(update)
        await _show(update, text, markup)
        return
    if not body.startswith("bulk:"):
        return
    rest = body[len("bulk:"):]
    if rest == "tmpl":
        await _send_template(update, context)
        return
    parts = rest.split(":")
    action = parts[0]
    job_id = parts[1] if len(parts) > 1 else ""
    extra = parts[2] if len(parts) > 2 else ""

    if action == "status":
        text, markup = _status_screen(job_id)
        await _show(update, text, markup)
        return
    if action == "confirm":
        activate = (extra == "active")
        try:
            service.confirm_import(uid, job_id, activate=activate)
        except Exception as exc:
            await _show(update, redact_text(str(exc))[:300], _markup([[("Back", f"{PREFIX}")]]))
            return
        _spawn_validator(job_id, context)
        text, markup = _status_screen(job_id)
        await _show(update, "Confirmed. Validation starting.\n\n" + text, markup)
        return
    if action == "pause":
        try:
            service.pause_import(uid, job_id)
        except Exception as exc:
            await _show(update, redact_text(str(exc))[:300], _markup([[("Back", f"{PREFIX}")]]))
            return
        text, markup = _status_screen(job_id)
        await _show(update, text, markup)
        return
    if action == "resume":
        try:
            service.resume_import(uid, job_id)
        except Exception as exc:
            await _show(update, redact_text(str(exc))[:300], _markup([[("Back", f"{PREFIX}")]]))
            return
        _spawn_validator(job_id, context)
        text, markup = _status_screen(job_id)
        await _show(update, text, markup)
        return
    if action == "cancel":
        try:
            service.cancel_import(uid, job_id)
        except Exception as exc:
            await _show(update, redact_text(str(exc))[:300], _markup([[("Back", f"{PREFIX}")]]))
            return
        text, markup = _status_screen(job_id)
        await _show(update, "Cancelled. Credentials wiped.\n\n" + text, markup)
        return
    if action == "retry":
        try:
            n = service.retry_failed(uid, job_id)
        except Exception as exc:
            await _show(update, redact_text(str(exc))[:300], _markup([[("Back", f"{PREFIX}")]]))
            return
        if n:
            _spawn_validator(job_id, context)
        text, markup = _status_screen(job_id)
        await _show(update, f"Requeued {n} failed row(s).\n\n" + text, markup)
        return
    if action == "retryrow":
        try:
            row_no = int(extra)
            service.retry_row(uid, job_id, row_no)
        except Exception as exc:
            await _show(update, redact_text(str(exc))[:300], _markup([[("Back", f"{PREFIX}:status:{job_id}")]]))
            return
        _spawn_validator(job_id, context)
        text, markup = _status_screen(job_id)
        await _show(update, f"Requeued row {row_no}.\n\n" + text, markup)
        return
    if action == "activate":
        enable = (extra == "on")
        try:
            activated = service.activate_registered_accounts(
                uid, job_id, enable_monitoring=enable
            )
        except Exception as exc:
            await _show(update, redact_text(str(exc))[:300], _markup([[("Back", f"{PREFIX}")]]))
            return
        text, markup = _status_screen(job_id)
        note = (
            f"Monitoring activated for {len(activated)} account(s) "
            "(organize/send still OFF)."
            if enable
            else f"{len(activated)} account(s) remain registered-but-inactive."
        )
        await _show(update, note + "\n\n" + text, markup)
        return
    await _notice(q, "Unknown bulk control.")


# ---------------------------------------------------------- public: documents


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Accept a CSV document upload.

    Returns True when the document was consumed by this handler.
    """
    msg = update.effective_message
    doc = getattr(msg, "document", None) if msg else None
    if doc is None:
        return False
    uid = _guard_admin(update)
    if uid is None:
        # Not an admin; silently ignore so the group doesn't learn anything.
        return False
    chat_id = _effective_chat_id(update)
    if chat_id is None:
        return False
    origin = "group" if _is_group(update) else "private"
    if origin == "group":
        ok, reason = service.group_upload_allowed(chat_id)
        if not ok:
            try:
                await msg.reply_text(
                    "CSV upload refused in this chat: " + reason
                )
            except Exception:
                pass
            # Still try to delete the message so the credentials don't linger.
            try:
                await msg.delete()
            except Exception:
                pass
            return True
    # Filename sanity.
    filename = (getattr(doc, "file_name", "") or "").lower()
    mime = (getattr(doc, "mime_type", "") or "").lower()
    looks_csv = (
        filename.endswith(".csv")
        or filename.endswith(".txt")
        or "csv" in mime
        or "text/plain" in mime
    )
    if not looks_csv:
        try:
            await msg.reply_text(
                "Only .csv (or plain .txt) files are accepted for bulk "
                "mailbox onboarding."
            )
        except Exception:
            pass
        return True
    cap = service.max_file_bytes()
    size = int(getattr(doc, "file_size", 0) or 0)
    if size and size > cap:
        try:
            await msg.reply_text(
                f"CSV is too large ({size} bytes). Hard cap is {cap} bytes."
            )
        except Exception:
            pass
        return True
    # Download via the authenticated Bot API. Never log the download URL.
    try:
        file = await context.bot.get_file(doc.file_id)
        data = await file.download_as_bytearray()
    except TelegramError as exc:
        log.warning("bulk document download failed: %s", type(exc).__name__)
        try:
            await msg.reply_text(
                "Could not download the uploaded CSV. Try again."
            )
        except Exception:
            pass
        return True
    data = bytes(data)
    # Immediate best-effort delete BEFORE parsing - the credentials now exist
    # only inside this process. If parse fails we tell the admin so they can
    # retry with a fresh upload.
    delete_ok = False
    try:
        await msg.delete()
        delete_ok = True
    except Exception as exc:
        log.warning("could not delete bulk upload message: %s", type(exc).__name__)

    try:
        result = service.ingest_document(
            uid,
            chat_id,
            origin,
            data=data,
            document_file_id="",  # not kept
            document_message_id=getattr(msg, "message_id", None),
        )
    except (csv_parse.CsvError, jobs.BulkJobError) as exc:
        safe = redact_text(str(exc))[:600]
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Bulk CSV refused:\n{safe}",
            )
        except Exception:
            pass
        return True
    finally:
        # Scrub local reference to the raw bytes.
        data = b""

    jobs.mark_document_deleted(result.job.job_id, success=delete_ok)
    text, markup = _status_screen(result.job.job_id)
    header_lines = []
    if origin == "group":
        header_lines.append(
            "⚠ Credential CSV was uploaded in a GROUP. Group members may "
            "have seen the file before the bot could delete it. "
            + ("Message deleted." if delete_ok else "⚠ MESSAGE NOT DELETED - delete it manually.")
        )
    else:
        if not delete_ok:
            header_lines.append(
                "⚠ The bot could not delete your upload message. Delete it "
                "manually to remove the file from Telegram cloud history."
            )
    header = "\n".join(header_lines)
    body = (header + "\n\n" + text) if header else text
    try:
        await context.bot.send_message(
            chat_id=chat_id, text=body[:3900], reply_markup=markup
        )
    except Exception:
        pass
    return True



