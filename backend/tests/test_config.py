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


def test_control_api_rejects_accidentally_injected_signer_secrets() -> None:
    with pytest.raises(ValidationError, match="control API must not receive signer secrets"):
        Settings(
            _env_file=None,
            component="api",
            polymarket_private_key="0xdeadbeef",
            signed_payload_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        )


def test_control_api_rejects_worker_only_provider_and_decryption_secrets() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(
            _env_file=None,
            component="api",
            openai_api_key="sk-must-live-in-worker",
        )

    with pytest.raises(ValidationError, match="CREDENTIAL_PRIVATE_KEY_PEM"):
        Settings(
            _env_file=None,
            component="api",
            credential_private_key_pem="worker-only-private-key",
        )


def test_tenant_queue_worker_uses_encrypted_tenant_inputs_instead_of_global_keys() -> None:
    settings = Settings(
        _env_file=None,
        mode="canary",
        component="worker",
        worker_execution_model="tenant_queue",
        live_ack=LIVE_ACK_TEXT,
        beta_sdk_ack=BETA_SDK_ACK_TEXT,
        dedicated_wallet_ack=DEDICATED_WALLET_ACK_TEXT,
        signed_payload_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        credential_private_key_pem="worker-only-private-key",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
    )

    assert settings.polymarket_private_key is None
    assert settings.openai_api_key is None
    assert settings.worker_execution_model == "tenant_queue"


def test_validated_copy_reapplies_endpoint_security_validation() -> None:
    settings = Settings(_env_file=None)

    with pytest.raises(ValidationError, match="remote.*HTTPS"):
        settings.validated_copy(
            ai_provider="litellm",
            litellm_api_key="third-party-key",
            litellm_base_url="http://gateway.example/v1",
        )


def test_dashboard_origins_are_exact_origins_without_paths_or_wildcards() -> None:
    settings = Settings(
        _env_file=None,
        dashboard_origins=(
            "https://dashboard.example, https://dashboard.example/, "
            "https://dashboard.example/path, http://localhost:3000, "
            "http://remote.example, https://*.example"
        ),
    )

    assert settings.allowed_dashboard_origins == [
        "https://dashboard.example",
        "http://localhost:3000",
    ]


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
