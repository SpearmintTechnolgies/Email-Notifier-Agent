from __future__ import annotations

from mailops.priority_rules import (
    ensure_priority_rules,
    match_priority,
    normalize_keywords,
    parse_rule_message,
    save_bucket_rules,
)


def test_normalize_keywords_splits_and_dedupes():
    out = normalize_keywords("Foo, bar\nfoo;  baz  qux ")
    assert out == ["foo", "bar", "baz qux"]


def test_parse_rule_message_structured():
    patch = parse_rule_message(
        "description: my bucket\nkeywords:\nalpha\nbeta\ndomains:\nexample.com"
    )
    assert patch["description"] == "my bucket"
    assert patch["keywords"] == ["alpha", "beta"]
    assert patch["domains"] == ["example.com"]


def test_parse_rule_message_plain_list():
    patch = parse_rule_message("one\ntwo, three")
    assert patch == {"keywords": ["one", "two", "three"]}


def test_save_bucket_rules_roundtrip(tmp_profile):
    rules = save_bucket_rules("p1", {"keywords": ["special deal"]})
    assert "special deal" in rules["p1"]["keywords"]
    again = ensure_priority_rules()
    assert "special deal" in again["p1"]["keywords"]


def test_match_priority_order_p0_beats_p3(tmp_profile):
    hit = match_priority(
        sender="x@example.com",
        subject="security alert: request a quotation",
        preview="",
    )
    assert hit is not None
    assert hit.priority == 0  # p0 checked before p3


def test_match_priority_no_hit(tmp_profile):
    assert match_priority(sender="a@b.co", subject="zzz nothing", preview="") is None
