from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

import polybot.stores.memory as memory_module
from polybot.brokers.polymarket import PolymarketBroker
from polybot.config import (
    BETA_SDK_ACK_TEXT,
    DEDICATED_WALLET_ACK_TEXT,
    LIVE_ACK_TEXT,
    Settings,
)
from polybot.models import Outcome, PortfolioState, Side, TradeCandidate, utc_now
from polybot.risk import RiskEngine
from polybot.stores.memory import MemoryStore


async def test_memory_equity_state_keeps_utc_day_start_and_rolls_from_latest(
    monkeypatch,
) -> None:
    clock = {"now": datetime(2026, 7, 25, 23, 50, tzinfo=UTC)}
    monkeypatch.setattr(memory_module, "utc_now", lambda: clock["now"])
    store = MemoryStore()

    first = await store.record_equity_state("account", Decimal("100"))
    same_day = await store.record_equity_state("account", Decimal("94"))
    clock["now"] = datetime(2026, 7, 26, 0, 1, tzinfo=UTC)
    next_day = await store.record_equity_state("account", Decimal("92"))

    assert first.day_start_equity_usd == Decimal("100")
    assert same_day.day_start_equity_usd == Decimal("100")
    assert same_day.peak_equity_usd == Decimal("100")
    assert next_day.day_start_equity_usd == Decimal("94")
    assert next_day.latest_equity_usd == Decimal("92")


def test_resolution_loss_uses_equity_delta_for_daily_kill(
    settings, market, yes_book
) -> None:
    portfolio = PortfolioState(
        bankroll_usd=Decimal("100"),
        cash_usd=Decimal("96"),
        equity_usd=Decimal("96"),
        day_start_equity_usd=Decimal("100"),
        realized_pnl_today_usd=Decimal("0"),
    )
    candidate = TradeCandidate(
        market_id=market.id,
        event_id=market.event_id,
        bucket="test",
        token_id=market.yes_token_id,
        outcome=Outcome.YES,
        side=Side.BUY,
        conservative_probability=Decimal("0.7"),
        expected_price=Decimal("0.4"),
        limit_price=Decimal("0.4"),
        size=Decimal("2"),
        notional_usd=Decimal("0.8"),
        fee_estimate_usd=Decimal("0"),
        edge_after_costs=Decimal("0.1"),
        forecast_id=None,
        book_captured_at=utc_now(),
    )
    bounded = settings.model_copy(
        update={"bankroll_usd": Decimal("100"), "daily_loss_limit_pct": Decimal("0.02")}
    )

    decision = RiskEngine(bounded).evaluate_candidate(
        candidate, market, yes_book, portfolio
    )

    assert portfolio.daily_equity_pnl_usd == Decimal("-4")
    assert portfolio.daily_risk_pnl_usd == Decimal("-4")
    assert "daily_loss_kill_switch" in decision.codes


class _Items:
    def __init__(self, items):
        self.items = items

    def iter_items(self):
        yield from self.items


class _MissingMarkClient:
    def list_positions(self, **kwargs):
        return _Items(
            [
                type(
                    "Position",
                    (),
                    {
                        "token_id": "yes",
                        "condition_id": "condition",
                        "event_id": None,
                        "size": Decimal("1"),
                        "current_value": None,
                    },
                )()
            ]
        )

    def list_open_orders(self):
        return _Items([])

    def get_balance_allowance(self, **kwargs):
        return type("Balance", (), {"balance": 10_000_000})()


async def test_live_portfolio_never_values_missing_mark_at_cost() -> None:
    settings = Settings(
        _env_file=None,
        mode="canary",
        component="worker",
        live_ack=LIVE_ACK_TEXT,
        beta_sdk_ack=BETA_SDK_ACK_TEXT,
        dedicated_wallet_ack=DEDICATED_WALLET_ACK_TEXT,
        ai_provider="openai",
        openai_api_key="test",
        polymarket_private_key="0xdeadbeef",
        signed_payload_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="test",
    )
    broker = PolymarketBroker(settings, MemoryStore(), client=_MissingMarkClient())

    with pytest.raises(RuntimeError, match="current value is missing"):
        await broker.portfolio_state()
