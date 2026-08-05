from __future__ import annotations

import httpx
import pytest

from polybot.notify import NotificationMessage, Notifier, build_notifier


@pytest.mark.asyncio
async def test_notifier_posts_public_webhook_and_returns_true(monkeypatch) -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def _public(value: str) -> str:
        return value

    monkeypatch.setattr(
        "polybot.notify.validate_public_ai_base_url",
        _public,
    )
    notifier = Notifier("https://hooks.example.com/alert", client=client)

    ok = await notifier.send(
        NotificationMessage(title="cycle failed", message="markets=2", severity="error")
    )

    assert ok is True
    assert captured["url"] == "https://hooks.example.com/alert"
    assert b"cycle failed" in captured["body"]
    await client.aclose()


@pytest.mark.asyncio
async def test_notifier_rejects_private_webhook_url() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    notifier = Notifier("http://127.0.0.1:9000/hook", client=client)

    ok = await notifier.send(NotificationMessage(title="t", message="m"))

    assert ok is False
    await client.aclose()


@pytest.mark.asyncio
async def test_notifier_never_raises_on_transport_error() -> None:
    async def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    notifier = Notifier("https://hooks.example.com/alert", client=client)

    ok = await notifier.send(NotificationMessage(title="t", message="m"))

    assert ok is False
    await client.aclose()


@pytest.mark.asyncio
async def test_notifier_noop_without_url() -> None:
    notifier = Notifier(None)
    assert await notifier.send(NotificationMessage(title="t", message="m")) is False
    await notifier.close()


def test_build_notifier_returns_none_without_url() -> None:
    assert build_notifier(None) is None
    assert build_notifier("   ") is None
    assert build_notifier("https://hooks.example.com/alert") is not None
