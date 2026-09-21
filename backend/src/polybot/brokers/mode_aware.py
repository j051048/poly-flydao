from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from typing import Any

from polybot.config import Settings, TradingMode
from polybot.models import ExecutionResult, OrderBookSnapshot, PortfolioState, TradeIntent
from polybot.runtime_mode import LIVE_MODES


class ModeAwareBroker:
    """Route execution to the backend that matches the live trading mode.

    A single-process personal deployment has to honour a mode the operator picks
    in the dashboard without a redeploy and without a restart. This wrapper owns
    one backend per mode and reads ``settings.mode`` on every call, so a mode
    change applied at a cycle boundary is atomic from the engine's point of view.

    The real-money backend is built lazily and only when the deployment enables
    real funds *and* a signer key exists. Until then the wrapper is a harmless
    pass-through to the simulator: no wallet client, no approvals, no orders.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        paper: Any,
        shadow: Any | None = None,
        live: Any | None = None,
        live_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings
        self._paper = paper
        self._shadow = shadow
        self._live_factory = live_factory
        self._live: Any | None = live
        self._execution_guard: Callable[[], Awaitable[int | None]] | None = None

    # -- backend selection -------------------------------------------------

    @property
    def paper(self) -> Any:
        return self._paper

    def replace_paper(self, broker: Any) -> None:
        """Swap in a freshly restored durable paper account."""

        self._paper = broker

    @property
    def live(self) -> Any | None:
        if self._live is None and self._live_factory is not None:
            self._live = self._live_factory()
        return self._live

    @property
    def active(self) -> Any:
        mode = self.settings.mode
        if mode is TradingMode.SHADOW:
            return self._shadow if self._shadow is not None else self._paper
        if mode in LIVE_MODES:
            broker = self.live
            if broker is None:
                raise RuntimeError("real-money mode requires a configured live broker")
            return broker
        return self._paper

    # -- broker surface ----------------------------------------------------

    async def portfolio_state(self) -> PortfolioState:
        return await self.active.portfolio_state()

    async def submit(self, intent: TradeIntent, book: OrderBookSnapshot) -> ExecutionResult:
        return await self.active.submit(intent, book)

    async def submit_batch(
        self,
        submissions: Sequence[tuple[TradeIntent, OrderBookSnapshot]],
    ) -> list[ExecutionResult]:
        return await self.active.submit_batch(submissions)

    async def cancel_order(self, order_id: str, reason: str) -> bool:
        return await self.active.cancel_order(order_id, reason)

    async def cancel_orders(self, order_ids: Sequence[str], reason: str) -> bool:
        return await self.active.cancel_orders(order_ids, reason)

    async def cancel_all(self, reason: str) -> bool:
        """Always cancel on the real venue: exposure exists there, not in the sim."""

        broker = self.live
        if broker is not None:
            return await broker.cancel_all(reason)
        return await self.active.cancel_all(reason)

    async def redeem_resolved(self) -> int:
        broker = self.live
        if broker is not None:
            return await broker.redeem_resolved()
        return await self.active.redeem_resolved()

    # -- live-only surface (never satisfied by the simulator) --------------

    def set_execution_guard(
        self,
        guard: Callable[[], Awaitable[int | None]],
    ) -> None:
        self._execution_guard = guard
        broker = self.live
        if broker is not None:
            broker.set_execution_guard(guard)

    async def ensure_trading_approvals(self) -> tuple[Decimal, bool]:
        broker = self.live
        if broker is None:
            raise RuntimeError("approval setup requires a configured live broker")
        if self._execution_guard is not None:
            broker.set_execution_guard(self._execution_guard)
        return await broker.ensure_trading_approvals()

    async def close(self) -> None:
        for broker in (self._live,):
            if broker is None:
                continue
            close = getattr(broker, "close", None)
            if close is None:
                continue
            result = close()
            if hasattr(result, "__await__"):
                await result
