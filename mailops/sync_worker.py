from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .classifier import Classification, classify_message
from .config import PRIORITY_TO_FOLDER, TAG_TO_FOLDER, load_accounts, load_runtime, load_settings
from .db import get_cursor, mark_notified, already_notified, queue_batch, set_cursor
from .himalaya_client import Himalaya
from .taxonomy import ensure_taxonomy

log = logging.getLogger("mailops.sync")

NotifyFn = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class ProcessedMail:
    account_id: str
    email: str
    msg_id: str
    subject: str
    sender: str
    priority: int
    tags: list[str]
    needs_reply: bool
    folder: str
    reason: str


def _uid(envelope: dict[str, Any]) -> int:
    raw = envelope.get("id") or envelope.get("uid") or 0
    try:
        return int(str(raw))
    except ValueError:
        m = re.search(r"\d+", str(raw))
        return int(m.group(0)) if m else 0


def _sender(envelope: dict[str, Any]) -> str:
    frm = envelope.get("from") or []
    if isinstance(frm, list) and frm:
        first = frm[0]
        if isinstance(first, dict):
            name = first.get("name") or ""
            email = first.get("email") or ""
            return f"{name} <{email}>".strip() if name else str(email)
        return str(first)
    return str(envelope.get("from") or "")


async def process_account(
    him: Himalaya,
    account_id: str,
    account_email: str,
    notify: NotifyFn | None = None,
) -> list[ProcessedMail]:
    ensure_taxonomy(account_id)
    runtime = load_runtime()
    settings = load_settings()
    envelopes = him.envelopes(account_id, "INBOX", page_size=30, page=1)
    if not envelopes:
        return []

    # Sort by uid ascending so cursor advances sanely
    envelopes = sorted(envelopes, key=_uid)
    last = get_cursor(account_id)
    # First run: set cursor to max without processing historic flood
    if last <= 0 and envelopes:
        max_uid = max(_uid(e) for e in envelopes)
        # Process only the newest 5 on first run
        to_process = envelopes[-5:]
        set_cursor(account_id, max_uid)
    else:
        to_process = [e for e in envelopes if _uid(e) > last]

    processed: list[ProcessedMail] = []
    max_seen = last
    for env in to_process:
        uid = _uid(env)
        max_seen = max(max_seen, uid)
        msg_id = str(env.get("id") or uid)
        try:
            card = him.read_card(account_id, msg_id, "INBOX")
            preview = f"{card.subject}\n{card.from_line}\n{card.body}"[:2000]
        except Exception:  # noqa: BLE001
            preview = him.read_message(account_id, msg_id, "INBOX")[:2000]
        classification: Classification = await classify_message(
            runtime.openai_base_url,
            runtime.openai_api_key,
            env,
            preview,
            runtime.model,
            account_id=account_id,
        )
        folder = PRIORITY_TO_FOLDER.get(classification.priority, "P2 Normal")
        move_r = him.move(account_id, msg_id, folder, "INBOX")
        if not move_r.ok:
            log.warning("move failed %s %s -> %s: %s", account_id, msg_id, folder, move_r.stderr)
            continue
        if classification.tags:
            tag = classification.tags[0]
            tag_folder = TAG_TO_FOLDER.get(tag)
            if tag_folder:
                him.copy(account_id, msg_id, tag_folder, folder)
        if classification.needs_reply:
            him.flag_add(account_id, msg_id, "flagged", folder)

        item = ProcessedMail(
            account_id=account_id,
            email=account_email,
            msg_id=msg_id,
            subject=str(env.get("subject") or "(no subject)"),
            sender=_sender(env),
            priority=classification.priority,
            tags=classification.tags,
            needs_reply=classification.needs_reply,
            folder=folder,
            reason=classification.reason,
        )
        processed.append(item)

        key = f"{account_id}:{folder}:{msg_id}"
        if already_notified(key):
            continue
        min_alert = int(settings.get("min_alert_priority", 2))
        # Higher number = lower urgency. Skip pushes above the configured floor.
        if classification.priority > min_alert:
            mark_notified(key)
            continue
        payload = {
            "type": "new_mail",
            "key": key,
            "account_id": account_id,
            "email": account_email,
            "msg_id": msg_id,
            "mailbox": folder,
            "subject": item.subject,
            "sender": item.sender,
            "priority": item.priority,
            "tags": item.tags,
            "needs_reply": item.needs_reply,
            "reason": item.reason,
        }
        if classification.priority <= 1:
            if notify:
                await notify(payload)
            else:
                mark_notified(key)
        elif classification.priority == 2:
            queue_batch(
                key,
                account_id,
                folder,
                msg_id,
                item.subject,
                item.sender,
                item.priority,
            )
        else:
            # P3: optional daily digest later; default is silent with dedupe key
            mark_notified(key)

    if max_seen > last:
        set_cursor(account_id, max_seen)
    return processed


async def sync_once(notify: NotifyFn | None = None) -> list[ProcessedMail]:
    him = Himalaya()
    out: list[ProcessedMail] = []
    for acc in load_accounts():
        try:
            out.extend(await process_account(him, acc.id, acc.email, notify))
        except Exception:  # noqa: BLE001
            log.exception("sync failed for %s", acc.id)
            if notify:
                await notify(
                    {
                        "type": "sync_error",
                        "account_id": acc.id,
                        "email": acc.email,
                        "key": f"syncerr:{acc.id}:{int(__import__('time').time())//300}",
                    }
                )
    return out


def _all_envelopes(him: Himalaya, account_id: str, mailbox: str, page_size: int = 50) -> list[dict[str, Any]]:
    """Page through a mailbox until empty."""
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = him.envelopes(account_id, mailbox, page_size=page_size, page=page)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page_size:
            break
        page += 1
        if page > 200:  # hard safety cap
            log.warning("envelope page cap hit for %s %s", account_id, mailbox)
            break
    return out


async def _classify_one(
    him: Himalaya,
    account_id: str,
    account_email: str,
    env: dict[str, Any],
    source_mailbox: str,
) -> ProcessedMail | None:
    runtime = load_runtime()
    msg_id = str(env.get("id") or _uid(env))
    try:
        card = him.read_card(account_id, msg_id, source_mailbox)
        preview = f"{card.subject}\n{card.from_line}\n{card.body}"[:2000]
    except Exception:  # noqa: BLE001
        preview = him.read_message(account_id, msg_id, source_mailbox)[:2000]
    classification: Classification = await classify_message(
        runtime.openai_base_url,
        runtime.openai_api_key,
        env,
        preview,
        runtime.model,
        account_id=account_id,
    )
    dest = PRIORITY_TO_FOLDER.get(classification.priority, "INBOX.P2-Normal")
    if dest != source_mailbox:
        move_r = him.move(account_id, msg_id, dest, source_mailbox)
        if not move_r.ok:
            log.warning(
                "rescan move failed %s %s %s -> %s: %s",
                account_id,
                msg_id,
                source_mailbox,
                dest,
                move_r.stderr,
            )
            return None
        placed_in = dest
    else:
        placed_in = source_mailbox

    if classification.tags:
        tag = classification.tags[0]
        tag_folder = TAG_TO_FOLDER.get(tag)
        if tag_folder:
            him.copy(account_id, msg_id, tag_folder, placed_in)
    if classification.needs_reply:
        him.flag_add(account_id, msg_id, "flagged", placed_in)

    return ProcessedMail(
        account_id=account_id,
        email=account_email,
        msg_id=msg_id,
        subject=str(env.get("subject") or "(no subject)"),
        sender=_sender(env),
        priority=classification.priority,
        tags=classification.tags,
        needs_reply=classification.needs_reply,
        folder=placed_in,
        reason=classification.reason,
    )


async def rescan_account(
    him: Himalaya,
    account_id: str,
    account_email: str,
) -> list[ProcessedMail]:
    """Classify and sort every message in INBOX + priority folders (full pass)."""
    ensure_taxonomy(account_id)
    folders = ["INBOX", *PRIORITY_TO_FOLDER.values()]
    # Snapshot all folders first so moves mid-run do not double-count.
    snapshots: list[tuple[str, dict[str, Any]]] = []
    for mailbox in folders:
        envelopes = _all_envelopes(him, account_id, mailbox)
        log.info("rescan %s %s: %d messages", account_id, mailbox, len(envelopes))
        for env in envelopes:
            snapshots.append((mailbox, env))

    processed: list[ProcessedMail] = []
    max_uid = 0
    seen_keys: set[str] = set()
    for mailbox, env in snapshots:
        msg_id = str(env.get("id") or _uid(env))
        key = f"{mailbox}:{msg_id}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        max_uid = max(max_uid, _uid(env))
        try:
            item = await _classify_one(him, account_id, account_email, env, mailbox)
        except Exception:  # noqa: BLE001
            log.exception("rescan classify failed %s %s in %s", account_id, msg_id, mailbox)
            continue
        if item:
            processed.append(item)
    if max_uid > 0:
        set_cursor(account_id, max_uid)
    return processed


async def rescan_all() -> list[ProcessedMail]:
    him = Himalaya()
    out: list[ProcessedMail] = []
    for acc in load_accounts():
        try:
            out.extend(await rescan_account(him, acc.id, acc.email))
        except Exception:  # noqa: BLE001
            log.exception("rescan failed for %s", acc.id)
    try:
        from .cache import refresh_all

        refresh_all()
    except Exception:  # noqa: BLE001
        log.exception("cache refresh after rescan failed")
    return out
