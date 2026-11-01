"""Stable message identity: protect against stale or renumbered message ids.

Himalaya's ``envelope list --json`` exposes a single ``id`` per message. It is
not labelled a UID and carries no UIDVALIDITY (verified against the live
Altcoin Informer mailbox: ids equal both the IMAP sequence numbers and the
UIDs, so the two cannot be told apart). Sequence numbers shift when anything is
expunged, UIDs are only stable per UIDVALIDITY, and a message that is moved
gets a NEW id in the destination folder. A Telegram button or a saved draft
that outlives such a change would otherwise point at a different email.

What IS stable is the RFC 5322 ``Message-ID`` header that Himalaya also
returns (``message-id``), together with subject, sender and date. Every
mailbox mutation and every approved reply therefore re-reads the envelope at
(account, folder, id) and must find the same message that was recorded when
Mailops first saw it (sync) or the user opened it. Anything else is rejected
and nothing is changed.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from .db import db

log = logging.getLogger("mailops.identity")

_EMAIL_IN_TEXT = re.compile(r"[\w.+'-]+@[\w.-]+\.[A-Za-z]{2,}")
_NO_SUBJECT = {"", "(no subject)"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_identity (
  account_id TEXT NOT NULL,
  mailbox TEXT NOT NULL,
  msg_id TEXT NOT NULL,
  message_id TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  sender TEXT NOT NULL DEFAULT '',
  date TEXT NOT NULL DEFAULT '',
  recorded_at REAL NOT NULL,
  PRIMARY KEY (account_id, mailbox, msg_id)
)
"""

#: Added Oct 2026 (stale-reference incident). Existing databases get the
#: column added in place; rows written before this have '' (unknown), which
#: behaves exactly as before.
_MIGRATIONS = (
    "ALTER TABLE message_identity ADD COLUMN uid_validity TEXT NOT NULL DEFAULT ''",
)


def _ensure_schema(conn: Any) -> None:
    conn.execute(_SCHEMA)
    for statement in _MIGRATIONS:
        try:
            conn.execute(statement)
        except Exception:  # noqa: BLE001 - already applied
            pass


@dataclass(frozen=True)
class MessageIdentity:
    account_id: str
    mailbox: str
    msg_id: str
    message_id: str = ""  # RFC 5322 Message-ID header
    subject: str = ""
    sender: str = ""  # bare, lower-case address
    date: str = ""
    #: IMAP UIDVALIDITY of ``mailbox`` when this identity was recorded, when
    #: the server exposed it. A UID is only meaningful within one UIDVALIDITY
    #: epoch: if the server renumbers the folder (UIDVALIDITY changes), every
    #: stored id for it is meaningless and must not be trusted even if a
    #: message happens to sit at that number now. Empty = unknown, in which
    #: case the header/subject/sender comparison is the sole guarantee.
    uid_validity: str = ""


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""


def _norm_subject(text: str) -> str:
    s = " ".join(str(text or "").split()).casefold()
    return "" if s in _NO_SUBJECT else s


def _bare_email(text: str) -> str:
    m = _EMAIL_IN_TEXT.search(str(text or ""))
    return m.group(0).lower() if m else ""


def _envelope_sender(env: dict[str, Any]) -> str:
    frm = env.get("from") or []
    if isinstance(frm, list) and frm:
        first = frm[0]
        if isinstance(first, dict):
            return _bare_email(first.get("email") or first.get("addr") or "") or str(
                first.get("email") or ""
            ).strip().lower()
        return _bare_email(first) or str(first).strip().lower()
    return _bare_email(env.get("from") or "") or str(env.get("from") or "").strip().lower()


def identity_from_envelope(
    account_id: str, mailbox: str, env: dict[str, Any], uid_validity: str = ""
) -> MessageIdentity:
    return MessageIdentity(
        account_id=account_id,
        mailbox=mailbox,
        msg_id=str(env.get("id") or env.get("uid") or ""),
        message_id=str(env.get("message-id") or env.get("message_id") or "").strip(),
        subject=str(env.get("subject") or ""),
        sender=_envelope_sender(env),
        date=str(env.get("date") or "").strip(),
        uid_validity=str(
            uid_validity
            or env.get("uid-validity")
            or env.get("uid_validity")
            or ""
        ).strip(),
    )


def uid_validity_of(him: Any, account_id: str, mailbox: str) -> str:
    """The folder's current UIDVALIDITY, or '' when the server/CLI hides it.

    Himalaya does not surface UIDVALIDITY in ``envelope list --json``, so this
    is best-effort: when it returns '' the identity comparison (Message-ID,
    subject, sender, date) remains the guarantee, exactly as before. When it
    IS available, a change invalidates every stored id for the folder.
    """
    getter = getattr(him, "uid_validity", None)
    if getter is None:
        return ""
    try:
        return str(getter(account_id, mailbox) or "").strip()
    except Exception:  # noqa: BLE001 - never block a read over this
        return ""


# --------------------------------------------------------------------- storage
def remember(identity: MessageIdentity) -> None:
    """Record what this (account, folder, id) pointed at. Idempotent."""
    if not identity.msg_id:
        return
    with db() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO message_identity
            (account_id, mailbox, msg_id, message_id, subject, sender, date,
             uid_validity, recorded_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id, mailbox, msg_id) DO UPDATE SET
              message_id=excluded.message_id, subject=excluded.subject,
              sender=excluded.sender, date=excluded.date,
              uid_validity=excluded.uid_validity,
              recorded_at=excluded.recorded_at
            """,
            (
                identity.account_id,
                identity.mailbox,
                identity.msg_id,
                identity.message_id,
                identity.subject,
                identity.sender,
                identity.date,
                identity.uid_validity,
                time.time(),
            ),
        )


def recall(account_id: str, mailbox: str, msg_id: str) -> MessageIdentity | None:
    """The recorded identity, or None. Mail classified before this guard
    existed has no row; fall back to the subject/sender saved at classification
    (``message_locations``) so already-processed mail stays actionable."""
    with db() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM message_identity WHERE account_id=? AND mailbox=? AND msg_id=?",
            (account_id, mailbox, str(msg_id)),
        ).fetchone()
        if row:
            keys = row.keys()
            return MessageIdentity(
                account_id, mailbox, str(msg_id), row["message_id"], row["subject"],
                row["sender"], row["date"],
                row["uid_validity"] if "uid_validity" in keys else "",
            )
        loc = None
        try:
            # INBOX only: other folders assign their own ids, so a location row
            # keyed by an INBOX id says nothing about them.
            loc = None if mailbox.upper() != "INBOX" else conn.execute(
                "SELECT subject, sender FROM message_locations WHERE account_id=? AND msg_id=?",
                (account_id, str(msg_id)),
            ).fetchone()
        except Exception:  # noqa: BLE001 - table absent in a bare database
            loc = None
    if loc and (_norm_subject(loc["subject"]) or _bare_email(loc["sender"])):
        return MessageIdentity(
            account_id, mailbox, str(msg_id), "", loc["subject"] or "", _bare_email(loc["sender"]), ""
        )
    return None


# --------------------------------------------------------------------- lookup
def find_envelope(
    him: Any,
    account_id: str,
    mailbox: str,
    msg_id: str,
    *,
    page_size: int = 100,
    max_pages: int = 10,
) -> dict[str, Any] | None:
    """Read-only: the envelope currently at (account, mailbox, msg_id)."""
    want = str(msg_id)
    for page in range(1, max_pages + 1):
        batch = him.envelopes(account_id, mailbox, page_size=page_size, page=page)
        if not batch:
            return None
        for env in batch:
            if str(env.get("id") or env.get("uid") or "") == want:
                return env
        if len(batch) < page_size:
            return None
    return None


def locate(
    him: Any, identity: MessageIdentity, mailbox: str, *, page_size: int = 100, max_pages: int = 10
) -> MessageIdentity | None:
    """Read-only: find the SAME message (by Message-ID header, else subject +
    sender + date) in ``mailbox`` and return it with its id THERE. Used after a
    move, because the destination folder assigns a new id."""
    validity = uid_validity_of(him, identity.account_id, mailbox)
    for page in range(1, max_pages + 1):
        batch = him.envelopes(identity.account_id, mailbox, page_size=page_size, page=page)
        if not batch:
            return None
        for env in batch:
            cand = identity_from_envelope(identity.account_id, mailbox, env, validity)
            if cand.msg_id and not _mismatches(identity, cand) and _compared(identity, cand):
                return cand
        if len(batch) < page_size:
            return None
    return None


# --------------------------------------------------------------------- compare
def _compared(base: MessageIdentity, cur: MessageIdentity) -> list[str]:
    fields: list[str] = []
    if base.message_id:
        fields.append("message-id")
    if _norm_subject(base.subject):
        fields.append("subject")
    if base.sender:
        fields.append("sender")
    if base.date and cur.date:
        fields.append("date")
    return fields


def _mismatches(base: MessageIdentity, cur: MessageIdentity) -> list[str]:
    bad: list[str] = []
    if base.message_id and base.message_id != cur.message_id:
        bad.append("message-id")
    if _norm_subject(base.subject) and _norm_subject(base.subject) != _norm_subject(cur.subject):
        bad.append("subject")
    if base.sender and base.sender != cur.sender:
        bad.append("sender")
    if base.date and cur.date and base.date != cur.date:
        bad.append("date")
    return bad


def verify(
    him: Any,
    account_id: str,
    mailbox: str,
    msg_id: str,
    expected: MessageIdentity | None = None,
) -> Verdict:
    """Is the message at (account, folder, id) still the one that was recorded?

    Read-only. Call before ANY mailbox mutation or approved reply and refuse
    the action unless ``ok``. ``expected`` overrides the recorded identity.
    """
    base = expected or recall(account_id, mailbox, msg_id)
    if base is None:
        return Verdict(
            False,
            "this message was never recorded, so it cannot be verified - open it from the "
            "dashboard first",
        )
    if base.account_id != account_id:
        return Verdict(False, "the message belongs to a different account")
    current_validity = uid_validity_of(him, account_id, mailbox)
    if base.uid_validity and current_validity and base.uid_validity != current_validity:
        # The folder was renumbered by the server: ids recorded under the old
        # epoch say nothing about what is at that number now. Refuse without
        # even looking, so a coincidental id collision cannot open the wrong
        # email. Callers relocate by Message-ID instead.
        log.warning(
            "UIDVALIDITY changed for %s %s (%s -> %s): stored ids are invalid",
            account_id, mailbox, base.uid_validity, current_validity,
        )
        return Verdict(
            False,
            f"{mailbox} was renumbered by the mail server (UIDVALIDITY "
            "changed), so saved message references for it are no longer valid",
        )
    try:
        env = find_envelope(him, account_id, mailbox, msg_id)
    except Exception as exc:  # noqa: BLE001 - cannot verify -> do not act
        log.warning("identity lookup failed for %s %s/%s: %s", account_id, mailbox, msg_id, exc)
        return Verdict(False, "the mailbox could not be read to verify the message")
    if env is None:
        return Verdict(
            False,
            f"the message is no longer at {mailbox} (moved, deleted or renumbered)",
        )
    cur = identity_from_envelope(account_id, mailbox, env, current_validity)
    bad = _mismatches(base, cur)
    if bad:
        return Verdict(False, "a different message is now at this position (" + ", ".join(bad) + " differ)")
    if not _compared(base, cur):
        return Verdict(False, "nothing recorded about this message can be compared")
    return Verdict(True, "")


def capture(him: Any, account_id: str, mailbox: str, msg_id: str) -> MessageIdentity | None:
    """Record the identity currently at (account, folder, id) - used when the
    user opens a message. Returns None if it cannot be read."""
    try:
        env = find_envelope(him, account_id, mailbox, msg_id)
    except Exception:  # noqa: BLE001
        return None
    if env is None:
        return None
    ident = identity_from_envelope(
        account_id, mailbox, env, uid_validity_of(him, account_id, mailbox)
    )
    remember(ident)
    return ident


STALE_TEXT = "Stale or mismatched message reference - nothing was changed or sent. {why}. Reopen it from a fresh dashboard."
