from __future__ import annotations

from decimal import Decimal

from polybot.brokers.paper import PaperBroker
from polybot.config import TradingMode
from polybot.models import BookLevel, ExecutionStatus, Outcome, Side, TradeIntent


async def test_paper_broker_partial_fill_and_duplicate(yes_book) -> None:
    broker = PaperBroker(Decimal("100"))
    intent = TradeIntent(
        intent_hash="a" * 64,
        account_id="00000000-0000-0000-0000-000000000001",
        run_id="r1",
        mode=TradingMode.PAPER,
        market_id="m1",
        event_id="e1",
        bucket="test",
        token_id="yes-1",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=Decimal("0.40"),
        size=Decimal("10"),
        notional_usd=Decimal("4"),
        edge_after_costs=Decimal("0.1"),
        forecast_id="11111111-1111-1111-1111-111111111111",
        strategy="test",
    )
    first = await broker.submit(intent, yes_book)
    assert first.status is ExecutionStatus.PAPER_FILLED
    assert first.filled_size == Decimal("8")
    second = await broker.submit(intent, yes_book)
    assert second.status is ExecutionStatus.DUPLICATE


async def test_paper_sell_releases_basis_and_records_realized_pnl(yes_book) -> None:
    broker = PaperBroker(Decimal("1000"))
    buy = TradeIntent(
        intent_hash="1" * 64,
        account_id="account",
        run_id="buy-run",
        mode=TradingMode.PAPER,
        market_id="m1",
        event_id="e1",
        bucket="test",
        token_id="yes-1",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=Decimal("0.41"),
        size=Decimal("2"),
        notional_usd=Decimal("0.82"),
        edge_after_costs=Decimal("0.1"),
        forecast_id=None,
        strategy="test",
    )
    await broker.submit(buy, yes_book)
    sell_book = yes_book.model_copy(
        update={"bids": [BookLevel(price=Decimal("0.50"), size=Decimal("10"))]}
    )
    sell = buy.model_copy(
        update={
            "intent_hash": "2" * 64,
            "run_id": "sell-run",
            "side": Side.SELL,
            "price": Decimal("0.50"),
            "notional_usd": Decimal("1.00"),
        }
    )
    result = await broker.submit(sell, sell_book)
    state = await broker.portfolio_state()
    assert result.status is ExecutionStatus.PAPER_FILLED
    assert state.cash_usd == Decimal("1000.18")
    assert state.token_positions["yes-1"] == 0
    assert state.realized_pnl_today_usd == Decimal("0.18")


async def test_paper_state_round_trip_preserves_cash_positions_and_idempotency(
    yes_book,
) -> None:
    broker = PaperBroker(Decimal("100"))
    intent = TradeIntent(
        intent_hash="f" * 64,
        account_id="account",
        run_id="run",
        mode=TradingMode.PAPER,
        market_id="m1",
        event_id="e1",
        bucket="crypto",
        token_id="yes-1",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=Decimal("0.41"),
        size=Decimal("2"),
        notional_usd=Decimal("0.82"),
        edge_after_costs=Decimal("0.1"),
        forecast_id=None,
        strategy="test",
    )
    await broker.submit(intent, yes_book)

    restored = PaperBroker.from_state(Decimal("100"), broker.export_state())
    portfolio = await restored.portfolio_state()
    duplicate = await restored.submit(intent, yes_book)

    assert portfolio.cash_usd == Decimal("99.18")
    assert portfolio.token_positions["yes-1"] == Decimal("2")
    assert duplicate.status is ExecutionStatus.DUPLICATE
