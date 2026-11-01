"""In-memory mail metadata cache. Filled on hourly sync; Manual refresh forces rebuild.

Two ways to build an account's priority view:

* physical - the account really organizes mail into the INBOX.P* folders
  (policy.organize on, not test mode): list those folders.
* logical  - read-only accounts, accounts
  not yet approved for organization, and test mode. Nothing is moved, so mail
  stays in INBOX and the view follows the classification Mailops recorded in
  ``message_locations``. Priority folders that don't exist are never needed.

Either way each item's ``ref`` / ``mailbox`` name the mailbox where the message
physically is (so opening/replying reads the real message), while ``folder`` is
the priority bucket it belongs to.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import PRIORITY_TO_FOLDER, load_accounts
from .db import get_message_locations
from .himalaya_client import Himalaya, MailboxUnavailable
from .policy import organizes_physically

log = logging.getLogger("mailops.cache")

_lock = threading.RLock()
_counts: list["AccountCounts"] = []
_buckets: dict[str, list[dict[str, str]]] = {}
_updated_at: float = 0.0

# Pluggable mail provider — tests inject a fake so no real Himalaya runs.
_provider: Any = None

# ----------------------------------------------------------- single flight
#: Guards the single-flight bookkeeping below (NOT the IMAP pass itself).
_sf_lock = threading.Lock()
#: Set to an Event while exactly one full-fleet pass is running; None when
#: idle. Late callers wait on the Event instead of starting a second pass.
_sf_event: "threading.Event | None" = None

#: Upper bound on how long a *blocking* caller (the hourly sync loop, a
#: Manual refresh) waits to join an in-flight pass. Generous because these
#: callers genuinely want fresh data; the interactive dashboard uses its own
#: much shorter budget and never calls refresh_all on a cold read.
_JOIN_WAIT_SECONDS = 20 * 60

# ------------------------------------------------------------- persistence
#: ``digest_state`` key holding the last completed count snapshot.
_COUNTS_KEY = "cache_counts_v1"
#: True once we have tried to load the snapshot from disk this process.
_hydrated = False
#: (scanned, total) for the pass in flight; (0, 0) when idle.
_progress: tuple[int, int] = (0, 0)

#: Accounts we have already logged an "unavailable" warning for, so a dead
#: host does not reprint the same line on every pass.
_warned_unavailable: set[str] = set()

#: Account ids skipped on the last pass because the server was unreachable.
_unreachable: set[str] = set()


def unreachable_accounts() -> list[str]:
    """Accounts whose server was unreachable on the most recent pass."""
    with _lock:
        return sorted(_unreachable)
#: Set to ask an in-flight pass to stop at the next account boundary.
#: Used for a clean shutdown (and by tests, so a leaked warm cannot keep
#: running into the next one). Never abandons work already published.
#:
#: Cleared when a pass CLAIMS the flight, not just by ``clear_abort``: a
#: sticky module-global would otherwise leak into unrelated callers and make
#: their refreshes return zero accounts.
_abort = threading.Event()


def request_abort() -> None:
    """Ask any in-flight refresh to stop after the current mailbox."""
    _abort.set()


def clear_abort() -> None:
    _abort.clear()


def refresh_progress() -> tuple[int, int]:
    """``(mailboxes scanned, total)`` for the pass in flight, else ``(0, 0)``."""
    with _lock:
        return _progress


def _counts_from_rows(rows: list[dict[str, Any]]) -> list["AccountCounts"]:
    out: list[AccountCounts] = []
    for r in rows:
        try:
            out.append(
                AccountCounts(
                    account_id=str(r["account_id"]),
                    email=str(r.get("email") or ""),
                    p0=int(r.get("p0") or 0),
                    p1=int(r.get("p1") or 0),
                    p2=int(r.get("p2") or 0),
                    p3=int(r.get("p3") or 0),
                    display_name=str(r.get("display_name") or ""),
                )
            )
        except Exception:  # noqa: BLE001 - a bad row must not poison startup
            continue
    return out


def save_counts() -> None:
    """Persist the current counts so a restart starts WARM.

    The cache is in-memory, so before this every restart left the dashboard
    with nothing to show until a full-fleet IMAP pass finished - minutes to
    tens of minutes on a 39-mailbox fleet. Counts are cheap, non-sensitive
    metadata (per-account p0..p3 and the address already in accounts.json);
    no subjects, senders or bodies are written.

    Only a snapshot covering the whole current account list is written, so a
    pass that was aborted (shutdown) cannot replace a good snapshot with a
    truncated one.
    """
    import json

    from .db import OPS_DB
    from .db import digest_state_set

    # A test must never write a snapshot into the live ops.db. Several tests
    # legitimately run refresh_all against the REAL account registry with an
    # empty mail double; without this guard that persisted an all-zeros
    # 39-mailbox snapshot into production, which the next restart then
    # restored as if it were real. Mirrors paths.log_file()'s refusal to
    # touch the live log under pytest.
    #
    # The comparison is against the repository's own data/ops.db, not
    # ``paths.OPS_DB``: the tmp_profile fixture redirects both module
    # globals, so comparing them to each other would match every time and
    # block the legitimate persistence tests too.
    if "PYTEST_CURRENT_TEST" in os.environ:
        try:
            live = Path(__file__).resolve().parent.parent / "data" / "ops.db"
            if Path(OPS_DB).resolve() == live.resolve():
                return
        except Exception:  # noqa: BLE001 - unresolvable path: fail closed
            return

    with _lock:
        rows = [
            {
                "account_id": c.account_id,
                "email": c.email,
                "p0": c.p0,
                "p1": c.p1,
                "p2": c.p2,
                "p3": c.p3,
                "display_name": c.display_name,
            }
            for c in _counts
        ]
        stamp = _updated_at
    if not rows:
        return
    try:
        known = {r["account_id"] for r in rows}
        if not {a.id for a in load_accounts()} <= known:
            return  # incomplete coverage: keep the previous snapshot
    except Exception:  # noqa: BLE001 - registry unreadable; snapshot anyway
        pass
    try:
        digest_state_set(
            _COUNTS_KEY, json.dumps({"updated_at": stamp, "counts": rows})
        )
    except Exception:  # noqa: BLE001 - persistence is best effort
        log.debug("could not persist dashboard counts", exc_info=True)


def seed_from_db() -> bool:
    """Derive counts from the local classification table. No IMAP at all.

    Why this exists
    ---------------
    Every message the sync worker classifies is recorded in
    ``message_locations`` (account, msg_id, mailbox, priority). That table IS
    the dashboard's data - ``_virtual_view`` already routes mail by it. So
    after a restart the counts can be computed from a single SQLite query in
    milliseconds, instead of waiting for a full-fleet IMAP pass.

    This is what made the cache "never become ready" in practice: the warm
    and the sync loop both walk all 39 mailboxes over shared SiteGround
    hosts, each throttling the other, so a pass that needs ~30s per mailbox
    could take 20+ minutes - and until it finished the dashboard had nothing.

    Counts seeded here are marked by an older ``_updated_at`` only if the
    cache is otherwise empty; a completed IMAP pass always wins.
    """
    global _counts, _updated_at

    from .db import count_locations_by_priority

    try:
        by_account = count_locations_by_priority()
    except Exception:  # noqa: BLE001 - db unavailable: fall back to IMAP warm
        log.debug("could not seed counts from db", exc_info=True)
        return False
    if not by_account:
        return False
    seeded: list[AccountCounts] = []
    for acc in load_accounts():
        nums = by_account.get(acc.id, {})
        seeded.append(
            AccountCounts(
                account_id=acc.id,
                email=acc.email,
                p0=int(nums.get(0, 0)),
                p1=int(nums.get(1, 0)),
                p2=int(nums.get(2, 0)),
                p3=int(nums.get(3, 0)),
                display_name=acc.display_name,
            )
        )
    if not seeded:
        return False
    with _lock:
        if _counts:
            return False  # already have something better
        _counts = seeded
        if not _updated_at:
            _updated_at = time.time()
    total = sum(c.p0 + c.p1 for c in seeded)
    log.info(
        "dashboard counts seeded from local classifications: %s mailboxes, "
        "%s actionable (P0+P1)",
        len(seeded),
        total,
    )
    return True


def hydrate() -> bool:
    """Load the persisted snapshot into memory. Idempotent, cheap, no IMAP.

    Returns True when counts are available afterwards. This is what restores
    the "instant dashboard" after a restart: the operator sees the last known
    counts (with an honest age stamp) immediately, and a background refresh
    replaces them when it completes.
    """
    global _hydrated, _counts, _updated_at

    with _lock:
        if _hydrated:
            return bool(_counts)
        _hydrated = True
    import json

    from .db import digest_state_get

    try:
        raw = digest_state_get(_COUNTS_KEY, "")
    except Exception:  # noqa: BLE001 - a missing/locked db must not break the bot
        log.debug("could not read persisted dashboard counts", exc_info=True)
        return False
    if not raw:
        return False
    try:
        blob = json.loads(raw)
        restored = _counts_from_rows(list(blob.get("counts") or []))
        stamp = float(blob.get("updated_at") or 0.0)
    except Exception:  # noqa: BLE001
        log.debug("persisted dashboard counts unreadable", exc_info=True)
        return False
    if not restored:
        return False
    with _lock:
        if not _counts:  # never clobber fresher in-memory data
            _counts = restored
            _updated_at = stamp
    log.info(
        "dashboard counts restored from disk: %s mailboxes (age %.0fs)",
        len(restored),
        max(0.0, time.time() - stamp) if stamp else -1,
    )
    return True


def _claim_pass() -> "threading.Event | None":
    """Claim the right to run the one full-fleet pass.

    Returns a fresh Event for the leader, or None when a pass is already in
    flight. Claiming is synchronous, so a caller that starts a background
    warm can immediately hand the Event to ``wait_for_refresh`` without
    racing the worker thread's start-up.

    Claiming also clears any stale abort request, so an abort only ever
    applies to the pass that was running when it was raised.
    """
    global _sf_event
    with _sf_lock:
        if _sf_event is not None:
            return None
        _sf_event = threading.Event()
        _abort.clear()
        return _sf_event


def _release_pass(event: "threading.Event") -> None:
    global _sf_event
    with _sf_lock:
        if _sf_event is event:
            _sf_event = None
    event.set()


def refresh_in_progress() -> bool:
    """True while a full-fleet IMAP pass is running somewhere in this process."""
    with _sf_lock:
        return _sf_event is not None


def is_cold() -> bool:
    """True when the dashboard has no usable cached counts at all.

    Tries, in order of cost: memory, the persisted snapshot, then the local
    classification table. Only if all three are empty is the dashboard
    genuinely cold - which now means "this deployment has never classified
    any mail", not merely "we restarted".
    """
    with _lock:
        if _counts:
            return False
    if hydrate():
        return False
    return not seed_from_db()


def counts_now() -> tuple[list["AccountCounts"], bool]:
    """Counts that are already known, doing **no** IMAP work at all.

    Returns ``(counts, cold)``. Safe to call straight from the event loop:
    it touches memory plus (once per process) one tiny SQLite read, never a
    Himalaya subprocess and never the network.
    """
    cold = is_cold()
    with _lock:
        return list(_counts), cold


def is_stale() -> bool:
    """True when the cached counts predate the current account list.

    Used to decide whether to kick off a background refresh; unlike
    ``is_cold`` it does not stop the dashboard from rendering.
    """
    with _lock:
        if not _counts:
            return True
        cached_ids = sorted(c.account_id for c in _counts)
    return cached_ids != sorted(a.id for a in load_accounts())


def ensure_refresh_running() -> bool:
    """Start a background full-fleet refresh unless one is already running.

    Returns True when a pass is in flight (started here or already going).
    The dashboard uses this so a cold cache warms itself *without* making the
    user's command wait for ~200 Himalaya subprocesses.
    """
    event = _claim_pass()
    if event is None:
        return True  # a pass is already running; nothing to add

    def _warm() -> None:
        try:
            _refresh_all_locked()
        except Exception:  # noqa: BLE001 - background warm must never crash
            log.exception("background cache warm failed")
        finally:
            global _progress
            with _lock:
                _progress = (0, 0)
            _release_pass(event)

    threading.Thread(target=_warm, name="mailops-cache-warm", daemon=True).start()
    log.info("background cache warm started")
    return True


def wait_for_refresh(timeout: float) -> bool:
    """Wait up to ``timeout`` seconds for the in-flight pass to finish.

    False means it is still running — the caller should render whatever is
    cached rather than keep the user waiting.
    """
    with _sf_lock:
        event = _sf_event
    if event is None:
        return True
    return event.wait(timeout)


#: Fakes that are swapped out mid-pass (tests) are still referenced by the
#: running thread, so latency must be cleared on the object the pass holds.
_last_provider: Any = None


def set_provider(him: Any) -> None:
    global _provider, _last_provider
    _provider = him
    if him is not None:
        _last_provider = him


def active_provider() -> Any:
    """The provider a pass in flight is actually using (tests)."""
    return _provider if _provider is not None else _last_provider


def _get_him() -> Any:
    return _provider if _provider is not None else Himalaya()


@dataclass
class AccountCounts:
    account_id: str
    email: str
    p0: int
    p1: int
    p2: int
    p3: int
    display_name: str = ""  # shown instead of the id when set ("Altcoin Informer")

    @property
    def label(self) -> str:
        return self.display_name or self.account_id

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


# {priority folder: [(envelope, physical mailbox the message sits in)]}
View = dict[str, list[tuple[dict[str, Any], str]]]


def _envelopes_checked(
    him: Any, account_id: str, mailbox: str, page_size: int
) -> list[dict[str, Any]]:
    """``him.envelopes_checked`` when available, else ``him.envelopes``.

    Test doubles implement only the lenient ``envelopes``; the real client
    offers both. Going through one helper keeps the views working with either
    without each call site duplicating the fallback.
    """
    checked = getattr(him, "envelopes_checked", None)
    if checked is not None:
        return checked(account_id, mailbox, page_size=page_size)
    return him.envelopes(account_id, mailbox, page_size=page_size)


def _physical_view(him: Any, account_id: str) -> View:
    """The account really keeps mail in the priority folders: list them.

    Mail classified while the account was still read-only / in test mode was
    never moved and still sits in INBOX; it is routed by its recorded location
    so approving organization later doesn't make earlier results vanish.

    Raises :class:`MailboxUnavailable` when the server cannot be reached, so
    the caller keeps the counts it already had instead of publishing zeroes.
    """
    view: View = {}
    placed: set[str] = set()
    for folder in PRIORITY_TO_FOLDER.values():
        try:
            envs = _envelopes_checked(him, account_id, folder, 100)
        except MailboxUnavailable:
            raise
        except Exception:  # noqa: BLE001
            envs = []
        view[folder] = [(e, folder) for e in envs]
        placed.update(str(e.get("id") or e.get("uid") or "") for e in envs)
    locations = get_message_locations(account_id)
    if locations:
        try:
            inbox = _envelopes_checked(him, account_id, "INBOX", 500)
        except MailboxUnavailable:
            raise
        except Exception:  # noqa: BLE001
            inbox = []
        for e in inbox:
            mid = str(e.get("id") or e.get("uid") or "")
            loc = locations.get(mid)
            if mid and mid not in placed and loc and loc.get("mailbox") in view:
                view[loc["mailbox"]].append((e, "INBOX"))
                placed.add(mid)
    return view


def _virtual_view(him: Any, account_id: str, page_size: int = 500) -> View:
    """Classification view for accounts whose mail is NOT moved.

    Mail sits in INBOX; each message is routed to the bucket recorded when it
    was classified. A recorded location always wins over the physical one (a
    rescan may re-classify without moving). Mail that was never classified has
    no recorded location and is not counted. Physical priority folders are
    also honoured for mail moved earlier (e.g. before an account became
    read-only) and are skipped when they don't exist.

    Raises :class:`MailboxUnavailable` when the server cannot be reached, so
    the caller keeps the counts it already had instead of publishing zeroes.
    """
    locations = get_message_locations(account_id)
    boxes = ["INBOX", *PRIORITY_TO_FOLDER.values()]
    try:
        existing = {str(n).strip().lower() for n in him.mailbox_list(account_id)}
    except Exception:  # noqa: BLE001
        existing = set()
    if existing:  # unknown (empty) listing: try every box rather than guess
        boxes = [b for b in boxes if b == "INBOX" or b.lower() in existing]

    view: View = {folder: [] for folder in PRIORITY_TO_FOLDER.values()}
    seen: set[str] = set()
    for box in boxes:
        try:
            envs = _envelopes_checked(him, account_id, box, page_size)
        except MailboxUnavailable:
            raise
        except Exception:  # noqa: BLE001
            continue
        for e in envs:
            mid = str(e.get("id") or e.get("uid") or "")
            if not mid or mid in seen:
                continue
            loc = locations.get(mid)
            target = loc.get("mailbox") if loc else (box if box != "INBOX" else None)
            if target in view:
                seen.add(mid)
                view[target].append((e, box))
    return view


def _account_view(him: Any, account_id: str) -> View:
    if organizes_physically(account_id):
        return _physical_view(him, account_id)
    return _virtual_view(him, account_id)


def _make_item(account_id: str, folder: str, env: dict[str, Any], mailbox: str) -> dict[str, str]:
    mid = str(env.get("id") or env.get("uid") or "")
    return {
        "ref": f"{account_id}|{mailbox}|{mid}",
        "subject": str(env.get("subject") or "(no subject)"),
        "from": _from_label(env),
        "account_id": account_id,
        "mailbox": mailbox,  # where the message physically is
        "folder": folder,  # priority bucket it is classified into
        "msg_id": mid,
    }


def _fetch_bucket(
    him: Himalaya, priority: int, account_id: str | None = None, limit: int = 200
) -> list[dict[str, str]]:
    folder = PRIORITY_TO_FOLDER[priority]
    accounts = load_accounts()
    if account_id:
        accounts = [a for a in accounts if a.id == account_id]
    items: list[dict[str, str]] = []
    for acc in accounts:
        for e, mailbox in _account_view(him, acc.id).get(folder, []):
            items.append(_make_item(acc.id, folder, e, mailbox))
            if len(items) >= limit:
                return items
    return items


def refresh_all() -> dict[str, Any]:
    """Full IMAP metadata refresh — hourly sync + Manual refresh.

    Fetches each account's priority folders ONCE and derives both the counts
    and every (priority × account) bucket from that single pass, instead of
    re-listing folders per bucket. Each folder listing spawns a Himalaya
    subprocess, so the old per-bucket fetching multiplied process spawns by
    (priorities × accounts) and took minutes.

    **Single flight.** With 39 accounts a pass spawns ~200 Himalaya
    subprocesses and takes 6-13 minutes (measured from the live log). If
    several callers ask at once — the hourly sync, a Manual refresh, and
    every cold-cache ``/dashboard`` — they used to each start their own
    pass, multiplying subprocess load and starving the machine. Now the
    first caller does the work and the rest wait for its result.
    """
    global _sf_event, _progress
    event = _claim_pass()
    if event is None:
        # Someone else is already doing exactly this work. Wait for it
        # rather than spawning a second full-fleet IMAP pass.
        log.info("cache refresh already in progress; joining it")
        wait_for_refresh(_JOIN_WAIT_SECONDS)
        with _lock:
            return {"accounts": len(_counts), "updated_at": _updated_at, "joined": True}
    started = time.monotonic()
    try:
        return _refresh_all_locked()
    finally:
        with _lock:
            _progress = (0, 0)
        _release_pass(event)
        log.info("cache refresh pass finished in %.1fs", time.monotonic() - started)


def _counts_from_locations(acc: Any) -> "AccountCounts | None":
    """One account's counts from the local classification table, or None."""
    from .db import count_locations_by_priority

    try:
        nums = count_locations_by_priority().get(acc.id)
    except Exception:  # noqa: BLE001
        return None
    if not nums:
        return None
    return AccountCounts(
        account_id=acc.id,
        email=acc.email,
        p0=int(nums.get(0, 0)),
        p1=int(nums.get(1, 0)),
        p2=int(nums.get(2, 0)),
        p3=int(nums.get(3, 0)),
        display_name=acc.display_name,
    )


def _refresh_all_locked() -> dict[str, Any]:
    """The actual full-fleet pass. Only ever one at a time (see above).

    **Publishes incrementally.** Previously ``_counts`` was replaced only
    after EVERY account had been scanned, so for the 6-13 minutes a
    39-mailbox pass takes the dashboard still saw an empty cache and showed
    nothing. Now each account's counts are published as soon as that account
    is scanned, so the dashboard fills in progressively and is useful within
    seconds of the first mailbox completing.
    """
    global _counts, _buckets, _updated_at, _progress
    him = _get_him()
    accounts = load_accounts()
    folder_to_priority = {folder: p for p, folder in PRIORITY_TO_FOLDER.items()}

    counts: list[AccountCounts] = []
    unreachable: list[str] = []
    buckets: dict[str, list[dict[str, str]]] = {}
    for priority in (0, 1, 2, 3):
        buckets[_bucket_key(priority, None)] = []
        for acc in accounts:
            buckets[_bucket_key(priority, acc.id)] = []

    # Counts already on screen (restored from disk or a previous pass), so a
    # refresh never blanks a mailbox it has not re-scanned yet.
    pass_started = time.monotonic()
    with _lock:
        previous = {c.account_id: c for c in _counts}
        previous_buckets = dict(_buckets)
        _progress = (0, len(accounts))

    for index, acc in enumerate(accounts, start=1):
        if _abort.is_set():
            log.info(
                "cache refresh aborted after %s/%s mailboxes (shutdown requested)",
                index - 1,
                len(accounts),
            )
            break
        nums = {"p0": 0, "p1": 0, "p2": 0, "p3": 0}
        unavailable = False
        try:
            view = _account_view(him, acc.id)
        except MailboxUnavailable as exc:
            # The server is unreachable. Publishing zeroes here is what made
            # the dashboard claim "0 emails" for mailboxes that really had
            # mail, so keep whatever this account last reported and move on.
            unavailable = True
            view = {}
            if acc.id not in _warned_unavailable:
                _warned_unavailable.add(acc.id)
                log.warning(
                    "mailbox unavailable, keeping last known counts for %s: %s",
                    acc.id,
                    exc.detail,
                )
        except Exception:  # noqa: BLE001
            log.exception("cache view failed for %s", acc.id)
            view = {}
        else:
            _warned_unavailable.discard(acc.id)

        if unavailable:
            # Keep the last known numbers; if we never had any, fall back to
            # the locally recorded classifications rather than zeroes.
            fallback = previous.get(acc.id) or _counts_from_locations(acc)
            if fallback is not None:
                counts.append(fallback)
                unreachable.append(acc.id)
                # Keep this account's previously fetched message lists too,
                # so the per-mailbox screens still show mail instead of
                # "0 emails" while the host is down.
                for priority in (0, 1, 2, 3):
                    key = _bucket_key(priority, acc.id)
                    kept = previous_buckets.get(key)
                    if kept:
                        buckets[key] = list(kept)
                        buckets[_bucket_key(priority, None)].extend(kept)
                with _lock:
                    _progress = (index, len(accounts))
                continue

        for folder, priority in folder_to_priority.items():
            pairs = view.get(folder, [])
            nums[f"p{priority}"] = len(pairs)
            for e, mailbox in pairs[:200]:
                item = _make_item(acc.id, folder, e, mailbox)
                buckets[_bucket_key(priority, acc.id)].append(item)
                buckets[_bucket_key(priority, None)].append(item)
        counts.append(
            AccountCounts(
                account_id=acc.id,
                email=acc.email,
                p0=nums["p0"],
                p1=nums["p1"],
                p2=nums["p2"],
                p3=nums["p3"],
                display_name=acc.display_name,
            )
        )
        # Publish what we have: accounts scanned so far get fresh numbers,
        # the rest keep their last known ones (omitted entirely if never
        # seen). The dashboard is therefore usable mid-pass.
        done_ids = {c.account_id for c in counts}
        partial = list(counts) + [
            previous[a.id]
            for a in accounts
            if a.id not in done_ids and a.id in previous
        ]
        with _lock:
            _counts = partial
            _progress = (index, len(accounts))
            for key, items in buckets.items():
                _buckets[key] = list(items)
            if not _updated_at:
                _updated_at = time.time()
        # A full-fleet pass takes minutes; without this the log went silent
        # for its whole duration and there was no way to tell a slow pass
        # from a wedged one.
        if index == 1 or index % 10 == 0 or index == len(accounts):
            log.info(
                "cache refresh progress %s/%s mailboxes (%.0fs elapsed)",
                index,
                len(accounts),
                time.monotonic() - pass_started,
            )
    complete = len(counts) == len(accounts)
    with _lock:
        _unreachable.clear()
        _unreachable.update(unreachable)
        if complete:
            # Full pass: replace everything, including the bucket set, so
            # retired mailboxes disappear.
            _counts = counts
            _buckets = buckets
        else:
            # Aborted part way: keep the merged view (fresh where we got to,
            # last known elsewhere) rather than dropping unscanned mailboxes.
            _counts = list(counts) + [
                previous[a.id]
                for a in accounts
                if a.id not in {c.account_id for c in counts} and a.id in previous
            ]
        _updated_at = time.time()
        _progress = (0, 0)
    # Survive the next restart: the dashboard comes back instantly with these
    # numbers instead of waiting on a fresh full-fleet pass. Only a COMPLETE
    # pass is persisted, so an aborted one cannot overwrite a good snapshot
    # with a truncated mailbox list.
    if complete:
        save_counts()
    log.info(
        "cache refreshed accounts=%s%s%s",
        len(counts),
        "" if complete else f" (partial, of {len(accounts)})",
        f", {len(unreachable)} unreachable" if unreachable else "",
    )
    if unreachable:
        log.warning(
            "%s/%s mailboxes were unreachable this pass; their counts are "
            "last-known, not live: %s",
            len(unreachable),
            len(accounts),
            ", ".join(unreachable[:10]) + (" ..." if len(unreachable) > 10 else ""),
        )
    return {
        "accounts": len(counts),
        "updated_at": _updated_at,
        "complete": complete,
        "unreachable": list(unreachable),
    }


def get_counts(*, force: bool = False) -> list[AccountCounts]:
    with _lock:
        empty = not _counts
        cached_ids = [c.account_id for c in _counts]
    # An account activated or retired since the last refresh (webmail-add,
    # allowlist change) must show up in / vanish from the dashboard at once.
    changed = cached_ids != [a.id for a in load_accounts()]
    if force or empty or changed:
        refresh_all()
    with _lock:
        return list(_counts)


def _bucket_from_locations(
    priority: int, account_id: str | None = None, limit: int = 200
) -> list[dict[str, str]]:
    """The bucket's message list from the local classification table.

    Used when the mail server is unreachable. ``message_locations`` already
    stores the subject, sender and recorded folder for every classified
    message, which is enough to render the list screen. Opening a message
    still needs the server, but the operator can at least SEE what is
    waiting instead of a false "0 emails".
    """
    from .db import locations_by_priority

    folder = PRIORITY_TO_FOLDER[priority]
    ids = [a.id for a in load_accounts()]
    if account_id:
        ids = [i for i in ids if i == account_id]
    out: list[dict[str, str]] = []
    for aid in ids:
        try:
            rows = locations_by_priority(aid, priority)
        except Exception:  # noqa: BLE001
            continue
        for r in rows:
            out.append(
                {
                    "ref": f"{aid}|{r['mailbox']}|{r['msg_id']}",
                    "subject": str(r["subject"] or "(no subject)"),
                    "from": str(r["sender"] or ""),
                    "account_id": aid,
                    "mailbox": str(r["mailbox"] or ""),
                    "folder": folder,
                    "msg_id": str(r["msg_id"]),
                    "stale": "1",  # rendered from local records, not live IMAP
                }
            )
            if len(out) >= limit:
                return out
    return out


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
            him = _get_him()
            try:
                items = _fetch_bucket(him, priority, account_id)
            except MailboxUnavailable as exc:
                # The server is down. Fall back to the locally recorded
                # classifications rather than reporting an empty folder,
                # which is what made the Important screen read "0 emails"
                # for mail that was really there.
                log.warning(
                    "bucket fetch unavailable (%s); serving local records",
                    exc.detail,
                )
                return _bucket_from_locations(priority, account_id)
            with _lock:
                _buckets[key] = items
                if not _updated_at:
                    _updated_at = time.time()
            return list(items)
        with _lock:
            cached = list(_buckets.get(key) or [])
        if not cached and account_id in _unreachable:
            return _bucket_from_locations(priority, account_id)
        return cached
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
