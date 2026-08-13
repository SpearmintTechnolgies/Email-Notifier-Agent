"""Structured P0/P1/P2/P3 rules + keyword matching."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config import load_settings, save_settings

PRIORITY_KEYS = ("p0", "p1", "p2", "p3")

PRIORITY_META = {
    "p0": {
        "level": 0,
        "label": "Critical",
        "meaning": "Act now — security, money at risk, VIP, legal deadline",
    },
    "p1": {
        "level": 1,
        "label": "Important",
        "meaning": "Reply / decide today — real leads, clients, partners",
    },
    "p2": {
        "level": 2,
        "label": "Normal",
        "meaning": "This week — vague but possibly real, FYI that needs a glance",
    },
    "p3": {
        "level": 3,
        "label": "Noise",
        "meaning": "Ignore / archive — spam, cold RFQ, newsletters, no-reply digests",
    },
}


DEFAULT_PRIORITY_RULES: dict[str, dict[str, Any]] = {
    "p0": {
        "label": "Critical",
        "description": "Act now: security, payment failures, legal threats, VIP client emergencies.",
        "keywords": [
            "password reset",
            "2fa",
            "two-factor",
            "security alert",
            "unauthorized login",
            "account compromised",
            "breach",
            "phishing",
            "payment failed",
            "payment declined",
            "wire transfer failed",
            "overdue invoice",
            "legal notice",
            "lawsuit",
            "dmca",
            "takedown notice",
            "domain suspended",
            "hosting suspended",
        ],
        "domains": [],
        "needs_reply": True,
    },
    "p1": {
        "label": "Important",
        "description": "Revenue today: ad/listing buyers asking rates, pricing, media kit, sponsorship, or ready to pay to post.",
        "keywords": [
            "advertise",
            "advertising",
            "advertisement",
            "advertis",
            "sponsorship",
            "sponsored post",
            "sponsored article",
            "sponsored content",
            "media kit",
            "rate card",
            "pricing",
            "price list",
            "how much",
            "what do you charge",
            "cost to publish",
            "cost to post",
            "publish on your site",
            "post on your website",
            "post on your site",
            "banner ad",
            "banner placement",
            "newsletter sponsorship",
            "press release",
            "pr submission",
            "paid article",
            "paid post",
            "paid promotion",
            "paid review",
            "native ad",
            "insert order",
            "campaign brief",
            "listing request",
            "listing fee",
            "token listing",
            "coin listing",
            "get listed",
            "feature our coin",
            "feature our token",
            "promo package",
            "marketing package",
            "want to advertise",
            "interested in advertising",
            "advertise on coinography",
            "advertise on memecoinist",
            "cpm",
            "cpt",
            "budget for campaign",
            "ready to pay",
            "invoice for ads",
            "ad rates",
            "publication fee",
        ],
        "domains": [],
        "needs_reply": True,
    },
    "p2": {
        "label": "Normal",
        "description": "Possibly useful later: soft intros, unclear collabs, follow-ups not clearly buying ads yet.",
        "keywords": [
            "quick question",
            "following up",
            "just checking in",
            "wanted to introduce",
            "collaboration idea",
            "possible partnership",
            "work together",
            "explore opportunities",
        ],
        "domains": [],
        "needs_reply": False,
    },
    "p3": {
        "label": "Noise",
        "description": "Non-revenue junk: cold RFQ/procurement, manufacturers, SEO link packages, newsletters.",
        "keywords": [
            "procurement department",
            "manufacturer partnership evaluation",
            "export product information",
            "overseas customers or distributors",
            "reliable manufacturers",
            "request a quotation",
            "unsubscribe",
            "newsletter",
            "noreply",
            "no-reply",
            "promotional",
            "seo backlink",
            "guest post package",
            "hydraulic",
            "rank@storm",
        ],
        "domains": [],
        "needs_reply": False,
    },
}


@dataclass
class MatchResult:
    priority: int
    needs_reply: bool
    reason: str
    matched: list[str] = field(default_factory=list)
    bucket: str = "p2"


def normalize_keywords(raw: str | list[str]) -> list[str]:
    """Accept comma/newline/semicolon separated text or a list."""
    if isinstance(raw, list):
        parts = [str(x) for x in raw]
    else:
        parts = re.split(r"[\n,;]+", str(raw))
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        k = " ".join(p.strip().lower().split())
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def ensure_priority_rules(settings: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Merge saved rules with defaults; migrate legacy keyword lists once."""
    settings = settings if settings is not None else load_settings()
    saved = settings.get("priority_rules") or {}
    rules: dict[str, dict[str, Any]] = {}
    for key in PRIORITY_KEYS:
        base = dict(DEFAULT_PRIORITY_RULES[key])
        override = saved.get(key) or {}
        merged = {**base, **override}
        merged["keywords"] = normalize_keywords(merged.get("keywords") or [])
        merged["domains"] = normalize_keywords(merged.get("domains") or [])
        rules[key] = merged

    # One-time migration from old flat lists
    if not saved:
        legacy_imp = normalize_keywords(settings.get("important_keywords") or [])
        legacy_noise = normalize_keywords(settings.get("noise_keywords") or [])
        if legacy_imp:
            rules["p1"]["keywords"] = normalize_keywords(rules["p1"]["keywords"] + legacy_imp)
        if legacy_noise:
            rules["p3"]["keywords"] = normalize_keywords(rules["p3"]["keywords"] + legacy_noise)
        settings["priority_rules"] = rules
        save_settings(settings)
    return rules


def format_rules_card(rules: dict[str, dict[str, Any]] | None = None) -> str:
    rules = rules or ensure_priority_rules()
    lines = [
        "Priority rule format",
        "",
        "Fill keywords for each bucket (one per line).",
        "Matcher checks From + Subject + body preview.",
        "Order: VIP/mute → Critical keywords → Noise keywords → Important → Normal → else AI/default.",
        "",
    ]
    for key in PRIORITY_KEYS:
        meta = PRIORITY_META[key]
        r = rules[key]
        kws = r.get("keywords") or []
        doms = r.get("domains") or []
        lines.append(f"{key.upper()} · {meta['label']}")
        lines.append(f"  Meaning: {meta['meaning']}")
        lines.append(f"  Your note: {r.get('description') or '(none)'}")
        lines.append(f"  Keywords ({len(kws)}):")
        if kws:
            for k in kws[:25]:
                lines.append(f"    - {k}")
            if len(kws) > 25:
                lines.append(f"    … +{len(kws) - 25} more")
        else:
            lines.append("    (empty — add some)")
        if doms:
            lines.append("  Domains: " + ", ".join(doms[:15]))
        lines.append("")
    lines.append("Tap Edit Critical / Important / Normal / Noise to paste keywords.")
    return "\n".join(lines)


def format_edit_prompt(bucket: str, rules: dict[str, dict[str, Any]]) -> str:
    meta = PRIORITY_META[bucket]
    r = rules[bucket]
    kws = r.get("keywords") or []
    return (
        f"Edit {bucket.upper()} · {meta['label']}\n"
        f"Meaning: {meta['meaning']}\n\n"
        f"Current description:\n{r.get('description') or '(none)'}\n\n"
        f"Current keywords ({len(kws)}):\n"
        + ("\n".join(f"- {k}" for k in kws[:40]) if kws else "(empty)")
        + "\n\n"
        "Send ONE message in this format:\n\n"
        "description: short note about this bucket\n"
        "keywords:\n"
        "keyword one\n"
        "keyword two\n"
        "domains:\n"
        "partner.com\n\n"
        "Or just paste a keyword list (one per line / commas) to replace keywords only."
    )


def parse_rule_message(text: str) -> dict[str, Any]:
    """Parse user paste into description/keywords/domains."""
    text = text.strip()
    if not text:
        return {}
    lower = text.lower()
    if "keywords:" in lower or "description:" in lower or "domains:" in lower:
        desc = ""
        keywords: list[str] = []
        domains: list[str] = []
        section = None
        for line in text.splitlines():
            raw = line.strip()
            if not raw:
                continue
            low = raw.lower()
            if low.startswith("description:"):
                section = "description"
                desc = raw.split(":", 1)[1].strip()
                continue
            if low.startswith("keywords:"):
                section = "keywords"
                rest = raw.split(":", 1)[1].strip()
                if rest:
                    keywords.extend(normalize_keywords(rest))
                continue
            if low.startswith("domains:"):
                section = "domains"
                rest = raw.split(":", 1)[1].strip()
                if rest:
                    domains.extend(normalize_keywords(rest))
                continue
            if section == "description":
                desc = (desc + " " + raw).strip() if desc else raw
            elif section == "domains":
                domains.extend(normalize_keywords(raw.lstrip("-• ").strip()))
            else:
                keywords.extend(normalize_keywords(raw.lstrip("-• ").strip()))
        out: dict[str, Any] = {}
        if desc:
            out["description"] = desc
        if keywords:
            out["keywords"] = normalize_keywords(keywords)
        if domains:
            out["domains"] = normalize_keywords(domains)
        return out
    # plain list → keywords only
    return {"keywords": normalize_keywords(text)}


def save_bucket_rules(bucket: str, patch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    settings = load_settings()
    rules = ensure_priority_rules(settings)
    cur = dict(rules[bucket])
    if "description" in patch:
        cur["description"] = str(patch["description"]).strip()
    if "keywords" in patch:
        cur["keywords"] = normalize_keywords(patch["keywords"])
    if "domains" in patch:
        cur["domains"] = normalize_keywords(patch["domains"])
    rules[bucket] = cur
    settings["priority_rules"] = rules
    save_settings(settings)
    return rules


def _keyword_hit(blob: str, keyword: str) -> bool:
    if not keyword:
        return False
    if " " in keyword or "@" in keyword or "." in keyword:
        return keyword in blob
    return bool(re.search(rf"\b{re.escape(keyword)}\b", blob))


def match_priority(
    *,
    sender: str,
    subject: str,
    preview: str,
    settings: dict[str, Any] | None = None,
) -> MatchResult | None:
    """
    Deterministic matcher.
    Returns None if no keyword/domain rule hit (caller may use LLM).
    Precedence after mute/VIP: P0 → P3 → P1 → P2.
    """
    settings = settings or load_settings()
    rules = ensure_priority_rules(settings)
    sender_l = (sender or "").lower()
    domain = sender_l.split("@")[-1] if "@" in sender_l else ""
    blob = f"{sender_l}\n{(subject or '').lower()}\n{(preview or '').lower()}"

    muted = {m.lower() for m in settings.get("muted_senders") or []}
    muted_domains = {m.lower() for m in settings.get("muted_domains") or []}
    if sender_l in muted or domain in muted_domains:
        return MatchResult(3, False, "muted sender/domain", [sender_l or domain], "p3")

    vip = {v.lower() for v in settings.get("vip_senders") or []}
    if sender_l in vip:
        return MatchResult(0, True, "VIP sender", [sender_l], "p0")

    def hits_for(bucket: str) -> list[str]:
        r = rules[bucket]
        found: list[str] = []
        for d in r.get("domains") or []:
            if d and (d == domain or d in sender_l or d in blob):
                found.append(f"domain:{d}")
        for kw in r.get("keywords") or []:
            if _keyword_hit(blob, kw):
                found.append(kw)
        return found

    for bucket in ("p0", "p3", "p1", "p2"):
        found = hits_for(bucket)
        if not found:
            continue
        meta = PRIORITY_META[bucket]
        level = meta["level"]
        needs = bool(rules[bucket].get("needs_reply", level <= 1))
        reason = f"{meta['label']} match: {', '.join(found[:4])}"
        return MatchResult(level, needs, reason, found[:8], bucket)

    return None
