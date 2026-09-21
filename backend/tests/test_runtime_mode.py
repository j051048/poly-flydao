from __future__ import annotations

from typing import Any

import pytest

from polybot.brokers.mode_aware import ModeAwareBroker
from polybot.config import Settings, TradingMode
from polybot.runtime_mode import LIVE_DISABLED_NOTE, resolve_effective_mode


class _RecordingBroker:
    """Minimal broker double that records which backend handled a call."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []
        self.guard: Any = None

    async def portfolio_state(self) -> str:
        self.calls.append("portfolio_state")
        return self.name

    async def submit(self, intent: Any, book: Any) -> str:
        del intent, book
        self.calls.append("submit")
        return f"{self.name}:submitted"

    async def submit_batch(self, submissions: Any) -> list[str]:
        del submissions
        self.calls.append("submit_batch")
        return [f"{self.name}:batch"]

    async def cancel_order(self, order_id: str, reason: str) -> bool:
        del order_id, reason
        self.calls.append("cancel_order")
        return True

    async def cancel_orders(self, order_ids: Any, reason: str) -> bool:
        del order_ids, reason
        self.calls.append("cancel_orders")
        return True

    async def cancel_all(self, reason: str) -> bool:
        del reason
        self.calls.append("cancel_all")
        return True

    async def redeem_resolved(self) -> int:
        self.calls.append("redeem_resolved")
        return 1

    def set_execution_guard(self, guard: Any) -> None:
        self.guard = guard

    async def ensure_trading_approvals(self) -> tuple[Any, bool]:
        self.calls.append("ensure_trading_approvals")
        return 0, True


def _settings(mode: TradingMode) -> Settings:
    return Settings(_env_file=None, mode=mode)


def _broker(mode: TradingMode = TradingMode.PAPER) -> tuple[ModeAwareBroker, dict[str, Any]]:
    settings = _settings(mode)
    paper = _RecordingBroker("paper")
    shadow = _RecordingBroker("shadow")
    live = _RecordingBroker("live")
    broker = ModeAwareBroker(settings, paper=paper, shadow=shadow, live=live)
    return broker, {"settings": settings, "paper": paper, "shadow": shadow, "live": live}


def test_resolve_effective_mode_keeps_non_live_requests() -> None:
    for mode in (TradingMode.PAPER, TradingMode.SHADOW):
        decision = resolve_effective_mode(mode, live_enabled=False)
        assert decision.effective is mode
        assert decision.note is None
        assert decision.downgraded is False


def test_resolve_effective_mode_clamps_live_requests_without_capability() -> None:
    decision = resolve_effective_mode(
        TradingMode.CANARY,
        live_enabled=False,
    )
    assert decision.desired is TradingMode.CANARY
    assert decision.effective is TradingMode.PAPER
    assert decision.note == LIVE_DISABLED_NOTE
    assert decision.downgraded is True


def test_resolve_effective_mode_clamps_live_requests_without_a_signer() -> None:
    decision = resolve_effective_mode(
        TradingMode.LIVE,
        live_enabled=True,
        signer_configured=False,
    )
    assert decision.effective is TradingMode.PAPER
    assert decision.note is not None
    assert "签名私钥" in decision.note


def test_resolve_effective_mode_allows_live_when_fully_configured() -> None:
    decision = resolve_effective_mode(
        TradingMode.CANARY,
        live_enabled=True,
        signer_configured=True,
    )
    assert decision.effective is TradingMode.CANARY
    assert decision.downgraded is False


async def test_mode_aware_broker_dispatches_by_the_current_mode() -> None:
    broker, parts = _broker(TradingMode.PAPER)
    settings: Settings = parts["settings"]

    assert await broker.portfolio_state() == "paper"
    settings.mode = TradingMode.SHADOW
    assert await broker.portfolio_state() == "shadow"
    settings.mode = TradingMode.CANARY
    assert await broker.portfolio_state() == "live"
    assert await broker.submit(None, None) == "live:submitted"


async def test_mode_aware_broker_always_cancels_on_the_real_venue() -> None:
    broker, parts = _broker(TradingMode.PAPER)
    assert await broker.cancel_all("fence lost") is True
    assert parts["live"].calls == ["cancel_all"]
    assert parts["paper"].calls == []


async def test_mode_aware_broker_never_redeems_from_the_simulator() -> None:
    broker, parts = _broker(TradingMode.PAPER)
    assert await broker.redeem_resolved() == 1
    assert parts["live"].calls == ["redeem_resolved"]
    assert parts["paper"].calls == []


async def test_mode_aware_broker_forwards_the_lease_guard_to_the_live_backend() -> None:
    broker, parts = _broker(TradingMode.PAPER)

    async def guard() -> int | None:
        return 7

    broker.set_execution_guard(guard)
    assert parts["live"].guard is guard
    await broker.ensure_trading_approvals()
    assert parts["live"].calls == ["ensure_trading_approvals"]


async def test_mode_aware_broker_without_a_live_backend_fails_closed() -> None:
    settings = _settings(TradingMode.PAPER)
    broker = ModeAwareBroker(settings, paper=_RecordingBroker("paper"))
    settings.mode = TradingMode.CANARY
    with pytest.raises(RuntimeError, match="live broker"):
        await broker.portfolio_state()
    with pytest.raises(RuntimeError, match="live broker"):
        await broker.ensure_trading_approvals()


async def test_mode_aware_broker_builds_the_live_backend_lazily_once() -> None:
    settings = _settings(TradingMode.PAPER)
    built: list[_RecordingBroker] = []

    def factory() -> _RecordingBroker:
        live = _RecordingBroker("live")
        built.append(live)
        return live

    broker = ModeAwareBroker(settings, paper=_RecordingBroker("paper"), live_factory=factory)
    assert built == []
    settings.mode = TradingMode.CANARY
    assert await broker.portfolio_state() == "live"
    assert await broker.portfolio_state() == "live"
    assert len(built) == 1
