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

PERSONAL_ACCOUNT_ID = "11111111-1111-4111-8111-111111111111"


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


def test_openai_compatible_provider_requires_safe_https_base_url() -> None:
    settings = Settings(
        _env_file=None,
        ai_provider="openai_compatible",
        litellm_api_key="relay-key",
        litellm_base_url="https://Relay.Example.com:443/v1/",
    )
    assert settings.litellm_base_url == "https://relay.example.com/v1"

    with pytest.raises(ValidationError, match="safe public HTTPS"):
        Settings(
            _env_file=None,
            ai_provider="openai_compatible",
            litellm_api_key="relay-key",
            litellm_base_url="http://relay.example.com/v1",
        )


def test_personal_mode_accepts_simple_ai_and_wallet_environment_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLYBOT_PERSONAL_MODE", "true")
    monkeypatch.setenv("POLYBOT_COMPONENT", "all")
    monkeypatch.setenv("POLYBOT_ACCOUNT_ID", PERSONAL_ACCOUNT_ID)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-test")
    monkeypatch.setenv("POLYBOT_AI_API_KEY", "personal-relay-key")
    monkeypatch.setenv("POLYBOT_AI_BASE_URL", "https://relay.example.com/v1/")
    monkeypatch.setenv("POLYBOT_AI_MODEL", "relay-model-v1")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0X" + ("12" * 32))

    settings = Settings(_env_file=None)

    assert settings.personal_mode is True
    assert settings.ai_provider == "openai_compatible"
    assert settings.litellm_base_url == "https://relay.example.com/v1"
    assert settings.forecast_model == "relay-model-v1"
    assert settings.critic_model == "relay-model-v1"
    assert settings.effective_ai_api_key is not None
    assert settings.polymarket_private_key is not None
    assert settings.polymarket_private_key.get_secret_value() == "0x" + ("12" * 32)
    assert settings.custom_ai_allowed_hosts == "relay.example.com"


def test_personal_mode_accepts_standard_openai_key_without_provider_setting() -> None:
    settings = Settings(
        _env_file=None,
        personal_mode=True,
        component="all",
        account_id=PERSONAL_ACCOUNT_ID,
        openai_api_key="standard-openai-key",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
    )

    assert settings.ai_provider == "openai"
    assert settings.effective_ai_api_key is not None
    assert settings.effective_ai_api_key.get_secret_value() == "standard-openai-key"


def test_personal_payload_key_is_stable_across_evm_key_prefix_and_case() -> None:
    bare_key = "12" * 32
    first = Settings(
        _env_file=None,
        personal_mode=True,
        component="all",
        account_id=PERSONAL_ACCOUNT_ID,
        polymarket_private_key=bare_key,
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
    )
    second = Settings(
        _env_file=None,
        personal_mode=True,
        component="all",
        account_id=PERSONAL_ACCOUNT_ID,
        polymarket_private_key="0X" + bare_key.upper(),
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
    )

    assert first.resolved_signed_payload_key is not None
    assert second.resolved_signed_payload_key is not None
    assert (
        first.resolved_signed_payload_key.get_secret_value()
        == second.resolved_signed_payload_key.get_secret_value()
    )


def test_personal_mode_rejects_invalid_wallet_and_real_money_modes() -> None:
    with pytest.raises(ValidationError, match="both SUPABASE_URL"):
        Settings(
            _env_file=None,
            personal_mode=True,
            component="all",
            account_id=PERSONAL_ACCOUNT_ID,
            supabase_url="https://example.supabase.co",
        )

    with pytest.raises(ValidationError, match="valid HTTPS URL"):
        Settings(
            _env_file=None,
            personal_mode=True,
            component="all",
            account_id=PERSONAL_ACCOUNT_ID,
            supabase_url="http://example.supabase.co",
            supabase_service_role_key="service-role-test",
        )

    with pytest.raises(ValidationError, match="Supabase Auth user UUID"):
        Settings(
            _env_file=None,
            personal_mode=True,
            component="all",
            account_id="replace-me",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="service-role-test",
        )

    with pytest.raises(ValidationError, match="POLYBOT_ACCOUNT_ID"):
        Settings(
            _env_file=None,
            personal_mode=True,
            component="all",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="service-role-test",
        )

    with pytest.raises(ValidationError, match="real Supabase Auth user UUID"):
        Settings(
            _env_file=None,
            personal_mode=True,
            component="all",
            account_id="00000000-0000-0000-0000-000000000000",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="service-role-test",
        )

    with pytest.raises(ValidationError, match="32-byte hexadecimal"):
        Settings(
            _env_file=None,
            personal_mode=True,
            component="all",
            account_id=PERSONAL_ACCOUNT_ID,
            polymarket_private_key="not-a-private-key",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="service-role-test",
        )

    with pytest.raises(ValidationError, match="POLYBOT_PERSONAL_LIVE_ENABLED=true"):
        Settings(
            _env_file=None,
            personal_mode=True,
            component="all",
            account_id=PERSONAL_ACCOUNT_ID,
            mode="canary",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="service-role-test",
        )


def test_personal_canary_uses_one_live_switch_and_keeps_hard_gates() -> None:
    settings = Settings(
        _env_file=None,
        personal_mode=True,
        personal_live_enabled=True,
        component="all",
        account_id=PERSONAL_ACCOUNT_ID,
        mode="canary",
        ai_api_key="personal-live-ai-key",
        ai_base_url="https://relay.example.com/v1",
        ai_model="relay-model-v1",
        polymarket_private_key="12" * 32,
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
    )

    assert settings.mode is TradingMode.CANARY
    assert settings.live_ack == ""
    assert settings.beta_sdk_ack == ""
    assert settings.dedicated_wallet_ack == ""
    assert settings.resolved_signed_payload_key is not None

    with pytest.raises(ValidationError) as locked:
        Settings(
            _env_file=None,
            personal_mode=True,
            personal_live_enabled=True,
            component="all",
            account_id=PERSONAL_ACCOUNT_ID,
            mode="live",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="service-role-test",
            geoblock_url="https://example.com/not-the-official-gate",
            min_evidence_items=1,
        )
    locked_message = str(locked.value)
    assert "POLYMARKET_PRIVATE_KEY" in locked_message
    assert "POLYBOT_AI_PROVIDER cannot be mock" in locked_message
    assert "POLYBOT_GEOBLOCK_URL" in locked_message
    assert "POLYBOT_MIN_EVIDENCE_ITEMS" in locked_message

    with pytest.raises(ValidationError, match="both SUPABASE_URL"):
        Settings(
            _env_file=None,
            personal_mode=True,
            personal_live_enabled=True,
            component="all",
            account_id=PERSONAL_ACCOUNT_ID,
            mode="canary",
            ai_api_key="personal-live-ai-key",
            polymarket_private_key="12" * 32,
        )

    with pytest.raises(ValidationError, match="hard-caps"):
        Settings(
            _env_file=None,
            personal_mode=True,
            personal_live_enabled=True,
            component="all",
            account_id=PERSONAL_ACCOUNT_ID,
            mode="canary",
            max_order_usd=Decimal("6"),
            ai_api_key="personal-live-ai-key",
            polymarket_private_key="12" * 32,
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="service-role-test",
        )


def test_control_api_rejects_simple_personal_ai_alias_without_personal_role() -> None:
    with pytest.raises(ValidationError, match="POLYBOT_AI_API_KEY"):
        Settings(
            _env_file=None,
            component="api",
            ai_api_key="must-not-enter-public-api",
        )
