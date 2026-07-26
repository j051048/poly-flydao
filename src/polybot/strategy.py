from __future__ import annotations

from decimal import Decimal

from polybot.config import Settings
from polybot.fees import estimated_fee_per_share, quantize_down
from polybot.models import (
    UNCLASSIFIED_EVENT_KEY,
    Forecast,
    MarketSpec,
    OrderBookSnapshot,
    Outcome,
    PortfolioState,
    Side,
    TradeCandidate,
    utc_now,
)


class ValueStrategy:
    """Enter below a conservative lower bound and exit above a conservative upper bound."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def choose(
        self,
        market: MarketSpec,
        forecast: Forecast,
        yes_book: OrderBookSnapshot,
        no_book: OrderBookSnapshot,
        portfolio: PortfolioState,
    ) -> TradeCandidate | None:
        if forecast.confidence < self.settings.min_forecast_confidence:
            return None

        exit_candidates = [
            self._sell_candidate(
                market=market,
                forecast=forecast,
                book=yes_book,
                outcome=Outcome.YES,
                conservative_upper=forecast.probability_high,
                portfolio=portfolio,
            ),
            self._sell_candidate(
                market=market,
                forecast=forecast,
                book=no_book,
                outcome=Outcome.NO,
                conservative_upper=Decimal("1") - forecast.probability_low,
                portfolio=portfolio,
            ),
        ]
        viable_exits = [candidate for candidate in exit_candidates if candidate is not None]
        if viable_exits:
            return max(viable_exits, key=lambda candidate: candidate.edge_after_costs)

        entry_candidates = [
            self._buy_candidate(
                market=market,
                forecast=forecast,
                book=yes_book,
                outcome=Outcome.YES,
                conservative_probability=forecast.probability_low,
                portfolio=portfolio,
            ),
            self._buy_candidate(
                market=market,
                forecast=forecast,
                book=no_book,
                outcome=Outcome.NO,
                conservative_probability=Decimal("1") - forecast.probability_high,
                portfolio=portfolio,
            ),
        ]
        viable = [candidate for candidate in entry_candidates if candidate is not None]
        if not viable:
            return None
        return max(viable, key=lambda candidate: candidate.edge_after_costs)

    def choose_risk_exit(
        self,
        market: MarketSpec,
        yes_book: OrderBookSnapshot,
        no_book: OrderBookSnapshot,
        portfolio: PortfolioState,
    ) -> TradeCandidate | None:
        """Liquidate held shares on a hard loss/drawdown breach without waiting for AI."""

        if not self.hard_risk_breached(portfolio):
            return None
        candidates = [
            self._risk_exit_candidate(market, yes_book, Outcome.YES, portfolio),
            self._risk_exit_candidate(market, no_book, Outcome.NO, portfolio),
        ]
        viable = [candidate for candidate in candidates if candidate is not None]
        return max(viable, key=lambda candidate: candidate.notional_usd, default=None)

    def hard_risk_breached(self, portfolio: PortfolioState) -> bool:
        return bool(
            portfolio.daily_risk_pnl_usd
            <= -(portfolio.daily_loss_basis_usd * self.settings.daily_loss_limit_pct)
            or portfolio.drawdown_pct >= self.settings.max_drawdown_pct
        )

    def _risk_exit_candidate(
        self,
        market: MarketSpec,
        book: OrderBookSnapshot,
        outcome: Outcome,
        portfolio: PortfolioState,
    ) -> TradeCandidate | None:
        position_size = portfolio.token_positions.get(book.token_id, Decimal("0"))
        best_bid = book.best_bid
        if position_size <= 0 or best_bid is None:
            return None
        share_cap = quantize_down(
            min(position_size, self.settings.max_order_usd / best_bid), Decimal("0.01")
        )
        minimum_size = max(market.minimum_order_size, book.minimum_order_size)
        if share_cap < minimum_size:
            return None
        remaining = share_cap
        filled = Decimal("0")
        gross = Decimal("0")
        fee = Decimal("0")
        limit_price: Decimal | None = None
        for level in sorted(book.bids, key=lambda item: item.price, reverse=True):
            take = min(level.size, remaining)
            if take <= 0:
                continue
            fee_per_share = estimated_fee_per_share(
                level.price,
                enabled=market.fees_enabled,
                rate=market.fee_rate,
                exponent=market.fee_exponent,
            )
            filled += take
            gross += take * level.price
            fee += take * fee_per_share
            remaining -= take
            limit_price = level.price
            if remaining <= 0:
                break
        raw_filled = filled
        filled = quantize_down(raw_filled, Decimal("0.01"))
        if raw_filled > 0 and filled < raw_filled:
            # Never count proceeds/fee for the sub-step remainder we cannot order.
            scale = filled / raw_filled
            gross *= scale
            fee *= scale
        if filled < minimum_size or limit_price is None or gross <= fee:
            return None
        return TradeCandidate(
            market_id=market.id,
            event_id=market.event_id,
            bucket=market.category or "other",
            token_id=book.token_id,
            outcome=outcome,
            side=Side.SELL,
            conservative_probability=Decimal("0"),
            expected_price=gross / filled,
            limit_price=limit_price,
            size=filled,
            notional_usd=gross - fee,
            fee_estimate_usd=fee,
            edge_after_costs=Decimal("0"),
            strategy="risk_exit_v1",
            forecast_id=None,
            decision_key=(
                f"risk:{book.token_id}:{position_size}:{int(utc_now().timestamp() // 60)}"
            ),
            book_captured_at=book.captured_at,
        )

    def _buy_candidate(
        self,
        *,
        market: MarketSpec,
        forecast: Forecast,
        book: OrderBookSnapshot,
        outcome: Outcome,
        conservative_probability: Decimal,
        portfolio: PortfolioState,
    ) -> TradeCandidate | None:
        best_ask = book.best_ask
        if best_ask is None or conservative_probability <= best_ask:
            return None

        raw_kelly = (conservative_probability - best_ask) / (Decimal("1") - best_ask)
        kelly_budget = (
            portfolio.bankroll_usd * self.settings.kelly_fraction * max(raw_kelly, Decimal("0"))
        )
        event_key = market.event_id or market.id
        bucket = market.category or "other"
        limits = [
            self.settings.max_order_usd,
            portfolio.bankroll_usd * self.settings.max_trade_risk_pct,
            portfolio.cash_usd,
            kelly_budget,
            max(
                Decimal("0"),
                portfolio.bankroll_usd * self.settings.max_event_exposure_pct
                - portfolio.event_exposure_usd.get(event_key, Decimal("0"))
                - portfolio.event_exposure_usd.get(UNCLASSIFIED_EVENT_KEY, Decimal("0")),
            ),
            max(
                Decimal("0"),
                portfolio.bankroll_usd * self.settings.max_bucket_exposure_pct
                - portfolio.bucket_exposure_usd.get(bucket, Decimal("0")),
            ),
            max(
                Decimal("0"),
                portfolio.bankroll_usd * self.settings.max_gross_exposure_pct
                - portfolio.gross_exposure_usd,
            ),
        ]
        budget = min(limits)
        if budget <= 0:
            return None

        shares, base_cost, fee_cost, limit_price = self._sweep_book(
            book=book,
            budget=budget,
            market=market,
        )
        size_step = Decimal("0.01")
        shares = quantize_down(shares, size_step)
        minimum_size = max(market.minimum_order_size, book.minimum_order_size)
        if shares < minimum_size or shares <= 0 or base_cost <= 0 or limit_price is None:
            return None

        # Using VWAP from the larger pre-quantized sweep slightly overstates cost, intentionally.
        vwap = base_cost / max(shares, Decimal("0.00000001"))
        fee_per_share = fee_cost / max(shares, Decimal("0.00000001"))
        edge = conservative_probability - vwap - fee_per_share - self.settings.uncertainty_reserve
        total_cost = base_cost + fee_cost
        if edge <= 0 or total_cost > budget * Decimal("1.02"):
            return None

        return TradeCandidate(
            market_id=market.id,
            event_id=market.event_id,
            bucket=bucket,
            token_id=book.token_id,
            outcome=outcome,
            conservative_probability=conservative_probability,
            expected_price=vwap,
            limit_price=limit_price,
            size=shares,
            notional_usd=total_cost,
            fee_estimate_usd=fee_cost,
            edge_after_costs=edge,
            forecast_id=forecast.id,
            book_captured_at=book.captured_at,
        )

    def _sell_candidate(
        self,
        *,
        market: MarketSpec,
        forecast: Forecast,
        book: OrderBookSnapshot,
        outcome: Outcome,
        conservative_upper: Decimal,
        portfolio: PortfolioState,
    ) -> TradeCandidate | None:
        position_size = portfolio.token_positions.get(book.token_id, Decimal("0"))
        best_bid = book.best_bid
        if position_size <= 0 or best_bid is None or best_bid <= conservative_upper:
            return None
        share_cap = quantize_down(
            min(position_size, self.settings.max_order_usd / best_bid), Decimal("0.01")
        )
        minimum_size = max(market.minimum_order_size, book.minimum_order_size)
        if share_cap < minimum_size:
            return None

        remaining = share_cap
        filled = Decimal("0")
        gross = Decimal("0")
        fee = Decimal("0")
        limit_price: Decimal | None = None
        for level in sorted(book.bids, key=lambda item: item.price, reverse=True):
            take = min(level.size, remaining)
            if take <= 0:
                break
            fee_per_share = estimated_fee_per_share(
                level.price,
                enabled=market.fees_enabled,
                rate=market.fee_rate,
                exponent=market.fee_exponent,
            )
            filled += take
            gross += take * level.price
            fee += take * fee_per_share
            remaining -= take
            limit_price = level.price
            if remaining <= 0:
                break
        raw_filled = filled
        filled = quantize_down(raw_filled, Decimal("0.01"))
        if raw_filled > 0 and filled < raw_filled:
            scale = filled / raw_filled
            gross *= scale
            fee *= scale
        if filled < minimum_size or limit_price is None:
            return None
        vwap = gross / filled
        fee_per_share = fee / filled
        edge = vwap - conservative_upper - fee_per_share - self.settings.uncertainty_reserve
        if edge <= 0:
            return None
        return TradeCandidate(
            market_id=market.id,
            event_id=market.event_id,
            bucket=market.category or "other",
            token_id=book.token_id,
            outcome=outcome,
            side=Side.SELL,
            conservative_probability=conservative_upper,
            expected_price=vwap,
            limit_price=limit_price,
            size=filled,
            notional_usd=gross - fee,
            fee_estimate_usd=fee,
            edge_after_costs=edge,
            forecast_id=forecast.id,
            book_captured_at=book.captured_at,
        )

    @staticmethod
    def _sweep_book(
        *,
        book: OrderBookSnapshot,
        budget: Decimal,
        market: MarketSpec,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal | None]:
        remaining = budget
        shares = Decimal("0")
        base_cost = Decimal("0")
        fee_cost = Decimal("0")
        last_price: Decimal | None = None
        for level in sorted(book.asks, key=lambda item: item.price):
            per_share_fee = estimated_fee_per_share(
                level.price,
                enabled=market.fees_enabled,
                rate=market.fee_rate,
                exponent=market.fee_exponent,
            )
            all_in_per_share = level.price + per_share_fee
            take = min(level.size, remaining / all_in_per_share)
            if take <= 0:
                break
            level_cost = take * level.price
            level_fee = take * per_share_fee
            shares += take
            base_cost += level_cost
            fee_cost += level_fee
            remaining -= level_cost + level_fee
            last_price = level.price
            if remaining <= Decimal("0.000001"):
                break
        return shares, base_cost, fee_cost, last_price
