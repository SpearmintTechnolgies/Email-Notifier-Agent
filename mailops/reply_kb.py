"""Reply knowledge base for purpose-aware AI drafts.

KB files live in data/kb/:
  _shared.md           — voice + global rules
  {account_id}.md      — per-inbox links, rates, signature, playbook

Hermes Phase 2 can deepen this (memory, learned replies) but Mailops
owns the source-of-truth files so drafts stay consistent offline.
"""
from __future__ import annotations

from pathlib import Path

from .paths import DATA_DIR

KB_DIR = DATA_DIR / "kb"


def ensure_kb_dir() -> Path:
    KB_DIR.mkdir(parents=True, exist_ok=True)
    return KB_DIR


def kb_path(account_id: str) -> Path:
    return ensure_kb_dir() / f"{account_id}.md"


def shared_kb_path() -> Path:
    return ensure_kb_dir() / "_shared.md"


def load_kb_text(account_id: str) -> str:
    """Load shared + account KB. Missing files → empty sections."""
    parts: list[str] = []
    shared = shared_kb_path()
    if shared.exists():
        parts.append(shared.read_text(encoding="utf-8").strip())
    path = kb_path(account_id)
    if path.exists():
        parts.append(path.read_text(encoding="utf-8").strip())
    return "\n\n---\n\n".join(p for p in parts if p)


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
) -> str:
    kb = load_kb_text(account_id)
    return (
        "You draft outbound email replies for a crypto media sales operation.\n"
        "Output ONLY the email body (no Subject line, no markdown fences).\n"
        "Use the knowledge base as ground truth for links, prices, and signature.\n"
        "If a price is not in the KB, do not invent one — ask for budget and point to the advertise/contact links.\n"
        "Keep replies concise (about 120–220 words unless the inbound needs a tighter answer).\n"
        "Include essential links from the KB when the inbound is about advertising, pricing, posting, listings, or PR.\n\n"
        f"Active inbox: {account_id} <{account_email}>\n\n"
        f"Overall business purpose:\n{(inbox_context or '(none)').strip()}\n\n"
        f"This inbox purpose:\n{(account_purpose or '(none)').strip()}\n\n"
        f"Knowledge base:\n{(kb or '(empty — be cautious; ask Master to fill data/kb)').strip()}"
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
