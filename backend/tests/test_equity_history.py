from __future__ import annotations

from decimal import Decimal

import pytest

from polybot.ai.evidence import NoopEvidenceCollector
from polybot.ai.graph import ForecastGraph
from polybot.ai.mock import StaticForecastProvider
from polybot.brokers.paper import PaperBroker
from polybot.engine import TradingEngine
from polybot.market import StaticMarketData
from polybot.models import ExecutionStatus, utc_now
from polybot.risk import RiskEngine
from polybot.stores.memory import MemoryStore
from polybot.strategy import ValueStrategy


async def test_memory_store_records_and_lists_chronological_history() -> None:
    store = MemoryStore()
    await store.record_equity_history("acct", Decimal("100"), source="paper_cycle")
    await store.record_equity_history("acct", Decimal("99"), source="paper_cycle")
    await store.record_equity_history("other", Decimal("1"), source="paper_cycle")

    points = await store.list_equity_history("acct", limit=10)

    assert len(points) == 2
    assert points[0].equity_usd == Decimal("100")
    assert points[1].equity_usd == Decimal("99")
    assert all(point.source == "paper_cycle" for point in points)
    assert points[0].recorded_at <= points[1].recorded_at


async def test_memory_store_rejects_negative_equity_and_empty_source() -> None:
    store = MemoryStore()
    with pytest.raises(ValueError, match="non-negative"):
        await store.record_equity_history("acct", Decimal("-1"), source="paper_cycle")
    with pytest.raises(ValueError, match="non-empty"):
        await store.record_equity_history("acct", Decimal("1"), source="   ")
    assert await store.list_equity_history("acct", limit=10) == []


async def test_engine_records_one_equity_point_per_cycle(
    settings, market, forecast, yes_book, no_book
) -> None:
    store = MemoryStore()
    engine = TradingEngine(
        settings=settings,
        market_data=StaticMarketData(
            [market], {market.yes_token_id: yes_book, market.no_token_id: no_book}
        ),
        evidence_collector=NoopEvidenceCollector(),
        forecaster=ForecastGraph(
            StaticForecastProvider(forecast), primary_model="p", critic_model="c"
        ),
        strategy=ValueStrategy(settings),
        risk=RiskEngine(settings),
        broker=PaperBroker(Decimal("1000")),
        store=store,
    )

    report = await engine.run_cycle()

    assert report.executions[0].status is ExecutionStatus.PAPER_FILLED
    points = await store.list_equity_history("00000000-0000-0000-0000-000000000001", limit=10)
    assert len(points) == 1
    assert points[0].source == "paper_cycle"
    assert points[0].equity_usd > 0


async def test_equity_history_recording_never_blocks_a_cycle(
    settings, market, forecast, yes_book, no_book
) -> None:
    class BrokenHistoryStore(MemoryStore):
        async def record_equity_history(self, account_id: str, equity_usd: Decimal, source: str):
            raise RuntimeError("history backend unavailable")

    store = BrokenHistoryStore()
    engine = TradingEngine(
        settings=settings,
        market_data=StaticMarketData(
            [market], {market.yes_token_id: yes_book, market.no_token_id: no_book}
        ),
        evidence_collector=NoopEvidenceCollector(),
        forecaster=ForecastGraph(
            StaticForecastProvider(forecast), primary_model="p", critic_model="c"
        ),
        strategy=ValueStrategy(settings),
        risk=RiskEngine(settings),
        broker=PaperBroker(Decimal("1000")),
        store=store,
    )

    report = await engine.run_cycle()

    assert report.markets_scanned == 1
    assert report.executions[0].status is ExecutionStatus.PAPER_FILLED


async def test_paper_cycle_records_equity_history(settings, market, yes_book, no_book) -> None:
    from polybot.models import Forecast

    forecast = Forecast(
        market_id=market.id,
        probability_yes=Decimal("0.70"),
        probability_low=Decimal("0.65"),
        probability_high=Decimal("0.75"),
        confidence=Decimal("0.9"),
        model="test",
    )
    store = MemoryStore()
    engine = TradingEngine(
        settings=settings,
        market_data=StaticMarketData(
            [market], {market.yes_token_id: yes_book, market.no_token_id: no_book}
        ),
        evidence_collector=NoopEvidenceCollector(),
        forecaster=ForecastGraph(
            StaticForecastProvider(forecast), primary_model="p", critic_model="c"
        ),
        strategy=ValueStrategy(settings),
        risk=RiskEngine(settings),
        broker=PaperBroker(Decimal("1000")),
        store=store,
    )

    await engine.run_cycle()

    points = await store.list_equity_history("00000000-0000-0000-0000-000000000001", limit=10)
    assert len(points) == 1
    assert points[0].recorded_at <= utc_now()
