from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from ..cache import AccountCounts
from ..config import PRIORITY_FOLDERS, TAG_TO_FOLDER, load_accounts


# Maximum per-account priority rows we attach to the dashboard before
# collapsing the remainder behind the 'Select Mailbox' entry point.
# Telegram's practical inline-keyboard row limit is ~100 and a 90-inbox fleet
# would blow past it (dashboards also carry a settings + manual-refresh row,
# a priority totals row, a priority buckets row, and sometimes a group
# gdl:* row). Twelve keeps the dashboard scrollable on a phone and leaves
# ~15 rows of headroom for the other dashboard controls.
_DASHBOARD_PER_ACCOUNT_ROWS = 12


def _pos(prefix: str, token: str, index: int) -> str:
    """Callback data for a positional (row-numbered) button.

    With a ``token`` the button names the exact render it came from
    (``gr:n:ab12cd:3``), so ``mailops.bot.nav`` can tell a stale keyboard
    from a current one instead of resolving position 3 against whatever list
    happens to be stored now. Without one it stays on the legacy
    ``gr:n:3`` form.
    """
    from .nav import position_data

    return position_data(prefix, token, index)


def main_menu(*, owner: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("Dashboard"), KeyboardButton("Critical")],
        [KeyboardButton("Important"), KeyboardButton("Needs reply")],
    ]
    if len(load_accounts()) > 1:  # account picker only makes sense with several
        rows.append([KeyboardButton("Inboxes"), KeyboardButton("Search")])
        rows.append([KeyboardButton("Settings"), KeyboardButton("Manual refresh")])
    else:
        rows.append([KeyboardButton("Search"), KeyboardButton("Settings")])
        rows.append([KeyboardButton("Manual refresh")])
    if owner:
        rows.append([KeyboardButton("Admin")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def _priority_rows(counts: list[AccountCounts]) -> list[list[InlineKeyboardButton]]:
    """Priority browse buttons shared by the private and group dashboards.

    One active inbox: plain Critical / Important / Normal / Noise buttons (no
    account picker). Several: totals plus a per-account row each, capped so
    the keyboard never exceeds Telegram's practical row limit even with a
    90-100 inbox fleet (point 9 of the digest redesign). When the fleet is
    larger than ``_DASHBOARD_PER_ACCOUNT_ROWS`` the busiest inboxes (highest
    P0+P1) take the per-account rows and the remainder is reachable via the
    'Select Mailbox' screen - which is paginated for exactly this case.
    """
    total_p0 = sum(c.p0 for c in counts)
    total_p1 = sum(c.p1 for c in counts)
    if len(counts) == 1:
        c = counts[0]
        return [
            [
                InlineKeyboardButton(f"Critical ({c.p0})", callback_data="b:p0"),
                InlineKeyboardButton(f"Important ({c.p1})", callback_data="b:p1"),
            ],
            [
                InlineKeyboardButton(f"Normal ({c.p2})", callback_data="b:p2"),
                InlineKeyboardButton(f"Noise ({c.p3})", callback_data="b:p3"),
            ],
        ]
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(f"All Critical ({total_p0})", callback_data="b:p0"),
            InlineKeyboardButton(f"All Important ({total_p1})", callback_data="b:p1"),
        ]
    ]
    # Busiest inboxes first so if we have to truncate the overflow goes to
    # inboxes with the least pending mail (which are the ones an operator
    # is least likely to need from a glance at the dashboard).
    ranked = sorted(counts, key=lambda c: (-(c.p0 + c.p1), c.account_id))
    shown = ranked[:_DASHBOARD_PER_ACCOUNT_ROWS]
    for c in shown:
        rows.append(
            [
                InlineKeyboardButton(
                    f"{c.account_id} · Critical ({c.p0})",
                    callback_data=f"ab:{c.account_id}|p0",
                ),
                InlineKeyboardButton(
                    f"{c.account_id} · Important ({c.p1})",
                    callback_data=f"ab:{c.account_id}|p1",
                ),
            ]
        )
    if len(counts) > _DASHBOARD_PER_ACCOUNT_ROWS:
        # Point the admin at the paginated Select Mailbox screen for the
        # remainder - it already covers EVERY registered mailbox with Prev
        # / Next and a per-mailbox pending badge.
        hidden = len(counts) - _DASHBOARD_PER_ACCOUNT_ROWS
        rows.append(
            [
                InlineKeyboardButton(
                    f"+ {hidden} more mailboxes - Select Mailbox",
                    callback_data="dg:pick:0",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("All Normal", callback_data="b:p2"),
            InlineKeyboardButton("All Noise", callback_data="b:p3"),
        ]
    )
    return rows


def dashboard_keyboard(
    counts: list[AccountCounts], *, show_settings: bool = True
) -> InlineKeyboardMarkup:
    rows = _priority_rows(counts)
    if show_settings:
        rows.append(
            [
                InlineKeyboardButton("Set priority rules", callback_data="set:rules"),
                InlineKeyboardButton("Set inbox purposes", callback_data="set:purposes"),
            ]
        )
        rows.append(
            [InlineKeyboardButton("Reply knowledge base", callback_data="set:kb")]
        )
        rows.append(
            [
                InlineKeyboardButton("Manual refresh", callback_data="dash:force"),
                InlineKeyboardButton("Settings", callback_data="nav:settings"),
            ]
        )
    else:
        rows.append([InlineKeyboardButton("Manual refresh", callback_data="dash:force")])
    return InlineKeyboardMarkup(rows)


def group_dashboard_keyboard(
    counts: list[AccountCounts],
    *,
    is_admin: bool = False,
    can_settings: bool = False,
    can_draft: bool = False,
    can_send: bool = False,
) -> InlineKeyboardMarkup:
    """Full control panel for the group chat.

    Non-sensitive buttons (``b:*``, ``ab:*``, ``g:all``, ``dash:force``) run
    directly in the group; the rest emit ``gdl:<action>`` which mints a
    one-shot deep link and edits the message so the user continues the
    sensitive step in a private chat with the bot. Permissions are
    re-checked on every tap, so this keyboard is a hint, never a grant.
    """
    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            InlineKeyboardButton("Dashboard", callback_data="dash"),
            InlineKeyboardButton("All Emails", callback_data="g:all"),
        ]
    )
    rows.append([InlineKeyboardButton("Search", callback_data="gr:search")])
    rows.extend(_priority_rows(counts))
    if can_draft:
        rows.append(
            [
                InlineKeyboardButton("AI Drafts", callback_data="gdl:ai_drafts"),
                InlineKeyboardButton("Reply", callback_data="gdl:reply_pick"),
            ]
        )
    if can_send:
        rows.append(
            [InlineKeyboardButton("Send / Approvals", callback_data="gdl:approvals")]
        )
    if is_admin:
        rows.append(
            [
                InlineKeyboardButton("Manage Users", callback_data="gdl:admin_users"),
                InlineKeyboardButton("Whitelist Groups", callback_data="gdl:admin_groups"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    "Mailboxes (in group)", callback_data="mb:m"
                ),
                InlineKeyboardButton(
                    "Mailboxes (private)", callback_data="gdl:mailboxes"
                ),
            ]
        )
    if can_settings:
        rows.append([InlineKeyboardButton("Settings", callback_data="gdl:settings")])
    if is_admin:
        rows.append([InlineKeyboardButton("Audit Logs", callback_data="gdl:audit")])
    rows.append(
        [InlineKeyboardButton("Manual Refresh", callback_data="dash:force")]
    )
    return InlineKeyboardMarkup(rows)


def group_all_emails_keyboard(
    page: int,
    total_pages: int,
    page_count: int,
    *,
    restricted: frozenset[int] | set[int] | None = None,
    token: str = "",
) -> InlineKeyboardMarkup:
    """Read-only "All Emails" list inside the group - now group-first
    readable (Phase 1/3): each row opens inline via ``gr:n:<N>`` unless its
    mailbox opted out of group sharing (``restricted``), in which case it
    mints a private deep link instead.

    ``token`` binds these buttons to one render (see ``mailops.bot.nav``).
    """
    restricted = restricted or frozenset()
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("Previous page", callback_data="g:all:p"))
    nav.append(
        InlineKeyboardButton(f"Page {page + 1} of {total_pages}", callback_data="noop")
    )
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Next page", callback_data="g:all:n"))
    rows.append(nav)
    row: list[InlineKeyboardButton] = []
    for n in range(1, page_count + 1):
        if n in restricted:
            row.append(
                InlineKeyboardButton(
                    f"🔒 {n} (private)",
                    callback_data=_pos("gr:restricted:", token, n),
                )
            )
        else:
            row.append(
                InlineKeyboardButton(
                    f"Open {n}", callback_data=_pos("gr:n:", token, n)
                )
            )
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton("Open privately", callback_data="gdl:open_pick"),
            InlineKeyboardButton("Back to dashboard", callback_data="dash"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def group_private_link_keyboard(url: str, action_label: str) -> InlineKeyboardMarkup:
    """Shown in the group after a sensitive button is tapped.

    One URL button that deep-links the user into a private bot session
    (``t.me/<bot>?start=mdl_<token>``), plus "Back to dashboard".
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Open privately · {action_label}", url=url)],
            [InlineKeyboardButton("Back to dashboard", callback_data="dash")],
        ]
    )


def group_alert_keyboard(bot_username: str | None = None) -> InlineKeyboardMarkup:
    """Keyboard attached to push alerts posted in a group."""
    rows = [[InlineKeyboardButton("Dashboard", callback_data="dash")]]
    if bot_username:
        rows[0].append(
            InlineKeyboardButton(
                "Open privately", url=f"https://t.me/{bot_username.lstrip('@')}"
            )
        )
    return InlineKeyboardMarkup(rows)


def page_keyboard(
    page: int,
    total_pages: int,
    page_count: int,
    *,
    group: bool = False,
    restricted: frozenset[int] | set[int] | None = None,
    token: str = "",
) -> InlineKeyboardMarkup:
    """Email-list keyboard.

    ``group=False``: original private-chat behaviour (every item opens via
    ``n:<N>``).

    ``group=True``: group-first reading (Phase 1/3). Items are opened via
    ``gr:n:<N>`` so the dispatcher's narrow read-only group allow-list is
    the only thing that can reach them. ``restricted`` is the set of
    1-based positions on THIS page whose mailbox opted out of group
    sharing (``mailops.policy.group_shareable`` is False) - those render a
    locked button that mints a private deep link instead of opening inline,
    so a restricted mailbox's content is never rendered in group chat
    history even via this list.

    ``token`` names the render these buttons belong to (see
    ``mailops.bot.nav``). When set, positional buttons carry it as
    ``gr:n:<token>:<N>`` so a tap from a superseded keyboard is detected as
    stale instead of resolving to whatever is now at position N — the
    "opened a different mailbox's email" risk. Empty token keeps the legacy
    bare-position form.
    """
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("Previous page", callback_data="pg:p"))
    nav.append(
        InlineKeyboardButton(f"Page {page + 1} of {total_pages}", callback_data="noop")
    )
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Next page", callback_data="pg:n"))
    rows.append(nav)

    restricted = restricted or frozenset()
    row: list[InlineKeyboardButton] = []
    for n in range(1, page_count + 1):
        if group:
            if n in restricted:
                row.append(
                    InlineKeyboardButton(
                        f"🔒 {n} (private)",
                        callback_data=_pos("gr:restricted:", token, n),
                    )
                )
            else:
                row.append(
                    InlineKeyboardButton(
                        f"Open {n}", callback_data=_pos("gr:n:", token, n)
                    )
                )
        else:
            row.append(
                InlineKeyboardButton(f"Open {n}", callback_data=_pos("n:", token, n))
            )
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if total_pages > 1:
        jumps: list[InlineKeyboardButton] = []
        for p in range(min(total_pages, 8)):
            label = f"Go page {p + 1}"
            if p == page:
                label = f"· {p + 1} ·"
            jumps.append(InlineKeyboardButton(label, callback_data=f"pj:{p}"))
        for i in range(0, len(jumps), 4):
            rows.append(jumps[i : i + 4])

    rows.append(
        [
            InlineKeyboardButton("Back to dashboard", callback_data="dash"),
            InlineKeyboardButton("Manual refresh", callback_data="page:force"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def thread_keyboard(
    account_id: str,
    mailbox: str,
    msg_id: str,
    *,
    has_prev: bool = False,
    has_next: bool = False,
    group: bool = False,
    body_page: int = 0,
    total_body_pages: int = 1,
) -> InlineKeyboardMarkup:
    """Full email-reading keyboard.

    ``group=False`` (default): the original private-chat keyboard, with
    Mark done / Change priority / Add tag / Mute sender (mailbox-mutating
    shortcuts). Unchanged for backward compatibility.

    ``group=True``: the group-first reading view scoped to the exact
    journey the group-reading fix covers (Back / Reply / Next / Previous /
    Home - see mailops/bot/app.py ``open_thread``). Every callback uses
    the ``gr:`` namespace so the dispatcher's group allow-list only ever
    opens this narrow, read-oriented surface - it never implicitly grants
    the mutating ``prio:``/``tags:``/``mute:``/``done:`` actions a group
    tap for this screen could otherwise reach.
    """
    cb = f"{account_id}|{mailbox}|{msg_id}"
    if not group:
        rows: list[list[InlineKeyboardButton]] = []
        nav = []
        if has_prev:
            nav.append(InlineKeyboardButton("Previous email", callback_data=f"browse:{cb}|p"))
        if has_next:
            nav.append(InlineKeyboardButton("Next email", callback_data=f"browse:{cb}|n"))
        if nav:
            rows.append(nav)
        rows.append(
            [
                InlineKeyboardButton("Reply", callback_data=f"reply:{cb}"),
                InlineKeyboardButton("Mark done", callback_data=f"done:{cb}"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton("Change priority", callback_data=f"prio:{cb}"),
                InlineKeyboardButton("Add tag", callback_data=f"tags:{cb}"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton("Mute sender", callback_data=f"mute:{cb}"),
                InlineKeyboardButton("Back to list", callback_data="backpage"),
            ]
        )
        return InlineKeyboardMarkup(rows)

    # --- group-first reading keyboard -------------------------------------
    rows = []
    body_nav: list[InlineKeyboardButton] = []
    if total_body_pages > 1:
        if body_page > 0:
            body_nav.append(
                InlineKeyboardButton("◀ Body", callback_data=f"gr:bp:{cb}|{body_page - 1}")
            )
        body_nav.append(
            InlineKeyboardButton(
                f"Body {body_page + 1}/{total_body_pages}", callback_data="noop"
            )
        )
        if body_page + 1 < total_body_pages:
            body_nav.append(
                InlineKeyboardButton("Body ▶", callback_data=f"gr:bp:{cb}|{body_page + 1}")
            )
        rows.append(body_nav)
    email_nav: list[InlineKeyboardButton] = []
    if has_prev:
        email_nav.append(InlineKeyboardButton("Previous email", callback_data=f"gr:b:{cb}|p"))
    if has_next:
        email_nav.append(InlineKeyboardButton("Next email", callback_data=f"gr:b:{cb}|n"))
    if email_nav:
        rows.append(email_nav)
    rows.append(
        [
            InlineKeyboardButton("Reply", callback_data=f"gr:reply:{cb}"),
            InlineKeyboardButton("Back to list", callback_data="gr:back"),
        ]
    )
    rows.append([InlineKeyboardButton("Home (dashboard)", callback_data="dash")])
    return InlineKeyboardMarkup(rows)


def purposes_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for acc in load_accounts():
        rows.append(
            [
                InlineKeyboardButton(
                    f"Purpose: {acc.id}", callback_data=f"set:purpose:{acc.id}"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("Overall notes", callback_data="set:context")]
    )
    rows.append(
        [InlineKeyboardButton("Mark purposes complete", callback_data="set:purposes_done")]
    )
    rows.append(
        [InlineKeyboardButton("Set priority rules (P0–P3)", callback_data="set:rules")]
    )
    rows.append(
        [InlineKeyboardButton("Reply knowledge base", callback_data="set:kb")]
    )
    rows.append([InlineKeyboardButton("Back to dashboard", callback_data="dash")])
    return InlineKeyboardMarkup(rows)


def kb_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for acc in load_accounts():
        rows.append(
            [
                InlineKeyboardButton(
                    f"View KB: {acc.id}", callback_data=f"set:kb:view:{acc.id}"
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    f"Add note to {acc.id} KB", callback_data=f"set:kb:note:{acc.id}"
                )
            ]
        )
    rows.append([InlineKeyboardButton("Back to purposes", callback_data="set:purposes")])
    rows.append([InlineKeyboardButton("Back to dashboard", callback_data="dash")])
    return InlineKeyboardMarkup(rows)


def rules_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Edit Critical P0", callback_data="set:rule:p0"),
                InlineKeyboardButton("Edit Important P1", callback_data="set:rule:p1"),
            ],
            [
                InlineKeyboardButton("Edit Normal P2", callback_data="set:rule:p2"),
                InlineKeyboardButton("Edit Noise P3", callback_data="set:rule:p3"),
            ],
            [InlineKeyboardButton("Show full rules card", callback_data="set:rules")],
            [InlineKeyboardButton("Mark rules complete", callback_data="set:rules_done")],
            [InlineKeyboardButton("Back to dashboard", callback_data="dash")],
        ]
    )


def list_item_buttons(
    account_id: str, mailbox: str, items: list[tuple[str, str]]
) -> InlineKeyboardMarkup:
    rows = []
    for msg_id, label in items:
        cb = f"{account_id}|{mailbox}|{msg_id}"
        rows.append([InlineKeyboardButton(label[:60], callback_data=f"open:{cb}")])
    rows.append([InlineKeyboardButton("Back to dashboard", callback_data="dash")])
    return InlineKeyboardMarkup(rows)


def accounts_keyboard(accounts: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{aid} · {email}", callback_data=f"acct:{aid}")]
        for aid, email in accounts
    ]
    rows.append([InlineKeyboardButton("Back to dashboard", callback_data="dash")])
    return InlineKeyboardMarkup(rows)


def account_folders_keyboard(account_id: str) -> InlineKeyboardMarkup:
    rows = []
    for folder in PRIORITY_FOLDERS:
        rows.append(
            [
                InlineKeyboardButton(
                    folder.replace("INBOX.", ""),
                    callback_data=f"list:{account_id}|{folder}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                f"{account_id} · Critical", callback_data=f"ab:{account_id}|p0"
            ),
            InlineKeyboardButton(
                f"{account_id} · Important", callback_data=f"ab:{account_id}|p1"
            ),
        ]
    )
    rows.append([InlineKeyboardButton("Back to dashboard", callback_data="dash")])
    return InlineKeyboardMarkup(rows)


def priority_keyboard(account_id: str, mailbox: str, msg_id: str) -> InlineKeyboardMarkup:
    cb = f"{account_id}|{mailbox}|{msg_id}"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Critical", callback_data=f"setprio:{cb}|0"),
                InlineKeyboardButton("Important", callback_data=f"setprio:{cb}|1"),
            ],
            [
                InlineKeyboardButton("Normal", callback_data=f"setprio:{cb}|2"),
                InlineKeyboardButton("Noise", callback_data=f"setprio:{cb}|3"),
            ],
            [InlineKeyboardButton("Back to email", callback_data=f"open:{cb}")],
        ]
    )


def tags_keyboard(account_id: str, mailbox: str, msg_id: str) -> InlineKeyboardMarkup:
    cb = f"{account_id}|{mailbox}|{msg_id}"
    rows = []
    row = []
    for tag in TAG_TO_FOLDER:
        row.append(InlineKeyboardButton(tag, callback_data=f"settag:{cb}|{tag}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Back to email", callback_data=f"open:{cb}")])
    return InlineKeyboardMarkup(rows)


def reply_keyboard(
    account_id: str, mailbox: str, msg_id: str, *, group: bool = False
) -> InlineKeyboardMarkup:
    cb = f"{account_id}|{mailbox}|{msg_id}"
    if not group:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("AI draft reply", callback_data=f"aidraft:{cb}"),
                    InlineKeyboardButton("Type my reply", callback_data=f"writeme:{cb}"),
                ],
                [InlineKeyboardButton("Cancel", callback_data=f"open:{cb}")],
            ]
        )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("AI draft reply", callback_data=f"gr:aidraft:{cb}"),
                InlineKeyboardButton("Type my reply", callback_data=f"gr:writeme:{cb}"),
            ],
            [InlineKeyboardButton("Cancel", callback_data=f"gr:o:{cb}")],
        ]
    )


def final_send_keyboard(token: str, *, group: bool = False) -> InlineKeyboardMarkup:
    """Single-use confirmation. The token is the only thing that can send."""
    prefix = "gr:" if group else ""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Send this reply", callback_data=f"{prefix}go:{token}")],
            [InlineKeyboardButton("Cancel", callback_data=f"{prefix}nogo:{token}")],
        ]
    )


def draft_keyboard(
    account_id: str,
    mailbox: str,
    msg_id: str,
    *,
    can_send: bool = True,
    group: bool = False,
) -> InlineKeyboardMarkup:
    cb = f"{account_id}|{mailbox}|{msg_id}"
    prefix = "gr:" if group else ""
    first = []
    if can_send:
        first.append(InlineKeyboardButton("Confirm send", callback_data=f"{prefix}send:{cb}"))
    first.append(InlineKeyboardButton("Edit reply", callback_data=f"{prefix}writeme:{cb}"))
    back_cb = f"gr:o:{cb}" if group else f"open:{cb}"
    return InlineKeyboardMarkup(
        [
            first,
            [
                InlineKeyboardButton("Regenerate draft", callback_data=f"{prefix}aidraft:{cb}"),
                InlineKeyboardButton("Back to email", callback_data=back_cb),
            ],
        ]
    )


def settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    min_p = settings.get("min_alert_priority", 1)
    noise = settings.get("noise_push", "off")
    mins = int(settings.get("sync_interval_seconds", 3600)) // 60
    confirmed = "yes" if settings.get("context_confirmed") else "no"
    digest_on = "ON" if settings.get("digest_enabled", True) else "OFF"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Alerts: Critical–P{min_p}", callback_data="set:alertcycle")],
            [InlineKeyboardButton(f"Noise push: {noise}", callback_data="set:noisecycle")],
            [InlineKeyboardButton(f"Auto-check every {mins}m", callback_data="set:synccycle")],
            [InlineKeyboardButton(f"Notification digest: {digest_on}", callback_data="set:notify")],
            [InlineKeyboardButton(f"Purposes confirmed: {confirmed}", callback_data="set:purposes")],
            [
                InlineKeyboardButton(
                    f"Rules confirmed: {'yes' if settings.get('rules_confirmed') else 'no'}",
                    callback_data="set:rules",
                )
            ],
            [InlineKeyboardButton("VIP senders", callback_data="set:vip")],
            [InlineKeyboardButton("Back to dashboard", callback_data="dash")],
        ]
    )


def notifications_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Notification Policy screen (point 8 of the digest redesign).

    Every control here is also exposed as plain settings keys in
    ``data/settings.json`` so power users / scripts can toggle them
    without the Telegram UI. The 15-minute default is spelled out on
    the buttons so operators can see what they are picking.
    """
    digest_on = bool(settings.get("digest_enabled", True))
    interval = int(settings.get("digest_interval_seconds", 900))
    minutes = max(1, interval // 60)
    p0_imm = bool(settings.get("digest_p0_immediate", False))
    include_p2 = bool(settings.get("digest_include_p2", False))
    silent_first = bool(settings.get("digest_initial_sync_silent", True))
    muted = len(settings.get("digest_mailboxes_disabled") or [])
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Digest: {'ON' if digest_on else 'OFF'}",
                    callback_data="set:notify:toggle",
                ),
                InlineKeyboardButton(
                    f"Interval: {minutes}m",
                    callback_data="set:notify:interval",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Immediate P0: {'ON' if p0_imm else 'OFF'}",
                    callback_data="set:notify:p0imm",
                ),
                InlineKeyboardButton(
                    f"Include P2: {'ON' if include_p2 else 'OFF'}",
                    callback_data="set:notify:p2",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Silent first sync: {'ON' if silent_first else 'OFF'}",
                    callback_data="set:notify:silent",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Per-mailbox mute ({muted})",
                    callback_data="set:notify:mb:0",
                ),
            ],
            [
                InlineKeyboardButton("Unhealthy Accounts", callback_data="health:view"),
            ],
            [
                InlineKeyboardButton("View Summary", callback_data="dg:pick:0"),
                InlineKeyboardButton("Refresh Summary", callback_data="dg:refresh"),
            ],
            [InlineKeyboardButton("Back to settings", callback_data="nav:settings")],
        ]
    )


def notifications_mailboxes_keyboard(
    page: int, settings: dict
) -> tuple[str, InlineKeyboardMarkup]:
    """Paginated per-mailbox enable/disable grid.

    Scales to 90-100+ accounts (point 9). Each button is a one-tap toggle:
    tapping an inbox name flips its membership in
    ``digest_mailboxes_disabled`` and bounces the user back to the same
    page.
    """
    page_size = max(5, int(settings.get("digest_mailbox_page_size", 20)))
    disabled = {str(x).strip() for x in (settings.get("digest_mailboxes_disabled") or [])}
    accounts = load_accounts()
    accounts.sort(key=lambda a: (a.display_name or a.id).lower())
    total = len(accounts)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    start = page * page_size
    slice_ = accounts[start : start + page_size]
    rows: list[list[InlineKeyboardButton]] = []
    for acc in slice_:
        is_off = acc.id in disabled
        state = "muted" if is_off else "on"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{(acc.display_name or acc.id)[:28]}  [{state}]",
                    callback_data=f"set:notify:mb:toggle:{acc.id}:{page}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"set:notify:mb:{page - 1}"))
    nav.append(
        InlineKeyboardButton(f"Page {page + 1}/{pages}", callback_data="noop")
    )
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("Next", callback_data=f"set:notify:mb:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("Back", callback_data="set:notify")])
    text = (
        f"Per-mailbox digest - {total} mailbox(es), {len(disabled)} muted.\n"
        f"Tap a name to mute / unmute it. Muted mailboxes are still synced "
        f"but excluded from the Telegram digest counts."
    )
    return text, InlineKeyboardMarkup(rows)


def home_inline(counts: dict[str, int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"All Critical ({counts.get('p0', 0)})", callback_data="b:p0"
                ),
                InlineKeyboardButton(
                    f"All Important ({counts.get('p1', 0)})", callback_data="b:p1"
                ),
            ],
            [InlineKeyboardButton("Back to dashboard", callback_data="dash")],
        ]
    )
