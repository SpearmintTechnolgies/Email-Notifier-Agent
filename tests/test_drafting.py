from __future__ import annotations

import asyncio
import re

from mailops.reply_kb import (
    append_account_kb_note,
    build_draft_system_prompt,
    format_kb_card,
    load_kb_text,
    save_account_kb,
)


def test_kb_roundtrip(tmp_profile):
    save_account_kb("sales", "# Sales KB\nRates: banner $200")
    text = load_kb_text("sales")
    assert "Rates: banner $200" in text


def test_kb_append_note(tmp_profile):
    save_account_kb("sales", "# Sales KB")
    append_account_kb_note("sales", "New media kit URL: https://example.com/kit")
    text = load_kb_text("sales")
    assert "Operator note" in text
    assert "media kit" in text


def test_draft_prompt_includes_kb_and_never_invent(tmp_profile):
    save_account_kb("sales", "Rates: sponsored post $150")
    prompt = build_draft_system_prompt(
        account_id="sales",
        account_email="sales@example.com",
        inbox_context="crypto media",
        account_purpose="close ads",
    )
    assert "sponsored post $150" in prompt
    assert "do not invent" in prompt.lower()
    assert "close ads" in prompt


def test_format_kb_card_empty(tmp_profile):
    card = format_kb_card("ghost")
    assert "empty" in card


def test_ai_draft_fails_clearly_without_api_key(tmp_profile, fake_him, monkeypatch):
    import pytest
    import mailops.bot.app as app_mod

    fake_him.add_message("sales", "INBOX.P1-Important", "7", subject="rates?", sender="b@b.co")
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        asyncio.run(app_mod.ai_draft("sales", "INBOX.P1-Important", "7"))


def test_ai_draft_rejects_auto_model_on_openai_host(tmp_profile, fake_him, monkeypatch):
    import pytest
    import mailops.bot.app as app_mod

    env = tmp_profile / ".env"
    env.write_text(
        env.read_text(encoding="utf-8") + "OPENAI_API_KEY=sk-test\nOPENAI_MODEL=auto\n",
        encoding="utf-8",
    )
    fake_him.add_message("sales", "INBOX.P1-Important", "7", subject="rates?", sender="b@b.co")
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)
    with pytest.raises(RuntimeError, match="OPENAI_MODEL"):
        asyncio.run(app_mod.ai_draft("sales", "INBOX.P1-Important", "7"))


def test_load_runtime_reads_openai_model(tmp_profile):
    from mailops.config import load_runtime

    env = tmp_profile / ".env"
    env.write_text(
        env.read_text(encoding="utf-8") + "OPENAI_MODEL=gpt-4o-mini\n",
        encoding="utf-8",
    )
    assert load_runtime().model == "gpt-4o-mini"


def test_load_runtime_custom_openai_compatible_base_url(tmp_profile):
    from mailops.config import load_runtime

    env = tmp_profile / ".env"
    env.write_text(
        "TELEGRAM_BOT_TOKEN=test-token\n"
        "TELEGRAM_ALLOWED_USERS=111\n"
        "OPENAI_API_KEY=sk-test\n"
        "OPENAI_BASE_URL=https://api.groq.com/openai/v1\n"
        "OPENAI_MODEL=openai/gpt-oss-20b\n",
        encoding="utf-8",
    )
    rt = load_runtime()
    assert rt.openai_base_url == "https://api.groq.com/openai/v1"
    assert rt.model == "openai/gpt-oss-20b"
    assert rt.openai_api_key == "sk-test"


def test_load_runtime_groq_env_key_not_copied_when_openai_key_empty(tmp_profile, monkeypatch):
    from mailops.config import load_runtime

    monkeypatch.setenv("GROQ_API_KEY", "gsk-test-not-a-real-key")
    env = tmp_profile / ".env"
    env.write_text(
        "TELEGRAM_BOT_TOKEN=test-token\n"
        "TELEGRAM_ALLOWED_USERS=111\n"
        "OPENAI_API_KEY=\n"
        "OPENAI_BASE_URL=https://api.groq.com/openai/v1\n"
        "OPENAI_MODEL=openai/gpt-oss-20b\n",
        encoding="utf-8",
    )
    rt = load_runtime()
    assert rt.openai_api_key == "gsk-test-not-a-real-key"
    # The Mailops .env must stay empty — no copy of the Groq secret.
    disk = env.read_text(encoding="utf-8")
    assert "gsk-test-not-a-real-key" not in disk
    assert re.search(r"^OPENAI_API_KEY=\s*$", disk, re.M)


def test_ai_draft_uses_llm_endpoint(tmp_profile, fake_him, monkeypatch):
    """ai_draft must call the configured LLM and return its text (mocked HTTP)."""
    import mailops.bot.app as app_mod

    env = tmp_profile / ".env"
    env.write_text(
        env.read_text(encoding="utf-8")
        + "OPENAI_API_KEY=sk-test\nOPENAI_MODEL=gpt-4o-mini\n",
        encoding="utf-8",
    )
    fake_him.add_message("sales", "INBOX.P1-Important", "7", subject="rates?", sender="b@b.co")
    monkeypatch.setattr(app_mod, "Himalaya", lambda: fake_him)

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "Draft body here"}}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            assert "chat/completions" in url
            assert json["model"] == "gpt-4o-mini"
            return FakeResp()

    monkeypatch.setattr(app_mod.httpx, "AsyncClient", FakeClient)
    draft = asyncio.run(app_mod.ai_draft("sales", "INBOX.P1-Important", "7"))
    assert draft == "Draft body here"
