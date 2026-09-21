from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, Field

from polybot.config import Settings
from polybot.metrics import Metrics
from polybot.models import MarketSpec, OrderBookSnapshot
from polybot.stores.base import StateStore

METRICS = Metrics()


def build_archive_worker(settings: Settings) -> MarketArchiveWorker:
    """Build the read-only archive worker for a durable Supabase deployment."""

    from polybot.market import PolymarketMarketData
    from polybot.stores.supabase_store import SupabaseStore

    if (
        not settings.uses_supabase
        or settings.supabase_service_role_key is None
    ):
        raise ValueError(
            "archive requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY"
        )
    store = SupabaseStore(
        settings.supabase_url,
        settings.supabase_service_role_key.get_secret_value(),
        account_id=settings.account_id,
        timeout_seconds=settings.supabase_timeout_seconds,
    )
    return MarketArchiveWorker(
        settings=settings,
        store=store,
        market_data=PolymarketMarketData(),
    )


class ArchiveMarketData(Protocol):
    async def list_markets(self, limit: int) -> list[MarketSpec]: ...

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBookSnapshot: ...


class ArchiveRunResult(BaseModel):
    """Summary of one read-only archive sweep.

    The archive worker is intentionally read-only with respect to trading:
    it never signs, submits, or cancels orders. A per-market failure must not
    abort the whole sweep, and a sweep failure must not affect the trading loop.
    """

    markets_seen: int = 0
    markets_saved: int = 0
    snapshots_saved: int = 0
    skipped: list[dict[str, str]] = Field(default_factory=list)

    @property
    def fully_successful(self) -> bool:
        return not self.skipped


@dataclass
class MarketArchiveWorker:
    """Persist market metadata and full two-sided L2 books for later research.

    This is the point-in-time data foundation for future backtesting: without
    archived books captured before resolution, no honest walk-forward or
    calibration study is possible. The worker runs in any trading mode and is
    safe to run alongside the trading loop because every write is an upsert or
    an append into the immutable snapshots table.
    """

    settings: Settings
    store: StateStore
    market_data: ArchiveMarketData
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("polybot.archive")
    )

    async def run_once(self) -> ArchiveRunResult:
        result = ArchiveRunResult()
        try:
            markets = await self.market_data.list_markets(
                self.settings.archive_market_limit
            )
        except Exception as exc:
            self.logger.warning("archive market discovery failed", exc_info=exc)
            result.skipped.append(
                {"scope": "discovery", "reason": f"{type(exc).__name__}"}
            )
            return result
        result.markets_seen = len(markets)
        METRICS.observe("polybot_archive_markets_seen", result.markets_seen)

        for market in markets:
            try:
                await self.store.save_market(market)
            except Exception as exc:
                self.logger.warning(
                    "archive market save failed",
                    exc_info=exc,
                    extra={"market_id": market.id},
                )
                result.skipped.append(
                    {
                        "scope": f"market:{market.id}",
                        "reason": f"save:{type(exc).__name__}",
                    }
                )
                continue
            result.markets_saved += 1
            METRICS.increment("polybot_archive_markets_saved_total")

            for token_id in (market.yes_token_id, market.no_token_id):
                try:
                    book = await self.market_data.get_order_book(market.id, token_id)
                    await self.store.save_snapshot(book)
                except Exception as exc:
                    self.logger.warning(
                        "archive order book failed",
                        exc_info=exc,
                        extra={"market_id": market.id, "token_id": token_id},
                    )
                    result.skipped.append(
                        {
                            "scope": f"market:{market.id}",
                            "reason": f"book:{token_id}:{type(exc).__name__}",
                        }
                    )
                    continue
                result.snapshots_saved += 1
                METRICS.increment("polybot_archive_snapshots_saved_total")
        return result

    async def serve(self, stop: asyncio.Event) -> None:
        """Run archive sweeps forever, never letting one sweep kill the loop."""

        interval = self.settings.archive_interval_seconds
        while not stop.is_set():
            try:
                result = await self.run_once()
                if not result.fully_successful:
                    self.logger.warning(
                        "archive sweep completed with skips",
                        extra={
                            "markets_seen": result.markets_seen,
                            "markets_saved": result.markets_saved,
                            "snapshots_saved": result.snapshots_saved,
                            "skipped": len(result.skipped),
                        },
                    )
                else:
                    self.logger.info(
                        "archive sweep completed",
                        extra={
                            "markets_seen": result.markets_seen,
                            "markets_saved": result.markets_saved,
                            "snapshots_saved": result.snapshots_saved,
                        },
                    )
            except Exception:
                self.logger.exception("archive sweep failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
