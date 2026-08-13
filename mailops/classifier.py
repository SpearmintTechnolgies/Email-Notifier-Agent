from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import TAG_TO_FOLDER, load_settings
from .priority_rules import ensure_priority_rules, format_rules_card, match_priority


@dataclass
class Classification:
    priority: int  # 0-3
    tags: list[str] = field(default_factory=list)
    needs_reply: bool = False
    reason: str = ""


def _sender_email(envelope: dict[str, Any]) -> str:
    frm = envelope.get("from") or []
    if isinstance(frm, list) and frm:
        first = frm[0]
        if isinstance(first, dict):
            return str(first.get("email") or "").lower()
        return str(first).lower()
    return str(envelope.get("from") or "").lower()


def _subject(envelope: dict[str, Any]) -> str:
    return str(envelope.get("subject") or "")


def classify_rules(envelope: dict[str, Any], preview: str = "") -> Classification | None:
    """Keyword / domain matcher from structured priority_rules."""
    settings = load_settings()
    ensure_priority_rules(settings)
    sender = _sender_email(envelope)
    subject = _subject(envelope)
    hit = match_priority(sender=sender, subject=subject, preview=preview, settings=settings)
    if hit is None:
        return None
    tags: list[str] = []
    blob = f"{sender}\n{subject}\n{preview}".lower()
    if hit.priority == 0 and any(k in blob for k in ("security", "password", "2fa", "breach")):
        tags = ["security"]
    elif hit.priority <= 1 and any(k in blob for k in ("invoice", "billing", "payment", "refund")):
        tags = ["billing"]
    elif hit.priority == 1 and any(k in blob for k in ("advert", "sponsor", "listing", "media kit")):
        tags = ["sales-lead"]
    return Classification(hit.priority, tags[:1], hit.needs_reply, hit.reason)


async def classify_llm(
    runtime_base_url: str,
    api_key: str,
    envelope: dict[str, Any],
    preview: str,
    model: str = "auto",
    account_id: str | None = None,
) -> Classification:
    settings = load_settings()
    rules = ensure_priority_rules(settings)
    context = (settings.get("inbox_context") or "").strip()
    purposes = settings.get("account_purposes") or {}
    account_purpose = (purposes.get(account_id) or "").strip() if account_id else ""
    # Compact rules summary for the model
    rules_txt = []
    for key in ("p0", "p1", "p2", "p3"):
        r = rules[key]
        kws = ", ".join((r.get("keywords") or [])[:12])
        rules_txt.append(f"{key.upper()} ({r.get('label')}): {r.get('description')} | keywords: {kws}")
    sender = _sender_email(envelope)
    subject = _subject(envelope)
    system = (
        "Classify an email. Return ONLY JSON: "
        '{"priority":0-3,"tags":["sales-lead|partnership|support|billing|legal|security|ops|recruiting"],'
        '"needs_reply":bool,"reason":"short"}. '
        "0=Critical act now, 1=Important today, 2=Normal this week, 3=Noise.\n\n"
        f"PRIORITY RULES:\n" + "\n".join(rules_txt) + "\n\n"
        f"OVERALL:\n{context or '(not set)'}\n\n"
        f"INBOX {account_id or '?'}:\n{account_purpose or '(not set)'}"
    )
    prompt = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 200,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"From: {sender}\nSubject: {subject}\nPreview:\n{preview[:1500]}",
            },
        ],
    }
    if not api_key:
        return Classification(2, [], False, "no llm key; default normal")
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(
                f"{runtime_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=prompt,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", content, re.S)
            data = json.loads(m.group(0) if m else content)
            tags = [t for t in (data.get("tags") or []) if t in TAG_TO_FOLDER]
            pr = max(0, min(3, int(data.get("priority", 2))))
            return Classification(
                pr,
                tags[:1],
                bool(data.get("needs_reply", pr <= 1)),
                str(data.get("reason") or "llm"),
            )
    except Exception as exc:  # noqa: BLE001
        return Classification(2, [], False, f"llm fallback: {exc}")


async def classify_message(
    runtime_base_url: str,
    api_key: str,
    envelope: dict[str, Any],
    preview: str,
    model: str = "auto",
    account_id: str | None = None,
) -> Classification:
    ruled = classify_rules(envelope, preview)
    if ruled is not None:
        return ruled
    return await classify_llm(
        runtime_base_url, api_key, envelope, preview, model, account_id=account_id
    )


# re-export helper for bot UI
__all__ = [
    "Classification",
    "classify_message",
    "classify_rules",
    "format_rules_card",
]
