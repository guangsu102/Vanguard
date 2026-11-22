"""Official GPT-6.1 Sol requests must follow the provider's accepted schema."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core.ai.llm_client import LLMClient
from app.core.config import settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url,model,official_sol",
    [
        (None, "gpt-6.1-sol", True),
        ("https://api.openai.com/v1/", "gpt-6.1-sol", True),
        ("https://proxy.example/v1", "gpt-6.1-sol", False),
        ("https://api.openai.com/v1", "gpt-4o", False),
    ],
)
async def test_official_sol_request_contract(monkeypatch, base_url, model, official_sol):
    monkeypatch.setattr(settings, "LLM_MODEL_OVERRIDE", "")
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", None)
    monkeypatch.setattr(settings, "LLM_REASONING_EFFORT", "low")
    client = LLMClient(api_key="test-key", base_url=base_url)
    client.cache = SimpleNamespace(get=AsyncMock(return_value=None))

    async def respond(**request):
        if official_sol:
            assert "max_tokens" not in request
            assert "temperature" not in request
            assert request["max_completion_tokens"] == 256
        else:
            assert request["max_tokens"] == 256
            assert request["temperature"] == 0.0
            assert "max_completion_tokens" not in request
        assert request["extra_body"] == {"reasoning_effort": "low"}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))])

    sdk = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=respond))),
        close=AsyncMock(),
    )
    with patch("openai.AsyncOpenAI", return_value=sdk):
        assert (
            await client._call_openai([{"role": "user", "content": "Reply OK"}], model, 0.0, 256)
            == "OK"
        )
    sdk.close.assert_awaited_once()
