from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from polybot.ai.evidence import NoopEvidenceCollector
from polybot.ai.graph import ForecastGraph
from polybot.ai.mock import StaticForecastProvider
from polybot.brokers.paper import PaperBroker
from polybot.config import Settings
from polybot.engine import TradingEngine
from polybot.market_filters.crypto_updown import SUPPORTED_ASSETS
from polybot.models import MarketSpec, OrderBookSnapshot, utc_now
from polybot.risk import RiskEngine
from polybot.runtime import _market_filter, build_runtime
from polybot.stores.memory import MemoryStore
from polybot.strategy import ValueStrategy


def _btc_updown_market() -> MarketSpec:
    now = utc_now()
    return MarketSpec(
        id="btc-5m",
        condition_id="btc-condition",
        event_id="btc-event",
        slug="bitcoin-up-or-down-september-21-1200",
        question="Bitcoin Up or Down in the next 5 minutes?",
        category="crypto",
        tags=("Crypto", "Bitcoin"),
        resolution_source="https://example.test/btc-usd",
        yes_token_id="btc-up",
        no_token_id="btc-down",
        yes_label="Up",
        no_label="Down",
        liquidity_usd=Decimal("50000"),
        minimum_order_size=Decimal("1"),
        tick_size=Decimal("0.01"),
        start_at=now,
        end_at=now + timedelta(minutes=5),
    )


def _empty_book(token_id: str) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        market_id="unused",
        token_id=token_id,
        bids=[],
        asks=[],
        minimum_order_size=Decimal("1"),
    )


class _Source:
    """Minimal market data double: lists what it is given, yields empty books."""

    def __init__(self, markets: list[MarketSpec]) -> None:
        self.markets = markets
        self.book_requests: list[tuple[str, str]] = []
        self.condition_lookups: list[str] = []

    async def list_markets(self, limit: int) -> list[MarketSpec]:
        return list(self.markets[:limit])

    async def get_market_by_condition(self, condition_id: str) -> MarketSpec:
        self.condition_lookups.append(condition_id)
        raise LookupError(condition_id)

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBookSnapshot:
        self.book_requests.append((market_id, token_id))
        return _empty_book(token_id).model_copy(update={"market_id": market_id})


def _engine(settings: Settings, forecast, source, broker, market_filter) -> TradingEngine:
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
        broker=broker,
        store=MemoryStore(),
        market_filter=market_filter,
    )


def test_supported_assets_cover_the_configured_defaults() -> None:
    assert {"BTC", "ETH"} <= SUPPORTED_ASSETS


def test_market_filter_helper_is_disabled_by_default(settings: Settings) -> None:
    assert _market_filter(settings) is None


def test_market_filter_helper_keeps_only_crypto_updown_markets(
    settings: Settings, market: MarketSpec
) -> None:
    configured = settings.validated_copy(market_filter="crypto_updown")
    apply = _market_filter(configured)

    assert apply is not None
    kept = apply([market, _btc_updown_market()])

    assert [item.id for item in kept] == ["btc-5m"]


def test_market_filter_helper_honours_configured_assets(
    settings: Settings, market: MarketSpec
) -> None:
    configured = settings.validated_copy(
        market_filter="crypto_updown", crypto_updown_assets="ETH"
    )
    apply = _market_filter(configured)

    assert apply is not None
    assert apply([market, _btc_updown_market()]) == []


def test_settings_reject_symbols_without_a_classifier(settings: Settings) -> None:
    with pytest.raises(ValidationError):
        settings.validated_copy(market_filter="crypto_updown", crypto_updown_assets="DOGE")

    with pytest.raises(ValidationError):
        settings.validated_copy(market_filter="crypto_updown", crypto_updown_assets="  , ")


async def test_engine_excludes_filtered_markets_without_spending_budget(
    settings: Settings, market: MarketSpec, forecast
) -> None:
    source = _Source([market, _btc_updown_market()])
    engine = _engine(
        settings,
        forecast,
        source,
        PaperBroker(Decimal("1000")),
        lambda markets: [item for item in markets if item.id == "btc-5m"],
    )

    report = await engine.run_cycle()

    assert report.markets_scanned == 1
    assert report.skipped["market_filter_excluded"] == 1
    assert {market_id for market_id, _ in source.book_requests} == {"btc-5m"}


async def test_held_market_excluded_by_filter_survives_as_reduce_only(
    settings: Settings, market: MarketSpec, forecast
) -> None:
    broker = PaperBroker(Decimal("1000"))
    broker.positions[market.yes_token_id] = Decimal("10")
    broker.position_costs[market.yes_token_id] = Decimal("4")
    source = _Source([market, _btc_updown_market()])
    engine = _engine(
        settings,
        forecast,
        source,
        broker,
        lambda markets: [item for item in markets if item.id == "btc-5m"],
    )

    report = await engine.run_cycle()

    # The holding is still visible to the cycle so it can be exited, but the
    # filter is never bypassed to open a new position in it.
    assert "market_filter_excluded" not in report.skipped
    assert report.skipped["market_filter_reduce_only"] == 1
    assert report.forecasts_created == 0
    assert {market_id for market_id, _ in source.book_requests} == {"m1", "btc-5m"}


def test_build_runtime_attaches_the_configured_market_filter(
    settings: Settings, market: MarketSpec
) -> None:
    configured = settings.validated_copy(market_filter="crypto_updown")

    runtime = build_runtime(configured, store_override=MemoryStore())

    assert runtime.engine.market_filter is not None
    kept = runtime.engine.market_filter([market, _btc_updown_market()])
    assert [item.id for item in kept] == ["btc-5m"]


def test_build_runtime_leaves_the_universe_unfiltered_by_default(settings: Settings) -> None:
    runtime = build_runtime(settings, store_override=MemoryStore())

    assert runtime.engine.market_filter is None
