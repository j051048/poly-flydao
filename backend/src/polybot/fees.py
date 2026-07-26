from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def estimated_fee_per_share(
    price: Decimal,
    *,
    enabled: bool,
    rate: Decimal,
    exponent: Decimal,
) -> Decimal:
    """Estimate the runtime fee schedule supplied by Polymarket.

    `rate` must be the normalized rate from the API/SDK, not a hard-coded market category.
    A small separate safety reserve is applied by the strategy layer.
    """

    if not enabled or rate <= 0:
        return Decimal("0")
    if price <= 0 or price >= 1:
        return Decimal("0")
    probability_term = price * (Decimal("1") - price)
    return rate * (probability_term**exponent)


def quantize_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("step must be positive")
    return (value // step) * step


def matched_taker_fee_usd(
    *,
    size: Decimal,
    price: Decimal,
    fee_rate_bps: Decimal,
    trader_side: str | None,
) -> Decimal:
    """Reconstruct the matched fee reported as a rate by CLOB account trades.

    Polymarket charges makers zero and calculates taker fees as
    ``shares * rate * price * (1-price)``, rounded to five USDC decimals.
    An absent trader-side tag is treated conservatively as taker.
    """

    if size <= 0 or price <= 0 or price >= 1 or fee_rate_bps <= 0:
        return Decimal("0")
    if str(trader_side or "").upper() == "MAKER":
        return Decimal("0")
    rate = fee_rate_bps / Decimal("10000")
    raw = size * rate * price * (Decimal("1") - price)
    return raw.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)
