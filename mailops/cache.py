"""In-memory mail metadata cache. Filled on hourly sync; Manual refresh forces rebuild."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import PRIORITY_TO_FOLDER, load_accounts
from .himalaya_client import Himalaya

log = logging.getLogger("mailops.cache")

_lock = threading.RLock()
_counts: list["AccountCounts"] = []
_buckets: dict[str, list[dict[str, str]]] = {}
_updated_at: float = 0.0


@dataclass
class AccountCounts:
    account_id: str
    email: str
    p0: int
    p1: int
    p2: int
    p3: int

    @property
    def priority_pending(self) -> int:
        return self.p0 + self.p1


def _from_label(envelope: dict[str, Any]) -> str:
    frm = envelope.get("from") or []
    if isinstance(frm, list) and frm and isinstance(frm[0], dict):
        return str(frm[0].get("email") or frm[0].get("name") or "?")
    return str(envelope.get("from") or "?")


def last_updated() -> float:
    return _updated_at


def age_seconds() -> float | None:
    if not _updated_at:
        return None
    return time.time() - _updated_at


def _bucket_key(priority: int, account_id: str | None) -> str:
    return f"{priority}|{account_id or '*'}"


def _fetch_counts(him: Himalaya) -> list[AccountCounts]:
    out: list[AccountCounts] = []
    folders = {
        "p0": "INBOX.P0-Critical",
        "p1": "INBOX.P1-Important",
        "p2": "INBOX.P2-Normal",
        "p3": "INBOX.P3-Noise",
    }
    for acc in load_accounts():
        nums: dict[str, int] = {}
        for key, folder in folders.items():
            try:
                nums[key] = len(him.envelopes(acc.id, folder, page_size=100))
            except Exception:  # noqa: BLE001
                nums[key] = 0
        out.append(
            AccountCounts(
                account_id=acc.id,
                email=acc.email,
                p0=nums["p0"],
                p1=nums["p1"],
                p2=nums["p2"],
                p3=nums["p3"],
            )
        )
    return out


def _fetch_bucket(
    him: Himalaya, priority: int, account_id: str | None = None, limit: int = 200
) -> list[dict[str, str]]:
    folder = PRIORITY_TO_FOLDER[priority]
    accounts = load_accounts()
    if account_id:
        accounts = [a for a in accounts if a.id == account_id]
    items: list[dict[str, str]] = []
    for acc in accounts:
        for page in (1, 2, 3, 4):
            envs = him.envelopes(acc.id, folder, page_size=50, page=page)
            if not envs:
                break
            for e in envs:
                mid = str(e.get("id") or "")
                items.append(
                    {
                        "ref": f"{acc.id}|{folder}|{mid}",
                        "subject": str(e.get("subject") or "(no subject)"),
                        "from": _from_label(e),
                        "account_id": acc.id,
                        "mailbox": folder,
                        "msg_id": mid,
                    }
                )
                if len(items) >= limit:
                    return items
    return items


def refresh_all() -> dict[str, Any]:
    """Full IMAP metadata refresh — hourly sync + Manual refresh."""
    global _counts, _buckets, _updated_at
    him = Himalaya()
    counts = _fetch_counts(him)
    buckets: dict[str, list[dict[str, str]]] = {}
    for priority in (0, 1, 2, 3):
        buckets[_bucket_key(priority, None)] = _fetch_bucket(him, priority, None)
        for acc in load_accounts():
            buckets[_bucket_key(priority, acc.id)] = _fetch_bucket(him, priority, acc.id)
    with _lock:
        _counts = counts
        _buckets = buckets
        _updated_at = time.time()
    log.info("cache refreshed accounts=%s", len(counts))
    return {"accounts": len(counts), "updated_at": _updated_at}


def get_counts(*, force: bool = False) -> list[AccountCounts]:
    with _lock:
        empty = not _counts
    if force or empty:
        refresh_all()
    with _lock:
        return list(_counts)


def get_bucket(
    priority: int, account_id: str | None = None, *, force: bool = False
) -> list[dict[str, str]]:
    global _updated_at
    key = _bucket_key(priority, account_id)
    with _lock:
        hit = _buckets.get(key)
        empty_cache = not _updated_at
    if force or empty_cache or hit is None:
        if force or empty_cache:
            refresh_all()
        else:
            him = Himalaya()
            items = _fetch_bucket(him, priority, account_id)
            with _lock:
                _buckets[key] = items
                if not _updated_at:
                    _updated_at = time.time()
            return list(items)
        with _lock:
            return list(_buckets.get(key) or [])
    return list(hit)


def cache_stamp() -> str:
    age = age_seconds()
    if age is None:
        return "Cache: empty"
    if age < 60:
        return "Cache: updated just now (auto)"
    if age < 3600:
        return f"Cache: auto-updated {int(age // 60)}m ago"
    return f"Cache: auto-updated {int(age // 3600)}h ago"
