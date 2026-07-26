from decimal import Decimal

from polybot.fees import matched_taker_fee_usd


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
