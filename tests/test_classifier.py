from __future__ import annotations

import asyncio

from mailops.classifier import classify_llm, classify_message, classify_rules


def _env(subject="", sender="buyer@example.com"):
    return {"subject": subject, "from": [{"email": sender}]}


def test_rules_hit_critical(tmp_profile):
    c = classify_rules(_env(subject="Security alert: unauthorized login"))
    assert c is not None
    assert c.priority == 0
    assert c.needs_reply is True
    assert "security" in c.tags
    assert c.source == "rules"


def test_rules_hit_noise(tmp_profile):
    c = classify_rules(_env(subject="Request a quotation for hydraulic pumps"))
    assert c is not None
    assert c.priority == 3
    assert c.needs_reply is False


def test_rules_hit_important_sales(tmp_profile):
    c = classify_rules(_env(subject="How much to publish a sponsored post?"))
    assert c is not None
    assert c.priority == 1
    assert "sales-lead" in c.tags


def test_rules_no_hit_returns_none(tmp_profile):
    assert classify_rules(_env(subject="hello there, random note")) is None


def test_muted_sender_goes_to_noise(tmp_profile):
    from mailops.config import load_settings, save_settings

    s = load_settings()
    s["muted_senders"] = ["spammy@example.com"]
    save_settings(s)
    c = classify_rules(_env(subject="how much to advertise", sender="spammy@example.com"))
    assert c is not None
    assert c.priority == 3


def test_vip_sender_goes_to_critical(tmp_profile):
    from mailops.config import load_settings, save_settings

    s = load_settings()
    s["vip_senders"] = ["boss@example.com"]
    save_settings(s)
    c = classify_rules(_env(subject="anything", sender="boss@example.com"))
    assert c is not None
    assert c.priority == 0


def test_llm_fallback_without_key_defaults_normal(tmp_profile):
    c = asyncio.run(classify_llm("https://api.openai.com/v1", "", _env("hi"), "preview"))
    assert c.priority == 2
    assert "no llm key" in c.reason
    assert c.source == "no-key"


def test_llm_unreachable_defaults_normal(tmp_profile):
    # Port 1 is closed; request must fail fast-ish and fall back to P2.
    c = asyncio.run(
        classify_llm("http://127.0.0.1:1", "fake-key", _env("hi"), "preview")
    )
    assert c.priority == 2
    assert "llm fallback" in c.reason
    assert c.source == "llm-fallback"


def test_classify_message_prefers_rules(tmp_profile):
    c = asyncio.run(
        classify_message("https://api.openai.com/v1", "", _env("security alert breach"), "p")
    )
    assert c.priority == 0
    assert c.source == "rules"
