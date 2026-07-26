from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from polybot.config import (
    BETA_SDK_ACK_TEXT,
    DEDICATED_WALLET_ACK_TEXT,
    LIVE_ACK_TEXT,
    Settings,
    TradingMode,
)


def test_defaults_are_non_trading() -> None:
    value = Settings(_env_file=None)
    assert value.mode is TradingMode.PAPER
    assert value.ai_provider == "mock"
    assert value.live_ack == ""


def test_live_mode_is_locked_without_every_secret() -> None:
    with pytest.raises(ValidationError, match="real-money mode is locked"):
        Settings(_env_file=None, mode="live")


def test_canary_has_non_configurable_five_dollar_ceiling() -> None:
    with pytest.raises(ValidationError, match="hard-caps"):
        Settings(
            _env_file=None,
            mode="canary",
            max_order_usd=Decimal("6"),
            live_ack=LIVE_ACK_TEXT,
            beta_sdk_ack=BETA_SDK_ACK_TEXT,
            dedicated_wallet_ack=DEDICATED_WALLET_ACK_TEXT,
            polymarket_private_key="0xdeadbeef",
            polymarket_deposit_wallet="0x0000000000000000000000000000000000000001",
            signed_payload_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            ai_provider="openai",
            openai_api_key="test-key",
            admin_token="admin",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="service-role-test",
        )


def test_live_api_component_does_not_require_signer_secrets() -> None:
    settings = Settings(
        _env_file=None,
        mode="canary",
        component="api",
        live_ack=LIVE_ACK_TEXT,
        beta_sdk_ack=BETA_SDK_ACK_TEXT,
        admin_token="admin",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
    )
    assert settings.polymarket_private_key is None


def test_canary_worker_accepts_one_litellm_key_and_derives_wallet() -> None:
    settings = Settings(
        _env_file=None,
        mode="canary",
        component="worker",
        live_ack=LIVE_ACK_TEXT,
        beta_sdk_ack=BETA_SDK_ACK_TEXT,
        dedicated_wallet_ack=DEDICATED_WALLET_ACK_TEXT,
        ai_provider="litellm",
        litellm_api_key="third-party-key",
        litellm_base_url="https://gateway.example/v1",
        polymarket_private_key="0xdeadbeef",
        signed_payload_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
    )

    assert settings.evidence_provider == "auto"
    assert settings.polymarket_deposit_wallet is None


def test_canary_worker_rejects_plaintext_remote_ai_endpoint() -> None:
    with pytest.raises(ValidationError, match="must use HTTPS"):
        Settings(
            _env_file=None,
            mode="canary",
            component="worker",
            live_ack=LIVE_ACK_TEXT,
            beta_sdk_ack=BETA_SDK_ACK_TEXT,
            dedicated_wallet_ack=DEDICATED_WALLET_ACK_TEXT,
            ai_provider="litellm",
            litellm_api_key="third-party-key",
            litellm_base_url="http://gateway.example/v1",
            polymarket_private_key="0xdeadbeef",
            signed_payload_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="service-role-test",
        )


def test_litellm_rejects_malformed_base_url_in_every_mode() -> None:
    with pytest.raises(ValidationError, match="valid HTTP"):
        Settings(
            _env_file=None,
            mode="paper",
            ai_provider="litellm",
            litellm_api_key="third-party-key",
            litellm_base_url="https://",
        )


def test_litellm_rejects_plaintext_remote_base_url_in_paper_mode() -> None:
    with pytest.raises(ValidationError, match="remote.*HTTPS"):
        Settings(
            _env_file=None,
            mode="paper",
            ai_provider="litellm",
            litellm_api_key="third-party-key",
            litellm_base_url="http://gateway.example/v1",
        )

    local = Settings(
        _env_file=None,
        mode="paper",
        ai_provider="litellm",
        litellm_api_key="third-party-key",
        litellm_base_url="http://litellm:4000/v1",
    )
    assert local.litellm_base_url.startswith("http://litellm:")


def test_canary_worker_rejects_mock_ai() -> None:
    with pytest.raises(ValidationError, match="cannot be mock"):
        Settings(
            _env_file=None,
            mode="canary",
            component="worker",
            live_ack=LIVE_ACK_TEXT,
            beta_sdk_ack=BETA_SDK_ACK_TEXT,
            dedicated_wallet_ack=DEDICATED_WALLET_ACK_TEXT,
            polymarket_private_key="0xdeadbeef",
            signed_payload_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="service-role-test",
        )


def test_canary_worker_requires_dedicated_wallet_acknowledgement() -> None:
    with pytest.raises(ValidationError, match="POLYBOT_DEDICATED_WALLET_ACK"):
        Settings(
            _env_file=None,
            mode="canary",
            component="worker",
            live_ack=LIVE_ACK_TEXT,
            beta_sdk_ack=BETA_SDK_ACK_TEXT,
            ai_provider="openai",
            openai_api_key="test-key",
            polymarket_private_key="0xdeadbeef",
            signed_payload_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="service-role-test",
        )
