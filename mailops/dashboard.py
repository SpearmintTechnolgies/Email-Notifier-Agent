"""Priority dashboard + page text (reads from cache)."""
from __future__ import annotations

from .cache import AccountCounts, cache_stamp, get_bucket, get_counts
from .config import load_accounts, load_settings

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


def format_dashboard(counts: list[AccountCounts], *, force_note: str = "") -> str:
    total_p0 = sum(c.p0 for c in counts)
    total_p1 = sum(c.p1 for c in counts)
    ok, missing = purposes_status()
    lines = [
        "Priority dashboard",
        cache_stamp(),
        f"Pending: Critical {total_p0} · Important {total_p1}",
        "",
    ]
    if force_note:
        lines.append(force_note)
        lines.append("")
    for c in counts:
        lines.append(f"{c.account_id}  <{c.email}>")
        lines.append(
            f"  Critical {c.p0} · Important {c.p1} · Normal {c.p2} · Noise {c.p3}"
        )
    lines.append("")
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
) -> str:
    lines = [
        title,
        f"Page {page + 1} of {total_pages} · {total_count} emails",
        cache_stamp(),
        "",
    ]
    if not items:
        lines.append("(empty)")
        return "\n".join(lines)
    for i, it in enumerate(items, start=1):
        lines.append(f"{i}. [{it['account_id']}] {it['subject']}")
        lines.append(f"    from {it['from']}")
    lines.append("")
    lines.append("Button guide:")
    lines.append("· Open 1 … Open 10 — open that email")
    lines.append("· Previous page / Next page — flip the list")
    lines.append("· Inside email: Reply · Done · Previous/Next email")
    return "\n".join(lines)
