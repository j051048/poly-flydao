from __future__ import annotations

import hashlib
from decimal import Decimal

from polybot.models import Forecast, ForecastRequest


class SafeMockForecastProvider:
    """A deliberately uncertain deterministic provider; safe default never creates a trade."""

    async def forecast(self, request: ForecastRequest, *, model: str) -> Forecast:
        digest = hashlib.sha256(request.market.question.encode()).digest()
        offset = Decimal(digest[0] % 5 - 2) / Decimal("100")
        probability = Decimal("0.50") + offset
        return Forecast(
            market_id=request.market.id,
            probability_yes=probability,
            probability_low=max(Decimal("0"), probability - Decimal("0.20")),
            probability_high=min(Decimal("1"), probability + Decimal("0.20")),
            confidence=Decimal("0.20"),
            assumptions=["No external evidence provider is configured."],
            invalidation_conditions=["Configure a cited evidence collector and calibrated model."],
            source_ids=[item.id for item in request.evidence],
            model=f"mock:{model}",
            rationale="Safe deterministic placeholder; it is intentionally not tradeable.",
        )


class StaticForecastProvider:
    def __init__(self, forecast: Forecast):
        self.value = forecast

    async def forecast(self, request: ForecastRequest, *, model: str) -> Forecast:
        return self.value.model_copy(
            update={"market_id": request.market.id, "model": model, "id": self.value.id}
        )
