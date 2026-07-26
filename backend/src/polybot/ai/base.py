from __future__ import annotations

from typing import Protocol

from polybot.models import EvidenceItem, Forecast, ForecastRequest, MarketSpec


class ForecastProvider(Protocol):
    async def forecast(self, request: ForecastRequest, *, model: str) -> Forecast: ...


class EvidenceCollector(Protocol):
    async def collect(self, market: MarketSpec) -> list[EvidenceItem]: ...
