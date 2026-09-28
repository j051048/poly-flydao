from decimal import Decimal

from polybot.fees import matched_taker_fee_usd, maximum_buy_cost


def test_matched_taker_fee_uses_price_symmetric_formula() -> None:
    low = matched_taker_fee_usd(
        size=Decimal("100"),
        price=Decimal("0.30"),
        fee_rate_bps=Decimal("700"),
        trader_side="TAKER",
    )
    high = matched_taker_fee_usd(
        size=Decimal("100"),
        price=Decimal("0.70"),
        fee_rate_bps=Decimal("700"),
        trader_side="TAKER",
    )

    assert low == Decimal("1.47000")
    assert high == low


def test_matched_maker_fee_is_zero() -> None:
    assert matched_taker_fee_usd(
        size=Decimal("100"),
        price=Decimal("0.50"),
        fee_rate_bps=Decimal("700"),
        trader_side="MAKER",
    ) == Decimal("0")


def test_buy_commitment_covers_rounding_across_tiny_partial_fills() -> None:
    # These sub-cent share fills each round the fee upward. Rounding only the
    # whole order's fee would under-reserve, even though the order size is 0.01.
    part_size = Decimal("0.0004")
    count = 25
    price = Decimal("0.5")
    fee = matched_taker_fee_usd(
        size=part_size, price=price, fee_rate_bps=Decimal("700"), trader_side="TAKER",
    )
    assert fee == Decimal("0.00001")
    bound = maximum_buy_cost(
        part_size * count, price,
        enabled=True, rate=Decimal("0.07"), exponent=Decimal("1"),
    )
    assert part_size * count * price + count * fee <= bound
