from __future__ import annotations

import httpx
import pytest

from polybot.geoblock import GeoblockChecker


@pytest.mark.asyncio
async def test_geoblock_allows_when_not_blocked() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"blocked": False, "country": "SG", "region": "APAC"},
            )
        )
    )
    checker = GeoblockChecker("https://polymarket.com/api/geoblock", client=client)

    result = await checker.check()

    assert result.allowed is True
    assert result.country == "SG"
    await client.aclose()


@pytest.mark.asyncio
async def test_geoblock_blocks_when_blocked() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"blocked": True})
        )
    )
    checker = GeoblockChecker("https://polymarket.com/api/geoblock", client=client)

    result = await checker.check()

    assert result.allowed is False
    assert result.reason == "blocked by Polymarket"
    await client.aclose()


@pytest.mark.asyncio
async def test_geoblock_fails_closed_on_malformed_and_errors() -> None:
    malformed = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": 1}))
    )
    malformed_checker = GeoblockChecker("https://example.com/geoblock", client=malformed)
    assert (await malformed_checker.check()).allowed is False
    await malformed.aclose()

    failing = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    )
    failing_checker = GeoblockChecker("https://example.com/geoblock", client=failing)
    assert (await failing_checker.check()).allowed is False
    await failing.aclose()
