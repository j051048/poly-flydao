from __future__ import annotations

from polybot.models import MarketSpec, Outcome
from polybot.resolution_worker import MarketResolutionWorker


class Repository:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str]] = []

    async def unresolved_market_conditions(self, *, limit: int = 100) -> list[str]:
        assert limit == 10
        return ["resolved", "pending"]

    async def record_market_resolution(self, *, condition_id: str, outcome: str) -> int:
        self.saved.append((condition_id, outcome))
        return 1


class MarketData:
    async def get_market_by_condition(self, condition_id: str) -> MarketSpec:
        return MarketSpec(
            id=condition_id,
            condition_id=condition_id,
            question="Resolved?",
            yes_token_id=f"{condition_id}-yes",
            no_token_id=f"{condition_id}-no",
            closed=condition_id == "resolved",
            active=condition_id != "resolved",
            accepting_orders=condition_id != "resolved",
            resolved_outcome=(Outcome.YES if condition_id == "resolved" else None),
        )


async def test_resolution_worker_only_records_explicit_closed_outcomes() -> None:
    repository = Repository()
    worker = MarketResolutionWorker(
        repository=repository,  # type: ignore[arg-type]
        market_data=MarketData(),
        batch_size=10,
        poll_interval_seconds=30,
    )

    assert await worker.run_once() == 1
    assert repository.saved == [("resolved", "YES")]
