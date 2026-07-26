from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from polybot.config import Settings
from polybot.models import (
    BookLevel,
    Forecast,
    MarketSpec,
    OrderBookSnapshot,
    utc_now,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        mode="paper",
        bankroll_usd=Decimal("1000"),
        min_liquidity_usd=Decimal("100"),
        min_edge=Decimal("0.04"),
        max_order_usd=Decimal("5"),
        max_trade_risk_pct=Decimal("0.01"),
        min_forecast_confidence=Decimal("0.55"),
    )


@pytest.fixture
def market() -> MarketSpec:
    return MarketSpec(
        id="m1",
        condition_id="c1",
        event_id="e1",
        question="Will the synthetic test event occur?",
        category="test",
        yes_token_id="yes-1",
        no_token_id="no-1",
        liquidity_usd=Decimal("50000"),
        minimum_order_size=Decimal("1"),
        tick_size=Decimal("0.01"),
        end_at=utc_now() + timedelta(days=30),
    )


@pytest.fixture
def yes_book() -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id="yes-1",
        market_id="m1",
        bids=[BookLevel(price=Decimal("0.39"), size=Decimal("100"))],
        asks=[
            BookLevel(price=Decimal("0.40"), size=Decimal("8")),
            BookLevel(price=Decimal("0.41"), size=Decimal("100")),
        ],
        minimum_order_size=Decimal("1"),
    )


@pytest.fixture
def no_book() -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id="no-1",
        market_id="m1",
        bids=[BookLevel(price=Decimal("0.58"), size=Decimal("100"))],
        asks=[BookLevel(price=Decimal("0.60"), size=Decimal("100"))],
        minimum_order_size=Decimal("1"),
    )


@pytest.fixture
def forecast() -> Forecast:
    return Forecast(
        id="11111111-1111-1111-1111-111111111111",
        market_id="m1",
        probability_yes=Decimal("0.70"),
        probability_low=Decimal("0.65"),
        probability_high=Decimal("0.75"),
        confidence=Decimal("0.80"),
        source_ids=[],
        model="test-model",
    )
