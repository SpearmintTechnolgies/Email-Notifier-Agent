"""Turn Himalaya message output into a short Telegram-readable card."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class MailCard:
    from_name: str
    from_email: str
    subject: str
    date: str
    body: str
    reply_to: str = ""
    # Recipient ("To:") header. Added for the group-first reading view
    # (requirement: sender, recipient, subject, date, priority, body all
    # visible without opening a private chat). Default "" keeps every
    # existing positional ``MailCard(...)`` call (production + test
    # doubles) working unchanged.
    to_name: str = ""
    to_email: str = ""

    @property
    def from_line(self) -> str:
        if self.from_name and self.from_email:
            return f"{self.from_name} <{self.from_email}>"
        return self.from_email or self.from_name or "?"

    @property
    def to_line(self) -> str:
        if self.to_name and self.to_email:
            return f"{self.to_name} <{self.to_email}>"
        return self.to_email or self.to_name or ""


def _header_name(h: dict[str, Any]) -> str:
    name = h.get("name")
    if isinstance(name, str):
        return name.lower()
    if isinstance(name, dict):
        if "other" in name:
            return str(name["other"]).lower()
        return str(next(iter(name.values()), "")).lower()
    return str(name or "").lower()


def _addr_list(value: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not isinstance(value, dict):
        return out
    addr = value.get("Address") or value
    if isinstance(addr, dict):
        rows = addr.get("List") or []
    elif isinstance(addr, list):
        rows = addr
    else:
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append((str(row.get("name") or ""), str(row.get("address") or "")))
    return out


def _text_value(value: Any) -> str:
    if isinstance(value, dict) and "Text" in value:
        return str(value["Text"])
    if isinstance(value, str):
        return value
    return ""


def _date_value(value: Any) -> str:
    if isinstance(value, dict) and "DateTime" in value:
        dt = value["DateTime"]
        try:
            return (
                f"{dt.get('year')}-{int(dt.get('month', 0)):02d}-{int(dt.get('day', 0)):02d} "
                f"{int(dt.get('hour', 0)):02d}:{int(dt.get('minute', 0)):02d}"
            )
        except Exception:  # noqa: BLE001
            return str(dt)
    return _text_value(value)


def _part_body(part: dict[str, Any]) -> str:
    body = part.get("body")
    if isinstance(body, dict) and "Text" in body:
        return str(body["Text"])
    if isinstance(body, str):
        return body
    return ""


def parse_message_json(raw: str) -> MailCard | None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    parts = data.get("parts") if isinstance(data, dict) else None
    if not isinstance(parts, list) or not parts:
        return None
    # Prefer first part with useful headers + text body
    part = parts[0]
    headers = part.get("headers") or []
    from_name = from_email = subject = date = reply_to = ""
    to_name = to_email = ""
    for h in headers:
        if not isinstance(h, dict):
            continue
        name = _header_name(h)
        val = h.get("value")
        if name == "from":
            addrs = _addr_list(val)
            if addrs:
                from_name, from_email = addrs[0]
        elif name == "to":
            addrs = _addr_list(val)
            if addrs:
                to_name, to_email = addrs[0]
        elif name == "subject":
            subject = _text_value(val)
        elif name == "date":
            date = _date_value(val)
        elif name in ("reply_to", "reply-to"):
            addrs = _addr_list(val)
            if addrs:
                reply_to = addrs[0][1] or addrs[0][0]
    body = _part_body(part)
    # Walk for a better text body if empty
    if not body.strip():
        for p in parts:
            body = _part_body(p)
            if body.strip():
                break
    return MailCard(
        from_name=from_name,
        from_email=from_email,
        subject=subject or "(no subject)",
        date=date,
        body=clean_body(body),
        reply_to=reply_to,
        to_name=to_name,
        to_email=to_email,
    )


_HEADER_LINE = re.compile(
    r"^(return-path|received|dkim-|arc-|authentication-results|x-|spf-|mime-version|"
    r"content-type|content-transfer|message-id|envelope-to|delivery-date|antispam|"
    r"received-spf|reply-to|from:|to:|subject:|date:)",
    re.I,
)


def clean_body(text: str) -> str:
    if not text:
        return ""
    # quoted-printable leftovers
    text = text.replace("=\r\n", "").replace("=\n", "")
    text = re.sub(r"=([0-9A-Fa-f]{2})", lambda m: bytes.fromhex(m.group(1)).decode("utf-8", "ignore"), text)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # Drop leading header-ish block if present
    while lines and (not lines[0].strip() or _HEADER_LINE.match(lines[0].strip())):
        lines.pop(0)
    # Collapse excessive blank lines
    out: list[str] = []
    blank = 0
    for line in lines:
        if not line.strip():
            blank += 1
            if blank <= 1:
                out.append("")
            continue
        blank = 0
        out.append(line.rstrip())
    return "\n".join(out).strip()


def strip_raw_headers(raw: str) -> str:
    """Fallback when JSON parse fails: drop header block before body."""
    text = raw.replace("\r\n", "\n")
    # Common Himalaya plain render: headers then blank line then body
    parts = re.split(r"\n\s*\n", text, maxsplit=1)
    if len(parts) == 2 and (
        "Return-Path:" in parts[0]
        or "DKIM-Signature:" in parts[0]
        or parts[0].lower().startswith("from:")
    ):
        return clean_body(parts[1])
    # Strip lines that look like headers from the top
    lines = text.split("\n")
    i = 0
    while i < len(lines) and (
        not lines[i].strip()
        or ":" in lines[i][:40]
        or lines[i].startswith(" ")
        or lines[i].startswith("\t")
    ):
        # stop if we hit a normal sentence without colon early
        if i > 0 and lines[i].strip() and ":" not in lines[i][:40] and not lines[i][0].isspace():
            break
        i += 1
    return clean_body("\n".join(lines[i:]))


# Telegram hard-caps a single message at 4096 UTF-16 code units. We budget
# well under that for the body so the header block (subject/from/to/date/
# priority/inbox + page marker) always fits alongside it in one message.
BODY_PAGE_SIZE = 3000

_PRIORITY_LABELS = {0: "Critical (P0)", 1: "Important (P1)", 2: "Normal (P2)", 3: "Noise (P3)"}


def priority_label(priority: int | None) -> str:
    if priority is None:
        return ""
    return _PRIORITY_LABELS.get(int(priority), f"P{priority}")


def body_page_count(body: str, page_size: int = BODY_PAGE_SIZE) -> int:
    if not body:
        return 1
    return max(1, (len(body) + page_size - 1) // page_size)


def _header_block(
    account_id: str,
    mailbox: str,
    card: MailCard,
    *,
    index: int | None,
    total: int | None,
    priority: int | None,
) -> list[str]:
    pos = ""
    if index is not None and total is not None and total > 0:
        pos = f"  ({index + 1}/{total})"
    folder_short = mailbox.replace("INBOX.", "")
    lines = [f"Subject: {card.subject}{pos}"]
    lines.append(f"From: {card.from_line}")
    if card.to_line:
        lines.append(f"To: {card.to_line}")
    if card.reply_to and card.reply_to.lower() not in card.from_line.lower():
        lines.append(f"Reply-To: {card.reply_to}")
    if card.date:
        lines.append(f"Date: {card.date}")
    plabel = priority_label(priority)
    if plabel:
        lines.append(f"Priority: {plabel}")
    lines.append(f"Inbox: {account_id} · {folder_short}")
    return lines


def format_card(
    account_id: str,
    mailbox: str,
    card: MailCard,
    *,
    index: int | None = None,
    total: int | None = None,
    max_body: int = 2200,
) -> str:
    """Legacy single-shot rendering (private-chat default): body truncated
    to ``max_body`` with a hint to use Reply/AI draft for full context.
    Kept for callers that do not need body pagination."""
    body = card.body
    if len(body) > max_body:
        body = body[:max_body].rstrip() + "\n\n… (tap Reply / AI draft for full context)"
    lines = _header_block(account_id, mailbox, card, index=index, total=total, priority=None)
    lines.append("")
    lines.append(body or "(empty body)")
    return "\n".join(lines)


def format_card_page(
    account_id: str,
    mailbox: str,
    card: MailCard,
    *,
    index: int | None = None,
    total: int | None = None,
    priority: int | None = None,
    body_page: int = 0,
    page_size: int = BODY_PAGE_SIZE,
) -> tuple[str, int, int]:
    """Full card (sender/recipient/subject/date/priority/body) with the
    body split into pages of ``page_size`` characters so a long email never
    exceeds Telegram's per-message limit.

    Returns ``(text, body_page, total_body_pages)``. ``body_page`` is
    clamped into range, so a stale "Next body" tap on a shrunk mailbox
    cannot go out of bounds.
    """
    body = card.body or ""
    total_pages = body_page_count(body, page_size)
    body_page = max(0, min(body_page, total_pages - 1))
    start = body_page * page_size
    chunk = body[start : start + page_size]
    lines = _header_block(
        account_id, mailbox, card, index=index, total=total, priority=priority
    )
    if total_pages > 1:
        lines.append(f"Body page {body_page + 1}/{total_pages}")
    lines.append("")
    lines.append(chunk or "(empty body)")
    return "\n".join(lines), body_page, total_pages
