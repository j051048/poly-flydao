from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from polybot.jobs import WorkerJobRepository
from polybot.market import PolymarketMarketData
from polybot.models import MarketSpec


class ResolutionMarketData(Protocol):
    async def get_market_by_condition(self, condition_id: str) -> MarketSpec: ...


class MarketResolutionWorker:
    """Backfill authoritative outcomes for calibration; it never submits orders."""

    def __init__(
        self,
        *,
        repository: WorkerJobRepository,
        market_data: ResolutionMarketData | None = None,
        poll_interval_seconds: float = 300,
        batch_size: int = 100,
        logger: logging.Logger | None = None,
    ):
        if poll_interval_seconds < 30:
            raise ValueError("resolution poll interval must be at least 30 seconds")
        if not 1 <= batch_size <= 500:
            raise ValueError("resolution batch size must be between 1 and 500")
        self.repository = repository
        self.market_data = market_data or PolymarketMarketData()
        self.poll_interval_seconds = poll_interval_seconds
        self.batch_size = batch_size
        self.logger = logger or logging.getLogger("polybot.resolution_worker")

    async def serve(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                self.logger.exception("market resolution sweep failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_interval_seconds)
            except TimeoutError:
                pass

    async def run_once(self) -> int:
        conditions = await self.repository.unresolved_market_conditions(limit=self.batch_size)
        resolved = 0
        for condition_id in conditions:
            try:
                market = await self.market_data.get_market_by_condition(condition_id)
            except Exception as exc:
                self.logger.info(
                    "resolution lookup deferred condition=%s error=%s",
                    condition_id,
                    type(exc).__name__,
                )
                continue
            if not market.closed or market.resolved_outcome is None:
                continue
            await self.repository.record_market_resolution(
                condition_id=condition_id,
                outcome=market.resolved_outcome.value,
            )
            resolved += 1
        return resolved
