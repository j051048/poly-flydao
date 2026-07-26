from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from polybot.config import TradingMode
from polybot.models import PortfolioState, Side, TradeIntent, utc_now
from polybot.risk import RiskEngine
from polybot.strategy import ValueStrategy


def test_strategy_uses_conservative_interval_and_real_depth(
    settings, market, forecast, yes_book, no_book
) -> None:
    portfolio = PortfolioState(bankroll_usd=Decimal("1000"), cash_usd=Decimal("1000"))
    candidate = ValueStrategy(settings).choose(market, forecast, yes_book, no_book, portfolio)
    assert candidate is not None
    assert candidate.outcome.value == "YES"
    assert candidate.conservative_probability == forecast.probability_low
    assert candidate.limit_price == Decimal("0.41")
    assert candidate.expected_price > Decimal("0.40")
    assert candidate.notional_usd <= settings.max_order_usd * Decimal("1.02")
    assert candidate.edge_after_costs > settings.min_edge


def test_risk_rejects_stale_data_and_drawdown(
    settings, market, forecast, yes_book, no_book
) -> None:
    portfolio = PortfolioState(
        bankroll_usd=Decimal("1000"),
        cash_usd=Decimal("1000"),
        peak_equity_usd=Decimal("1000"),
        equity_usd=Decimal("900"),
    )
    candidate = ValueStrategy(settings).choose(market, forecast, yes_book, no_book, portfolio)
    assert candidate is not None
    stale = yes_book.model_copy(
        update={"captured_at": utc_now() - timedelta(seconds=settings.max_book_age_seconds + 1)}
    )
    decision = RiskEngine(settings).evaluate_candidate(candidate, market, stale, portfolio)
    assert not decision.approved
    assert "stale_order_book" in decision.codes
    assert "drawdown_kill_switch" in decision.codes


def test_strategy_refuses_low_confidence(settings, market, forecast, yes_book, no_book) -> None:
    uncertain = forecast.model_copy(update={"confidence": Decimal("0.2")})
    portfolio = PortfolioState(bankroll_usd=Decimal("1000"), cash_usd=Decimal("1000"))
    assert ValueStrategy(settings).choose(market, uncertain, yes_book, no_book, portfolio) is None


def test_existing_position_can_exit_even_during_drawdown(
    settings, market, forecast, yes_book, no_book
) -> None:
    bearish = forecast.model_copy(
        update={
            "probability_yes": Decimal("0.35"),
            "probability_low": Decimal("0.25"),
            "probability_high": Decimal("0.45"),
        }
    )
    rich_bid = yes_book.model_copy(
        update={
            "bids": [
                yes_book.bids[0].model_copy(
                    update={"price": Decimal("0.60"), "size": Decimal("20")}
                )
            ],
            "asks": [
                yes_book.asks[0].model_copy(
                    update={"price": Decimal("0.62"), "size": Decimal("20")}
                )
            ],
        }
    )
    portfolio = PortfolioState(
        bankroll_usd=Decimal("1000"),
        cash_usd=Decimal("100"),
        gross_exposure_usd=Decimal("10"),
        token_positions={"yes-1": Decimal("5")},
        peak_equity_usd=Decimal("1000"),
        equity_usd=Decimal("800"),
    )
    candidate = ValueStrategy(settings).choose(market, bearish, rich_bid, no_book, portfolio)
    assert candidate is not None
    assert candidate.side.value == "SELL"
    decision = RiskEngine(settings).evaluate_candidate(candidate, market, rich_bid, portfolio)
    assert decision.approved


def test_hard_risk_exit_does_not_require_a_new_forecast(
    settings, market, yes_book, no_book
) -> None:
    portfolio = PortfolioState(
        bankroll_usd=Decimal("1000"),
        cash_usd=Decimal("800"),
        token_positions={market.yes_token_id: Decimal("10")},
        peak_equity_usd=Decimal("1000"),
        equity_usd=Decimal("900"),
    )
    candidate = ValueStrategy(settings).choose_risk_exit(market, yes_book, no_book, portfolio)
    assert candidate is not None
    assert candidate.side is Side.SELL
    assert candidate.strategy == "risk_exit_v1"
    assert candidate.forecast_id is None
    assert candidate.decision_key is not None
    assert candidate.decision_key.startswith(f"risk:{market.yes_token_id}:10:")
    first = TradeIntent.from_candidate(
        candidate,
        account_id="account",
        run_id="cycle-one",
        mode=TradingMode.PAPER,
    )
    same_decision = TradeIntent.from_candidate(
        candidate,
        account_id="account",
        run_id="cycle-two",
        mode=TradingMode.PAPER,
    )
    next_decision = TradeIntent.from_candidate(
        candidate.model_copy(update={"decision_key": f"{candidate.decision_key}:next"}),
        account_id="account",
        run_id="cycle-three",
        mode=TradingMode.PAPER,
    )
    assert first.intent_hash == same_decision.intent_hash
    assert first.intent_hash != next_decision.intent_hash
