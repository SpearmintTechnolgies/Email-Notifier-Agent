from __future__ import annotations

import logging
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..config import (
    PRIORITY_TO_FOLDER,
    TAG_TO_FOLDER,
    load_accounts,
    load_runtime,
    load_settings,
    save_settings,
)
from ..dashboard import (
    collect_bucket,
    collect_counts,
    format_dashboard,
    format_page,
    page_slice,
)
from ..db import clear_draft, get_draft, save_draft
from ..himalaya_client import Himalaya
from ..mail_format import format_card
from . import keyboards as kb

log = logging.getLogger("mailops.bot")

WAITING_TEXT: dict[int, dict[str, str]] = {}


def _allowed(user_id: int) -> bool:
    runtime = load_runtime()
    if not runtime.allowed_users:
        return True
    return user_id in runtime.allowed_users


def _parse_ref(ref: str) -> tuple[str, str, str]:
    parts = ref.split("|", 2)
    if len(parts) != 3:
        raise ValueError("bad ref")
    return parts[0], parts[1], parts[2]


def _from_label(envelope: dict[str, Any]) -> str:
    frm = envelope.get("from") or []
    if isinstance(frm, list) and frm and isinstance(frm[0], dict):
        return str(frm[0].get("email") or frm[0].get("name") or "?")
    return str(envelope.get("from") or "?")


def _prio_title(priority: int, account_id: str | None = None) -> str:
    name = {0: "Critical", 1: "Important", 2: "Normal", 3: "Noise"}.get(priority, f"P{priority}")
    if account_id:
        return f"{account_id} · {name}"
    return name


async def guard(update: Update) -> bool:
    user = update.effective_user
    if not user or not _allowed(user.id):
        if update.effective_message:
            await update.effective_message.reply_text("Unauthorized.")
        return False
    return True


async def _edit_or_reply(update: Update, text: str, markup=None) -> None:
    if len(text) > 3900:
        text = text[:3900] + "\n…"
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
            return
        except Exception:  # noqa: BLE001
            pass
    await update.effective_message.reply_text(text, reply_markup=markup)


async def show_dashboard(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, force: bool = False
) -> None:
    note = ""
    if force:
        from ..cache import refresh_all

        refresh_all()
        note = "Manual refresh done — cache rebuilt from IMAP."
    counts = collect_counts(force=False)
    text = format_dashboard(counts, force_note=note)
    await _edit_or_reply(update, text, kb.dashboard_keyboard(counts))


def _load_view_items(
    context: ContextTypes.DEFAULT_TYPE, *, force: bool = False
) -> list[dict[str, str]]:
    view = context.user_data.get("view") or {}
    priority = int(view.get("priority", 1))
    account_id = view.get("account_id")
    items = collect_bucket(priority, account_id, force=force)
    context.user_data["view_items"] = items
    context.user_data["browse_refs"] = [it["ref"] for it in items]
    return items


async def show_bucket_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    priority: int,
    account_id: str | None = None,
    page: int = 0,
    reload: bool = True,
    force: bool = False,
) -> None:
    prev = context.user_data.get("view") or {}
    context.user_data["view"] = {
        "priority": priority,
        "account_id": account_id,
        "page": page,
        "flagged": prev.get("flagged") if prev.get("priority") == priority else False,
        "search": prev.get("search"),
    }
    if force:
        items = _load_view_items(context, force=True)
    elif reload:
        items = _load_view_items(context, force=False)
    else:
        items = list(context.user_data.get("view_items") or [])
        if not items:
            items = _load_view_items(context, force=False)
    slice_items, page, total_pages = page_slice(items, page)
    context.user_data["view"]["page"] = page
    context.user_data["page_refs"] = [it["ref"] for it in slice_items]
    view = context.user_data.get("view") or {}
    if view.get("flagged"):
        title = "Needs reply"
    elif view.get("search"):
        title = f"Search: {view['search']}"
    else:
        title = _prio_title(priority, account_id)
    text = format_page(title, slice_items, page, total_pages, len(items))
    markup = kb.page_keyboard(page, total_pages, len(slice_items))
    await _edit_or_reply(update, text, markup)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.effective_message.reply_text(
        "Mail dashboard ready.\n"
        "Hourly auto-check caches your folders.\n"
        "Manual refresh = pull IMAP now.\n"
        "Buttons: Open 1…Open 10 · Previous/Next page · Reply.\n"
        "First: Set inbox purposes so ranking knows what matters.",
        reply_markup=kb.main_menu(),
    )
    await show_dashboard(update, context)


async def open_thread(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    account_id: str,
    mailbox: str,
    msg_id: str,
) -> None:
    him = Himalaya()
    card = him.read_card(account_id, msg_id, mailbox)
    ref = f"{account_id}|{mailbox}|{msg_id}"
    refs = list(context.user_data.get("browse_refs") or [])
    index = refs.index(ref) if ref in refs else None
    total = len(refs) if refs else None
    has_prev = bool(index is not None and index > 0)
    has_next = bool(index is not None and index < len(refs) - 1)
    text = format_card(account_id, mailbox, card, index=index, total=total)
    markup = kb.thread_keyboard(
        account_id, mailbox, msg_id, has_prev=has_prev, has_next=has_next
    )
    await _edit_or_reply(update, text, markup)


async def browse_step(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    account_id: str,
    mailbox: str,
    msg_id: str,
    direction: str,
) -> None:
    ref = f"{account_id}|{mailbox}|{msg_id}"
    refs = list(context.user_data.get("browse_refs") or [])
    if ref not in refs:
        await open_thread(update, context, account_id, mailbox, msg_id)
        return
    i = refs.index(ref)
    if direction == "n" and i + 1 < len(refs):
        a, m, mid = _parse_ref(refs[i + 1])
        await open_thread(update, context, a, m, mid)
        return
    if direction == "p" and i > 0:
        a, m, mid = _parse_ref(refs[i - 1])
        await open_thread(update, context, a, m, mid)
        return
    await open_thread(update, context, account_id, mailbox, msg_id)


async def ai_draft(account_id: str, mailbox: str, msg_id: str) -> str:
    from ..reply_kb import build_draft_system_prompt

    him = Himalaya()
    runtime = load_runtime()
    card = him.read_card(account_id, msg_id, mailbox)
    settings = load_settings()
    acc = next((a for a in load_accounts() if a.id == account_id), None)
    purposes = settings.get("account_purposes") or {}
    system = build_draft_system_prompt(
        account_id=account_id,
        account_email=(acc.email if acc else ""),
        inbox_context=str(settings.get("inbox_context") or ""),
        account_purpose=str(purposes.get(account_id) or ""),
    )
    prompt = {
        "model": runtime.model,
        "temperature": 0.35,
        "max_tokens": 700,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    "Draft a reply to this inbound email.\n"
                    f"From: {card.from_line}\n"
                    f"Subject: {card.subject}\n\n"
                    f"{card.body[:3000]}"
                ),
            },
        ],
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{runtime.openai_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {runtime.openai_api_key}",
                "Content-Type": "application/json",
            },
            json=prompt,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


async def show_flagged(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Reuse page UI over flagged items across folders
    him = Himalaya()
    items: list[dict[str, str]] = []
    for acc in load_accounts():
        for folder in [
            "INBOX.P0-Critical",
            "INBOX.P1-Important",
            "INBOX.P2-Normal",
            "INBOX.Waiting",
        ]:
            for e in him.envelopes(acc.id, folder, page_size=30):
                flags = e.get("flags") or []
                flagged = any(
                    (isinstance(f, dict) and f.get("iana") == "flagged")
                    or (isinstance(f, str) and "flag" in f.lower())
                    for f in flags
                )
                if not flagged:
                    continue
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
    context.user_data["view"] = {"priority": 1, "account_id": None, "page": 0, "flagged": True}
    context.user_data["view_items"] = items
    context.user_data["browse_refs"] = [it["ref"] for it in items]
    slice_items, page, total_pages = page_slice(items, 0)
    context.user_data["page_refs"] = [it["ref"] for it in slice_items]
    text = format_page("Needs reply", slice_items, page, total_pages, len(items))
    await _edit_or_reply(update, text, kb.page_keyboard(page, total_pages, len(slice_items)))


async def show_list_clean(
    update: Update, context: ContextTypes.DEFAULT_TYPE, account_id: str, mailbox: str
) -> None:
    # Map folder to priority browse when possible
    inv = {v: k for k, v in PRIORITY_TO_FOLDER.items()}
    if mailbox in inv:
        await show_bucket_page(
            update, context, priority=inv[mailbox], account_id=account_id, page=0
        )
        return
    him = Himalaya()
    envs = him.envelopes(account_id, mailbox, page_size=50)
    items = []
    for e in envs:
        mid = str(e.get("id") or "")
        items.append(
            {
                "ref": f"{account_id}|{mailbox}|{mid}",
                "subject": str(e.get("subject") or "(no subject)"),
                "from": _from_label(e),
                "account_id": account_id,
                "mailbox": mailbox,
                "msg_id": mid,
            }
        )
    context.user_data["view"] = {
        "priority": 2,
        "account_id": account_id,
        "page": 0,
        "mailbox": mailbox,
    }
    context.user_data["view_items"] = items
    context.user_data["browse_refs"] = [it["ref"] for it in items]
    slice_items, page, total_pages = page_slice(items, 0)
    context.user_data["page_refs"] = [it["ref"] for it in slice_items]
    text = format_page(f"{account_id} · {mailbox}", slice_items, page, total_pages, len(items))
    await _edit_or_reply(update, text, kb.page_keyboard(page, total_pages, len(slice_items)))


async def do_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    q = query.lower()
    him = Himalaya()
    items: list[dict[str, str]] = []
    for acc in load_accounts():
        for folder in [
            "INBOX.P0-Critical",
            "INBOX.P1-Important",
            "INBOX.P2-Normal",
            "INBOX",
            "INBOX.Waiting",
        ]:
            for e in him.envelopes(acc.id, folder, page_size=25):
                blob = f"{e.get('subject')} {_from_label(e)}".lower()
                if q not in blob:
                    continue
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
                if len(items) >= 100:
                    break
            if len(items) >= 100:
                break
        if len(items) >= 100:
            break
    context.user_data["view"] = {"priority": 1, "account_id": None, "page": 0, "search": query}
    context.user_data["view_items"] = items
    context.user_data["browse_refs"] = [it["ref"] for it in items]
    slice_items, page, total_pages = page_slice(items, 0)
    context.user_data["page_refs"] = [it["ref"] for it in slice_items]
    text = format_page(f"Search: {query}", slice_items, page, total_pages, len(items))
    await _edit_or_reply(update, text, kb.page_keyboard(page, total_pages, len(slice_items)))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text = (update.effective_message.text or "").strip()
    uid = update.effective_user.id

    if text == "Dashboard":
        await show_dashboard(update, context, force=False)
        return
    if text in ("Manual refresh", "Refresh"):
        await show_dashboard(update, context, force=True)
        return
    if text == "Critical":
        await show_bucket_page(update, context, priority=0, page=0)
        return
    if text == "Important":
        await show_bucket_page(update, context, priority=1, page=0)
        return
    if text == "Needs reply":
        await show_flagged(update, context)
        return
    if text == "Inboxes":
        accounts = [(a.id, a.email) for a in load_accounts()]
        await update.effective_message.reply_text(
            "Accounts", reply_markup=kb.accounts_keyboard(accounts)
        )
        return
    if text == "Settings":
        settings = load_settings()
        ctx = (settings.get("inbox_context") or "")[:400]
        await update.effective_message.reply_text(
            "Settings\n\nCurrent context:\n" + (ctx or "(empty — tap Context to set)"),
            reply_markup=kb.settings_keyboard(settings),
        )
        return
    if text == "Search":
        WAITING_TEXT[uid] = {"mode": "search"}
        await update.effective_message.reply_text(
            "Type a search query (from/subject). Send Cancel to abort."
        )
        return
    if text.lower() == "cancel":
        WAITING_TEXT.pop(uid, None)
        await update.effective_message.reply_text("Cancelled.", reply_markup=kb.main_menu())
        return

    pending = WAITING_TEXT.get(uid)
    if pending and pending.get("mode") == "search":
        WAITING_TEXT.pop(uid, None)
        await do_search(update, context, text)
        return
    if pending and pending.get("mode") == "write":
        account_id, mailbox, msg_id = pending["account_id"], pending["mailbox"], pending["msg_id"]
        WAITING_TEXT.pop(uid, None)
        save_draft(uid, account_id, mailbox, msg_id, text)
        await update.effective_message.reply_text(
            f"Draft saved:\n\n{text[:3000]}",
            reply_markup=kb.draft_keyboard(account_id, mailbox, msg_id),
        )
        return
    if pending and pending.get("mode") == "vip":
        WAITING_TEXT.pop(uid, None)
        settings = load_settings()
        vips = list(settings.get("vip_senders") or [])
        email = text.strip().lower()
        if email and email not in vips:
            vips.append(email)
            settings["vip_senders"] = vips
            save_settings(settings)
        await update.effective_message.reply_text(
            f"VIP list ({len(vips)}):\n" + "\n".join(vips[:30] or ["(none)"]),
            reply_markup=kb.settings_keyboard(settings),
        )
        return
    if pending and pending.get("mode") == "context":
        WAITING_TEXT.pop(uid, None)
        settings = load_settings()
        settings["inbox_context"] = text.strip()
        settings["context_confirmed"] = False
        save_settings(settings)
        await update.effective_message.reply_text(
            "Overall ranking rules saved.\n"
            "Still set each inbox purpose, then tap Mark purposes complete.\n\n"
            + text.strip()[:1200],
            reply_markup=kb.purposes_keyboard(),
        )
        return
    if pending and pending.get("mode") == "kb_note":
        from ..reply_kb import append_account_kb_note, format_kb_card

        account_id = pending["account_id"]
        WAITING_TEXT.pop(uid, None)
        append_account_kb_note(account_id, text.strip())
        await update.effective_message.reply_text(
            f"Added to {account_id} reply KB.\n\n" + format_kb_card(account_id)[:3500],
            reply_markup=kb.kb_keyboard(),
        )
        return
    if pending and pending.get("mode") == "purpose":
        account_id = pending.get("account_id") or ""
        WAITING_TEXT.pop(uid, None)
        settings = load_settings()
        purposes = dict(settings.get("account_purposes") or {})
        purposes[account_id] = text.strip()
        settings["account_purposes"] = purposes
        settings["context_confirmed"] = False
        save_settings(settings)
        await update.effective_message.reply_text(
            f"Purpose saved for {account_id}.\n\n{text.strip()[:1200]}\n\n"
            "Set the other inboxes, then tap Mark purposes complete.",
            reply_markup=kb.purposes_keyboard(),
        )
        return
    if pending and pending.get("mode") == "rule":
        from ..priority_rules import parse_rule_message, save_bucket_rules, format_rules_card

        bucket = pending.get("bucket") or "p1"
        WAITING_TEXT.pop(uid, None)
        patch = parse_rule_message(text)
        if not patch:
            await update.effective_message.reply_text(
                "Empty rules — try again.", reply_markup=kb.rules_keyboard()
            )
            return
        rules = save_bucket_rules(bucket, patch)
        settings = load_settings()
        settings["rules_confirmed"] = False
        save_settings(settings)
        kws = rules[bucket].get("keywords") or []
        await update.effective_message.reply_text(
            f"Saved {bucket.upper()} · {len(kws)} keywords.\n\n"
            + format_rules_card(rules)[:3000],
            reply_markup=kb.rules_keyboard(),
        )
        return

    await update.effective_message.reply_text(
        "Use the menu buttons below.", reply_markup=kb.main_menu()
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    him = Himalaya()
    uid = update.effective_user.id

    if data in ("dash", "nav:home"):
        await show_dashboard(update, context, force=False)
        return
    if data == "dash:force":
        await show_dashboard(update, context, force=True)
        return
    if data == "page:force":
        view = context.user_data.get("view") or {}
        await show_bucket_page(
            update,
            context,
            priority=int(view.get("priority", 1)),
            account_id=view.get("account_id"),
            page=int(view.get("page", 0)),
            force=True,
        )
        return
    if data == "noop":
        return
    if data == "nav:settings":
        settings = load_settings()
        await q.edit_message_text(
            "Settings", reply_markup=kb.settings_keyboard(settings)
        )
        return
    if data == "set:rules":
        from ..priority_rules import ensure_priority_rules, format_rules_card

        rules = ensure_priority_rules()
        await q.edit_message_text(
            format_rules_card(rules)[:3900], reply_markup=kb.rules_keyboard()
        )
        return
    if data.startswith("set:rule:"):
        from ..priority_rules import ensure_priority_rules, format_edit_prompt

        bucket = data.split(":")[-1]
        if bucket not in ("p0", "p1", "p2", "p3"):
            await q.answer("Unknown bucket", show_alert=True)
            return
        WAITING_TEXT[uid] = {"mode": "rule", "bucket": bucket}
        rules = ensure_priority_rules()
        await q.edit_message_text(format_edit_prompt(bucket, rules)[:3900])
        return
    if data == "set:rules_done":
        from ..priority_rules import ensure_priority_rules

        settings = load_settings()
        rules = ensure_priority_rules(settings)
        empty = [k for k in ("p0", "p1", "p3") if not (rules[k].get("keywords") or [])]
        if empty:
            await q.answer("Still empty: " + ", ".join(empty), show_alert=True)
            return
        settings["rules_confirmed"] = True
        save_settings(settings)
        await q.edit_message_text(
            "Priority rules confirmed. Keyword matcher is active "
            "(Critical → Noise → Important → Normal, then AI if no hit).",
            reply_markup=kb.dashboard_keyboard(collect_counts(force=False)),
        )
        return
    if data == "set:purposes":
        settings = load_settings()
        purposes = settings.get("account_purposes") or {}
        lines = ["Set what each inbox is for.", ""]
        for acc in load_accounts():
            cur = (purposes.get(acc.id) or "").strip()
            lines.append(f"{acc.id} <{acc.email}>")
            lines.append(f"  {(cur[:160] + '…') if len(cur) > 160 else (cur or '(not set)')}")
            lines.append("")
        overall = (settings.get("inbox_context") or "").strip()
        lines.append("Overall notes:")
        lines.append(overall[:400] if overall else "(not set)")
        lines.append("")
        lines.append("Tap an inbox → send one message describing its purpose.")
        lines.append("Also set Priority rules (P0–P3 keywords) — that drives sorting.")
        lines.append("Reply knowledge base drives AI draft links/rates.")
        await q.edit_message_text("\n".join(lines)[:3900], reply_markup=kb.purposes_keyboard())
        return
    if data == "set:kb":
        await q.edit_message_text(
            "Reply knowledge base\n\n"
            "AI drafts use this KB for essential links, packages/prices, tone, and signature.\n"
            "Files live in mailops/data/kb/ — Telegram notes append; full edits can be in those files.\n"
            "Hermes Phase 2 can deepen learning on top of this KB.",
            reply_markup=kb.kb_keyboard(),
        )
        return
    if data.startswith("set:kb:view:"):
        from ..reply_kb import format_kb_card

        account_id = data.split(":")[-1]
        await q.edit_message_text(
            format_kb_card(account_id)[:3900],
            reply_markup=kb.kb_keyboard(),
        )
        return
    if data.startswith("set:kb:note:"):
        account_id = data.split(":")[-1]
        WAITING_TEXT[uid] = {"mode": "kb_note", "account_id": account_id}
        await q.edit_message_text(
            f"Send ONE message to append to the {account_id} reply KB.\n"
            "Examples:\n"
            "- Listing fee starts at $X\n"
            "- Always CC advertising@…\n"
            "- New media kit URL: https://…\n\n"
            "Send Cancel to abort."
        )
        return
    if data.startswith("set:purpose:"):
        account_id = data.split(":", 2)[2]
        acc = next((a for a in load_accounts() if a.id == account_id), None)
        WAITING_TEXT[uid] = {"mode": "purpose", "account_id": account_id}
        settings = load_settings()
        cur = ((settings.get("account_purposes") or {}).get(account_id) or "").strip()
        await q.edit_message_text(
            f"Purpose for {account_id}"
            + (f" <{acc.email}>" if acc else "")
            + "\n\nSend ONE message, e.g.\n"
            "Coinography ads/sales inbox — real advertisers & listing partners are important; "
            "cold RFQ/procurement is noise.\n\n"
            "Current:\n"
            + (cur or "(empty)")
        )
        return
    if data == "set:purposes_done":
        settings = load_settings()
        purposes = settings.get("account_purposes") or {}
        missing = [
            a.id for a in load_accounts() if not (purposes.get(a.id) or "").strip()
        ]
        if missing or not (settings.get("inbox_context") or "").strip():
            await q.answer(
                "Still missing: " + (", ".join(missing) or "overall rules"),
                show_alert=True,
            )
            return
        settings["context_confirmed"] = True
        save_settings(settings)
        await q.edit_message_text(
            "Purposes confirmed. New mail will be ranked with your rules.",
            reply_markup=kb.dashboard_keyboard(collect_counts(force=False)),
        )
        return
    if data.startswith("b:p"):
        priority = int(data.split("p", 1)[1])
        await show_bucket_page(update, context, priority=priority, page=0)
        return
    if data.startswith("ab:"):
        rest = data.split(":", 1)[1]
        account_id, pcode = rest.split("|", 1)
        priority = int(pcode[1]) if pcode.startswith("p") else int(pcode)
        await show_bucket_page(
            update, context, priority=priority, account_id=account_id, page=0
        )
        return
    if data == "pg:n":
        view = context.user_data.get("view") or {}
        await show_bucket_page(
            update,
            context,
            priority=int(view.get("priority", 1)),
            account_id=view.get("account_id"),
            page=int(view.get("page", 0)) + 1,
            reload=False,
        )
        return
    if data == "pg:p":
        view = context.user_data.get("view") or {}
        await show_bucket_page(
            update,
            context,
            priority=int(view.get("priority", 1)),
            account_id=view.get("account_id"),
            page=int(view.get("page", 0)) - 1,
            reload=False,
        )
        return
    if data.startswith("pj:"):
        page = int(data.split(":", 1)[1])
        view = context.user_data.get("view") or {}
        await show_bucket_page(
            update,
            context,
            priority=int(view.get("priority", 1)),
            account_id=view.get("account_id"),
            page=page,
            reload=False,
        )
        return
    if data.startswith("n:"):
        num = int(data.split(":", 1)[1])
        page_refs = list(context.user_data.get("page_refs") or [])
        if num < 1 or num > len(page_refs):
            await q.answer("That Open button is empty on this page", show_alert=True)
            return
        account_id, mailbox, msg_id = _parse_ref(page_refs[num - 1])
        await open_thread(update, context, account_id, mailbox, msg_id)
        return
    if data == "backpage":
        view = context.user_data.get("view") or {}
        if view.get("flagged"):
            await show_flagged(update, context)
            return
        await show_bucket_page(
            update,
            context,
            priority=int(view.get("priority", 1)),
            account_id=view.get("account_id"),
            page=int(view.get("page", 0)),
            reload=True,
        )
        return
    if data == "nav:flagged":
        await show_flagged(update, context)
        return
    if data == "nav:accounts":
        accounts = [(a.id, a.email) for a in load_accounts()]
        await q.edit_message_text("Accounts", reply_markup=kb.accounts_keyboard(accounts))
        return
    if data.startswith("acct:"):
        aid = data.split(":", 1)[1]
        await q.edit_message_text(
            f"Account {aid}", reply_markup=kb.account_folders_keyboard(aid)
        )
        return
    if data.startswith("list:"):
        _, ref = data.split(":", 1)
        account_id, mailbox = ref.split("|", 1)
        await show_list_clean(update, context, account_id, mailbox)
        return
    if data.startswith("browse:"):
        payload, direction = data.split(":", 1)[1].rsplit("|", 1)
        account_id, mailbox, msg_id = _parse_ref(payload)
        await browse_step(update, context, account_id, mailbox, msg_id, direction)
        return
    if data.startswith("open:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        await open_thread(update, context, account_id, mailbox, msg_id)
        return
    if data.startswith("prio:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        await q.edit_message_reply_markup(
            reply_markup=kb.priority_keyboard(account_id, mailbox, msg_id)
        )
        return
    if data.startswith("setprio:"):
        payload, prio_s = data.split(":", 1)[1].rsplit("|", 1)
        account_id, mailbox, msg_id = _parse_ref(payload)
        dest = PRIORITY_TO_FOLDER[int(prio_s)]
        him.move(account_id, msg_id, dest, mailbox)
        await open_thread(update, context, account_id, dest, msg_id)
        return
    if data.startswith("tags:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        await q.edit_message_reply_markup(
            reply_markup=kb.tags_keyboard(account_id, mailbox, msg_id)
        )
        return
    if data.startswith("settag:"):
        payload, tag = data.split(":", 1)[1].rsplit("|", 1)
        account_id, mailbox, msg_id = _parse_ref(payload)
        folder = TAG_TO_FOLDER[tag]
        r = him.copy(account_id, msg_id, folder, mailbox)
        await q.answer(f"Tagged {tag}" if r.ok else "Tag failed", show_alert=not r.ok)
        await open_thread(update, context, account_id, mailbox, msg_id)
        return
    if data.startswith("done:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        him.move(account_id, msg_id, "INBOX.Handled", mailbox)
        him.flag_remove(account_id, msg_id, "flagged", "INBOX.Handled")
        ref = f"{account_id}|{mailbox}|{msg_id}"
        refs = [x for x in (context.user_data.get("browse_refs") or []) if x != ref]
        context.user_data["browse_refs"] = refs
        items = [it for it in (context.user_data.get("view_items") or []) if it.get("ref") != ref]
        context.user_data["view_items"] = items
        if refs:
            # open next remaining, or return to page
            a, m, mid = _parse_ref(refs[0])
            view = context.user_data.get("view") or {}
            await show_bucket_page(
                update,
                context,
                priority=int(view.get("priority", 1)),
                account_id=view.get("account_id"),
                page=int(view.get("page", 0)),
                reload=False,
            )
        else:
            await show_dashboard(update, context)
        return
    if data.startswith("mute:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        card = him.read_card(account_id, msg_id, mailbox)
        email = (card.from_email or "").lower()
        settings = load_settings()
        muted = list(settings.get("muted_senders") or [])
        if email:
            if email not in muted:
                muted.append(email)
                settings["muted_senders"] = muted
                save_settings(settings)
            him.move(account_id, msg_id, "INBOX.P3-Noise", mailbox)
            await q.edit_message_text(f"Muted {email} → Noise")
        else:
            await q.answer("Could not detect sender email", show_alert=True)
        return
    if data.startswith("reply:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        card = him.read_card(account_id, msg_id, mailbox)
        await q.edit_message_text(
            f"Reply to:\n{card.subject}\nFrom: {card.from_line}",
            reply_markup=kb.reply_keyboard(account_id, mailbox, msg_id),
        )
        return
    if data.startswith("writeme:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        WAITING_TEXT[uid] = {
            "mode": "write",
            "account_id": account_id,
            "mailbox": mailbox,
            "msg_id": msg_id,
        }
        await q.edit_message_text("Send your reply text as the next message.")
        return
    if data.startswith("aidraft:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        await q.edit_message_text("Drafting…")
        try:
            draft = await ai_draft(account_id, mailbox, msg_id)
        except Exception as exc:  # noqa: BLE001
            await q.edit_message_text(f"Draft failed: {exc}")
            return
        save_draft(uid, account_id, mailbox, msg_id, draft)
        await q.edit_message_text(
            f"AI draft:\n\n{draft[:3500]}",
            reply_markup=kb.draft_keyboard(account_id, mailbox, msg_id),
        )
        return
    if data.startswith("send:"):
        account_id, mailbox, msg_id = _parse_ref(data.split(":", 1)[1])
        draft = get_draft(uid)
        if not draft or draft.get("msg_id") != msg_id:
            await q.answer("No draft — write or AI draft first", show_alert=True)
            return
        acc = next((a for a in load_accounts() if a.id == account_id), None)
        from_addr = acc.email if acc else None
        await q.edit_message_text("Sending…")
        r = him.reply_send(account_id, msg_id, draft["body"], mailbox, from_addr)
        ok_text = (r.stdout or "") + (r.stderr or "")
        sent_ok = r.ok and ("successfully sent" in ok_text.lower() or "successfully saved and sent" in ok_text.lower())
        log.info(
            "reply_send account=%s msg=%s mailbox=%s code=%s ok=%s out=%s",
            account_id,
            msg_id,
            mailbox,
            r.code,
            sent_ok,
            ok_text[:500],
        )
        if sent_ok:
            him.move(account_id, msg_id, "INBOX.Waiting", mailbox)
            clear_draft(uid)
            await q.edit_message_text(
                f"Sent via {from_addr or account_id}.\n"
                f"Copy saved to Sent · original filed to Waiting.\n\n"
                f"{ok_text.strip()[:400]}"
            )
        else:
            await q.edit_message_text(
                "Send failed — draft kept so you can retry.\n\n"
                f"{(r.stderr or r.stdout or 'no error detail')[:1500]}"
            )
        return
    if data == "set:alertcycle":
        settings = load_settings()
        cur = int(settings.get("min_alert_priority", 1))
        settings["min_alert_priority"] = (cur + 1) % 4
        save_settings(settings)
        await q.edit_message_reply_markup(reply_markup=kb.settings_keyboard(settings))
        return
    if data == "set:noisecycle":
        settings = load_settings()
        settings["noise_push"] = "daily" if settings.get("noise_push") == "off" else "off"
        save_settings(settings)
        await q.edit_message_reply_markup(reply_markup=kb.settings_keyboard(settings))
        return
    if data == "set:synccycle":
        settings = load_settings()
        # cycle 15m → 30m → 60m → 15m
        cur = int(settings.get("sync_interval_seconds", 3600))
        nxt = {900: 1800, 1800: 3600, 3600: 900}.get(cur, 3600)
        settings["sync_interval_seconds"] = nxt
        save_settings(settings)
        await q.edit_message_reply_markup(reply_markup=kb.settings_keyboard(settings))
        return
    if data == "set:vip":
        WAITING_TEXT[uid] = {"mode": "vip"}
        settings = load_settings()
        vips = settings.get("vip_senders") or []
        await q.edit_message_text(
            "Send a VIP email to add.\nCurrent:\n" + ("\n".join(vips) if vips else "(none)")
        )
        return
    if data == "set:context":
        WAITING_TEXT[uid] = {"mode": "context"}
        settings = load_settings()
        cur = settings.get("inbox_context") or ""
        await q.edit_message_text(
            "Send ONE message describing what mail matters to you.\n\nCurrent:\n"
            + (cur[:1500] if cur else "(empty)")
        )
        return


def build_application(post_init=None) -> Application:
    runtime = load_runtime()
    builder = Application.builder().token(runtime.telegram_token)
    if post_init is not None:
        builder = builder.post_init(post_init)
    app = builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
