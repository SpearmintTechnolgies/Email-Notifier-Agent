from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from ..cache import AccountCounts
from ..config import PRIORITY_FOLDERS, TAG_TO_FOLDER, load_accounts


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("Dashboard"), KeyboardButton("Critical")],
            [KeyboardButton("Important"), KeyboardButton("Needs reply")],
            [KeyboardButton("Inboxes"), KeyboardButton("Search")],
            [KeyboardButton("Settings"), KeyboardButton("Manual refresh")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def dashboard_keyboard(counts: list[AccountCounts]) -> InlineKeyboardMarkup:
    total_p0 = sum(c.p0 for c in counts)
    total_p1 = sum(c.p1 for c in counts)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(f"All Critical ({total_p0})", callback_data="b:p0"),
            InlineKeyboardButton(f"All Important ({total_p1})", callback_data="b:p1"),
        ]
    ]
    for c in counts:
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
    rows.append(
        [
            InlineKeyboardButton("All Normal", callback_data="b:p2"),
            InlineKeyboardButton("All Noise", callback_data="b:p3"),
        ]
    )
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
    return InlineKeyboardMarkup(rows)


def page_keyboard(
    page: int,
    total_pages: int,
    page_count: int,
) -> InlineKeyboardMarkup:
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

    # Open N buttons
    row: list[InlineKeyboardButton] = []
    for n in range(1, page_count + 1):
        row.append(InlineKeyboardButton(f"Open {n}", callback_data=f"n:{n}"))
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
) -> InlineKeyboardMarkup:
    cb = f"{account_id}|{mailbox}|{msg_id}"
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


def reply_keyboard(account_id: str, mailbox: str, msg_id: str) -> InlineKeyboardMarkup:
    cb = f"{account_id}|{mailbox}|{msg_id}"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("AI draft reply", callback_data=f"aidraft:{cb}"),
                InlineKeyboardButton("Type my reply", callback_data=f"writeme:{cb}"),
            ],
            [InlineKeyboardButton("Cancel", callback_data=f"open:{cb}")],
        ]
    )


def draft_keyboard(account_id: str, mailbox: str, msg_id: str) -> InlineKeyboardMarkup:
    cb = f"{account_id}|{mailbox}|{msg_id}"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm send", callback_data=f"send:{cb}"),
                InlineKeyboardButton("Edit reply", callback_data=f"writeme:{cb}"),
            ],
            [
                InlineKeyboardButton("Regenerate draft", callback_data=f"aidraft:{cb}"),
                InlineKeyboardButton("Back to email", callback_data=f"open:{cb}"),
            ],
        ]
    )


def settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    min_p = settings.get("min_alert_priority", 1)
    noise = settings.get("noise_push", "off")
    mins = int(settings.get("sync_interval_seconds", 3600)) // 60
    confirmed = "yes" if settings.get("context_confirmed") else "no"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Alerts: Critical–P{min_p}", callback_data="set:alertcycle")],
            [InlineKeyboardButton(f"Noise push: {noise}", callback_data="set:noisecycle")],
            [InlineKeyboardButton(f"Auto-check every {mins}m", callback_data="set:synccycle")],
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
