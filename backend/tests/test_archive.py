from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from polybot.archive import MarketArchiveWorker, build_archive_worker
from polybot.models import BookLevel, MarketSpec, OrderBookSnapshot, utc_now
from polybot.stores.memory import MemoryStore


class FakeArchiveMarketData:
    def __init__(self, markets: list[MarketSpec], books: dict[str, OrderBookSnapshot]):
        self.markets = markets
        self.books = books
        self.calls: list[str] = []

    async def list_markets(self, limit: int) -> list[MarketSpec]:
        self.calls.append(f"list:{limit}")
        return self.markets[:limit]

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBookSnapshot:
        self.calls.append(f"book:{market_id}:{token_id}")
        book = self.books.get(token_id)
        if book is None:
            raise RuntimeError(f"no book for {token_id}")
        return book


def _market(market_id: str, yes_token: str, no_token: str) -> MarketSpec:
    return MarketSpec(
        id=market_id,
        condition_id=f"condition-{market_id}",
        question=f"Will {market_id} resolve?",
        yes_token_id=yes_token,
        no_token_id=no_token,
        liquidity_usd=Decimal("50000"),
        minimum_order_size=Decimal("1"),
        tick_size=Decimal("0.01"),
        end_at=utc_now(),
    )


def _book(token_id: str, market_id: str) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        market_id=market_id,
        bids=[BookLevel(price=Decimal("0.40"), size=Decimal("100"))],
        asks=[BookLevel(price=Decimal("0.41"), size=Decimal("100"))],
        minimum_order_size=Decimal("1"),
    )


def _settings(**overrides) -> object:
    from polybot.config import Settings

    return Settings(
        _env_file=None,
        mode="paper",
        archive_market_limit=10,
        archive_interval_seconds=60,
        **overrides,
    )


async def test_run_once_saves_markets_and_two_sided_books() -> None:
    market_a = _market("m-a", "yes-a", "no-a")
    market_b = _market("m-b", "yes-b", "no-b")
    store = MemoryStore()
    market_data = FakeArchiveMarketData(
        [market_a, market_b],
        {
            "yes-a": _book("yes-a", "m-a"),
            "no-a": _book("no-a", "m-a"),
            "yes-b": _book("yes-b", "m-b"),
            "no-b": _book("no-b", "m-b"),
        },
    )
    worker = MarketArchiveWorker(
        settings=_settings(),
        store=store,
        market_data=market_data,
    )

    result = await worker.run_once()

    assert result.markets_seen == 2
    assert result.markets_saved == 2
    assert result.snapshots_saved == 4
    assert result.fully_successful
    assert sorted(market_data.calls) == [
        "book:m-a:no-a",
        "book:m-a:yes-a",
        "book:m-b:no-b",
        "book:m-b:yes-b",
        "list:10",
    ]
    assert len(store.markets) == 2
    assert len(store.snapshots) == 4


async def test_run_once_isolates_per_market_failures() -> None:
    market_a = _market("m-a", "yes-a", "no-a")
    market_b = _market("m-b", "yes-b", "no-b")
    store = MemoryStore()
    market_data = FakeArchiveMarketData(
        [market_a, market_b],
        {
            "yes-a": _book("yes-a", "m-a"),
            # no-a deliberately missing: the failure must not abort market_b
            "yes-b": _book("yes-b", "m-b"),
            "no-b": _book("no-b", "m-b"),
        },
    )
    worker = MarketArchiveWorker(
        settings=_settings(),
        store=store,
        market_data=market_data,
    )

    result = await worker.run_once()

    assert result.markets_seen == 2
    assert result.markets_saved == 2
    assert result.snapshots_saved == 3
    assert not result.fully_successful
    assert len(result.skipped) == 1
    assert result.skipped[0]["scope"] == "market:m-a"
    assert "no-a" in result.skipped[0]["reason"]


async def test_serve_runs_sweep_then_responds_to_stop() -> None:
    market_a = _market("m-a", "yes-a", "no-a")
    store = MemoryStore()
    market_data = FakeArchiveMarketData(
        [market_a],
        {"yes-a": _book("yes-a", "m-a"), "no-a": _book("no-a", "m-a")},
    )
    worker = MarketArchiveWorker(
        settings=_settings(),
        store=store,
        market_data=market_data,
    )
    stop = asyncio.Event()

    task = asyncio.create_task(worker.serve(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=5)

    assert len(store.snapshots) == 2


def test_build_archive_worker_requires_supabase() -> None:
    with pytest.raises(ValueError, match="SUPABASE"):
        build_archive_worker(_settings())
