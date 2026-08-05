from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from polybot.ai_diagnostics import AIDiagnosticWorker
from polybot.config import Settings
from polybot.credentials import AIProvider
from polybot.jobs import AIDiagnosticJob


def _job(provider: AIProvider, base_url: str | None = None) -> AIDiagnosticJob:
    now = datetime.now(UTC)
    return AIDiagnosticJob(
        id=uuid4(),
        account_id=uuid4(),
        credential_id=uuid4(),
        provider=provider,
        ai_base_url=base_url,
        model="gpt-4o-mini",
        status="claimed",
        created_at=now,
        updated_at=now,
    )


def _worker() -> AIDiagnosticWorker:
    return AIDiagnosticWorker(
        jobs=object(),  # type: ignore[arg-type]
        credentials=object(),  # type: ignore[arg-type]
        decryptor=object(),  # type: ignore[arg-type]
        settings=Settings(_env_file=None),
        owner_id="worker-1",
    )


async def test_provider_openai_returns_openai_provider() -> None:
    worker = _worker()
    provider = await worker._provider(_job(AIProvider.OPENAI), api_key="key")
    assert provider.__class__.__name__ == "OpenAIForecastProvider"


async def test_provider_custom_rejects_unsafe_base_url() -> None:
    worker = _worker()
    with pytest.raises(ValueError, match="custom_endpoint_unsafe"):
        await worker._provider(
            _job(AIProvider.CUSTOM, base_url="http://127.0.0.1:8000/v1"),
            api_key="key",
        )


async def test_provider_unsupported_fails_closed() -> None:
    worker = _worker()
    with pytest.raises(ValueError, match="provider_unsupported"):
        await worker._provider(_job(AIProvider.PLATFORM), api_key="key")


async def test_provider_litellm_uses_configured_base_url(monkeypatch) -> None:
    worker = _worker()

    class FakeLiteLLM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(
        "polybot.ai_diagnostics.LiteLLMForecastProvider",
        FakeLiteLLM,
    )
    provider = await worker._provider(_job(AIProvider.LITELLM), api_key="key")

    assert provider.kwargs["api_base"] == "http://litellm:4000/v1"


async def test_provider_anthropic_uses_default_endpoint(monkeypatch) -> None:
    worker = _worker()

    class FakeLiteLLM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(
        "polybot.ai_diagnostics.LiteLLMForecastProvider",
        FakeLiteLLM,
    )
    provider = await worker._provider(_job(AIProvider.ANTHROPIC), api_key="key")

    assert provider.kwargs["api_base"] == "https://api.anthropic.com"
