from __future__ import annotations

from decimal import Decimal

import pytest

from polybot.ai.graph import ForecastGraph
from polybot.ai.mock import StaticForecastProvider
from polybot.ai.usage import build_usage, estimate_cost_usd
from polybot.models import AIUsageRecord, ForecastRequest
from polybot.stores.memory import MemoryStore


def test_estimate_cost_usd_known_and_unknown_models() -> None:
    cost = estimate_cost_usd("gpt-4o-mini", 1000, 500)
    assert cost is not None
    assert cost == Decimal("0.00045")
    assert estimate_cost_usd("custom-model-xyz", 1000, 500) is None


def test_build_usage_clamps_negative_tokens() -> None:
    usage = build_usage(
        provider="openai",
        model="gpt-4o-mini",
        market_id="m1",
        request_id="req-1",
        input_tokens=-1,
        output_tokens=500,
        latency_ms=123,
    )
    assert usage.input_tokens == 0
    assert usage.total_tokens == 500
    assert usage.cost_usd is not None


class UsageAwareProvider(StaticForecastProvider):
    def __init__(self, forecast):
        super().__init__(forecast)
        self._usage_log = [
            AIUsageRecord(
                provider="test",
                model="test-model",
                market_id=forecast.market_id,
                input_tokens=10,
                output_tokens=20,
                total_tokens=30,
                latency_ms=42,
                cost_usd=Decimal("0.00001"),
            ),
            AIUsageRecord(
                provider="test",
                model="test-model",
                market_id=forecast.market_id,
                input_tokens=5,
                output_tokens=8,
                total_tokens=13,
                latency_ms=33,
            ),
        ]

    def last_usage(self):
        return self._usage_log[-1] if self._usage_log else None

    def drain_usage(self):
        records = self._usage_log
        self._usage_log = []
        return records


async def test_graph_drain_returns_all_usage_records_and_clears(forecast, market) -> None:
    graph = ForecastGraph(
        UsageAwareProvider(forecast),
        primary_model="primary",
        critic_model="critic",
    )
    await graph.forecast(ForecastRequest(market=market, evidence=[]))

    records = graph.drain_usage()
    assert [record.total_tokens for record in records] == [30, 13]
    assert graph.usage() is None
    assert graph.drain_usage() == []


async def test_graph_exposes_usage_from_usage_aware_provider(forecast, market) -> None:
    graph = ForecastGraph(
        UsageAwareProvider(forecast),
        primary_model="primary",
        critic_model="critic",
    )
    await graph.forecast(ForecastRequest(market=market, evidence=[]))
    usage = graph.usage()
    assert usage is not None
    assert usage.provider == "test"
    assert usage.total_tokens == 13  # most recent model call


async def test_graph_usage_is_none_for_plain_provider(forecast, market) -> None:
    graph = ForecastGraph(
        StaticForecastProvider(forecast),
        primary_model="primary",
        critic_model="critic",
    )
    await graph.forecast(ForecastRequest(market=market, evidence=[]))
    assert graph.usage() is None


async def test_memory_store_records_ai_usage() -> None:
    store = MemoryStore()
    usage = AIUsageRecord(
        provider="openai",
        model="gpt-4o-mini",
        market_id="m1",
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        latency_ms=10,
    )
    await store.record_ai_usage("acct", usage)
    records = await store.list_ai_usage("acct", limit=10)
    assert len(records) == 1
    assert records[0].provider == "openai"
    with pytest.raises(ValueError, match="non-negative"):
        await store.record_ai_usage("acct", usage.model_copy(update={"total_tokens": -1}))
