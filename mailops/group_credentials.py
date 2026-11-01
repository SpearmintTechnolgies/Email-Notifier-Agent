"""Policy for optional, explicitly-enabled in-group password entry.

Default behaviour (unchanged, Phase 1 preservation): mailbox onboarding
NEVER accepts a password typed in a group. The existing group -> private DM
handoff (``mailops.bot.mailboxes._handoff_from_group``) remains the only
path unless an operator opts in to the feature described here.

Opt-in behaviour (new): a server operator may explicitly enable password
capture inside ONE named, trusted group (expected: the two-owner group
``-5531260744``) by setting BOTH:

  MAILOPS_ALLOW_GROUP_CREDENTIALS=1
  MAILOPS_GROUP_CREDENTIALS_GROUP_ID=<the exact numeric chat id>

Both must be set. There is deliberately no "any whitelisted group" fallback
(unlike the bulk-CSV group-upload flag) - password capture is high enough
risk that naming the exact group is mandatory, not optional-with-a-default.

This module is pure (no Telegram, no DB) so the policy itself is trivially
unit-testable; ``mailops.bot.mailboxes`` owns all the Telegram glue (the
reply-to-prompt capture, deletion, nonce/TTL binding, run_pending call).
"""
from __future__ import annotations

import os

# Short-lived by design: a password sitting "armed" in a group for longer
# than this is a bigger risk than the same window in a private DM, so this
# matches (not exceeds) the existing DM capture window.
ARM_TTL_S = 60


def enabled() -> bool:
    return (os.environ.get("MAILOPS_ALLOW_GROUP_CREDENTIALS") or "").strip() == "1"


def pinned_group_id() -> int | None:
    raw = (os.environ.get("MAILOPS_GROUP_CREDENTIALS_GROUP_ID") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def allowed_for_group(chat_id: int | None) -> bool:
    """True only when the feature flag is on AND a specific group id has
    been pinned AND ``chat_id`` matches it exactly.

    Missing the pin means the feature stays inert even with the flag on -
    there is no safe "any group" default for password capture.
    """
    if not enabled():
        return False
    pin = pinned_group_id()
    if pin is None or chat_id is None:
        return False
    return int(chat_id) == pin


# Shown to the admin every time group password entry is armed (satisfies
# "show this warning before the first use" without needing to track a
# per-admin "have they seen it" flag - showing it every time is a safe
# superset of "at least once before the first use").
RETENTION_WARNING = (
    "Telegram and any device that already received this chat message may "
    "retain a copy even after Mailops deletes it (local caches, other "
    "logged-in devices/sessions, Telegram's own servers in the instant "
    "before deletion completes). Deleting the message reduces exposure but "
    "is NOT guaranteed erasure."
)
