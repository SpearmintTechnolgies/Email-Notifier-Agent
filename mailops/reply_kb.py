"""Reply knowledge base for purpose-aware AI drafts.

KB files live in data/kb/:
  _shared.md           - company-wide, brand-neutral voice + safety rules ONLY
  {account_id}.md      - THAT inbox's links, rates, signature, playbook
  _archive/            - preserved historical material, never loaded

A draft for one inbox is built from ``_shared.md`` plus that inbox's own file
and nothing else, so one brand's pricing, links, identity or policies cannot
leak into another brand's replies. Facts that are not in the account file must
not be invented.

Hermes Phase 2 can deepen this (memory, learned replies) but Mailops
owns the source-of-truth files so drafts stay consistent offline.
"""
from __future__ import annotations

import re
from pathlib import Path

from .paths import DATA_DIR

KB_DIR = DATA_DIR / "kb"

# Account ids double as file names: refuse anything that could escape KB_DIR or
# hit a reserved file (``_shared``, ``_archive``).
_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def ensure_kb_dir() -> Path:
    KB_DIR.mkdir(parents=True, exist_ok=True)
    return KB_DIR


def kb_path(account_id: str) -> Path:
    if not _ACCOUNT_ID_RE.match(account_id or ""):
        raise ValueError(f"invalid account id for a knowledge base: {account_id!r}")
    return ensure_kb_dir() / f"{account_id}.md"


def shared_kb_path() -> Path:
    return ensure_kb_dir() / "_shared.md"


def load_kb_text(account_id: str) -> str:
    """Load shared + ONLY this account's KB. Missing files -> empty sections."""
    parts: list[str] = []
    shared = shared_kb_path()
    if shared.exists():
        parts.append(shared.read_text(encoding="utf-8").strip())
    path = kb_path(account_id)
    if path.exists():
        parts.append(path.read_text(encoding="utf-8").strip())
    return "\n\n---\n\n".join(p for p in parts if p)


def has_account_kb(account_id: str) -> bool:
    try:
        path = kb_path(account_id)
    except ValueError:
        return False
    return path.exists() and bool(path.read_text(encoding="utf-8").strip())


def save_account_kb(account_id: str, text: str) -> Path:
    path = kb_path(account_id)
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def append_account_kb_note(account_id: str, note: str) -> Path:
    path = kb_path(account_id)
    ensure_kb_dir()
    existing = path.read_text(encoding="utf-8") if path.exists() else f"# {account_id}\n"
    block = f"\n\n## Operator note ({_stamp()})\n{note.strip()}\n"
    path.write_text(existing.rstrip() + block, encoding="utf-8")
    return path


def _stamp() -> str:
    import datetime as _dt

    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def build_draft_system_prompt(
    *,
    account_id: str,
    account_email: str,
    inbox_context: str,
    account_purpose: str,
    display_name: str = "",
) -> str:
    kb = load_kb_text(account_id)
    brand = (display_name or account_id).strip()
    account_kb = has_account_kb(account_id)
    return (
        "You draft outbound email replies for a crypto media company's business mailbox.\n"
        "Output ONLY the email body (no Subject line, no markdown fences).\n"
        f"You write ONLY as {brand} <{account_email}>. Use this mailbox's knowledge base as the "
        "sole ground truth for links, prices, services, policies and signature. Never mention, "
        "quote or borrow anything from any other brand or inbox.\n"
        "If a price, service, term, deadline or publication guarantee is not in the knowledge "
        "base, do NOT invent it: say it depends on the brief, ask for budget/timeline/format, "
        "or say you will confirm. Never promise publication or commit the business to anything.\n"
        "Keep replies concise (about 120-220 words unless the inbound needs a tighter answer).\n"
        "Include a link only if it appears in the knowledge base.\n\n"
        f"Active inbox: {account_id} <{account_email}>\n\n"
        f"Company-wide context:\n{(inbox_context or '(none)').strip()}\n\n"
        f"This inbox purpose:\n{(account_purpose or '(none)').strip()}\n\n"
        + (
            ""
            if account_kb
            else "NOTE: this inbox has NO account knowledge base yet - use no links, prices or "
            "signature beyond the plain brand name, and keep the reply cautious.\n\n"
        )
        + f"Knowledge base:\n{(kb or '(empty - be cautious; ask Master to fill data/kb)').strip()}"
    )


def format_kb_card(account_id: str, limit: int = 3500) -> str:
    text = load_kb_text(account_id)
    if not text.strip():
        return f"Reply KB for {account_id} is empty.\nAdd content under data/kb/{account_id}.md"
    header = f"Reply KB · {account_id}\n(edit via Telegram note or data/kb/{account_id}.md)\n\n"
    body = text.strip()
    if len(header) + len(body) > limit:
        body = body[: max(0, limit - len(header) - 20)].rstrip() + "\n\n… (truncated)"
    return header + body
