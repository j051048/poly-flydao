from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from litellm import (
    AuthenticationError,
    BadRequestError,
    ContentPolicyViolationError,
    RateLimitError,
    Timeout,
    UnsupportedParamsError,
)

from polybot.ai.litellm_provider import LiteLLMForecastProvider
from polybot.models import ForecastRequest


def _response(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


async def test_litellm_falls_back_only_when_json_schema_is_unsupported(monkeypatch, market) -> None:
    calls: list[dict] = []
    payload = {
        "probability_yes": "0.55",
        "probability_low": "0.45",
        "probability_high": "0.65",
        "confidence": "0.60",
        "evidence_for": [],
        "evidence_against": [],
        "assumptions": ["synthetic test"],
        "invalidation_conditions": ["new evidence"],
        "source_ids": ["invented-id"],
        "rationale": "Test forecast.",
    }

    async def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise UnsupportedParamsError(
                "provider does not support response_format json_schema",
                llm_provider="vendor",
                model="vendor/model",
            )
        return _response(json.dumps(payload))

    monkeypatch.setattr(
        "polybot.ai.litellm_provider.acompletion",
        fake_completion,
    )
    provider = LiteLLMForecastProvider(
        api_key="test-key",
        api_base="https://gateway.example/v1",
    )

    forecast = await provider.forecast(ForecastRequest(market=market), model="vendor/model")

    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
    assert forecast.source_ids == []

    # The explicit capability mismatch is cached per model. Subsequent calls
    # go directly to locally validated JSON mode instead of paying for another
    # predictably rejected request.
    await provider.forecast(ForecastRequest(market=market), model="vendor/model")
    assert len(calls) == 3
    assert "response_format" not in calls[2]


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError(
            "invalid api key for json_schema request",
            llm_provider="vendor",
            model="vendor/model",
        ),
        RateLimitError(
            "rate limit while processing response_format",
            llm_provider="vendor",
            model="vendor/model",
        ),
        Timeout(
            "response_format request timed out",
            llm_provider="vendor",
            model="vendor/model",
        ),
        ContentPolicyViolationError(
            "content policy blocked structured output",
            llm_provider="vendor",
            model="vendor/model",
        ),
        RuntimeError("transport failed while sending response_format"),
    ],
)
async def test_litellm_does_not_retry_non_capability_failures(monkeypatch, market, error) -> None:
    calls = 0

    async def fake_completion(**_):
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(
        "polybot.ai.litellm_provider.acompletion",
        fake_completion,
    )
    provider = LiteLLMForecastProvider(
        api_key="test-key",
        api_base="https://gateway.example/v1",
    )

    with pytest.raises(type(error)):
        await provider.forecast(ForecastRequest(market=market), model="vendor/model")

    assert calls == 1


async def test_litellm_bad_request_requires_explicit_capability_mismatch(
    monkeypatch, market
) -> None:
    calls = 0

    async def fake_completion(**_):
        nonlocal calls
        calls += 1
        raise BadRequestError(
            "response_format schema failed semantic validation",
            model="vendor/model",
            llm_provider="vendor",
        )

    monkeypatch.setattr("polybot.ai.litellm_provider.acompletion", fake_completion)
    provider = LiteLLMForecastProvider(
        api_key="test-key",
        api_base="https://gateway.example/v1",
    )

    with pytest.raises(BadRequestError):
        await provider.forecast(ForecastRequest(market=market), model="vendor/model")

    assert calls == 1


async def test_litellm_explicit_bad_request_capability_mismatch_can_fallback(
    monkeypatch, market
) -> None:
    calls = 0
    payload = {
        "probability_yes": "0.50",
        "probability_low": "0.40",
        "probability_high": "0.60",
        "confidence": "0.50",
        "evidence_for": [],
        "evidence_against": [],
        "assumptions": [],
        "invalidation_conditions": [],
        "source_ids": [],
        "rationale": "Locally validated fallback.",
    }

    async def fake_completion(**_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise BadRequestError(
                "unsupported parameter: response_format json_schema is not supported",
                model="vendor/model",
                llm_provider="vendor",
            )
        return _response(json.dumps(payload))

    monkeypatch.setattr("polybot.ai.litellm_provider.acompletion", fake_completion)
    provider = LiteLLMForecastProvider(
        api_key="test-key",
        api_base="https://gateway.example/v1",
    )

    forecast = await provider.forecast(ForecastRequest(market=market), model="vendor/model")

    assert forecast.probability_yes == Decimal("0.50")
    assert calls == 2
