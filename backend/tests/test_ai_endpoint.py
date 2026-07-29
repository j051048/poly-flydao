from __future__ import annotations

import pytest

from polybot.ai_endpoint import (
    UnsafeAIBaseURLError,
    normalize_ai_base_url,
    validate_public_ai_base_url,
)


def test_ai_base_url_normalizes_public_https_endpoint() -> None:
    assert (
        normalize_ai_base_url(" https://Relay.Example.com:443/v1/ ")
        == "https://relay.example.com/v1"
    )


@pytest.mark.parametrize(
    "value",
    [
        "http://relay.example.com/v1",
        "https://localhost/v1",
        "https://worker.internal/v1",
        "https://127.0.0.1/v1",
        "https://10.0.0.8/v1",
        "https://169.254.169.254/latest/meta-data",
        "https://user:pass@relay.example.com/v1",
        "https://relay.example.com/v1?token=secret",
        "https://relay.example.com/v1#fragment",
        "https://relay.example.com/v1/%2e%2e/admin",
    ],
)
def test_ai_base_url_rejects_unsafe_syntax_and_literal_targets(value: str) -> None:
    with pytest.raises(UnsafeAIBaseURLError):
        normalize_ai_base_url(value)


async def test_ai_base_url_rejects_mixed_public_and_private_dns_answers() -> None:
    async def resolver(_: str, __: int) -> list[str]:
        return ["104.18.1.1", "10.0.0.8"]

    with pytest.raises(UnsafeAIBaseURLError, match="private"):
        await validate_public_ai_base_url(
            "https://relay.example.com/v1",
            resolver=resolver,
        )


async def test_ai_base_url_enforces_optional_worker_allowlist() -> None:
    async def resolver(_: str, __: int) -> list[str]:
        return ["104.18.1.1"]

    assert (
        await validate_public_ai_base_url(
            "https://api.relay.example.com/v1",
            allowed_hosts="gateway.example.net,*.relay.example.com",
            resolver=resolver,
        )
        == "https://api.relay.example.com/v1"
    )
    with pytest.raises(UnsafeAIBaseURLError, match="allowlist"):
        await validate_public_ai_base_url(
            "https://untrusted.example.org/v1",
            allowed_hosts="*.relay.example.com",
            resolver=resolver,
        )
