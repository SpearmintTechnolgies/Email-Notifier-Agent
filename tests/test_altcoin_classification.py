"""Altcoin Informer inbox purpose: initial guidance, not rigid universal rules."""
from __future__ import annotations

from mailops.classifier import classify_rules
from mailops.config import load_settings, save_settings


def _env(subject, sender="buyer@example.com"):
    return {"subject": subject, "from": [{"email": sender}]}


def _altcoin_settings():
    s = load_settings()
    s["account_purposes"] = {
        "altcoininformer": (
            "info@altcoininformer.com - Altcoin Informer cryptocurrency news/media mailbox. "
            "CRITICAL: genuine account-security incidents, compromised access, urgent operational "
            "disruptions, serious payment or service failures. IMPORTANT: legitimate advertising "
            "and sponsorship enquiries, paying customers, time-sensitive partnerships, support "
            "needing a response, relevant business enquiries. NORMAL: general correspondence. "
            "NOISE: irrelevant promotional outreach, unsolicited bulk marketing, obvious spam."
        ),
        "coinography": "Coinography ads — do not use this purpose for Altcoin Informer.",
    }
    s["account_priority_rules"] = {
        "altcoininformer": {
            "p0": {
                "keywords": ["suspicious login", "site is down", "chargeback"],
            },
            "p1": {
                "keywords": ["advertising enquiry", "sponsorship enquiry", "customer support"],
            },
            "p3": {
                "keywords": ["bulk marketing", "web design services"],
            },
        }
    }
    save_settings(s)
    return s


def test_altcoin_security_incident_is_critical(tmp_profile):
    _altcoin_settings()
    c = classify_rules(_env("Suspicious login to your hosting panel"), account_id="altcoininformer")
    assert c is not None and c.priority == 0 and c.needs_reply is True


def test_altcoin_payment_failure_is_critical(tmp_profile):
    _altcoin_settings()
    c = classify_rules(_env("Payment failed for invoice 4412"), account_id="altcoininformer")
    assert c is not None and c.priority == 0


def test_altcoin_advertising_enquiry_is_important(tmp_profile):
    _altcoin_settings()
    c = classify_rules(_env("Advertising enquiry: sponsorship on the site"), account_id="altcoininformer")
    assert c is not None and c.priority == 1
    assert "sales-lead" in c.tags


def test_altcoin_support_request_is_important(tmp_profile):
    _altcoin_settings()
    c = classify_rules(_env("Customer support: cannot access our booking"), account_id="altcoininformer")
    assert c is not None and c.priority == 1


def test_altcoin_bulk_marketing_is_noise(tmp_profile):
    _altcoin_settings()
    c = classify_rules(_env("Boost traffic with our bulk marketing list"), account_id="altcoininformer")
    assert c is not None and c.priority == 3 and c.needs_reply is False


def test_altcoin_general_note_is_not_forced_into_a_bucket(tmp_profile):
    _altcoin_settings()
    assert classify_rules(_env("Hello, just introducing our team"), account_id="altcoininformer") is None


def test_altcoin_does_not_use_another_brand_purpose_keywords(tmp_profile):
    _altcoin_settings()
    # Coinography-only overlay must not fire on the Altcoin Informer inbox.
    s = load_settings()
    s["account_priority_rules"]["coinography"] = {
        "p1": {"keywords": ["guest post for coinography"]}
    }
    save_settings(s)
    assert classify_rules(_env("Guest post for Coinography"), account_id="altcoininformer") is None
