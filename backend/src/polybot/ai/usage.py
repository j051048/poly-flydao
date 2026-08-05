from __future__ import annotations

import time
from decimal import Decimal

from polybot.models import AIUsageRecord, utc_now

# Approximate USD per 1M tokens for a small set of known OpenAI model families.
# Unknown models keep cost_usd=None; the tokens and latency are always recorded.
_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4.1-nano": (Decimal("0.10"), Decimal("0.40")),
    "gpt-4.1-mini": (Decimal("0.40"), Decimal("1.60")),
    "gpt-4.1": (Decimal("2.00"), Decimal("8.00")),
    "o4-mini": (Decimal("1.10"), Decimal("4.40")),
    "o3": (Decimal("2.00"), Decimal("8.00")),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    """Best-effort cost estimate; unknown families are intentionally None."""

    normalized = model.strip().casefold()
    pricing = _PRICING.get(normalized)
    if pricing is None:
        return None
    input_price, output_price = pricing
    return (
        Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price
    ) / Decimal("1000000")


def build_usage(
    *,
    provider: str,
    model: str,
    market_id: str | None,
    request_id: str | None,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int | None,
) -> AIUsageRecord:
    input_tokens = max(0, input_tokens)
    output_tokens = max(0, output_tokens)
    return AIUsageRecord(
        provider=provider,
        model=model,
        market_id=market_id,
        request_id=request_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        latency_ms=latency_ms,
        cost_usd=estimate_cost_usd(model, input_tokens, output_tokens),
        created_at=utc_now(),
    )


class Stopwatch:
    """Tiny monotonic timer for provider latency."""

    def __init__(self) -> None:
        self._started = time.monotonic()

    def elapsed_ms(self) -> int:
        return max(0, int((time.monotonic() - self._started) * 1000))
