from __future__ import annotations

from decimal import Decimal

import pytest

from polybot.ai.calibration import (
    CalibrationConfig,
    Calibrator,
    build_curve,
    refresh_calibrator,
)
from polybot.ai.evidence import NoopEvidenceCollector
from polybot.ai.graph import ForecastGraph
from polybot.ai.mock import StaticForecastProvider
from polybot.brokers.paper import PaperBroker
from polybot.engine import AI_UNITS_PER_FORECAST, TradingEngine
from polybot.models import (
    BookLevel,
    Forecast,
    MarketSpec,
    OrderBookSnapshot,
)
from polybot.performance import CalibrationBin, PerformanceSnapshot
from polybot.risk import RiskEngine
from polybot.stores.memory import MemoryStore
from polybot.strategy import ValueStrategy


def _snapshot(*, samples: int, forecast: str, observed: str) -> PerformanceSnapshot:
    return PerformanceSnapshot(
        sample_size=samples,
        calibration=[
            CalibrationBin(
                lower=Decimal("0.7"),
                upper=Decimal("0.8"),
                samples=samples,
                mean_forecast=Decimal(forecast),
                observed_frequency=Decimal(observed),
            )
        ],
    )


def _resolved_rows(count: int, *, probability: str = "0.75") -> list[dict[str, object]]:
    return [
        {
            "market_id": f"m{index}",
            "probability_yes": probability,
            "resolved_yes": index % 2 == 0,
            "brier_score": "0.1",
            "log_loss": "0.2",
        }
        for index in range(count)
    ]


def test_curve_requires_enough_samples() -> None:
    assert build_curve(None) is None
    assert build_curve(PerformanceSnapshot(sample_size=0)) is None
    assert build_curve(_snapshot(samples=10, forecast="0.75", observed="0.55")) is None
    assert build_curve(
        _snapshot(samples=60, forecast="0.75", observed="0.55"),
        CalibrationConfig(enabled=False),
    ) is None


def test_curve_shrinks_an_over_confident_model_toward_observed_frequency() -> None:
    snapshot = _snapshot(samples=60, forecast="0.75", observed="0.55")
    calibrator = Calibrator.from_snapshot(snapshot)

    assert calibrator.active
    assert calibrator.samples == 60
    # One anchor, 60 samples, shrinkage 60 -> half the correction is applied.
    assert calibrator.apply(Decimal("0.75")) == Decimal("0.65")


def test_curve_never_moves_a_forecast_further_than_the_configured_cap() -> None:
    snapshot = _snapshot(samples=600, forecast="0.75", observed="0")
    calibrator = Calibrator.from_snapshot(snapshot)

    adjusted = calibrator.apply(Decimal("0.75"))

    assert adjusted == Decimal("0.60")
    assert calibrator.apply(Decimal("0")) == Decimal("0")
    assert calibrator.apply(Decimal("1")) <= Decimal("1")


def test_inactive_calibrator_is_the_identity() -> None:
    calibrator = Calibrator()

    assert not calibrator.active
    assert calibrator.apply(Decimal("0.42")) == Decimal("0.42")
    assert "inactive" in calibrator.describe()


def test_curve_is_monotone_even_when_bins_are_inverted() -> None:
    snapshot = PerformanceSnapshot(
        sample_size=80,
        calibration=[
            CalibrationBin(
                lower=Decimal("0.3"),
                upper=Decimal("0.4"),
                samples=40,
                mean_forecast=Decimal("0.35"),
                observed_frequency=Decimal("0.60"),
            ),
            CalibrationBin(
                lower=Decimal("0.4"),
                upper=Decimal("0.5"),
                samples=40,
                mean_forecast=Decimal("0.45"),
                observed_frequency=Decimal("0.30"),
            ),
        ],
    )

    curve = build_curve(snapshot)

    assert curve is not None
    assert [anchor.observed for anchor in curve.anchors] == [
        Decimal("0.6"),
        Decimal("0.6"),
    ]


async def test_refresh_calibrator_reads_the_ledger() -> None:
    store = MemoryStore()
    for row in _resolved_rows(60):
        await store.record_forecast_outcome("account", row)
    calibrator = Calibrator()

    active = await refresh_calibrator(store, account_id="account", calibrator=calibrator)

    assert active
    assert calibrator.samples == 60
    assert calibrator.observed_at is not None


async def test_graph_applies_the_reliability_curve() -> None:
    forecast = Forecast(
        id="11111111-1111-1111-1111-111111111111",
        market_id="m1",
        probability_yes=Decimal("0.75"),
        probability_low=Decimal("0.70"),
        probability_high=Decimal("0.80"),
        confidence=Decimal("0.90"),
        source_ids=[],
        model="test-model",
    )
    graph = ForecastGraph(
        StaticForecastProvider(forecast),
        primary_model="primary",
        critic_model="critic",
    )
    request = _request_for(forecast.market_id)

    uncalibrated = await graph.forecast(request)
    graph.set_calibrator(
        Calibrator.from_snapshot(_snapshot(samples=60, forecast="0.75", observed="0.55"))
    )
    calibrated = await graph.forecast(request)

    assert uncalibrated.probability_yes == Decimal("0.75")
    assert calibrated.probability_yes == Decimal("0.65")
    assert "Reliability calibration" in calibrated.rationale
    assert calibrated.probability_low <= calibrated.probability_yes
    assert calibrated.probability_high >= uncalibrated.probability_high


async def test_memory_store_budget_reservation_is_all_or_nothing() -> None:
    store = MemoryStore()
    store.set_ai_budget_limit("account", 3)

    assert await store.consume_ai_budget("account", units=2) == (True, 2, 3)
    # A partial reservation must not be taken when the remainder does not fit.
    assert await store.consume_ai_budget("account", units=2) == (False, 2, 3)
    assert await store.consume_ai_budget("account", units=1) == (True, 3, 3)
    with pytest.raises(ValueError):
        await store.consume_ai_budget("account", units=0)


def _request_for(market_id: str):
    from polybot.models import ForecastRequest, MarketSpec

    return ForecastRequest(
        market=MarketSpec(
            id=market_id,
            question="Will the synthetic test event occur?",
            yes_token_id="yes-1",
            no_token_id="no-1",
        ),
        evidence=[],
    )


class _BookSource:
    """Lists the given markets and serves a two-sided book for each token."""

    def __init__(self, markets: list[MarketSpec]) -> None:
        self.markets = markets
        self.book_requests: list[tuple[str, str]] = []

    async def list_markets(self, limit: int) -> list[MarketSpec]:
        return list(self.markets[:limit])

    async def get_market_by_condition(self, condition_id: str) -> MarketSpec:
        raise LookupError(condition_id)

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBookSnapshot:
        self.book_requests.append((market_id, token_id))
        yes = token_id == self.markets[0].yes_token_id
        return OrderBookSnapshot(
            market_id=market_id,
            token_id=token_id,
            bids=[
                BookLevel(
                    price=Decimal("0.39") if yes else Decimal("0.58"),
                    size=Decimal("100"),
                )
            ],
            asks=[
                BookLevel(
                    price=Decimal("0.40") if yes else Decimal("0.60"),
                    size=Decimal("100"),
                )
            ],
            minimum_order_size=Decimal("1"),
        )


def _engine(settings, forecast, source, store) -> TradingEngine:
    return TradingEngine(
        settings=settings,
        market_data=source,
        evidence_collector=NoopEvidenceCollector(),
        forecaster=ForecastGraph(
            StaticForecastProvider(forecast),
            primary_model="primary",
            critic_model="critic",
        ),
        strategy=ValueStrategy(settings),
        risk=RiskEngine(settings),
        broker=PaperBroker(Decimal("1000")),
        store=store,
    )


async def test_engine_reserves_ai_budget_before_forecasting(
    settings, market: MarketSpec, forecast
) -> None:
    store = MemoryStore()
    engine = _engine(settings, forecast, _BookSource([market]), store)

    report = await engine.run_cycle()

    assert report.forecasts_created == 1
    assert report.ai_units_reserved == AI_UNITS_PER_FORECAST
    assert await store.consume_ai_budget(settings.account_id, units=1) == (
        True,
        AI_UNITS_PER_FORECAST + 1,
        100,
    )


async def test_engine_hard_stops_ai_when_the_budget_is_exhausted(
    settings, market: MarketSpec, forecast
) -> None:
    store = MemoryStore()
    store.set_ai_budget_limit(settings.account_id, 1)
    source = _BookSource([market])
    engine = _engine(settings, forecast, source, store)

    report = await engine.run_cycle()

    assert report.forecasts_created == 0
    assert report.ai_units_reserved == 0
    assert report.skipped["ai_budget_exhausted"] == 1
    # No provider call may happen once the reservation is refused.
    assert source.book_requests == [
        (market.id, market.yes_token_id),
        (market.id, market.no_token_id),
    ]


async def test_engine_fails_closed_when_the_budget_cannot_be_read(
    settings, market: MarketSpec, forecast
) -> None:
    class _BrokenBudgetStore(MemoryStore):
        async def consume_ai_budget(self, account_id: str, units: int = 1):
            raise RuntimeError("ledger unavailable")

    engine = _engine(settings, forecast, _BookSource([market]), _BrokenBudgetStore())
    report = await engine.run_cycle()

    assert report.forecasts_created == 0
    assert report.skipped["ai_budget_unavailable:RuntimeError"] == 1
