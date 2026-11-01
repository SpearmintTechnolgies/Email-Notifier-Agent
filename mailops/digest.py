"""Consolidated 15-minute Telegram notification digest.

Design (October 2026 Mailops redesign)
======================================

Before: for every classified P0 or P1 email, ``mailops.notify.handle_event``
posted ONE Telegram message per inbox per message, with the full
Open/Reply/Priority/Done/Snooze keyboard. When Alien FI was onboarded that
sent 12-13 individual alerts in a single sync pass; at 90-100 mailboxes
this would mean hundreds of messages per 15-minute poll - unusable.

After (this module): the sync worker records each new actionable
message in the ``digest_pending`` table (see ``mailops.db``) and NEVER
calls ``notify()`` for P0/P1 by default. On a schedule (also 15 minutes,
piggybacked on the existing sync loop so no second scheduler is created)
``build_and_send_digest`` groups the pending rows by mailbox, posts ONE
summary to the authorized home targets, and marks the covered rows as
delivered so no email is ever counted in two digests.

Guarantees
----------
* Idempotent across restarts: pending rows survive process crashes; a
  ``digest_state`` single-flight token stops two concurrent builds (manual
  Refresh + scheduled tick) from double-sending the same window.
* 'No new mail => no message': empty digest windows send nothing.
* First-ever sync of a mailbox is silent: those rows are flagged seeded
  by the sync worker and excluded from the digest counts.
* Per-mailbox notification toggle (``digest_mailboxes_disabled`` in
  settings.json) hides a mailbox from the digest without disabling sync.
* Telegram rate-limits are respected: ONE edit/send per digest window per
  target, batched sequentially with the existing ``send_to_home`` helper.

Buttons
-------
The group digest keyboard carries exactly the four actions the redesign
spec names (point 5):

    Open Inbox        -> private deep link into the top-pending inbox
    View Critical     -> jumps to Dashboard P0 view in the current chat
    Select Mailbox    -> paginated list of mailboxes with pending counts
    Refresh Summary   -> manual re-emit (dedupe: covers the same rows,
                         does not re-notify emails already covered by the
                         last successful digest)

Individual Open/Reply/Priority/Done/Snooze keyboards remain unchanged and
are shown ONLY when an admin opens a specific email card from one of
those entry points.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from .config import (
    active_account_ids,
    digest_mailbox_enabled,
    load_accounts,
    load_settings,
)
from .db import (
    count_digest_pending,
    digest_state_get,
    digest_state_set,
    list_undelivered_digest_rows,
    mark_digest_delivered,
    purge_delivered_digest_rows,
)

log = logging.getLogger("mailops.digest")

# Single-flight lock prevents two overlapping ticks from double-sending.
_build_lock = asyncio.Lock()

# ``digest_state`` keys
_LAST_RUN_KEY = "last_run_at"
_LAST_RUN_TOKEN_KEY = "last_run_token"
_LAST_COUNT_KEY = "last_window_count"
_LAST_MESSAGE_KEY = "last_message_summary"
# Per-chat {chat_id: message_id} of the living control card, so later
# windows EDIT the same Telegram message instead of sending a new one
# (phase 3 of the MAILOPS activation redesign - "update the previous
# control card in place"). See ``mailops.notify.send_or_edit_home``.
_CARD_REF_KEY = "digest_card_message_refs"

# How many top mailboxes to list inline in the digest body before 'and X more'.
# More than this and the message starts hitting Telegram's 4096-char cap.
MAX_INLINE_MAILBOXES = 15


@dataclass
class MailboxBucket:
    """Per-mailbox slice of the pending digest window."""

    account_id: str
    display_name: str
    email: str
    p0: int = 0
    p1: int = 0
    p2: int = 0
    p3: int = 0
    keys: list[str] = field(default_factory=list)
    top_subjects: list[tuple[int, str]] = field(default_factory=list)

    @property
    def actionable(self) -> int:
        return self.p0 + self.p1

    @property
    def total_new(self) -> int:
        return self.p0 + self.p1 + self.p2 + self.p3

    @property
    def label(self) -> str:
        return self.display_name or self.account_id


@dataclass
class DigestWindow:
    """Everything a single digest run needs to render + confirm."""

    buckets: list[MailboxBucket]
    total_p0: int
    total_p1: int
    total_actionable: int
    all_keys: list[str]
    window_started_at: float
    window_ended_at: float
    # All-four-priorities totals (MAILOPS activation redesign). Kept
    # separate from ``total_actionable`` (still strictly P0+P1, unchanged)
    # so "do we need to push" logic never changes while the card can still
    # report Normal/Noise volume for the whole fleet.
    total_p2: int = 0
    total_p3: int = 0

    @property
    def total_new(self) -> int:
        return self.total_p0 + self.total_p1 + self.total_p2 + self.total_p3

    @property
    def mailboxes_with_new(self) -> int:
        return len(self.buckets)

    @property
    def is_empty(self) -> bool:
        """True iff literally nothing new arrived this window, across all
        four priorities. This is the signal used to decide whether an
        unprompted push is warranted at all; when True the card is, at
        most, refreshed in place (see ``build_and_send_digest``) rather
        than triggering a brand-new Telegram notification."""
        return self.total_new == 0


def _display_for(account_id: str) -> tuple[str, str]:
    """(display_name, email) for the active account, or ("", "") if gone."""
    for acc in load_accounts():
        if acc.id == account_id:
            return acc.display_name or acc.id, acc.email
    return "", ""


def collect_window(
    settings: dict[str, Any] | None = None,
) -> DigestWindow:
    """Group undelivered digest rows into per-mailbox buckets.

    Pure read: safe to call from a 'Preview' button without side-effects.

    Fetches ALL FOUR priorities (P0 Critical .. P3 Noise) unconditionally -
    the MAILOPS fleet redesign requires every priority category to be
    represented in the consolidated card. ``total_actionable`` (what decides
    whether a window is "empty" / worth an unprompted push) is unchanged:
    strictly P0 + P1, exactly as before ``digest_include_p2`` existed.
    """
    s = settings if settings is not None else load_settings()
    actives = active_account_ids()
    rows = list_undelivered_digest_rows(
        account_ids=actives,
        max_priority=3,
        include_seeded=False,
        limit=50000,
    )
    # Also filter out mailboxes the operator has muted from the digest.
    rows = [r for r in rows if digest_mailbox_enabled(str(r["account_id"]), s)]

    buckets: dict[str, MailboxBucket] = {}
    total_p0 = total_p1 = total_p2 = total_p3 = 0
    all_keys: list[str] = []
    started = time.time()
    ended = 0.0
    for r in rows:
        aid = str(r["account_id"])
        bucket = buckets.get(aid)
        if bucket is None:
            name, email = _display_for(aid)
            bucket = MailboxBucket(account_id=aid, display_name=name, email=email)
            buckets[aid] = bucket
        prio = int(r["priority"])
        if prio == 0:
            bucket.p0 += 1
            total_p0 += 1
        elif prio == 1:
            bucket.p1 += 1
            total_p1 += 1
        elif prio == 2:
            bucket.p2 += 1
            total_p2 += 1
        elif prio >= 3:
            bucket.p3 += 1
            total_p3 += 1
        bucket.keys.append(str(r["key"]))
        all_keys.append(str(r["key"]))
        subject = str(r.get("subject") or "").strip() or "(no subject)"
        bucket.top_subjects.append((prio, subject))
        discovered = float(r.get("discovered_at") or 0)
        if discovered and discovered < started:
            started = discovered
        if discovered > ended:
            ended = discovered

    # Order mailboxes by actionable desc, then P0 desc, then name.
    ordered = sorted(
        buckets.values(),
        key=lambda b: (-b.actionable, -b.p0, b.label.lower()),
    )
    return DigestWindow(
        buckets=ordered,
        total_p0=total_p0,
        total_p1=total_p1,
        total_actionable=total_p0 + total_p1,
        all_keys=all_keys,
        window_started_at=started if ordered else time.time(),
        window_ended_at=ended if ordered else time.time(),
        total_p2=total_p2,
        total_p3=total_p3,
    )


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


def format_digest(
    window: DigestWindow,
    pending_counts: list[Any] | None = None,
    *,
    unhealthy_count: int = 0,
    test_tag: str = "",
) -> str:
    """Human-readable digest text (plain, no Markdown - safer for Telegram).

    ``pending_counts`` is an optional list of per-account objects exposing
    ``account_id``/``label`` (or ``display_name``)/``email``/``p0``/``p1``/
    ``p2``/``p3`` - e.g. ``mailops.cache.AccountCounts`` - representing the
    CURRENT, persisted classification state of every monitored mailbox
    (distinction "B" of the fleet redesign: pending/unhandled mail, as
    opposed to "A", what is NEW in this 15-minute window). Passing ``None``
    (the default; every existing caller/test does this) renders only the
    new-window section, exactly as before.
    """
    s = load_settings()
    interval = int(s.get("digest_interval_seconds", 900))
    header = "[TEST] " if test_tag else ""
    header += f"Mailops control card - every {_fmt_duration(interval)}"
    total_accounts = len(active_account_ids())
    lines = [header, ""]
    # --- A: new mail discovered during the last completed window --------
    lines.append(
        f"New this window: {window.total_new} email(s) across "
        f"{window.mailboxes_with_new}/{total_accounts} monitored mailbox(es)"
    )
    lines.append(
        f"  Critical {window.total_p0} | Important {window.total_p1} | "
        f"Normal {window.total_p2} | Noise {window.total_p3}"
    )
    lines.append(f"  (of which actionable P0+P1: {window.total_actionable})")
    # --- B: current pending / unhandled, from persisted classification ---
    if pending_counts is not None:
        pp0 = sum(getattr(c, "p0", 0) for c in pending_counts)
        pp1 = sum(getattr(c, "p1", 0) for c in pending_counts)
        pp2 = sum(getattr(c, "p2", 0) for c in pending_counts)
        pp3 = sum(getattr(c, "p3", 0) for c in pending_counts)
        lines.append("")
        lines.append(
            f"Pending / unhandled (persisted, not 'new'): "
            f"Critical {pp0} | Important {pp1} | Normal {pp2} | Noise {pp3}"
        )
    lines.append("")
    lines.append(f"Monitored mailboxes: {total_accounts}")
    lines.append(f"Unhealthy mailboxes: {unhealthy_count}")
    if test_tag:
        lines.append(f"({test_tag})")
    lines.append("")
    if window.is_empty and pending_counts is None:
        lines.append("No new email in this window.")
        return "\n".join(lines)
    # --- New mail this window, mailbox by mailbox (only mailboxes that
    # actually had something new - up to MAX_INLINE_MAILBOXES, with up to
    # two example subjects each so an operator can triage without opening
    # the inbox for low-stakes items).
    if window.buckets:
        lines.append("New mail by mailbox this window:")
        shown_new = window.buckets[:MAX_INLINE_MAILBOXES]
        for b in shown_new:
            parts = [f"P0 {b.p0}", f"P1 {b.p1}"]
            if b.p2:
                parts.append(f"P2 {b.p2}")
            if b.p3:
                parts.append(f"P3 {b.p3}")
            lines.append(f"- {b.label}  <{b.email}>")
            lines.append("    " + " | ".join(parts) + f"  (total {b.total_new})")
            for prio, subj in b.top_subjects[:2]:
                tag = {0: "P0", 1: "P1", 2: "P2"}.get(prio, "P3")
                lines.append(f"      {tag}: {subj[:80]}")
        if len(window.buckets) > MAX_INLINE_MAILBOXES:
            extra = len(window.buckets) - MAX_INLINE_MAILBOXES
            lines.append(f"- ... and {extra} more mailbox(es) with new mail")
        lines.append("")
    # --- Full-fleet mailbox breakdown (B): every monitored mailbox,
    # Critical/Important first, zero-new ones included when we have the
    # persisted pending snapshot.
    lines.append("Mailbox breakdown (Critical | Important | Normal | Noise):")
    breakdown_rows = _breakdown_rows(window, pending_counts)
    total_rows = len(breakdown_rows)
    budget = 3600  # leave headroom for the footer + the final hard cap below
    shown: list[str] = []
    used = len("\n".join(lines))
    count_shown = 0
    for row_text in breakdown_rows:
        if count_shown >= MAX_INLINE_MAILBOXES:
            break
        candidate_len = used + len(row_text) + 1
        if candidate_len > budget:
            break
        shown.append(row_text)
        used = candidate_len
        count_shown += 1
    lines.extend(shown)
    if total_rows > count_shown:
        extra = total_rows - count_shown
        lines.append(f"- ... and {extra} more mailbox(es) not shown here")
    lines.append("")
    lines.append(
        "Tap View All Mailboxes to browse every monitored inbox "
        "(including zero-new ones), or Select Mailbox to drill in."
    )
    text = "\n".join(lines)
    if len(text) > 4096:
        text = text[:4090] + "\n…"
    return text


def _breakdown_rows(window: DigestWindow, pending_counts: list[Any] | None) -> list[str]:
    """One compact line per mailbox, Critical/Important first.

    Uses the full-fleet ``pending_counts`` snapshot when available (so EVERY
    monitored mailbox appears, including ones with zero new mail this
    window - point 4 of the redesign); otherwise falls back to the
    new-window buckets only (legacy/test behaviour, mailboxes with nothing
    new are simply absent).
    """
    if pending_counts is not None:
        ranked = sorted(
            pending_counts,
            key=lambda c: (
                -(getattr(c, "p0", 0) + getattr(c, "p1", 0)),
                -getattr(c, "p0", 0),
                str(getattr(c, "label", None) or getattr(c, "display_name", "") or getattr(c, "account_id", "")).lower(),
            ),
        )
        out = []
        for c in ranked:
            label = getattr(c, "label", None) or getattr(c, "display_name", "") or c.account_id
            out.append(
                f"- {label}: Critical {getattr(c, 'p0', 0)} | Important {getattr(c, 'p1', 0)} "
                f"| Normal {getattr(c, 'p2', 0)} | Noise {getattr(c, 'p3', 0)}"
            )
        return out
    out = []
    for b in window.buckets:
        out.append(
            f"- {b.label}: Critical {b.p0} | Important {b.p1} | Normal {b.p2} | Noise {b.p3}"
        )
    return out


def digest_keyboard(
    window: DigestWindow,
    pending_counts: list[Any] | None = None,
    *,
    unhealthy_count: int = 0,
    bot_username: str | None = None,
) -> InlineKeyboardMarkup:
    """Full interactive control-card keyboard (MAILOPS activation redesign,
    phase 5): Open Inbox, View All Mailboxes, Select Mailbox, View
    Critical/Important/Normal/Noise, Check Unhealthy Accounts, Refresh
    Summary and Return to Dashboard. Every callback here is in the
    group-safe ``dg:*`` / ``health:*`` / ``dash`` namespace (see
    ``mailops.bot.app.GROUP_CALLBACK_PREFIXES`` and
    ``GROUP_CALLBACK_EXACT``) so no button posted to a group ever routes to
    a private-only callback.

    'View Critical/Important/Normal/Noise' counts prefer the full-fleet
    ``pending_counts`` snapshot (current/unhandled - what actually shows up
    when the button is tapped) and fall back to the new-window totals when
    that snapshot was not supplied (back-compat with existing callers).
    """
    if pending_counts is not None:
        c0 = sum(getattr(c, "p0", 0) for c in pending_counts)
        c1 = sum(getattr(c, "p1", 0) for c in pending_counts)
        c2 = sum(getattr(c, "p2", 0) for c in pending_counts)
        c3 = sum(getattr(c, "p3", 0) for c in pending_counts)
    else:
        c0, c1, c2, c3 = window.total_p0, window.total_p1, window.total_p2, window.total_p3
    rows: list[list[InlineKeyboardButton]] = []
    top = window.buckets[0] if window.buckets else None
    first_row: list[InlineKeyboardButton] = []
    if top is not None:
        first_row.append(
            InlineKeyboardButton(
                f"Open Inbox: {top.label[:24]}",
                callback_data=f"dg:open:{top.account_id}",
            )
        )
    else:
        first_row.append(
            InlineKeyboardButton("Open Inbox", callback_data="dg:open:_top")
        )
    first_row.append(
        InlineKeyboardButton("Refresh Summary", callback_data="dg:refresh")
    )
    rows.append(first_row)
    rows.append(
        [
            InlineKeyboardButton(f"View Critical ({c0})", callback_data="dg:crit"),
            InlineKeyboardButton(f"View Important ({c1})", callback_data="dg:imp"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(f"View Normal ({c2})", callback_data="dg:norm"),
            InlineKeyboardButton(f"View Noise ({c3})", callback_data="dg:noise"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("View All Mailboxes", callback_data="dg:all:0"),
            InlineKeyboardButton("Select Mailbox", callback_data="dg:pick:0"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                f"Check Unhealthy Accounts ({unhealthy_count})", callback_data="health:view"
            ),
            InlineKeyboardButton("Return to Dashboard", callback_data="dash"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def select_mailbox_keyboard(
    page: int = 0, settings: dict[str, Any] | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    """Paginated 'Select Mailbox' screen.

    Point 9: 90-100 accounts cannot fit on one screen, so we paginate at
    the configured page size (default 20 - fits a mobile and desktop
    Telegram screen with Prev / Next / Page X/Y controls).

    What appears
    ------------
    * EVERY registered, non-muted mailbox the admin manages, sorted by
      pending count (most-urgent first) then by display name. Mailboxes
      with zero actionable rows still appear so the admin can drill into
      an empty inbox on purpose - this is the primary 'pick an inbox'
      entry point for the group control panel.
    * Mailboxes muted from the digest (``digest_mailboxes_disabled``) are
      hidden here: that is exactly what the per-mailbox mute toggle in
      Notification Policy exists to do, and surfacing them would defeat
      the point of muting.
    * The pending-count badge reads ``(n new)`` when the window has
      actionable mail for that inbox and ``(no new)`` otherwise, so the
      operator can see at a glance which mailboxes are busy without
      opening each one.
    """
    s = settings if settings is not None else load_settings()
    page_size = max(5, int(s.get("digest_mailbox_page_size", 20)))
    counts = count_digest_pending(active_account_ids())
    accounts = [a for a in load_accounts() if digest_mailbox_enabled(a.id, s)]
    # (account_id, display_name, email, pending_count)
    entries = [
        (a.id, (a.display_name or a.id), a.email, int(counts.get(a.id, 0)))
        for a in accounts
    ]
    # Busiest inboxes first, then alphabetical by display name so a 90+
    # inbox fleet is still deterministic.
    entries.sort(key=lambda row: (-row[3], row[1].lower()))
    total = len(entries)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    slice_ = entries[start : start + page_size]
    rows: list[list[InlineKeyboardButton]] = []
    for aid, name, _email, n in slice_:
        badge = f"({n} new)" if n > 0 else "(no new)"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{name[:28]}  {badge}",
                    callback_data=f"dg:open:{aid}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"dg:pick:{page - 1}"))
    nav.append(
        InlineKeyboardButton(f"Page {page + 1}/{pages}", callback_data="noop")
    )
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("Next", callback_data=f"dg:pick:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton("Refresh Summary", callback_data="dg:refresh"),
            InlineKeyboardButton("Dashboard", callback_data="dash"),
        ]
    )
    with_mail = sum(1 for e in entries if e[3] > 0)
    if total == 0:
        text = (
            "Select Mailbox\n\n"
            "No mailboxes registered yet. Tap Mailboxes from the dashboard "
            "to onboard the first one."
        )
    else:
        text = (
            f"Select Mailbox - page {page + 1}/{pages}\n"
            f"{total} inbox(es) available, {with_mail} with new mail.\n"
            f"Tap an inbox to open its Priority view in this chat."
        )
    return text, InlineKeyboardMarkup(rows)


def all_mailboxes_keyboard(
    page: int = 0,
    settings: dict[str, Any] | None = None,
    pending_counts: list[Any] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Paginated 'View All Mailboxes' screen (phase 4 of the redesign).

    Unlike ``select_mailbox_keyboard`` (a tappable list used to pick ONE
    inbox to drill into), this screen's TEXT is the payload: every
    monitored mailbox's full Critical/Important/Normal/Noise breakdown,
    Critical/Important-first, paginated so 50-500+ mailboxes never blow
    past Telegram's 4096-char message cap and never requires more than
    ONE edited/sent message to browse the entire fleet - browsing never
    sends 500 separate group messages, only paginates ONE.
    """
    s = settings if settings is not None else load_settings()
    page_size = max(5, int(s.get("digest_mailbox_page_size", 20)))
    if pending_counts is None:
        try:
            from .cache import get_counts

            pending_counts = get_counts(force=False)
        except Exception:  # noqa: BLE001
            pending_counts = None
    if pending_counts is not None:
        rows_src = [
            (
                getattr(c, "account_id", ""),
                str(
                    getattr(c, "label", None)
                    or getattr(c, "display_name", "")
                    or getattr(c, "account_id", "")
                ),
                int(getattr(c, "p0", 0)),
                int(getattr(c, "p1", 0)),
                int(getattr(c, "p2", 0)),
                int(getattr(c, "p3", 0)),
            )
            for c in pending_counts
        ]
    else:
        accounts = [a for a in load_accounts() if digest_mailbox_enabled(a.id, s)]
        counts = count_digest_pending(active_account_ids())
        rows_src = [
            (a.id, (a.display_name or a.id), 0, 0, int(counts.get(a.id, 0)), 0)
            for a in accounts
        ]
    # Critical/Important first (point 4), then alphabetical for determinism.
    rows_src.sort(key=lambda r: (-(r[2] + r[3]), -r[2], r[1].lower()))
    total = len(rows_src)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    slice_ = rows_src[start : start + page_size]
    body_lines = [
        f"All mailboxes - page {page + 1}/{pages} ({total} total):",
        "",
    ]
    for aid, name, p0, p1, p2, p3 in slice_:
        body_lines.append(
            f"- {name[:40]}: Critical {p0} | Important {p1} | Normal {p2} | Noise {p3}"
        )
    if total == 0:
        body_lines.append("No mailboxes registered yet.")
    text = "\n".join(body_lines)
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"dg:all:{page - 1}"))
    nav.append(InlineKeyboardButton(f"Page {page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("Next", callback_data=f"dg:all:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton("Select Mailbox", callback_data="dg:pick:0"),
            InlineKeyboardButton("Refresh Summary", callback_data="dg:refresh"),
        ]
    )
    rows.append([InlineKeyboardButton("Return to Dashboard", callback_data="dash")])
    return text, InlineKeyboardMarkup(rows)


SendFn = Callable[[str, InlineKeyboardMarkup | None], Awaitable[None]]


def _should_run(now: float, settings: dict[str, Any]) -> bool:
    """True iff the next scheduled window is due.

    The caller is already inside the sync loop (which fires every
    ``sync_interval_seconds``). Returning False here keeps the sync loop
    going without emitting a digest, which happens when:
      - the digest is disabled;
      - ``digest_interval_seconds`` is larger than ``sync_interval_seconds``
        and we are not yet at a window boundary (prevents sync=5m,
        digest=15m from firing three times);
      - another coroutine is mid-build (``_build_lock``).
    """
    if not bool(settings.get("digest_enabled", True)):
        return False
    interval = max(60, int(settings.get("digest_interval_seconds", 900)))
    try:
        last = float(digest_state_get(_LAST_RUN_KEY, "0") or 0)
    except ValueError:
        last = 0.0
    if last == 0.0:
        return True
    # Allow a small clock-skew / scheduler drift window so an interval of
    # 900s does not get delayed to 901s and skipped forever.
    return (now - last) >= (interval - 5)


async def build_and_send_digest(
    bot: Bot | None,
    *,
    force: bool = False,
    reason: str = "scheduled",
    test_tag: str = "",
) -> dict[str, Any]:
    """Compute the window and deliver AT MOST ONE Telegram control-card
    notification for it (MAILOPS activation redesign, phases 2/3/6).

    Returns a small diagnostic dict so callers can log + surface results in
    tests and the live-verify script:

        {
          "sent": bool,
          "reason": "scheduled" | "manual" | "test",
          "actionable": int,
          "mailboxes": int,
          "covered_keys": int,
          "skipped": "" | "disabled" | "empty" | "locked" | "no_bot",
          "edited": int,
        }

    Delivery model
    --------------
    * Non-empty window (new P0/P1, or P2/P3 when the operator cares):
      exactly one Telegram push per target - a brand-new ``send_message``
      the very first time, an in-place ``edit_message_text`` on every
      later occasion (Telegram does not notify on an edit, so this still
      counts as "the" one scheduled notification for the window, not an
      extra one). Covered rows (ALL priorities, not just P0/P1) are marked
      delivered so nothing is ever double-counted across windows.
    * Empty window (nothing new at all): the card is refreshed IN PLACE
      (``edit_only=True`` - no brand-new message is ever created just to
      say "no change") so its health/pending numbers stay current; this
      does not count as a notification and never fails loudly - a failed
      cosmetic edit just leaves the previous card on screen.
    * A genuine Telegram delivery failure on the non-empty path leaves
      every covered row pending so the next tick retries the SAME window
      (``digest_pending`` rows are never marked delivered until Telegram
      actually confirms) - no failed delivery is ever silently discarded.

    When ``force`` is True the scheduling gate (interval not elapsed) is
    skipped - this is how the operator's Refresh Summary button works.
    ``test_tag`` prefixes the message with ``[TEST]`` so a manual
    verification send into the production group is clearly labelled.
    """
    settings = load_settings()
    now = time.time()
    diag = {
        "sent": False,
        "reason": reason,
        "actionable": 0,
        "mailboxes": 0,
        "covered_keys": 0,
        "skipped": "",
        "edited": 0,
    }
    if not force and not _should_run(now, settings):
        diag["skipped"] = "disabled" if not settings.get("digest_enabled", True) else "not_due"
        return diag
    if _build_lock.locked():
        diag["skipped"] = "locked"
        return diag
    async with _build_lock:
        token = secrets.token_hex(4)
        window = collect_window(settings)
        diag["actionable"] = window.total_actionable
        diag["mailboxes"] = len(window.buckets)
        pending_counts, unhealthy = _fleet_snapshot()
        edit_in_place = bool(settings.get("digest_edit_in_place", True))
        if window.is_empty and not test_tag:
            # Point 7 / hard rule: nothing new => no NEW Telegram message.
            # Still refresh the existing card in place (if any) so its
            # pending/unhealthy numbers do not go stale, and still advance
            # the run marker so the next tick is scheduled normally.
            digest_state_set(_LAST_RUN_KEY, str(now))
            digest_state_set(_LAST_RUN_TOKEN_KEY, token)
            digest_state_set(_LAST_COUNT_KEY, "0")
            diag["skipped"] = "empty"
            if bot is not None and edit_in_place:
                try:
                    text = format_digest(
                        window, pending_counts, unhealthy_count=unhealthy, test_tag=test_tag
                    )
                    markup = digest_keyboard(
                        window, pending_counts, unhealthy_count=unhealthy
                    )
                    from .notify import send_or_edit_home

                    result = await send_or_edit_home(
                        bot,
                        text,
                        reply_markup=markup,
                        group_markup=markup,
                        group_text=text,
                        state_key=_CARD_REF_KEY,
                        edit_only=True,
                    )
                    diag["edited"] = result.get("edited", 0)
                except Exception:  # noqa: BLE001
                    log.debug("cosmetic empty-window card refresh failed", exc_info=True)
            return diag
        if bot is None:
            diag["skipped"] = "no_bot"
            return diag
        text = format_digest(
            window, pending_counts, unhealthy_count=unhealthy, test_tag=test_tag
        )
        markup = digest_keyboard(window, pending_counts, unhealthy_count=unhealthy)
        # Import locally to avoid an import cycle at module load.
        from .notify import send_or_edit_home, send_to_home

        try:
            if edit_in_place:
                result = await send_or_edit_home(
                    bot,
                    text,
                    reply_markup=markup,
                    group_markup=markup,
                    group_text=text,
                    state_key=_CARD_REF_KEY,
                )
                diag["edited"] = result.get("edited", 0)
            else:
                await send_to_home(
                    bot,
                    text,
                    reply_markup=markup,
                    group_markup=markup,
                    group_text=text,
                )
        except Exception:  # noqa: BLE001
            # Delivery failed on ALL targets. Rows stay undelivered so the
            # next tick retries exactly the same window - point 10.
            log.exception("digest delivery failed; rows remain pending")
            raise
        # Confirmed on at least one target: mark the covered rows delivered.
        # ALL FOUR priorities are covered now, not only P0/P1, so Normal and
        # Noise rows are never double-counted across consecutive windows.
        covered = mark_digest_delivered(window.all_keys)
        diag["covered_keys"] = covered
        diag["sent"] = True
        digest_state_set(_LAST_RUN_KEY, str(now))
        digest_state_set(_LAST_RUN_TOKEN_KEY, token)
        digest_state_set(_LAST_COUNT_KEY, str(window.total_actionable))
        digest_state_set(_LAST_MESSAGE_KEY, text[:3800])
        # Housekeeping: trim delivered rows older than the retention window.
        try:
            retention = float(
                settings.get("digest_delivered_retention_seconds", 7 * 24 * 3600)
            )
            purge_delivered_digest_rows(retention)
        except Exception:  # noqa: BLE001
            log.debug("digest retention purge failed", exc_info=True)
        log.info(
            "digest sent reason=%s mailboxes=%d actionable=%d covered=%d edited=%s",
            reason, diag["mailboxes"], diag["actionable"], covered, diag["edited"],
        )
        return diag


def _fleet_snapshot() -> tuple[list[Any] | None, int]:
    """(pending_counts, unhealthy_count) for the full monitored fleet.

    Pure read, best-effort: a cache or health-table hiccup must never stop
    the digest from sending - it just falls back to the new-window-only
    view (``pending_counts=None``) and ``unhealthy=0``.
    """
    pending_counts: list[Any] | None = None
    unhealthy = 0
    try:
        from .cache import get_counts

        pending_counts = get_counts(force=False)
    except Exception:  # noqa: BLE001
        log.debug("fleet pending-count snapshot failed", exc_info=True)
    try:
        from .db import list_open_incidents

        unhealthy = len(list_open_incidents())
    except Exception:  # noqa: BLE001
        log.debug("fleet unhealthy-count snapshot failed", exc_info=True)
    return pending_counts, unhealthy


def last_run_summary() -> dict[str, Any]:
    """Readback for the Notification Policy screen."""
    return {
        "last_run_at": float(digest_state_get(_LAST_RUN_KEY, "0") or 0),
        "last_count": int(digest_state_get(_LAST_COUNT_KEY, "0") or 0),
        "last_message": digest_state_get(_LAST_MESSAGE_KEY, ""),
    }


__all__ = [
    "MailboxBucket",
    "DigestWindow",
    "collect_window",
    "format_digest",
    "digest_keyboard",
    "select_mailbox_keyboard",
    "build_and_send_digest",
    "last_run_summary",
]
