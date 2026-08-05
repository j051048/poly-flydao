from __future__ import annotations

from datetime import timedelta

from polybot.models import MarketSpec, Outcome, utc_now
from polybot.resolution_worker import MarketResolutionWorker


class FakeRepository:
    def __init__(self, conditions: list[str]):
        self.conditions = conditions
        self.recorded: list[tuple[str, str]] = []

    async def unresolved_market_conditions(self, *, limit: int) -> list[str]:
        return self.conditions[:limit]

    async def record_market_resolution(self, *, condition_id: str, outcome: str) -> int:
        self.recorded.append((condition_id, outcome))
        return 1


class FakeResolutionMarketData:
    def __init__(self, markets: dict[str, MarketSpec]):
        self.markets = markets

    async def get_market_by_condition(self, condition_id: str) -> MarketSpec:
        market = self.markets.get(condition_id)
        if market is None:
            raise LookupError(f"no market for {condition_id}")
        return market


def _closed_market(condition_id: str, outcome: Outcome) -> MarketSpec:
    return MarketSpec(
        id=condition_id,
        condition_id=condition_id,
        question="Will the resolution test resolve?",
        yes_token_id="yes",
        no_token_id="no",
        active=False,
        closed=True,
        resolved_outcome=outcome,
        end_at=utc_now() - timedelta(days=1),
    )


async def test_run_once_records_only_closed_resolved_markets() -> None:
    repository = FakeRepository(["c1", "c2", "c3"])
    market_data = FakeResolutionMarketData(
        {
            "c1": _closed_market("c1", Outcome.YES),
            "c2": _closed_market("c2", Outcome.NO),
            # c3 deliberately missing: lookup failure must not abort the sweep
        }
    )
    worker = MarketResolutionWorker(
        repository=repository,  # type: ignore[arg-type]
        market_data=market_data,
    )

    resolved = await worker.run_once()

    assert resolved == 2
    assert repository.recorded == [("c1", "YES"), ("c2", "NO")]


async def test_run_once_skips_open_markets() -> None:
    repository = FakeRepository(["c1"])
    market = _closed_market("c1", Outcome.YES).model_copy(
        update={"closed": False, "resolved_outcome": None}
    )
    worker = MarketResolutionWorker(
        repository=repository,  # type: ignore[arg-type]
        market_data=FakeResolutionMarketData({"c1": market}),
    )

    resolved = await worker.run_once()

    assert resolved == 0
    assert repository.recorded == []


async def test_worker_validates_constructor_bounds() -> None:
    repository = FakeRepository([])
    try:
        MarketResolutionWorker(
            repository=repository,  # type: ignore[arg-type]
            poll_interval_seconds=5,
        )
    except ValueError as exc:
        assert "30" in str(exc)
    else:
        raise AssertionError("short poll interval must be rejected")
