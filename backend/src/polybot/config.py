from __future__ import annotations

import ipaddress
import re
from base64 import urlsafe_b64encode
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from hashlib import sha256
from typing import Literal
from urllib.parse import SplitResult, urlsplit
from uuid import UUID

import idna
from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from polybot.ai_endpoint import UnsafeAIBaseURLError, normalize_ai_base_url


class TradingMode(StrEnum):
    PAPER = "paper"
    SHADOW = "shadow"
    CANARY = "canary"
    LIVE = "live"


LIVE_ACK_TEXT = "I_UNDERSTAND_REAL_FUNDS_CAN_BE_LOST"
BETA_SDK_ACK_TEXT = "I_ACCEPT_BETA_SDK_CANARY_ONLY"
DEDICATED_WALLET_ACK_TEXT = "I_CONFIRM_DEDICATED_WALLET_NO_EXTERNAL_FLOWS"
OFFICIAL_GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
DEFAULT_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"
_EVM_PRIVATE_KEY = re.compile(r"^(?:0[xX])?[0-9a-fA-F]{64}$")
_SECP256K1_ORDER = int(
    "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141",
    16,
)


def _canonical_evm_private_key(value: str) -> str:
    normalized = value.strip()
    if not _EVM_PRIVATE_KEY.fullmatch(normalized):
        raise ValueError("personal EVM private key must be a 32-byte hexadecimal value")
    canonical = normalized[2:] if normalized.lower().startswith("0x") else normalized
    canonical = canonical.lower()
    scalar = int(canonical, 16)
    if scalar == 0 or scalar >= _SECP256K1_ORDER:
        raise ValueError("personal EVM private key is outside the secp256k1 scalar range")
    return canonical


def _http_endpoint(value: str) -> SplitResult | None:
    if not value or any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    host = parsed.hostname.rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            idna.encode(host, uts46=True)
        except idna.IDNAError:
            return None
    return parsed


def _is_local_endpoint(endpoint: SplitResult) -> bool:
    host = endpoint.hostname
    if host is None:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        normalized = host.rstrip(".").lower()
        return normalized == "localhost" or "." not in normalized


class Settings(BaseSettings):
    """Runtime settings with capital-preserving defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="POLYBOT_",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    mode: TradingMode = TradingMode.PAPER
    component: Literal["all", "api", "worker"] = "all"
    worker_execution_model: Literal["single_account", "tenant_queue"] = "single_account"
    personal_mode: bool = False
    personal_auto_run: bool = True
    personal_live_enabled: bool = False
    tenant_worker_max_concurrency: int = Field(default=4, ge=1, le=32)
    tenant_job_lease_seconds: int = Field(default=60, ge=10, le=300)
    tenant_job_poll_seconds: float = Field(default=1.0, gt=0, le=30)
    account_id: str = DEFAULT_ACCOUNT_ID
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    worker_health_port: int = Field(
        default=8080,
        ge=1,
        le=65535,
        validation_alias=AliasChoices("PORT", "POLYBOT_WORKER_HEALTH_PORT"),
    )
    scan_interval_seconds: int = Field(default=60, ge=10)
    reconcile_interval_seconds: int = Field(default=30, ge=10, le=300)
    market_limit: int = Field(default=20, ge=1, le=200)
    max_ai_markets_per_cycle: int = Field(default=3, ge=1, le=20)
    ai_timeout_seconds: int = Field(default=45, ge=10, le=180)
    forecast_cooldown_seconds: int = Field(default=900, ge=60, le=86400)
    resolution_poll_seconds: int = Field(default=300, ge=30, le=3600)
    archive_enabled: bool = True
    archive_market_limit: int = Field(default=20, ge=1, le=100)
    archive_interval_seconds: int = Field(default=300, ge=60, le=86400)

    bankroll_usd: Decimal = Field(default=Decimal("1000"), gt=0)
    min_liquidity_usd: Decimal = Field(default=Decimal("10000"), ge=0)
    min_edge: Decimal = Field(default=Decimal("0.04"), ge=0, le=1)
    uncertainty_reserve: Decimal = Field(default=Decimal("0.01"), ge=0, le=1)
    max_order_usd: Decimal = Field(default=Decimal("5"), gt=0)
    max_trade_risk_pct: Decimal = Field(default=Decimal("0.005"), gt=0, le=1)
    max_event_exposure_pct: Decimal = Field(default=Decimal("0.02"), gt=0, le=1)
    max_bucket_exposure_pct: Decimal = Field(default=Decimal("0.05"), gt=0, le=1)
    max_gross_exposure_pct: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)
    daily_loss_limit_pct: Decimal = Field(default=Decimal("0.02"), gt=0, le=1)
    max_drawdown_pct: Decimal = Field(default=Decimal("0.08"), gt=0, le=1)
    kelly_fraction: Decimal = Field(default=Decimal("0.25"), gt=0, le=1)
    max_book_age_seconds: int = Field(default=5, ge=1, le=60)
    min_hours_to_resolution: int = Field(default=6, ge=0)
    min_forecast_confidence: Decimal = Field(default=Decimal("0.55"), ge=0, le=1)
    min_evidence_items: int = Field(default=2, ge=0, le=20)

    ai_provider: str = "mock"
    evidence_provider: Literal["auto", "openai_web", "gdelt", "none"] = "auto"
    forecast_model: str = "gpt-5.6-terra"
    critic_model: str = "gpt-5.6-sol"
    ai_fallback_providers: str = ""
    ai_api_key: SecretStr | None = None
    ai_base_url: str | None = None
    ai_model: str | None = None
    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "POLYBOT_OPENAI_API_KEY"),
    )
    litellm_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("LITELLM_API_KEY", "POLYBOT_LITELLM_API_KEY"),
    )
    litellm_base_url: str = "http://litellm:4000/v1"
    custom_ai_allowed_hosts: str = ""

    supabase_url: str | None = Field(
        default=None, validation_alias=AliasChoices("SUPABASE_URL", "POLYBOT_SUPABASE_URL")
    )
    supabase_service_role_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SUPABASE_SERVICE_ROLE_KEY", "POLYBOT_SUPABASE_SERVICE_ROLE_KEY"
        ),
    )

    polymarket_private_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "POLYMARKET_PRIVATE_KEY",
            "POLYBOT_PRIVATE_KEY",
            "EVM_PRIVATE_KEY",
            "POLYBOT_EVM_PRIVATE_KEY",
        ),
    )
    polymarket_deposit_wallet: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "POLYMARKET_DEPOSIT_WALLET", "POLYBOT_POLYMARKET_DEPOSIT_WALLET"
        ),
    )
    signed_payload_key: SecretStr | None = None
    payload_key_version: int = Field(default=1, ge=1)
    credential_public_key_pem: SecretStr | None = None
    credential_private_key_pem: SecretStr | None = None
    credential_private_keys_json: SecretStr | None = None
    credential_fingerprint_key: SecretStr | None = None
    geoblock_url: str = OFFICIAL_GEOBLOCK_URL
    auto_redeem_resolved: bool = False
    notify_webhook_url: str | None = None
    live_ack: str = ""
    beta_sdk_ack: str = ""
    dedicated_wallet_ack: str = ""
    admin_token: SecretStr | None = None
    dashboard_origins: str = ""

    @model_validator(mode="after")
    def validate_live_configuration(self) -> Settings:
        if self.personal_mode:
            if (
                not self.uses_supabase
                or self.supabase_service_role_key is None
                or not self.supabase_service_role_key.get_secret_value().strip()
            ):
                raise ValueError(
                    "POLYBOT_PERSONAL_MODE requires both SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY"
                )
            endpoint = _http_endpoint(self.supabase_url or "")
            if endpoint is None or endpoint.scheme.lower() != "https":
                raise ValueError(
                    "POLYBOT_PERSONAL_MODE requires SUPABASE_URL to be a valid HTTPS URL"
                )
            try:
                personal_account_id = UUID(self.account_id)
            except ValueError as exc:
                raise ValueError(
                    "POLYBOT_PERSONAL_MODE requires POLYBOT_ACCOUNT_ID to be the "
                    "owner's Supabase Auth user UUID"
                ) from exc
            if personal_account_id.int == 0 or str(personal_account_id) == DEFAULT_ACCOUNT_ID:
                raise ValueError(
                    "POLYBOT_PERSONAL_MODE requires POLYBOT_ACCOUNT_ID to match "
                    "the owner's real Supabase Auth user UUID"
                )
            if self.component != "all":
                raise ValueError(
                    "POLYBOT_PERSONAL_MODE requires POLYBOT_COMPONENT=all "
                    "(use SERVICE_ROLE=personal in Docker)"
                )
            if self.worker_execution_model != "single_account":
                raise ValueError(
                    "POLYBOT_PERSONAL_MODE requires POLYBOT_WORKER_EXECUTION_MODEL=single_account"
                )
            if (
                self.mode in {TradingMode.CANARY, TradingMode.LIVE}
                and not self.personal_live_enabled
            ):
                raise ValueError("personal canary/live requires POLYBOT_PERSONAL_LIVE_ENABLED=true")
            if self.polymarket_private_key is not None:
                canonical_key = _canonical_evm_private_key(
                    self.polymarket_private_key.get_secret_value()
                )
                self.polymarket_private_key = SecretStr("0x" + canonical_key)
            if self.ai_model and self.ai_model.strip():
                model = self.ai_model.strip()
                self.forecast_model = model
                self.critic_model = model
            if self.ai_base_url and self.ai_base_url.strip():
                self.litellm_base_url = self.ai_base_url.strip()
                if self.ai_provider.lower() == "mock":
                    self.ai_provider = "openai_compatible"
            elif (
                self.ai_api_key is not None or self.openai_api_key is not None
            ) and self.ai_provider.lower() == "mock":
                self.ai_provider = "openai"
        if self.component == "api":
            forbidden = {
                "POLYMARKET_PRIVATE_KEY": self.polymarket_private_key,
                "POLYBOT_SIGNED_PAYLOAD_KEY": self.signed_payload_key,
                "POLYBOT_CREDENTIAL_PRIVATE_KEY_PEM": self.credential_private_key_pem,
                "POLYBOT_CREDENTIAL_PRIVATE_KEYS_JSON": (self.credential_private_keys_json),
                "OPENAI_API_KEY": self.openai_api_key,
                "LITELLM_API_KEY": self.litellm_api_key,
                "POLYBOT_AI_API_KEY": self.ai_api_key,
            }
            exposed = [
                name
                for name, value in forbidden.items()
                if value is not None and bool(value.get_secret_value())
            ]
            if exposed:
                raise ValueError(
                    "control API must not receive signer secrets: " + ", ".join(exposed)
                )
        if self.uses_supabase:
            try:
                self.account_id = str(UUID(self.account_id))
            except ValueError as exc:
                raise ValueError("POLYBOT_ACCOUNT_ID must be a Supabase Auth UUID") from exc
        if self.credential_private_key_pem and self.credential_private_keys_json:
            raise ValueError(
                "configure exactly one of POLYBOT_CREDENTIAL_PRIVATE_KEY_PEM "
                "or POLYBOT_CREDENTIAL_PRIVATE_KEYS_JSON"
            )
        provider_name = self.ai_provider.lower()
        if provider_name == "litellm":
            endpoint = _http_endpoint(self.litellm_base_url)
            if endpoint is None:
                raise ValueError("POLYBOT_LITELLM_BASE_URL must be a valid HTTP(S) endpoint")
            if endpoint.scheme.lower() != "https" and not _is_local_endpoint(endpoint):
                raise ValueError("remote POLYBOT_LITELLM_BASE_URL must use HTTPS")
        elif provider_name == "openai_compatible":
            try:
                self.litellm_base_url = normalize_ai_base_url(self.litellm_base_url)
            except UnsafeAIBaseURLError as exc:
                raise ValueError(
                    "POLYBOT_LITELLM_BASE_URL must be a safe public HTTPS endpoint"
                ) from exc
            if self.personal_mode and self.ai_base_url and not self.custom_ai_allowed_hosts.strip():
                hostname = urlsplit(self.litellm_base_url).hostname
                if hostname is None:
                    raise ValueError("personal AI Base URL is missing a hostname")
                self.custom_ai_allowed_hosts = hostname
        if self.mode in {TradingMode.CANARY, TradingMode.LIVE}:
            missing: list[str] = []
            if not self.personal_mode:
                if self.live_ack != LIVE_ACK_TEXT:
                    missing.append("POLYBOT_LIVE_ACK")
                if self.beta_sdk_ack != BETA_SDK_ACK_TEXT:
                    missing.append("POLYBOT_BETA_SDK_ACK")
            tenant_queue_worker = (
                self.component == "worker" and self.worker_execution_model == "tenant_queue"
            )
            if self.component in {"all", "worker"}:
                if (
                    not self.personal_mode
                    and self.dedicated_wallet_ack != DEDICATED_WALLET_ACK_TEXT
                ):
                    missing.append("POLYBOT_DEDICATED_WALLET_ACK")
                if self.resolved_signed_payload_key is None:
                    missing.append("POLYBOT_SIGNED_PAYLOAD_KEY")
                if tenant_queue_worker:
                    if (
                        self.credential_private_key_pem is None
                        and self.credential_private_keys_json is None
                    ):
                        missing.append(
                            "POLYBOT_CREDENTIAL_PRIVATE_KEY_PEM/"
                            "POLYBOT_CREDENTIAL_PRIVATE_KEYS_JSON"
                        )
                else:
                    if self.polymarket_private_key is None:
                        missing.append("POLYMARKET_PRIVATE_KEY")
                    if self.evidence_provider == "none":
                        missing.append("POLYBOT_EVIDENCE_PROVIDER")
                    provider = provider_name
                    if provider == "mock":
                        missing.append("POLYBOT_AI_PROVIDER cannot be mock")
                    elif provider == "openai":
                        if self.effective_ai_api_key is None:
                            missing.append("OPENAI_API_KEY/POLYBOT_AI_API_KEY")
                    elif provider in {"litellm", "openai_compatible"}:
                        if self.effective_ai_api_key is None:
                            missing.append("LITELLM_API_KEY/POLYBOT_AI_API_KEY")
                        if provider == "litellm":
                            endpoint = _http_endpoint(self.litellm_base_url)
                            if endpoint is None or endpoint.scheme.lower() != "https":
                                missing.append("POLYBOT_LITELLM_BASE_URL must use HTTPS")
                    else:
                        missing.append("POLYBOT_AI_PROVIDER")
                    evidence_name = (
                        "openai_web"
                        if self.evidence_provider == "auto" and provider == "openai"
                        else "gdelt"
                        if self.evidence_provider == "auto"
                        and provider in {"litellm", "openai_compatible"}
                        else self.evidence_provider
                    )
                    if evidence_name == "openai_web" and self.effective_ai_api_key is None:
                        missing.append("OPENAI_API_KEY/POLYBOT_AI_API_KEY for web evidence")
            if not self.uses_supabase:
                missing.append("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY")
            elif not self.supabase_url.lower().startswith("https://"):
                missing.append("SUPABASE_URL must use HTTPS in real-money modes")
            if self.geoblock_url.rstrip("/") != OFFICIAL_GEOBLOCK_URL:
                missing.append("POLYBOT_GEOBLOCK_URL must remain the official endpoint")
            if self.min_evidence_items < 2:
                missing.append("POLYBOT_MIN_EVIDENCE_ITEMS must be at least 2")
            if missing:
                joined = ", ".join(missing)
                raise ValueError(f"real-money mode is locked; missing/invalid: {joined}")
            if self.mode is TradingMode.CANARY and self.max_order_usd > Decimal("5"):
                raise ValueError("canary mode hard-caps POLYBOT_MAX_ORDER_USD at 5 pUSD")
        return self

    def validated_copy(self, **updates: object) -> Settings:
        """Rebuild settings through validation instead of Pydantic's unchecked model_copy."""

        return type(self).model_validate({**self.model_dump(), **updates})

    @property
    def effective_ai_api_key(self) -> SecretStr | None:
        """Return the provider key while supporting the simple personal-mode alias."""

        if self.ai_api_key is not None and self.ai_api_key.get_secret_value():
            return self.ai_api_key
        if self.ai_provider.lower() == "openai":
            return self.openai_api_key or self.litellm_api_key
        if self.ai_provider.lower() in {"litellm", "openai_compatible"}:
            return self.litellm_api_key or self.openai_api_key
        return None

    @property
    def resolved_signed_payload_key(self) -> SecretStr | None:
        """Resolve the durable-order cipher key for a personal single-wallet runtime.

        A dedicated configured key remains authoritative. Personal mode can derive a
        stable, domain-separated Fernet key from its high-entropy wallet key so a
        second deployment secret is not required. Changing the wallet invalidates
        outstanding encrypted payloads and therefore still requires a clean handoff.
        """

        if self.signed_payload_key is not None and self.signed_payload_key.get_secret_value():
            return self.signed_payload_key
        if not self.personal_mode or self.polymarket_private_key is None:
            return None
        private_key = _canonical_evm_private_key(self.polymarket_private_key.get_secret_value())
        digest = sha256(
            b"polybot-personal-signed-payload:v1\x00" + private_key.encode("ascii")
        ).digest()
        return SecretStr(urlsafe_b64encode(digest).decode("ascii"))

    @property
    def uses_supabase(self) -> bool:
        return bool(
            self.supabase_url
            and self.supabase_service_role_key
            and self.supabase_service_role_key.get_secret_value()
        )

    @property
    def allowed_dashboard_origins(self) -> list[str]:
        result: list[str] = []
        for item in self.dashboard_origins.split(","):
            endpoint = _http_endpoint(item.strip())
            if (
                endpoint is None
                or endpoint.path not in {"", "/"}
                or (endpoint.scheme.lower() != "https" and not _is_local_endpoint(endpoint))
            ):
                continue
            origin = f"{endpoint.scheme.lower()}://{endpoint.netloc}".rstrip("/")
            if origin not in result:
                result.append(origin)
        return result


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
