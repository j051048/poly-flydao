from __future__ import annotations

from typing import Protocol

from polybot.models import ExecutionResult, OrderBookSnapshot, PortfolioState, TradeIntent


class Broker(Protocol):
    async def portfolio_state(self) -> PortfolioState: ...

    async def submit(self, intent: TradeIntent, book: OrderBookSnapshot) -> ExecutionResult: ...

    async def cancel_all(self, reason: str) -> bool: ...

    async def redeem_resolved(self) -> int: ...
