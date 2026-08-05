from __future__ import annotations

import logging
from contextlib import suppress

from polybot.models import Forecast, ForecastRequest

LOGGER = logging.getLogger(__name__)


class FallbackForecastProvider:
    """Ordered read-only forecast fallback.

    Only `forecast()` is wrapped: signing, submission, and cancellation stay
    single-provider paths. A failure in one provider falls through to the next;
    if every provider fails the cycle is skipped exactly like a single-provider
    error, so no new trading semantics are introduced.
    """

    def __init__(self, providers: list):
        if not providers:
            raise ValueError("fallback requires at least one provider")
        self.providers = list(providers)

    async def forecast(self, request: ForecastRequest, *, model: str) -> Forecast:
        last_error: Exception | None = None
        for index, provider in enumerate(self.providers):
            try:
                return await provider.forecast(request, model=model)
            except Exception as exc:
                last_error = exc
                LOGGER.warning(
                    "forecast provider %s failed; trying next",
                    index,
                    exc_info=exc,
                )
        raise RuntimeError("all forecast providers failed") from last_error

    def last_usage(self):
        for provider in reversed(self.providers):
            last_usage = getattr(provider, "last_usage", None)
            if callable(last_usage):
                usage = last_usage()
                if usage is not None:
                    return usage
        return None

    def drain_usage(self) -> list:
        records: list = []
        for provider in self.providers:
            drain = getattr(provider, "drain_usage", None)
            if callable(drain):
                records.extend(drain())
        return records

    async def close(self) -> None:
        for provider in self.providers:
            close = getattr(provider, "close", None)
            if close is not None:
                with suppress(Exception):
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
