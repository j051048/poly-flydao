from __future__ import annotations

from decimal import Decimal
from typing import Any

from polybot.ai.evidence import NoopEvidenceCollector
from polybot.ai.graph import ForecastGraph
from polybot.ai.mock import StaticForecastProvider
from polybot.brokers.paper import PaperBroker
from polybot.engine import TradingEngine
from polybot.market import PolymarketMarketData
from polybot.models import (
    ExecutionStatus,
    MarketSpec,
    OrderBookSnapshot,
)
from polybot.risk import RiskEngine
from polybot.stores.memory import MemoryStore
from polybot.strategy import ValueStrategy


class _Paginator:
    def __init__(self, items: list[Any]):
        self.items = items

    def iter_items(self):
        yield from self.items


class _PublicClient:
    def __init__(self, raw_market: dict[str, Any]):
        self.raw_market = raw_market
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def list_markets(self, **kwargs):
        self.list_calls.append(kwargs)
        return _Paginator([{"id": self.raw_market["id"]}])

    def get_market(self, **kwargs):
        self.get_calls.append(kwargs)
        return self.raw_market


def _raw_market(market: MarketSpec) -> dict[str, Any]:
    return {
        "id": market.id,
        "condition_id": market.condition_id,
        "question": market.question,
        "category": market.category,
        "outcomes": {
            "yes": {"token_id": market.yes_token_id},
            "no": {"token_id": market.no_token_id},
        },
        "state": {"active": True, "closed": False, "accepting_orders": True},
        "metrics": {"liquidity": "10000", "volume_24hr": "1000"},
        "trading": {"minimum_order_size": "1", "minimum_tick_size": "0.01"},
        "resolution": {},
        "events": [{"id": market.event_id}],
    }


async def test_official_adapter_resolves_condition_then_gets_exact_market(
    market: MarketSpec,
) -> None:
    client = _PublicClient(_raw_market(market))
    source = PolymarketMarketData(client=client)

    resolved = await source.get_market_by_condition("c1")

    assert resolved.id == "m1"
    assert resolved.condition_id == "c1"
    assert client.list_calls == [{"condition_ids": ["c1"], "page_size": 1}]
    assert client.get_calls == [{"id": "m1"}]


class _HeldMarketSource:
    def __init__(
        self,
        held_market: MarketSpec,
        books: dict[str, OrderBookSnapshot],
        *,
        listed: list[MarketSpec] | None = None,
        lookup_error: Exception | None = None,
    ):
        self.held_market = held_market
        self.books = books
        self.listed = listed or []
        self.lookup_error = lookup_error
        self.lookups: list[str] = []
        self.book_requests: list[tuple[str, str]] = []

    async def list_markets(self, limit: int) -> list[MarketSpec]:
        return self.listed[:limit]

    async def get_market_by_condition(self, condition_id: str) -> MarketSpec:
        self.lookups.append(condition_id)
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.held_market

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBookSnapshot:
        self.book_requests.append((market_id, token_id))
        return self.books[token_id].model_copy(update={"market_id": market_id})


def _seed_breached_position(broker: PaperBroker, market: MarketSpec) -> None:
    token_id = market.yes_token_id
    broker.positions[token_id] = Decimal("10")
    broker.position_costs[token_id] = Decimal("4")
    broker.event_exposure[market.event_id or market.id] = Decimal("4")
    broker.bucket_exposure[market.category] = Decimal("4")
    broker.token_events[token_id] = market.event_id or market.id
    broker.token_buckets[token_id] = market.category
    broker.token_condition_ids[token_id] = market.condition_id or market.id
    broker.realized_pnl = Decimal("-30")


def _engine(settings, forecast, source, broker) -> TradingEngine:
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
    )


async def test_held_market_omitted_from_scan_is_added_and_risk_exited(
    settings,
    market: MarketSpec,
    forecast,
    yes_book: OrderBookSnapshot,
    no_book: OrderBookSnapshot,
) -> None:
    class RecordingPaperBroker(PaperBroker):
        cancel_calls = 0

        async def cancel_all(self, reason: str) -> bool:
            self.cancel_calls += 1
            return await super().cancel_all(reason)

    broker = RecordingPaperBroker(Decimal("1000"))
    _seed_breached_position(broker, market)
    popular = market.model_copy(
        update={
            "id": "popular",
            "condition_id": "popular-condition",
            "event_id": "popular-event",
            "yes_token_id": "popular-yes",
            "no_token_id": "popular-no",
            "liquidity_usd": Decimal("999999"),
        }
    )
    source = _HeldMarketSource(
        market,
        {
            market.yes_token_id: yes_book,
            market.no_token_id: no_book,
            popular.yes_token_id: yes_book.model_copy(
                update={"token_id": popular.yes_token_id, "market_id": popular.id}
            ),
            popular.no_token_id: no_book.model_copy(
                update={"token_id": popular.no_token_id, "market_id": popular.id}
            ),
        },
        listed=[popular],
    )

    report = await _engine(settings, forecast, source, broker).run_cycle()

    assert source.lookups == ["c1"]
    assert report.markets_scanned == 1
    # The held market is processed before a much more liquid discovery result,
    # so the hard exit completes before any non-held market can consume AI.
    assert [market_id for market_id, _ in source.book_requests[:2]] == ["m1", "m1"]
    assert broker.cancel_calls == 1
    assert report.forecasts_created == 0
    assert report.candidates_created >= 1
    assert report.executions[0].status is ExecutionStatus.PAPER_FILLED
    assert broker.positions[market.yes_token_id] == 0


async def test_held_market_lookup_failure_is_audited_without_aborting_cycle(
    settings,
    market: MarketSpec,
    forecast,
) -> None:
    broker = PaperBroker(Decimal("1000"))
    _seed_breached_position(broker, market)
    listed = market.model_copy(
        update={
            "id": "listed",
            "condition_id": "listed-condition",
            "event_id": "listed-event",
            "yes_token_id": "listed-yes",
            "no_token_id": "listed-no",
            "active": False,
        }
    )
    source = _HeldMarketSource(
        market,
        {},
        listed=[listed],
        lookup_error=LookupError("not found"),
    )

    report = await _engine(settings, forecast, source, broker).run_cycle()

    assert report.completed_at is not None
    assert report.markets_scanned == 0
    assert report.skipped["held_market_lookup_error:LookupError"] == 1


async def test_hard_risk_stops_cycle_when_cancel_cannot_be_verified(
    settings,
    market: MarketSpec,
    forecast,
    yes_book: OrderBookSnapshot,
    no_book: OrderBookSnapshot,
) -> None:
    class UnverifiedCancelBroker(PaperBroker):
        async def cancel_all(self, reason: str) -> bool:
            return False

    broker = UnverifiedCancelBroker(Decimal("1000"))
    _seed_breached_position(broker, market)
    source = _HeldMarketSource(
        market,
        {market.yes_token_id: yes_book, market.no_token_id: no_book},
        listed=[market],
    )

    report = await _engine(settings, forecast, source, broker).run_cycle()

    assert report.markets_scanned == 0
    assert not report.executions
    assert report.skipped["hard_risk_cancel_unverified"] == 1
