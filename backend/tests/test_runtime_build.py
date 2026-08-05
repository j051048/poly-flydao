from __future__ import annotations

from decimal import Decimal

import pytest

from polybot.ai.litellm_provider import LiteLLMForecastProvider
from polybot.ai.mock import SafeMockForecastProvider
from polybot.ai.openai_compatible_provider import OpenAICompatibleForecastProvider
from polybot.ai.openai_provider import OpenAIForecastProvider
from polybot.brokers.paper import PaperBroker
from polybot.config import Settings
from polybot.runtime import _ai, build_runtime


def test_build_runtime_paper_memory_returns_engine_and_paper_broker() -> None:
    settings = Settings(
        _env_file=None,
        mode="paper",
        bankroll_usd=Decimal("500"),
        min_liquidity_usd=Decimal("100"),
        min_edge=Decimal("0.04"),
    )

    runtime = build_runtime(settings)

    try:
        assert runtime.settings is settings
        assert isinstance(runtime.broker, PaperBroker)
        assert runtime.engine is not None
        assert runtime.store is not None
        assert runtime.market_data is not None
    finally:
        import asyncio

        asyncio.run(runtime.close())


def test_build_runtime_canary_without_supabase_fails_closed() -> None:
    with pytest.raises(ValueError):
        settings = Settings(
            _env_file=None,
            mode="canary",
            bankroll_usd=Decimal("500"),
        )
        build_runtime(settings)


def test_build_runtime_openai_without_key_fails_closed() -> None:
    settings = Settings(
        _env_file=None,
        mode="paper",
        ai_provider="openai",
    )
    with pytest.raises(ValueError):
        build_runtime(settings)


def test_ai_resolution_mock_litellm_openai_and_compatible() -> None:
    provider, _ = _ai(Settings(_env_file=None, mode="paper", ai_provider="mock"))
    assert isinstance(provider, SafeMockForecastProvider)

    provider, _ = _ai(
        Settings(
            _env_file=None,
            mode="paper",
            ai_provider="litellm",
            litellm_api_key="key",
            litellm_base_url="https://relay.example.com/v1",
        )
    )
    assert isinstance(provider, LiteLLMForecastProvider)

    provider, _ = _ai(
        Settings(
            _env_file=None,
            mode="paper",
            ai_provider="openai",
            openai_api_key="key",
        )
    )
    assert isinstance(provider, OpenAIForecastProvider)

    provider, _ = _ai(
        Settings(
            _env_file=None,
            mode="paper",
            ai_provider="openai_compatible",
            ai_api_key="key",
            ai_base_url="https://relay.example.com/v1",
            litellm_base_url="https://relay.example.com/v1",
        )
    )
    assert isinstance(provider, OpenAICompatibleForecastProvider)


def test_ai_resolution_rejects_unknown_provider() -> None:
    settings = Settings(_env_file=None, mode="paper", ai_provider="unknown-provider")
    with pytest.raises(ValueError):
        _ai(settings)
