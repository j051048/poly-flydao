from __future__ import annotations

from decimal import Decimal

import pytest

from polybot.ai.fallback import FallbackForecastProvider
from polybot.config import Settings
from polybot.models import Forecast, ForecastRequest, MarketSpec
from polybot.runtime import _ai


class FlakyProvider:
    def __init__(self, name: str, fail: bool):
        self.name = name
        self.fail = fail
        self.calls = 0
        self.usage_log: list = []

    async def forecast(self, request, *, model):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.name} unavailable")
        return Forecast(
            market_id=request.market.id,
            probability_yes=Decimal("0.5"),
            probability_low=Decimal("0.4"),
            probability_high=Decimal("0.6"),
            confidence=Decimal("0.8"),
            model=model,
        )

    def last_usage(self):
        return self.usage_log[-1] if self.usage_log else None

    def drain_usage(self):
        records = self.usage_log
        self.usage_log = []
        return records

    async def close(self):
        return None


def _request() -> ForecastRequest:
    return ForecastRequest(
        market=MarketSpec(
            id="m1",
            question="Will the fallback test resolve?",
            yes_token_id="yes",
            no_token_id="no",
        ),
        evidence=[],
    )


async def test_fallback_tries_next_provider_on_failure() -> None:
    primary = FlakyProvider("primary", fail=True)
    secondary = FlakyProvider("secondary", fail=False)
    provider = FallbackForecastProvider([primary, secondary])

    forecast = await provider.forecast(_request(), model="test-model")

    assert forecast.probability_yes == Decimal("0.5")
    assert primary.calls == 1
    assert secondary.calls == 1


async def test_fallback_raises_when_all_providers_fail() -> None:
    provider = FallbackForecastProvider(
        [FlakyProvider("a", fail=True), FlakyProvider("b", fail=True)]
    )
    with pytest.raises(RuntimeError, match="all forecast providers failed"):
        await provider.forecast(_request(), model="test-model")


async def test_fallback_drains_usage_from_successful_provider() -> None:
    primary = FlakyProvider("primary", fail=True)
    secondary = FlakyProvider("secondary", fail=False)
    secondary.usage_log = [
        type(
            "Usage",
            (),
            {"provider": "secondary", "total_tokens": 7},
        )()
    ]
    provider = FallbackForecastProvider([primary, secondary])

    await provider.forecast(_request(), model="test-model")
    records = provider.drain_usage()

    assert len(records) == 1
    assert records[0].total_tokens == 7


async def test_fallback_close_closes_all_providers() -> None:
    primary = FlakyProvider("primary", fail=False)
    secondary = FlakyProvider("secondary", fail=False)
    provider = FallbackForecastProvider([primary, secondary])

    await provider.close()


def test_runtime_wraps_fallback_providers_when_configured() -> None:
    settings = Settings(
        _env_file=None,
        mode="paper",
        ai_provider="openai",
        openai_api_key="key",
        ai_fallback_providers="litellm, openai",
        litellm_base_url="https://relay.example.com/v1",
    )
    provider, _ = _ai(settings)

    assert isinstance(provider, FallbackForecastProvider)
    assert len(provider.providers) == 2
