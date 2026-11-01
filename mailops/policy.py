"""Per-account mailbox policies.

Every account is deny-by-default for anything that changes a mailbox or sends
mail. What an account may do is decided here, in ONE place, and enforced in
three layers (sync pipeline, bot handlers, and the Himalaya client itself),
so a single missed call site cannot touch a mailbox it must not.

Capabilities
------------
read       always allowed: read, classify, cache, notify.
organize   create folders, move, copy, flag/unflag (priority-folder
           organization). Disabled unless explicitly enabled.
send       reply/send mail. Disabled unless explicitly enabled, and even then
           every message still needs an explicit per-message approval
           (see ``Himalaya.reply_send(approved=True)``).

Locked accounts
---------------
The Gmail inbox is shared with the Sales-Agent (it reads replies/bounces from
it and owns outbound mail from it). Moving, copying, flagging or sending from
Mailops would hide those messages from the Sales-Agent or bypass its
suppression/pacing rules. Such accounts are *locked* read-only: no setting can
unlock them. A locked account is identified by id (``gmail``) or by a Gmail
address, so a second Gmail account added later is safe by default as well.

Everything else is configured in ``data/settings.json``::

    "account_policies": {
      "altcoininformer": {"organize": false, "send": false}
    }

Missing entries mean "off". Only the JSON literal ``true`` enables a
capability (``"yes"``, ``1`` ... do not). Turning ``organize`` on for an
account IS the production activation for that account: do it only after
explicit approval. Test mode (``MAILOPS_TEST_MODE=1``) still turns every
mutation into a logged no-op regardless of policy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import (
    GMAIL_ACCOUNT_IDS,
    GMAIL_EMAIL_DOMAINS,
    PRIORITY_TO_FOLDER,
    load_settings,
    registered_email,
    save_settings,
)
from .safe_mode import test_mode_enabled

log = logging.getLogger("mailops.policy")

# Mailbox-mutating operations (see himalaya_client.Himalaya).
ORGANIZE_OPS = frozenset({"create_mailbox", "move", "copy", "flag_add", "flag_remove"})
# Operations that put mail on the wire.
SEND_OPS = frozenset({"reply_send", "send_raw"})

# Accounts that can never be organized or sent from by Mailops.
LOCKED_READ_ONLY_IDS = GMAIL_ACCOUNT_IDS
LOCKED_EMAIL_DOMAINS = GMAIL_EMAIL_DOMAINS

PRIORITY_FOLDERS = frozenset(PRIORITY_TO_FOLDER.values())


@dataclass(frozen=True)
class AccountPolicy:
    account_id: str
    locked: bool  # shared inbox: read-only, cannot be unlocked by settings
    organize: bool
    send: bool

    @property
    def read_only(self) -> bool:
        return not self.organize

    def label(self) -> str:
        if self.locked:
            org = "read-only (locked: shared inbox)"
        elif not self.organize:
            org = "read-only (organization off until approved)"
        elif test_mode_enabled():
            org = "organization on (simulated in test mode)"
        else:
            org = "organization on"
        snd = "sending on (each email needs approval)" if self.send else "sending off"
        return f"{org} · {snd}"


def _account_email(account_id: str) -> str:
    try:
        return registered_email(account_id)  # registry, not just active accounts
    except Exception:  # noqa: BLE001 - unreadable registry: fall through to deny
        return ""


def _is_locked(account_id: str) -> bool:
    if account_id.strip().lower() in LOCKED_READ_ONLY_IDS:
        return True
    email = _account_email(account_id).strip().lower()
    return "@" in email and email.rsplit("@", 1)[1] in LOCKED_EMAIL_DOMAINS


def policy_for(account_id: str) -> AccountPolicy:
    """Effective policy for an account. Never raises; any error means deny."""
    if _is_locked(account_id):
        return AccountPolicy(account_id, locked=True, organize=False, send=False)
    try:
        raw = (load_settings().get("account_policies") or {}).get(account_id) or {}
        if not isinstance(raw, dict):
            raw = {}
    except Exception:  # noqa: BLE001
        log.exception("account policy unreadable for %s - denying", account_id)
        raw = {}
    return AccountPolicy(
        account_id,
        locked=False,
        organize=raw.get("organize") is True,
        send=raw.get("send") is True,
    )


def can_organize(account_id: str) -> bool:
    return policy_for(account_id).organize


def can_send(account_id: str) -> bool:
    return policy_for(account_id).send


def organizes_physically(account_id: str) -> bool:
    """True when this account's mail really lives in the priority folders.

    False for read-only accounts and in test mode: classified mail stays in
    INBOX and the dashboard follows the locally recorded classification.
    """
    return can_organize(account_id) and not test_mode_enabled()


def denial(account_id: str, op: str) -> str | None:
    """Why ``op`` is forbidden for this account, or None when allowed."""
    pol = policy_for(account_id)
    if op in ORGANIZE_OPS and not pol.organize:
        why = "locked read-only (shared inbox)" if pol.locked else "organization is disabled"
        return f"{op} denied for account '{account_id}': {why}"
    if op in SEND_OPS and not pol.send:
        why = "locked read-only (shared inbox)" if pol.locked else "sending is disabled"
        return f"{op} denied for account '{account_id}': {why}"
    return None


def describe(account_id: str) -> str:
    return policy_for(account_id).label()


# ---------------------------------------------------------------------------
# Group-read sharing policy (Telegram group-first UX, AWS-migration prep).
#
# Telegram groups have shared, persistent message history: anything the bot
# posts there is readable by every current AND future member of that group
# for as long as the chat history exists, not just the admin who tapped the
# button. Mailops' default therefore has to be a conscious choice, not an
# accident of whichever code path happened to run first.
#
# Decision: DEFAULT ALLOW. The feature this policy gates is "read permitted
# mail inside the authorized group instead of being forced into a private
# chat" - the group is already access-controlled (mailops.access: whitelisted
# group + per-user role), so by default every active account's mail may be
# opened there. An operator who has one mailbox that must never appear in
# shared group history (an HR inbox, a single VIP/legal account, etc.) opts
# it OUT explicitly via ``group_restricted_accounts`` in settings.json (or
# the "Group sharing" admin screen). Credentials, tokens and admin/user
# management are NEVER covered by this flag - those stay private-only
# regardless (enforced separately in mailops/bot/app.py + mailboxes.py).
# ---------------------------------------------------------------------------


def _restricted_set() -> set[str]:
    try:
        raw = load_settings().get("group_restricted_accounts") or []
    except Exception:  # noqa: BLE001 - unreadable settings: fail closed (restrict)
        log.exception("group_restricted_accounts unreadable - treating as unrestricted set empty")
        return set()
    if not isinstance(raw, list):
        return set()
    return {str(x).strip() for x in raw if str(x).strip()}


def group_shareable(account_id: str) -> bool:
    """True when this account's envelopes/bodies may be shown inside an
    authorized Telegram GROUP chat (as opposed to only a private DM).

    This governs CONTENT exposure only (subjects were already group-visible
    via the dashboard/digest). It never affects credentials, which are
    always private-only, and never affects send/organize permission, which
    is governed by ``policy_for`` / ``access.can`` independently.
    """
    try:
        if account_id.strip() in _restricted_set():
            return False
    except Exception:  # noqa: BLE001
        return False
    return True


def set_group_restricted(account_id: str, restricted: bool) -> set[str]:
    """Opt an account in/out of group-shared reading. Returns the new set."""
    settings = load_settings()
    current = _restricted_set()
    account_id = account_id.strip()
    if restricted:
        current.add(account_id)
    else:
        current.discard(account_id)
    settings["group_restricted_accounts"] = sorted(current)
    save_settings(settings)
    log.info("group_restricted_accounts set %s=%s -> %s", account_id, restricted, sorted(current))
    return current


def group_restricted_accounts() -> set[str]:
    return _restricted_set()
