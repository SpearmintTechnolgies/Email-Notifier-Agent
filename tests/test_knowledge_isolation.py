"""One brand's pricing, links, identity and policies must not leak into another."""
from __future__ import annotations

from pathlib import Path

from mailops.classifier import classify_rules
from mailops.config import load_settings, save_settings
from mailops.priority_rules import match_priority
from mailops.reply_kb import build_draft_system_prompt, load_kb_text, save_account_kb

ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / "data" / "kb"


def test_live_shared_kb_is_brand_neutral():
    text = (KB / "_shared.md").read_text(encoding="utf-8").lower()
    for needle in (
        "coinography.com",
        "memecoinist.com",
        "altcoininformer.com",
        "$450",
        "authority bundle",
        "sales@memecoinist",
        "advertising@coinography",
    ):
        assert needle not in text
    archive = KB / "_archive" / "_shared.coinography-memecoinist.original.md"
    assert archive.exists() and archive.read_text(encoding="utf-8").strip()


def test_live_account_kbs_do_not_cross_brands():
    alt = (KB / "altcoininformer.md").read_text(encoding="utf-8").lower()
    coin = (KB / "coinography.md").read_text(encoding="utf-8").lower()
    meme = (KB / "memecoinist.md").read_text(encoding="utf-8").lower()
    sales = (KB / "sales.md").read_text(encoding="utf-8").lower()
    for needle in ("coinography.com", "memecoinist.com", "$450", "authority bundle"):
        assert needle not in alt
    assert "memecoinist.com" not in coin and "altcoininformer.com" not in coin
    assert "coinography.com" not in meme and "$450" not in meme
    assert "memecoinist.com" not in sales
    assert "https://coinography.com" in coin
    assert "https://memecoinist.com" in meme
    assert "not provided yet" in alt


def test_draft_prompt_loads_only_that_inbox_kb(tmp_profile):
    save_account_kb("altcoininformer", "Altcoin Informer only. No prices. Sign-off: Altcoin Informer")
    save_account_kb(
        "coinography",
        "Coinography https://coinography.com Authority Bundle $450 advertise: https://coinography.com/advertise",
    )
    save_account_kb(
        "memecoinist",
        "Memecoinist https://memecoinist.com sales@memecoinist.com listing packages TBD",
    )
    alt = build_draft_system_prompt(
        account_id="altcoininformer",
        account_email="info@altcoininformer.com",
        inbox_context="company-wide, brand-neutral",
        account_purpose="Altcoin Informer business correspondence",
        display_name="Altcoin Informer",
    )
    assert "Altcoin Informer only" in alt
    assert "$450" not in alt
    assert "coinography.com" not in alt
    assert "memecoinist.com" not in alt
    coin = build_draft_system_prompt(
        account_id="coinography",
        account_email="info@coinography.com",
        inbox_context="company-wide, brand-neutral",
        account_purpose="Coinography ads inbox",
        display_name="Coinography",
    )
    assert "$450" in coin and "https://coinography.com" in coin
    assert "memecoinist.com" not in coin
    assert "Altcoin Informer only" not in coin
    meme = load_kb_text("memecoinist")
    assert "memecoinist.com" in meme and "$450" not in meme and "coinography.com" not in meme


def test_account_priority_overlay_does_not_apply_to_other_inboxes(tmp_profile):
    s = load_settings()
    s["account_priority_rules"] = {
        "coinography": {"p1": {"keywords": ["guest post for coinography"]}},
        "memecoinist": {"p1": {"keywords": ["advertise on memecoinist"]}},
        "altcoininformer": {"p3": {"keywords": ["web design services"]}},
    }
    save_settings(s)
    env = {"subject": "Guest post for Coinography", "from": [{"email": "seo@x.co"}]}
    assert classify_rules(env, account_id="altcoininformer") is None
    coin = classify_rules(env, account_id="coinography")
    assert coin is not None and coin.priority == 1
    noise = classify_rules(
        {"subject": "Our web design services", "from": [{"email": "shop@x.co"}]},
        account_id="altcoininformer",
    )
    assert noise is not None and noise.priority == 3
    other = classify_rules(
        {"subject": "Our web design services", "from": [{"email": "shop@x.co"}]},
        account_id="coinography",
    )
    assert other is None
    hit = match_priority(
        sender="a@b.co",
        subject="Want to advertise on memecoinist",
        preview="",
        account_id="memecoinist",
    )
    assert hit is not None and hit.priority == 1
