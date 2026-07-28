from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from polybot.fees import estimated_fee_per_share, quantize_down
from polybot.models import (
    LiquidityRole,
    MarketSpec,
    OrderBookSnapshot,
    OrderGroup,
    OrderLeg,
    OrderLegPurpose,
    OrderPlan,
    Outcome,
    Side,
    utc_now,
)


class PairAccumulatorConfig(BaseModel):
    """The strategy is deliberately disabled and research-only by default."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    research_only: Literal[True] = True
    target_pair_size: Decimal = Field(default=Decimal("5"), gt=0)
    size_step: Decimal = Field(default=Decimal("0.01"), gt=0)
    minimum_net_edge_per_share: Decimal = Field(default=Decimal("0.01"), ge=0, lt=1)
    leg_risk_buffer_per_share: Decimal = Field(default=Decimal("0.005"), ge=0, lt=1)
    capital_cost_per_share: Decimal = Field(default=Decimal("0.001"), ge=0, lt=1)
    leg_deadline: timedelta = Field(default=timedelta(seconds=8), gt=timedelta(0))
    improve_queue_by_one_tick: bool = True


class PairQuote(BaseModel):
    yes_price: Decimal = Field(gt=0, lt=1)
    no_price: Decimal = Field(gt=0, lt=1)
    shares: Decimal = Field(gt=0)
    base_cost_usd: Decimal = Field(ge=0)
    maker_fee_usd: Decimal = Field(ge=0)
    taker_hedge_fee_buffer_usd: Decimal = Field(ge=0)
    leg_risk_buffer_usd: Decimal = Field(ge=0)
    capital_cost_usd: Decimal = Field(ge=0)
    expected_payout_usd: Decimal = Field(gt=0)
    net_edge_usd: Decimal

    @property
    def net_edge_per_share(self) -> Decimal:
        return self.net_edge_usd / self.shares


class PairPricer:
    """Price equal-share maker legs without treating batch submission as atomic."""

    def __init__(self, config: PairAccumulatorConfig | None = None):
        self.config = config or PairAccumulatorConfig()

    def quote(
        self,
        market: MarketSpec,
        yes_book: OrderBookSnapshot,
        no_book: OrderBookSnapshot,
        *,
        requested_shares: Decimal | None = None,
    ) -> PairQuote | None:
        if market.neg_risk or market.closed or not market.active or not market.accepting_orders:
            return None
        if yes_book.token_id != market.yes_token_id or no_book.token_id != market.no_token_id:
            return None
        if yes_book.market_id != market.id or no_book.market_id != market.id:
            return None

        shares = quantize_down(
            requested_shares or self.config.target_pair_size,
            self.config.size_step,
        )
        minimum = max(
            market.minimum_order_size,
            yes_book.minimum_order_size,
            no_book.minimum_order_size,
        )
        if shares < minimum:
            return None
        yes_price = self._maker_buy_price(yes_book)
        no_price = self._maker_buy_price(no_book)
        if yes_price is None or no_price is None:
            return None

        base_cost = shares * (yes_price + no_price)
        maker_fee = shares * (
            self._maker_fee_per_share(market, yes_price)
            + self._maker_fee_per_share(market, no_price)
        )
        hedge_fee_buffer = shares * max(
            self._taker_fee_at_best_ask(market, yes_book),
            self._taker_fee_at_best_ask(market, no_book),
        )
        leg_risk_buffer = shares * self.config.leg_risk_buffer_per_share
        capital_cost = shares * self.config.capital_cost_per_share
        payout = shares
        net_edge = (
            payout
            - base_cost
            - maker_fee
            - hedge_fee_buffer
            - leg_risk_buffer
            - capital_cost
        )
        quote = PairQuote(
            yes_price=yes_price,
            no_price=no_price,
            shares=shares,
            base_cost_usd=base_cost,
            maker_fee_usd=maker_fee,
            taker_hedge_fee_buffer_usd=hedge_fee_buffer,
            leg_risk_buffer_usd=leg_risk_buffer,
            capital_cost_usd=capital_cost,
            expected_payout_usd=payout,
            net_edge_usd=net_edge,
        )
        if quote.net_edge_per_share < self.config.minimum_net_edge_per_share:
            return None
        return quote

    def build_plan(
        self,
        *,
        account_id: str,
        market: MarketSpec,
        yes_book: OrderBookSnapshot,
        no_book: OrderBookSnapshot,
        trading_wallet_id: str | None = None,
        requested_shares: Decimal | None = None,
    ) -> OrderPlan | None:
        if not self.config.enabled:
            return None
        quote = self.quote(
            market,
            yes_book,
            no_book,
            requested_shares=requested_shares,
        )
        if quote is None:
            return None
        now = utc_now()
        deadline = now + self.config.leg_deadline
        group = OrderGroup(
            account_id=account_id,
            trading_wallet_id=trading_wallet_id,
            market_id=market.id,
            condition_id=market.condition_id,
            target_pair_size=quote.shares,
            expected_net_edge_usd=quote.net_edge_usd,
            leg_deadline_at=deadline,
            research_only=self.config.research_only,
            created_at=now,
            updated_at=now,
        )
        common = {
            "group_id": group.id,
            "side": Side.BUY,
            "purpose": OrderLegPurpose.PAIR_ENTRY,
            "liquidity_role": LiquidityRole.MAKER,
            "post_only": True,
            "size": quote.shares,
            "deadline_at": deadline,
            "created_at": now,
            "updated_at": now,
        }
        legs = (
            OrderLeg(
                **common,
                outcome=Outcome.YES,
                token_id=market.yes_token_id,
                price=quote.yes_price,
            ),
            OrderLeg(
                **common,
                outcome=Outcome.NO,
                token_id=market.no_token_id,
                price=quote.no_price,
            ),
        )
        return OrderPlan(
            group=group,
            legs=legs,
            base_cost_usd=quote.base_cost_usd,
            maker_fee_usd=quote.maker_fee_usd,
            taker_hedge_fee_buffer_usd=quote.taker_hedge_fee_buffer_usd,
            leg_risk_buffer_usd=quote.leg_risk_buffer_usd,
            capital_cost_usd=quote.capital_cost_usd,
            expected_payout_usd=quote.expected_payout_usd,
            enabled=self.config.enabled,
            research_only=self.config.research_only,
        )

    def _maker_buy_price(self, book: OrderBookSnapshot) -> Decimal | None:
        """Return a valid post-only BUY price that is strictly below best ask."""

        best_ask = book.best_ask
        if best_ask is None:
            return None
        tick = book.tick_size
        if book.best_bid is not None and book.best_bid >= best_ask:
            return None
        maximum_post_only = quantize_down(best_ask - tick, tick)
        if maximum_post_only <= 0:
            return None
        best_bid = book.best_bid
        if best_bid is None:
            return min(tick, maximum_post_only)
        candidate = best_bid
        if self.config.improve_queue_by_one_tick:
            candidate += tick
        candidate = quantize_down(candidate, tick)
        price = min(candidate, maximum_post_only)
        if price <= 0 or price >= best_ask or price % tick != 0:
            return None
        return price

    @staticmethod
    def _maker_fee_per_share(market: MarketSpec, price: Decimal) -> Decimal:
        # Rebates are intentionally excluded until they appear in settled fills.
        return estimated_fee_per_share(
            price,
            enabled=market.fees_enabled and not market.fee_taker_only,
            rate=market.fee_rate,
            exponent=market.fee_exponent,
        )

    @staticmethod
    def _taker_fee_at_best_ask(
        market: MarketSpec,
        book: OrderBookSnapshot,
    ) -> Decimal:
        if book.best_ask is None:
            return Decimal("1")
        return estimated_fee_per_share(
            book.best_ask,
            enabled=market.fees_enabled,
            rate=market.fee_rate,
            exponent=market.fee_exponent,
        )
