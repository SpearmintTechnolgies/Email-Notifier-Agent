from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .classifier import Classification, classify_message
from .config import (
    PRIORITY_TO_FOLDER,
    TAG_TO_FOLDER,
    active_account_ids,
    load_accounts,
    load_runtime,
    load_settings,
)
from .db import (
    mark_notified,
    already_notified,
    queue_batch,
    queue_notification,
    set_message_location,
    get_message_locations,
    list_pending_notifications,
    bump_notification_attempt,
    mark_notification_delivered,
    is_message_processed,
    mark_message_processed,
    enqueue_digest_pending,
    mailbox_first_sync_at,
    record_first_sync,
)
from .himalaya_client import Himalaya
from .identity import identity_from_envelope, locate, remember, verify
from .policy import can_organize
from .safe_mode import test_mode_enabled
from .taxonomy import ensure_taxonomy

log = logging.getLogger("mailops.sync")


def _was_simulated(result: Any) -> bool:
    """True when a mutation was a test-mode no-op (nothing really happened)."""
    return str(getattr(result, "stdout", "") or "").startswith("test-mode")

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


def _msg_id(envelope: dict[str, Any]) -> str:
    """Gmail message ID as a string. Never coerced to int — Gmail IDs are
    hexadecimal, so numeric comparison corrupts them."""
    return str(envelope.get("id") or envelope.get("uid") or "")


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
    # Read, classify, cache and notify are always allowed. Anything that
    # touches the mailbox (folders, moves, copies, flags) needs the account's
    # explicit organize policy - Gmail is locked read-only (shared with the
    # Sales-Agent) and Altcoin Informer stays off until approved.
    organize = can_organize(account_id)
    ensure_taxonomy(account_id, him=him)  # no-op for read-only accounts
    runtime = load_runtime()
    settings = load_settings()
    # First-sync seeding (point 10: avoid floods during initial sync + bulk
    # CSV imports). ``mailbox_first_sync_at`` returns None until the first
    # pass has completed; discoveries made during that first pass are added
    # to the digest table as ``seeded=True`` so the scheduler never counts
    # them. Everything found after this moment is a genuinely new email.
    is_first_sync = (
        bool(settings.get("digest_initial_sync_silent", True))
        and mailbox_first_sync_at(account_id) is None
    )
    digest_enabled = bool(settings.get("digest_enabled", True))
    digest_includes_p2 = bool(settings.get("digest_include_p2", False))
    p0_immediate = bool(settings.get("digest_p0_immediate", False))
    # Use the checked variant: an unreachable server must NOT look like an
    # empty inbox. Before this, a dead host made this function return [] via
    # the early return below, sync_once recorded a SUCCESS, and the health
    # subsystem stayed silent while the mailbox went stale for days.
    envelopes = him.envelopes_checked(account_id, "INBOX", page_size=30, page=1)
    if not envelopes:
        # First sync of a brand-new mailbox with an empty inbox: still record
        # the completed-first-pass marker so the next arrival alerts normally.
        if is_first_sync:
            record_first_sync(account_id, 0)
        return []

    # Track Gmail messages by their actual (hexadecimal) IDs — never numeric.
    to_process = [
        e for e in envelopes
        if not is_message_processed(account_id, _msg_id(e))
    ]
    processed: list[ProcessedMail] = []
    for env in to_process:
        msg_id = _msg_id(env)
        # Record what this id points at right now (Message-ID header, subject,
        # sender, date) so later actions on it can be verified (identity.py).
        ident = identity_from_envelope(account_id, "INBOX", env)
        remember(ident)
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
        log.info(
            "classified %s/%s P%s path=%s %s",
            account_id,
            msg_id,
            classification.priority,
            classification.source or "unknown",
            classification.reason,
        )
        folder = PRIORITY_TO_FOLDER.get(classification.priority, "INBOX.P2-Normal")
        # Where the message physically sits after this pass. Stays INBOX when
        # the account is read-only / not approved for organization, or when
        # the move is a test-mode no-op.
        physical_box = "INBOX"
        act_id = msg_id  # the message's id inside ``physical_box``
        if organize:
            if not test_mode_enabled():
                verdict = verify(him, account_id, "INBOX", msg_id, expected=ident)
                if not verdict.ok:
                    log.warning(
                        "skipping %s %s: stale message reference (%s)",
                        account_id, msg_id, verdict.reason,
                    )
                    continue
            move_r = him.move(account_id, msg_id, folder, "INBOX")
            if not move_r.ok:
                # Idempotent retry: if the message is already at the destination
                # (e.g. a previous run moved it but failed before notifying), treat
                # as placed so we re-notify instead of dropping the alert.
                already_there = bool(him.envelopes(account_id, folder, page_size=200))
                if already_there and any(
                    _msg_id(e) == msg_id for e in him.envelopes(account_id, folder, page_size=200)
                ):
                    log.info("message %s already in %s; continuing", msg_id, folder)
                    physical_box = folder
                else:
                    log.warning("move failed %s %s -> %s: %s", account_id, msg_id, folder, move_r.stderr)
                    continue
            elif not _was_simulated(move_r):
                physical_box = folder
                # The destination folder assigns a NEW id: follow the same
                # message (by Message-ID) so copy/flag/alert buttons hit it,
                # never whatever now carries the old id.
                moved = locate(him, ident, folder)
                if moved is not None:
                    act_id = moved.msg_id
                    remember(moved)
                else:
                    act_id = ""
                    log.warning(
                        "moved %s %s to %s but could not re-locate it; skipping tag/flag",
                        account_id, msg_id, folder,
                    )
        # Record the logical destination. For read-only accounts and in test
        # mode no physical move happens, so this row is what lets the
        # dashboard show the classification without touching the mailbox.
        set_message_location(
            account_id,
            msg_id,
            folder,
            subject=str(env.get("subject") or "(no subject)"),
            sender=_sender(env),
            priority=classification.priority,
        )
        if organize and act_id:
            if classification.tags:
                tag = classification.tags[0]
                tag_folder = TAG_TO_FOLDER.get(tag)
                if tag_folder:
                    him.copy(account_id, act_id, tag_folder, physical_box)
            if classification.needs_reply:
                him.flag_add(account_id, act_id, "flagged", physical_box)

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
        min_alert = int(settings.get("min_alert_priority", 1))
        # NEW: digest-first notification path. See mailops.digest and the
        # design doc in db.py for the full rationale. In short:
        #   - EVERY classified message (P0 Critical .. P3 Noise) is recorded
        #     in ``digest_pending`` so the ONE consolidated 15-minute fleet
        #     card can report all four priority categories - mailbox by
        #     mailbox - for the whole fleet (MAILOPS activation redesign).
        #     This NEVER fires a per-email, per-priority or per-mailbox
        #     Telegram message by itself; mailops.digest is still the only
        #     thing that calls Telegram for new mail, at most once a window.
        #   - If this is the mailbox's FIRST sync, every row is enqueued as
        #     ``seeded`` so no catch-up alert goes out when a new mailbox is
        #     connected (point 10, Alien FI regression) - this is exactly
        #     what keeps turning on all 33 (or 500) mailboxes at once silent.
        #   - When ``digest_p0_immediate`` is ON, true P0 still produces an
        #     immediate per-email card (full Reply/Priority/Done buttons);
        #     the row is also added to the digest so the summary agrees
        #     with reality. Default OFF per the "no flood" hard rule.
        #   - The legacy ``pending_notifications`` queue is preserved for
        #     restart recovery but is NEVER filled from this path anymore.
        seed_this = is_first_sync
        if digest_enabled:
            enqueue_digest_pending(
                account_id,
                physical_box,
                act_id or msg_id,
                classification.priority,
                subject=item.subject,
                sender=item.sender,
                seeded=seed_this,
            )
        is_actionable_digest = classification.priority <= 1 or (
            digest_includes_p2 and classification.priority == 2
        )
        if is_actionable_digest:
            # Mark the legacy per-mail dedupe key as notified so retry
            # loops left over from older code paths NEVER fire for it.
            mark_notified(key)
        if classification.priority <= 1 and classification.priority > min_alert:
            # Below the alert floor the user configured; the digest still
            # counts it, but we never trigger an immediate push.
            pass
        elif (
            classification.priority == 0
            and p0_immediate
            and not is_first_sync
            and notify is not None
        ):
            # Opt-in: ONE immediate P0 Critical push with full actions.
            # P1 is never immediate (that was the 12-13 alert flood).
            payload = {
                "type": "new_mail",
                "key": key,
                "account_id": account_id,
                "email": account_email,
                "msg_id": act_id or msg_id,
                "mailbox": physical_box,
                "subject": item.subject,
                "sender": item.sender,
                "priority": item.priority,
                "tags": item.tags,
                "needs_reply": item.needs_reply,
                "reason": item.reason,
            }
            try:
                await notify(payload)
            except Exception:  # noqa: BLE001
                # Immediate P0 is best-effort; the digest still carries the
                # same email so a Telegram hiccup does not lose visibility.
                log.exception(
                    "immediate P0 notify failed for %s %s; digest will carry it",
                    account_id,
                    msg_id,
                )
        elif classification.priority == 2 and not digest_includes_p2:
            # P2 digest off: keep the legacy batch table filled so operators
            # who re-enable per-mail P2 batching later still get it. The
            # sync loop never auto-flushes it anymore (notify.flush_p2_batch
            # is now called only from manual admin actions).
            queue_batch(
                key,
                account_id,
                physical_box,
                act_id or msg_id,
                item.subject,
                item.sender,
                item.priority,
            )
        elif classification.priority >= 3:
            # P3 Noise: always silent, keep legacy dedupe key.
            mark_notified(key)

        # Mark as processed only after successful classification, move, and notification handling.
        mark_message_processed(account_id, msg_id)

    # First-sync completed: record the marker so future discoveries alert.
    # Done AFTER the loop so a crash midway through the first sync leaves
    # the account still 'uninitialised' and the next pass continues to
    # seed silently (correct for 'never flood during initial sync').
    if is_first_sync:
        seeded = sum(1 for p in processed if p.priority <= 1 or (
            digest_includes_p2 and p.priority == 2
        ))
        record_first_sync(account_id, seeded)
        log.info(
            "mailbox %s completed first sync (seeded=%d actionable rows)",
            account_id, seeded,
        )

    return processed


async def sync_once(notify: NotifyFn | None = None) -> list[ProcessedMail]:
    import asyncio as _asyncio

    from . import health

    settings = load_settings()
    # Rate-limit (point 8 of the MAILOPS activation redesign): several of
    # this deployment's mailboxes share a handful of physical SiteGround /
    # cPanel hosts. Sync is already strictly sequential (one account's IMAP
    # session at a time - never concurrent), which alone caps "concurrent
    # IMAP sessions per host" at 1; this optional stagger additionally
    # spaces consecutive logins in time so a 33-500 mailbox fleet never
    # opens a rapid-fire burst of new sessions against the same shared
    # host. 0 (default) = off, used by every unit test.
    stagger = max(0.0, float(settings.get("sync_account_stagger_seconds", 0) or 0))
    him = Himalaya()
    out: list[ProcessedMail] = []
    accounts = load_accounts()
    for i, acc in enumerate(accounts):
        if stagger and i > 0:
            try:
                await _asyncio.sleep(stagger)
            except _asyncio.CancelledError:
                raise
        try:
            out.extend(await process_account(him, acc.id, acc.email, notify))
            # Successful pass: clear any open incident for this account. This
            # is a local DB update ONLY (see mailops.health) — it never sends
            # a Telegram message by itself; the consolidated ops alert does
            # that once per poll, covering every account that changed state.
            try:
                health.record_sync_success(acc.id)
            except Exception:  # noqa: BLE001
                log.exception("health recovery bookkeeping failed for %s", acc.id)
        except Exception as exc:  # noqa: BLE001
            log.exception("sync failed for %s", acc.id)
            try:
                health.record_sync_failure(acc.id, exc)
            except Exception:  # noqa: BLE001
                log.exception("health failure bookkeeping failed for %s", acc.id)
    return out


async def retry_pending_notifications(notify: NotifyFn | None) -> int:
    """Historical per-email alert queue drainer.

    DEPRECATED behaviour (pre-digest): used to replay queued P0/P1 alerts
    one Telegram message per email. That is exactly the flood the digest
    redesign is removing (see Alien FI 12-13 messages regression).

    New behaviour: this function still consumes the ``pending_notifications``
    rows left over from older versions so the table does not grow forever,
    but it NEVER sends an individual Telegram message anymore. Instead each
    queued payload is translated into a ``digest_pending`` row (if it is
    not already covered) so the next consolidated digest reports it, and
    then the pending row is marked delivered.

    Returns the number of rows migrated for log parity.
    """
    migrated = 0
    settings = load_settings()
    digest_enabled = bool(settings.get("digest_enabled", True))
    for row in list_pending_notifications(active_account_ids()):
        key = row["key"]
        try:
            payload = json.loads(row["payload"])
        except Exception:  # noqa: BLE001
            log.error("dropping pending notification with corrupt payload: %s", key)
            mark_notification_delivered(key)
            continue
        if digest_enabled and payload.get("type") == "new_mail":
            prio = int(payload.get("priority", 1))
            if prio <= 1 or (
                bool(settings.get("digest_include_p2", False)) and prio == 2
            ):
                enqueue_digest_pending(
                    payload.get("account_id") or row["account_id"],
                    payload.get("mailbox") or row["mailbox"],
                    payload.get("msg_id") or row["msg_id"],
                    prio,
                    subject=str(payload.get("subject") or ""),
                    sender=str(payload.get("sender") or ""),
                    seeded=False,
                )
                migrated += 1
        # Drop the row regardless: the digest path owns alerting now.
        mark_notification_delivered(key)
    if migrated:
        log.info(
            "migrated %d legacy pending notification(s) into the digest queue",
            migrated,
        )
    return migrated


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
    msg_id = _msg_id(env)
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
    organize = can_organize(account_id)
    ident = identity_from_envelope(account_id, source_mailbox, env)
    remember(ident)
    act_id = msg_id  # the message's id inside ``placed_in``
    if not organize:
        # Read-only account: record the classification only, never move.
        placed_in = dest
    elif dest != source_mailbox:
        if not test_mode_enabled():
            verdict = verify(him, account_id, source_mailbox, msg_id, expected=ident)
            if not verdict.ok:
                log.warning(
                    "rescan skipped %s %s/%s: stale message reference (%s)",
                    account_id, source_mailbox, msg_id, verdict.reason,
                )
                return None
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
        if not _was_simulated(move_r):
            moved = locate(him, ident, dest)  # new id in the destination folder
            if moved is not None:
                act_id = moved.msg_id
                remember(moved)
            else:
                act_id = ""
                log.warning("rescan moved %s %s but could not re-locate it; skipping tag/flag", account_id, msg_id)
    else:
        placed_in = source_mailbox

    set_message_location(
        account_id,
        msg_id,
        placed_in,
        subject=str(env.get("subject") or "(no subject)"),
        sender=_sender(env),
        priority=classification.priority,
    )
    if organize and act_id:
        if classification.tags:
            tag = classification.tags[0]
            tag_folder = TAG_TO_FOLDER.get(tag)
            if tag_folder:
                him.copy(account_id, act_id, tag_folder, placed_in)
        if classification.needs_reply:
            him.flag_add(account_id, act_id, "flagged", placed_in)

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
    seen_keys: set[str] = set()
    for mailbox, env in snapshots:
        msg_id = _msg_id(env)
        key = f"{mailbox}:{msg_id}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        try:
            item = await _classify_one(him, account_id, account_email, env, mailbox)
        except Exception:  # noqa: BLE001
            log.exception("rescan classify failed %s %s in %s", account_id, msg_id, mailbox)
            continue
        if item:
            processed.append(item)
    return processed


async def backfill_message_locations(
    him: Himalaya,
    account_id: str,
) -> int:
    """Record message_locations for messages processed before that table
    existed. Read-only against the mailbox: no moves, copies, flags, or
    notifications. Idempotent — skips anything that already has a location.

    Returns the number of location rows written.
    """
    existing = get_message_locations(account_id)
    # Test mode never moved mail, so unlocated processed mail sits in INBOX.
    # Also scan priority folders in case some moves were real. Snapshot first
    # so a message visible from two folders is only handled once.
    folders = ["INBOX", *PRIORITY_TO_FOLDER.values()]
    snapshots: list[tuple[str, dict[str, Any]]] = []
    for mailbox in folders:
        for env in _all_envelopes(him, account_id, mailbox):
            snapshots.append((mailbox, env))

    runtime = load_runtime()
    seen: set[str] = set()
    written = 0
    for mailbox, env in snapshots:
        msg_id = _msg_id(env)
        if not msg_id or msg_id in seen or msg_id in existing:
            continue
        seen.add(msg_id)
        if not is_message_processed(account_id, msg_id):
            continue  # untouched mail: leave it for the normal sync path
        try:
            card = him.read_card(account_id, msg_id, mailbox)
            preview = f"{card.subject}\n{card.from_line}\n{card.body}"[:2000]
        except Exception:  # noqa: BLE001
            preview = him.read_message(account_id, msg_id, mailbox)[:2000]
        try:
            classification = await classify_message(
                runtime.openai_base_url,
                runtime.openai_api_key,
                env,
                preview,
                runtime.model,
                account_id=account_id,
            )
        except Exception:  # noqa: BLE001
            log.exception("backfill classify failed %s %s", account_id, msg_id)
            continue
        folder = PRIORITY_TO_FOLDER.get(classification.priority, "INBOX.P2-Normal")
        # Record ONLY — no move/copy/flag, no notification.
        set_message_location(
            account_id,
            msg_id,
            folder,
            subject=str(env.get("subject") or "(no subject)"),
            sender=_sender(env),
            priority=classification.priority,
        )
        written += 1
        log.info("backfill %s %s -> %s", account_id, msg_id, folder)
    return written


async def backfill_all_locations(him: Himalaya | None = None) -> dict[str, int]:
    """One-time repair pass across all accounts. Never notifies."""
    if him is None:
        try:
            from .cache import _get_him  # honor an injected provider (tests)

            him = _get_him()
        except Exception:  # noqa: BLE001
            him = Himalaya()
    out: dict[str, int] = {}
    for acc in load_accounts():
        try:
            out[acc.id] = await backfill_message_locations(him, acc.id)
        except Exception:  # noqa: BLE001
            log.exception("backfill failed for %s", acc.id)
            out[acc.id] = 0
    try:
        from .cache import refresh_all

        refresh_all()
    except Exception:  # noqa: BLE001
        log.exception("cache refresh after backfill failed")
    return out


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
