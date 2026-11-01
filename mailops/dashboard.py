"""Priority dashboard + page text (reads from cache)."""
from __future__ import annotations

from .cache import AccountCounts, cache_stamp, get_bucket, get_counts
from .config import load_accounts, load_settings
from .policy import describe, organizes_physically

PAGE_SIZE = 10

# re-export for callers
__all__ = [
    "PAGE_SIZE",
    "AccountCounts",
    "collect_counts",
    "collect_bucket",
    "format_dashboard",
    "format_page",
    "page_slice",
    "purposes_status",
]


def collect_counts(*, force: bool = False) -> list[AccountCounts]:
    return get_counts(force=force)


def collect_bucket(
    priority: int,
    account_id: str | None = None,
    *,
    force: bool = False,
    limit: int = 200,
) -> list[dict[str, str]]:
    return get_bucket(priority, account_id, force=force)[:limit]


def purposes_status() -> tuple[bool, list[str]]:
    settings = load_settings()
    purposes = settings.get("account_purposes") or {}
    missing = []
    for acc in load_accounts():
        if not (purposes.get(acc.id) or "").strip():
            missing.append(acc.id)
    overall = (settings.get("inbox_context") or "").strip()
    confirmed = bool(settings.get("context_confirmed"))
    return confirmed and not missing and bool(overall), missing


def format_dashboard(
    counts: list[AccountCounts],
    *,
    force_note: str = "",
    group: bool = False,
    summaries_only: bool = False,
) -> str:
    total_p0 = sum(c.p0 for c in counts)
    total_p1 = sum(c.p1 for c in counts)
    ok, missing = purposes_status()
    single = len(counts) == 1
    lines = [
        f"{counts[0].label} · Priority dashboard" if single else "Priority dashboard",
        cache_stamp(),
        f"Pending: Critical {total_p0} · Important {total_p1}",
        "",
    ]
    # Be explicit when numbers are last-known rather than live. Silence here
    # is what let 32 unreachable mailboxes look healthy for 28+ hours.
    try:
        from .cache import unreachable_accounts

        down = unreachable_accounts()
    except Exception:  # noqa: BLE001
        down = []
    if down:
        shown = ", ".join(down[:4]) + (" ..." if len(down) > 4 else "")
        lines.append(
            f"WARNING {len(down)} mailbox(es) unreachable — counts are "
            f"last-known, not live: {shown}"
        )
        lines.append("")
    if force_note:
        lines.append(force_note)
        lines.append("")
    logical = False
    for c in counts:
        lines.append(f"{c.label if single else c.account_id}  <{c.email}>")
        lines.append(
            f"  Critical {c.p0} · Important {c.p1} · Normal {c.p2} · Noise {c.p3}"
        )
        try:
            lines.append(f"  Mode: {describe(c.account_id)}")
            logical = logical or not organizes_physically(c.account_id)
        except Exception:  # noqa: BLE001 - never break the dashboard over a label
            pass
    if logical:
        lines.append("")
        lines.append(
            "Counts follow Mailops' classification; mail in read-only, "
            "not-yet-approved or test-mode accounts is not moved."
        )
    lines.append("")
    if group:
        lines.append("Group view: counts and subjects only.")
        lines.append(
            "Tap Critical / Important / All Emails to read mail here in this group."
        )
        lines.append(
            "Mailbox credentials and admin settings stay in a private chat with the bot."
        )
        return "\n".join(lines)
    if summaries_only:
        lines.append("Summary view: counts and subjects only.")
        lines.append("Opening bodies, drafts and sending require a higher role.")
        return "\n".join(lines)
    if not ok:
        if missing:
            lines.append("Inbox purposes missing: " + ", ".join(missing))
        lines.append("Tap Set inbox purposes so ranking matches what you care about.")
    else:
        lines.append("Inbox purposes set — used for importance ranking.")
    lines.append("")
    lines.append("Open Critical / Important = browse 10 emails per page.")
    lines.append("Manual refresh = pull live IMAP now (hourly job also auto-caches).")
    return "\n".join(lines)


def page_slice(items: list[dict[str, str]], page: int) -> tuple[list[dict[str, str]], int, int]:
    total = len(items)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE) if total else 1
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    return items[start : start + PAGE_SIZE], page, total_pages


def format_page(
    title: str,
    items: list[dict[str, str]],
    page: int,
    total_pages: int,
    total_count: int,
    *,
    group: bool = False,
    can_open: bool = True,
) -> str:
    """Render one page of an email list.

    ``group`` / ``can_open`` are deliberately SEPARATE flags. They used to be
    conflated into a single ``group`` bool, which is what produced the live
    group-reading failure (Oct 25, 2026): a group list that really did carry
    working ``gr:n:<N>`` Open buttons still printed "Open an email in a
    private chat with the bot", because the caller passed
    ``group=(in_group or not can_read)``. Now:

    * ``can_open=False`` (viewer role, anywhere): summaries-only copy, and
      the caller renders no Open buttons at all.
    * ``group=True, can_open=True``: group-first reading copy - the Open
      buttons on this very keyboard read the email in THIS chat.
    * private chat: the original button guide.
    """
    lines = [
        title,
        f"Page {page + 1} of {total_pages} · {total_count} emails",
        cache_stamp(),
        "",
    ]
    if not items:
        lines.append("(empty)")
        return "\n".join(lines)
    single = len(load_accounts()) == 1  # one inbox: no per-line account tag
    for i, it in enumerate(items, start=1):
        tag = "" if single else f"[{it['account_id']}] "
        lines.append(f"{i}. {tag}{it['subject']}")
        lines.append(f"    from {it['from']}")
    lines.append("")
    if not can_open:
        lines.append("Summary view: counts and subjects only.")
        lines.append("Opening bodies, drafts and sending require a higher role.")
        return "\n".join(lines)
    if group:
        lines.append("Button guide:")
        lines.append("· Open 1 … Open 10 — read that email here in this group")
        lines.append("· 🔒 N (private) — restricted mailbox: opens privately instead")
        lines.append("· Previous page / Next page — flip the list")
        lines.append("· Inside email: Reply · Back to list · Previous/Next email")
        return "\n".join(lines)
    lines.append("Button guide:")
    lines.append("· Open 1 … Open 10 — open that email")
    lines.append("· Previous page / Next page — flip the list")
    lines.append("· Inside email: Reply · Done · Previous/Next email")
    return "\n".join(lines)
