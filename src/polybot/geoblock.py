from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class GeoblockResult:
    allowed: bool
    country: str | None = None
    region: str | None = None
    reason: str = ""


class GeoblockChecker:
    """Fail-closed official location eligibility check; never attempts a bypass."""

    def __init__(self, url: str, *, client: httpx.AsyncClient | None = None):
        self.url = url
        self.client = client

    async def check(self) -> GeoblockResult:
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=5, follow_redirects=True)
        try:
            response = await client.get(self.url, headers={"Accept": "application/json"})
            response.raise_for_status()
            data = response.json()
            blocked = data.get("blocked")
            if not isinstance(blocked, bool):
                return GeoblockResult(False, reason="malformed geoblock response")
            return GeoblockResult(
                allowed=not blocked,
                country=data.get("country"),
                region=data.get("region"),
                reason="blocked by Polymarket" if blocked else "eligible",
            )
        except Exception as exc:
            return GeoblockResult(False, reason=f"geoblock check failed: {type(exc).__name__}")
        finally:
            if owns_client:
                await client.aclose()
