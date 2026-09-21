from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from polybot.experimental.pair_accumulator import PairAccumulatorConfig, PairPricer
from polybot.market_filters.crypto_updown import CryptoUpDownFilter
from polybot.models import BookLevel, MarketSpec, OrderBookSnapshot, Outcome, utc_now


def _updown_market(**updates) -> MarketSpec:
    now = utc_now()
    values = {
        "id": "btc-15m",
        "condition_id": "condition",
        "slug": "bitcoin-up-or-down-july-28-1200",
        "event_slug": "bitcoin-up-or-down",
        "question": "Bitcoin Up or Down in the next 15 minutes?",
        "category": "crypto",
        "tags": ("Crypto", "Bitcoin"),
        "resolution_source": "https://example.test/btc-usd",
        "yes_token_id": "up-token",
        "no_token_id": "down-token",
        "yes_label": "Up",
        "no_label": "Down",
        "liquidity_usd": Decimal("50000"),
        "fees_enabled": True,
        "fee_rate": Decimal("0.02"),
        "fee_exponent": Decimal("2"),
        "fee_taker_only": True,
        "minimum_order_size": Decimal("1"),
        "tick_size": Decimal("0.01"),
        "start_at": now,
        "end_at": now + timedelta(minutes=15),
    }
    values.update(updates)
    return MarketSpec(**values)


def _book(token_id: str, bid: str, ask: str) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        market_id="btc-15m",
        token_id=token_id,
        bids=[BookLevel(price=Decimal(bid), size=Decimal("100"))],
        asks=[BookLevel(price=Decimal(ask), size=Decimal("100"))],
        tick_size=Decimal("0.01"),
        minimum_order_size=Decimal("1"),
    )


def test_crypto_updown_filter_uses_raw_labels_not_yes_no_assumptions() -> None:
    classified = CryptoUpDownFilter().classify(_updown_market())
    assert classified is not None
    assert classified.asset_symbol == "BTC"
    assert classified.up_outcome is Outcome.YES
    assert classified.up_token_id == "up-token"

    inverted = CryptoUpDownFilter().classify(
        _updown_market(
            yes_label="Down",
            no_label="Up",
            yes_token_id="down-token",
            no_token_id="up-token",
        )
    )
    assert inverted is not None
    assert inverted.up_outcome is Outcome.NO
    assert inverted.up_token_id == "up-token"


def test_crypto_updown_filter_fails_closed_without_timing_source_or_labels() -> None:
    assert CryptoUpDownFilter().classify(_updown_market(resolution_source=None)) is None
    assert CryptoUpDownFilter().classify(_updown_market(start_at=None)) is None
    assert (
        CryptoUpDownFilter().classify(
            _updown_market(yes_label="Yes", no_label="No")
        )
        is None
    )
    assert CryptoUpDownFilter().classify(_updown_market(neg_risk=True)) is None


def test_pair_pricer_uses_equal_shares_real_fee_buffer_and_post_only_prices() -> None:
    market = _updown_market()
    yes_book = _book("up-token", "0.44", "0.46")
    no_book = _book("down-token", "0.48", "0.50")
    pricer = PairPricer(
        PairAccumulatorConfig(
            enabled=True,
            target_pair_size=Decimal("5"),
            minimum_net_edge_per_share=Decimal("0.001"),
        )
    )

    quote = pricer.quote(market, yes_book, no_book)
    assert quote is not None
    assert quote.yes_price == Decimal("0.45")
    assert quote.no_price == Decimal("0.49")
    assert quote.yes_price < yes_book.best_ask
    assert quote.no_price < no_book.best_ask
    assert quote.base_cost_usd == Decimal("4.70")
    assert quote.maker_fee_usd == 0
    assert quote.taker_hedge_fee_buffer_usd > 0

    plan = pricer.build_plan(
        account_id="account",
        market=market,
        yes_book=yes_book,
        no_book=no_book,
    )
    assert plan is not None
    assert plan.legs[0].size == plan.legs[1].size == Decimal("5")
    assert all(leg.post_only for leg in plan.legs)
    assert plan.net_edge_usd == quote.net_edge_usd
    assert not plan.live_eligible


def test_pair_strategy_is_disabled_and_research_only_by_default() -> None:
    market = _updown_market()
    pricer = PairPricer()
    assert pricer.config.enabled is False
    assert pricer.config.research_only is True
    assert (
        pricer.build_plan(
            account_id="account",
            market=market,
            yes_book=_book("up-token", "0.44", "0.46"),
            no_book=_book("down-token", "0.48", "0.50"),
        )
        is None
    )


def test_maker_and_taker_fees_are_budgeted_independently_without_rebate_assumption() -> None:
    pricer = PairPricer(
        PairAccumulatorConfig(
            enabled=True,
            minimum_net_edge_per_share=Decimal("0"),
            leg_risk_buffer_per_share=Decimal("0"),
            capital_cost_per_share=Decimal("0"),
        )
    )
    quote = pricer.quote(
        _updown_market(
            fee_taker_only=False,
            maker_rebate_rate=Decimal("0.50"),
        ),
        _book("up-token", "0.44", "0.46"),
        _book("down-token", "0.48", "0.50"),
        requested_shares=Decimal("1"),
    )
    assert quote is not None
    assert quote.maker_fee_usd > 0
    assert quote.taker_hedge_fee_buffer_usd > 0
    # A configured maker rebate is not projected into expected edge.
    assert quote.net_edge_usd < quote.expected_payout_usd - quote.base_cost_usd


def test_one_tick_spread_never_creates_a_crossing_post_only_quote() -> None:
    quote = PairPricer(
        PairAccumulatorConfig(
            enabled=True,
            minimum_net_edge_per_share=Decimal("0"),
            leg_risk_buffer_per_share=Decimal("0"),
            capital_cost_per_share=Decimal("0"),
        )
    ).quote(
        _updown_market(fees_enabled=False),
        _book("up-token", "0.45", "0.46"),
        _book("down-token", "0.48", "0.49"),
        requested_shares=Decimal("1"),
    )
    assert quote is not None
    assert quote.yes_price == Decimal("0.45")
    assert quote.no_price == Decimal("0.48")
