from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from polybot.config import TradingMode
from polybot.fees import estimated_fee_per_share, maximum_buy_cost
from polybot.models import BookLevel, PortfolioState, Side, TradeIntent, utc_now
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
    assert candidate.size * candidate.limit_price <= settings.max_order_usd
    assert candidate.max_commitment_usd <= settings.max_order_usd
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


def test_risk_rejects_cash_caps_liquidity_and_fee_gaps(
    settings, market, forecast, yes_book, no_book
) -> None:
    healthy = PortfolioState(bankroll_usd=Decimal("1000"), cash_usd=Decimal("1000"))
    candidate = ValueStrategy(settings).choose(market, forecast, yes_book, no_book, healthy)
    assert candidate is not None
    portfolio = PortfolioState(
        bankroll_usd=Decimal("1000"),
        cash_usd=Decimal("1"),
        gross_exposure_usd=Decimal("990"),
        event_exposure_usd={"__unclassified_event__": Decimal("990")},
        bucket_exposure_usd={"__unclassified__": Decimal("990")},
    )
    decision = RiskEngine(settings).evaluate_candidate(candidate, market, yes_book, portfolio)
    assert not decision.approved
    for code in (
        "insufficient_cash",
        "event_exposure_cap_exceeded",
        "correlated_bucket_cap_exceeded",
        "gross_exposure_cap_exceeded",
    ):
        assert code in decision.codes


def test_risk_rejects_invalid_tick_and_minimum_size(
    settings, market, forecast, yes_book, no_book
) -> None:
    portfolio = PortfolioState(bankroll_usd=Decimal("1000"), cash_usd=Decimal("1000"))
    candidate = ValueStrategy(settings).choose(market, forecast, yes_book, no_book, portfolio)
    assert candidate is not None
    off_grid = candidate.model_copy(update={"limit_price": Decimal("0.405")})
    decision = RiskEngine(settings).evaluate_candidate(off_grid, market, yes_book, portfolio)
    assert "invalid_tick_price" in decision.codes

    tiny = candidate.model_copy(update={"size": Decimal("0.0001")})
    decision = RiskEngine(settings).evaluate_candidate(tiny, market, yes_book, portfolio)
    assert "below_minimum_order_size" in decision.codes


def test_risk_rejects_sell_exceeding_position(
    settings, market, forecast, yes_book, no_book
) -> None:
    portfolio = PortfolioState(
        bankroll_usd=Decimal("1000"),
        cash_usd=Decimal("100"),
        token_positions={market.yes_token_id: Decimal("1")},
        peak_equity_usd=Decimal("1000"),
        equity_usd=Decimal("900"),
    )
    bearish = forecast.model_copy(
        update={
            "probability_yes": Decimal("0.20"),
            "probability_low": Decimal("0.10"),
            "probability_high": Decimal("0.30"),
        }
    )
    rich_bid = yes_book.model_copy(
        update={"bids": [yes_book.bids[0].model_copy(update={"price": Decimal("0.55")})]}
    )
    candidate = ValueStrategy(settings).choose(market, bearish, rich_bid, no_book, portfolio)
    assert candidate is not None
    oversized = candidate.model_copy(update={"size": Decimal("5")})
    decision = RiskEngine(settings).evaluate_candidate(oversized, market, rich_bid, portfolio)
    assert "sell_exceeds_position" in decision.codes


def test_risk_rejects_market_gates_and_empty_book(
    settings, market, forecast, yes_book, no_book
) -> None:
    portfolio = PortfolioState(bankroll_usd=Decimal("1000"), cash_usd=Decimal("1000"))
    candidate = ValueStrategy(settings).choose(market, forecast, yes_book, no_book, portfolio)
    assert candidate is not None
    closed = market.model_copy(update={"active": False, "closed": True})
    decision = RiskEngine(settings).evaluate_candidate(candidate, closed, yes_book, portfolio)
    assert "market_not_tradeable" in decision.codes

    empty = yes_book.model_copy(update={"asks": []})
    decision = RiskEngine(settings).evaluate_candidate(candidate, market, empty, portfolio)
    assert "empty_ask_book" in decision.codes

    daily_loss = portfolio.model_copy(
        update={
            "daily_risk_pnl_usd": Decimal("-30"),
            "daily_loss_basis_usd": Decimal("1000"),
            "realized_pnl_today_usd": Decimal("-30"),
        }
    )
    decision = RiskEngine(settings).evaluate_candidate(candidate, market, yes_book, daily_loss)
    assert "daily_loss_kill_switch" in decision.codes


@pytest.mark.parametrize("rate", [Decimal("0"), Decimal("0.07"), Decimal("0.25")])
def test_multilevel_buy_sizes_for_full_limit_commitment(
    settings, market, forecast, yes_book, no_book, rate,
) -> None:
    market = market.model_copy(update={"fees_enabled": rate > 0, "fee_rate": rate})
    book = yes_book.model_copy(update={"asks": [
        BookLevel(price=Decimal("0.10"), size=Decimal("40")),
        BookLevel(price=Decimal("0.50"), size=Decimal("100")),
    ]})
    portfolio = PortfolioState(bankroll_usd=Decimal("1000"), cash_usd=Decimal("1000"))
    candidate = ValueStrategy(settings).choose(market, forecast, book, no_book, portfolio)
    assert candidate is not None
    assert candidate.limit_price == Decimal("0.10")
    assert 0 < candidate.size <= 40
    assert candidate.condition_id == market.condition_id
    assert candidate.notional_usd <= candidate.max_commitment_usd <= settings.max_order_usd
    # Every possible fill price at/below this order's signed limit fits the cap.
    for price in (Decimal("0.01"), Decimal("0.05"), candidate.limit_price):
        fee = estimated_fee_per_share(
            price, enabled=market.fees_enabled, rate=rate, exponent=market.fee_exponent,
        )
        assert candidate.size * (price + fee) <= settings.max_order_usd


def test_risk_does_not_trust_expected_cost_or_claimed_commitment(
    settings, market, forecast, yes_book, no_book,
) -> None:
    portfolio = PortfolioState(bankroll_usd=Decimal("1000"), cash_usd=Decimal("1000"))
    candidate = ValueStrategy(settings).choose(market, forecast, yes_book, no_book, portfolio)
    assert candidate is not None
    forged = candidate.model_copy(update={
        "size": Decimal("42"), "limit_price": Decimal("0.50"),
        "notional_usd": Decimal("5"), "max_commitment_usd": Decimal("5"),
    })
    settings = settings.model_copy(update={
        "max_event_exposure_pct": Decimal("0.01"),
        "max_bucket_exposure_pct": Decimal("0.01"),
        "max_gross_exposure_pct": Decimal("0.01"),
    })
    portfolio.cash_usd = Decimal("10")
    decision = RiskEngine(settings).evaluate_candidate(forged, market, yes_book, portfolio)
    assert not decision.approved
    assert set(decision.codes) >= {
        "order_cap_exceeded", "insufficient_cash", "trade_risk_cap_exceeded",
        "event_exposure_cap_exceeded", "correlated_bucket_cap_exceeded",
        "gross_exposure_cap_exceeded",
    }
    assert Decimal(decision.details["max_commitment_usd"]) == Decimal("21")


def test_fee_bound_covers_better_prices_above_midpoint() -> None:
    commitment = maximum_buy_cost(
        Decimal("10"), Decimal("0.9"),
        enabled=True, rate=Decimal("0.07"), exponent=Decimal("1"),
    )
    assert commitment >= Decimal("9") + Decimal("10") * Decimal("0.07") * Decimal("0.25")
