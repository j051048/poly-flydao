from __future__ import annotations

from decimal import Decimal

from polybot.ai.openai_provider import OpenAIForecastProvider
from polybot.models import ForecastPayload, ForecastRequest, MarketSpec


class FakeUsage:
    input_tokens = 12
    output_tokens = 7


class FakeResponse:
    id = "resp_diagnostic_1"
    usage = FakeUsage()

    def __init__(self, payload: ForecastPayload):
        self.output_parsed = payload


class FakeClient:
    def __init__(self, response):
        self._response = response

    @property
    def responses(self):
        return self

    def parse(self, **kwargs):
        assert kwargs["store"] is False
        assert kwargs["max_output_tokens"] == 1800
        return self._response


def _request() -> ForecastRequest:
    return ForecastRequest(
        market=MarketSpec(
            id="m1",
            question="Will the usage test resolve?",
            yes_token_id="yes",
            no_token_id="no",
        ),
        evidence=[],
    )


async def test_openai_provider_records_usage_without_breaking_forecast() -> None:
    payload = ForecastPayload(
        probability_yes=Decimal("0.6"),
        probability_low=Decimal("0.5"),
        probability_high=Decimal("0.7"),
        confidence=Decimal("0.9"),
        evidence_for=[],
        evidence_against=[],
        assumptions=[],
        invalidation_conditions=[],
        source_ids=[],
        rationale="usage test",
    )
    provider = OpenAIForecastProvider(
        api_key="unused",
        client=FakeClient(FakeResponse(payload)),  # type: ignore[arg-type]
    )

    forecast = await provider.forecast(_request(), model="gpt-4o-mini")
    usage = provider.last_usage()

    assert forecast.probability_yes == Decimal("0.6")
    assert usage is not None
    assert usage.request_id == "resp_diagnostic_1"
    assert usage.input_tokens == 12
    assert usage.output_tokens == 7
    assert usage.total_tokens == 19
    assert usage.latency_ms is not None
    assert usage.cost_usd is not None and usage.cost_usd > 0
