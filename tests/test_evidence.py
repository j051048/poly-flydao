from __future__ import annotations

from types import SimpleNamespace

import httpx

from polybot.ai.evidence import (
    GDELTNewsEvidenceCollector,
    OpenAIWebEvidenceCollector,
    canonical_source_url,
    source_identity,
)
from polybot.models import EvidenceSearchItem, EvidenceSearchPayload


def test_canonical_source_url_removes_tracking_and_normalizes_host() -> None:
    first = canonical_source_url("HTTPS://WWW.Example.com/news/?utm_source=x&id=7#section")
    second = canonical_source_url("https://example.com/news?id=7")

    assert first == second
    assert source_identity(first) == "example.com"


def test_source_identity_uses_idna_registrable_domain() -> None:
    first = canonical_source_url("https://NEWS.BÜCHER.de/report")
    second = canonical_source_url("https://sports.xn--bcher-kva.de/other")

    assert first == "https://news.xn--bcher-kva.de/report"
    assert source_identity(first) == "xn--bcher-kva.de"
    assert source_identity(second) == "xn--bcher-kva.de"
    assert source_identity("https://news.service.example.co.uk/a") == "example.co.uk"
    assert source_identity("https://sports.service.example.co.uk/b") == "example.co.uk"
    assert source_identity("https://127.0.0.1/report") is None


async def test_gdelt_context_evidence_requires_same_sentence_snippet(market) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.host == "api.gdeltproject.org"
        assert request.url.path == "/api/v2/context/context"
        assert request.url.params["timespan"] == "72h"
        return httpx.Response(
            200,
            json={
                "articles": [
                    {
                        "url": "https://world.news-a.com/story?utm_source=feed",
                        "title": "First relevant report",
                        "seendate": "20260726T010203Z",
                        "language": "English",
                        "sentence": (
                            "The synthetic test event will occur before December 2026, "
                            "according to the filing."
                        ),
                        "context": (
                            "Officials stated that the synthetic test event will occur "
                            "before December 2026, according to the filing."
                        ),
                    },
                    {
                        "url": "https://local.news-a.com/copy",
                        "title": "Same publisher copy",
                        "language": "English",
                        "sentence": ("The synthetic test event will occur before December 2026."),
                        "context": "The synthetic test event will occur before December 2026.",
                    },
                    {
                        "url": "https://news-b.com/report",
                        "title": "Independent relevant report",
                        "language": "English",
                        "sentence": ("The synthetic test event may occur before December 2026."),
                        "context": "The synthetic test event may occur before December 2026.",
                    },
                    {
                        "url": "https://news-c.com/title-only",
                        "title": "Title-only lead",
                        "language": "English",
                    },
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = GDELTNewsEvidenceCollector(client=client)
        evidence = await collector.collect(
            market.model_copy(
                update={
                    "question": "Will synthetic test event occur before December 2026?",
                    "resolution_source": "https://official.example/rules",
                }
            )
        )
        cached = await collector.collect(
            market.model_copy(
                update={
                    "question": "Will synthetic test event occur before December 2026?",
                    "resolution_source": "https://official.example/rules",
                }
            )
        )

    assert {source_identity(item.source_url) for item in evidence if item.source_url} == {
        "news-a.com",
        "news-b.com",
    }
    assert all("utm_" not in item.source_url for item in evidence if item.source_url)
    metadata = [item for item in evidence if item.source_url is None]
    assert any(item.title == "Market-designated resolution source" for item in metadata)
    assert any(item.title == "Title-only lead" for item in metadata)
    assert all("does not count" in item.summary for item in metadata)
    assert all(
        "GDELT Context 2.0 extracted" in item.summary for item in evidence if item.source_url
    )
    assert [item.content_hash for item in cached] == [item.content_hash for item in evidence]
    assert calls == 2


async def test_gdelt_rate_limit_fails_closed_and_backs_off(market) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"retry-after": "120"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = GDELTNewsEvidenceCollector(
            client=client,
            cache_ttl_seconds=900,
            rate_limit_backoff_seconds=60,
        )
        limited_market = market.model_copy(
            update={
                "question": "Will synthetic test event occur before December 2026?",
                "resolution_source": "https://official.example/rules",
            }
        )
        first = await collector.collect(limited_market)
        second = await collector.collect(limited_market)

    assert [item.source_url for item in first] == [None]
    assert [item.content_hash for item in second] == [item.content_hash for item in first]
    assert calls == 1


async def test_gdelt_transport_failure_fails_closed_and_backs_off(market) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.RemoteProtocolError("server disconnected")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = GDELTNewsEvidenceCollector(
            client=client,
            cache_ttl_seconds=900,
            rate_limit_backoff_seconds=60,
        )
        first = await collector.collect(market)
        second = await collector.collect(market)

    assert all(item.source_url is None for item in first)
    assert [item.content_hash for item in second] == [item.content_hash for item in first]
    assert calls == 1


async def test_openai_evidence_accepts_only_actual_web_search_sources(market) -> None:
    parsed = EvidenceSearchPayload(
        items=[
            EvidenceSearchItem(
                title="Grounded",
                summary="Grounded summary",
                source_url="https://www.grounded.com/report?utm_source=search",
            ),
            EvidenceSearchItem(
                title="Invented",
                summary="Model-invented URL",
                source_url="https://invented.net/report",
            ),
        ]
    )
    captured: dict = {}

    class FakeResponses:
        def parse(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                output_parsed=parsed,
                output=[
                    SimpleNamespace(
                        type="web_search_call",
                        action=SimpleNamespace(
                            type="search",
                            sources=[
                                SimpleNamespace(
                                    type="url",
                                    url="https://grounded.com/report",
                                )
                            ],
                        ),
                    )
                ],
            )

    client = SimpleNamespace(responses=FakeResponses())
    collector = OpenAIWebEvidenceCollector(
        "unused",
        model="test-model",
        client=client,
    )

    evidence = await collector.collect(market)

    assert [item.title for item in evidence] == ["Grounded"]
    assert evidence[0].source_url == "https://grounded.com/report"
    assert captured["include"] == ["web_search_call.action.sources"]
    assert captured["store"] is False


async def test_openai_evidence_without_tool_metadata_fails_closed(market) -> None:
    parsed = EvidenceSearchPayload(
        items=[
            EvidenceSearchItem(
                title="Unverified",
                summary="No citation metadata",
                source_url="https://unverified.com/report",
            )
        ]
    )

    class FakeResponses:
        def parse(self, **_):
            return SimpleNamespace(output_parsed=parsed, output=[])

    collector = OpenAIWebEvidenceCollector(
        "unused",
        model="test-model",
        client=SimpleNamespace(responses=FakeResponses()),
    )

    assert await collector.collect(market) == []
