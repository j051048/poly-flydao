from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from polybot.ai.openai_compatible_provider import (
    OpenAICompatibleForecastProvider,
    PublicAIEndpointTransport,
)
from polybot.ai_endpoint import UnsafeAIBaseURLError
from polybot.models import ForecastRequest


class FakeCompletions:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content),
                )
            ]
        )


class FakeClient:
    def __init__(self, content: str):
        self.chat = SimpleNamespace(completions=FakeCompletions(content))


async def test_openai_compatible_provider_validates_and_parses(
    market,
) -> None:
    validations: list[str] = []

    async def validate(value: str) -> str:
        validations.append(value)
        return value

    payload = {
        "probability_yes": "0.55",
        "probability_low": "0.45",
        "probability_high": "0.65",
        "confidence": "0.60",
        "evidence_for": [],
        "evidence_against": [],
        "assumptions": ["test"],
        "invalidation_conditions": ["new evidence"],
        "source_ids": ["invented"],
        "rationale": "Relay forecast.",
    }
    client = FakeClient(json.dumps(payload))
    provider = OpenAICompatibleForecastProvider(
        api_key="unused-test-key",
        api_base="https://relay.example.com/v1",
        client=client,  # type: ignore[arg-type]
        endpoint_validator=validate,
    )

    forecast = await provider.forecast(
        ForecastRequest(market=market),
        model="relay-model",
    )

    assert forecast.probability_yes == Decimal("0.55")
    assert forecast.source_ids == []
    assert validations == ["https://relay.example.com/v1"]
    assert client.chat.completions.calls[0]["response_format"]


async def test_public_transport_blocks_private_dns_before_sending() -> None:
    async def resolver(_: str, __: int) -> list[str]:
        return ["127.0.0.1"]

    transport = PublicAIEndpointTransport(resolver=resolver)
    request = httpx.Request(
        "POST",
        "https://relay.example.com/v1/chat/completions",
        headers={"Authorization": "Bearer must-not-leave"},
    )
    with pytest.raises(UnsafeAIBaseURLError):
        await transport.handle_async_request(request)
    await transport.aclose()
