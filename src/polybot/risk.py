from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from polybot.config import Settings
from polybot.models import (
    UNCLASSIFIED_EVENT_KEY,
    MarketSpec,
    OrderBookSnapshot,
    PortfolioState,
    RiskDecision,
    Side,
    TradeCandidate,
    utc_now,
)


class RiskEngine:
    """Deterministic hard gates. No LLM call can override these checks."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def evaluate_candidate(
        self,
        candidate: TradeCandidate,
        market: MarketSpec,
        book: OrderBookSnapshot,
        portfolio: PortfolioState,
    ) -> RiskDecision:
        codes: list[str] = []
        details: dict[str, str] = {}
        now = utc_now()

        if not market.active or market.closed or not market.accepting_orders:
            codes.append("market_not_tradeable")
        if market.fees_enabled and market.fee_rate <= 0:
            codes.append("invalid_fee_schedule")
        risk_exit = candidate.side is Side.SELL and candidate.strategy == "risk_exit_v1"
        if not risk_exit and market.liquidity_usd < self.settings.min_liquidity_usd:
            codes.append("insufficient_market_liquidity")
        if now - book.captured_at > timedelta(seconds=self.settings.max_book_age_seconds):
            codes.append("stale_order_book")
        if (
            candidate.side is Side.BUY
            and market.end_at
            and market.end_at - now < timedelta(hours=self.settings.min_hours_to_resolution)
        ):
            codes.append("too_close_to_resolution")
        if not risk_exit and candidate.edge_after_costs < self.settings.min_edge:
            codes.append("edge_below_threshold")
        if candidate.notional_usd > self.settings.max_order_usd:
            codes.append("order_cap_exceeded")
        if candidate.limit_price % book.tick_size != 0:
            codes.append("invalid_tick_price")
        if candidate.size < max(market.minimum_order_size, book.minimum_order_size):
            codes.append("below_minimum_order_size")
        bankroll = portfolio.bankroll_usd
        if candidate.side is Side.BUY:
            if candidate.notional_usd > portfolio.cash_usd:
                codes.append("insufficient_cash")
            if candidate.notional_usd > bankroll * self.settings.max_trade_risk_pct:
                codes.append("trade_risk_cap_exceeded")

            event_key = candidate.event_id or candidate.market_id
            event_after = portfolio.event_exposure_usd.get(event_key, Decimal("0"))
            event_after += portfolio.event_exposure_usd.get(
                UNCLASSIFIED_EVENT_KEY, Decimal("0")
            )
            event_after += candidate.notional_usd
            if event_after > bankroll * self.settings.max_event_exposure_pct:
                codes.append("event_exposure_cap_exceeded")

            bucket_after = portfolio.bucket_exposure_usd.get(candidate.bucket, Decimal("0"))
            bucket_after += portfolio.bucket_exposure_usd.get("__unclassified__", Decimal("0"))
            bucket_after += candidate.notional_usd
            if bucket_after > bankroll * self.settings.max_bucket_exposure_pct:
                codes.append("correlated_bucket_cap_exceeded")
            if (
                portfolio.gross_exposure_usd + candidate.notional_usd
                > bankroll * self.settings.max_gross_exposure_pct
            ):
                codes.append("gross_exposure_cap_exceeded")
            if portfolio.daily_risk_pnl_usd <= -(
                portfolio.daily_loss_basis_usd * self.settings.daily_loss_limit_pct
            ):
                codes.append("daily_loss_kill_switch")
            if portfolio.drawdown_pct >= self.settings.max_drawdown_pct:
                codes.append("drawdown_kill_switch")
            if book.best_ask is None:
                codes.append("empty_ask_book")
            elif candidate.limit_price < book.best_ask:
                codes.append("limit_not_marketable")
        else:
            if candidate.size > portfolio.token_positions.get(candidate.token_id, Decimal("0")):
                codes.append("sell_exceeds_position")
            if book.best_bid is None:
                codes.append("empty_bid_book")
            elif candidate.limit_price > book.best_bid:
                codes.append("sell_limit_not_marketable")

        details["edge_after_costs"] = str(candidate.edge_after_costs)
        details["notional_usd"] = str(candidate.notional_usd)
        details["drawdown_pct"] = str(portfolio.drawdown_pct)
        details["realized_pnl_today_usd"] = str(portfolio.realized_pnl_today_usd)
        details["daily_equity_pnl_usd"] = str(portfolio.daily_equity_pnl_usd)
        details["daily_risk_pnl_usd"] = str(portfolio.daily_risk_pnl_usd)
        return RiskDecision(approved=not codes, codes=codes, details=details)
