from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from polybot.models import ExecutionResult, OrderBookSnapshot, PortfolioState, TradeIntent


class Broker(Protocol):
    async def portfolio_state(self) -> PortfolioState: ...

    async def submit(self, intent: TradeIntent, book: OrderBookSnapshot) -> ExecutionResult: ...

    async def submit_batch(
        self,
        submissions: Sequence[tuple[TradeIntent, OrderBookSnapshot]],
    ) -> list[ExecutionResult]: ...

    async def cancel_order(self, order_id: str, reason: str) -> bool: ...

    async def cancel_orders(self, order_ids: Sequence[str], reason: str) -> bool: ...

    async def cancel_all(self, reason: str) -> bool: ...

    async def redeem_resolved(self) -> int: ...
