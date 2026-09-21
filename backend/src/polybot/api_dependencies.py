"""FastAPI dependencies shared by every route in :mod:`polybot.api`.

Split out of :mod:`polybot.api`: authentication, the fail-closed control-plane
placeholders, the public status projection, and the dependency aliases the
route table wires into ``create_app``. ``polybot.api`` re-exports them.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status

from polybot.api_models import (
    PersonalAIStatus,
    PersonalCycleStatus,
    PersonalReconciliationStatus,
    PersonalStatusResponse,
    PersonalWalletStatus,
)
from polybot.auth import (
    AuthPrincipal,
    JWTVerificationError,
    SupabaseJWTVerifier,
    TokenVerifier,
)
from polybot.config import Settings, TradingMode
from polybot.credentials import (
    CredentialConfigurationError,
    CredentialRepository,
    CredentialService,
    InMemoryCredentialRepository,
    SupabaseCredentialRepository,
    build_api_encryptor_from_environment,
    fingerprint_key_from_environment,
)
from polybot.jobs import (
    InMemoryJobRepository,
    JobRepository,
    SupabaseJobRepository,
)
from polybot.models import utc_now
from polybot.personal_execution import (
    PersonalRuntimeBinding,
)
from polybot.runtime_mode import resolve_effective_mode
from polybot.stores.supabase_client import create_supabase_client
from polybot.worker import is_worker_ready


class ControlPlaneUnavailable(RuntimeError):
    pass


class DisabledControlPlane:
    """Fail-closed placeholder used when durable Supabase storage is absent."""

    def __init__(self, reason: str):
        self.reason = reason

    async def health(self) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        async def unavailable(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise ControlPlaneUnavailable(self.reason)

        return unavailable


def _personal_status(
    settings: Settings,
    *,
    binding: PersonalRuntimeBinding | None = None,
    cycle_count: int = 0,
    last_cycle: dict[str, object] | None = None,
    reconciliation: PersonalReconciliationStatus | None = None,
    desired_mode: TradingMode | None = None,
) -> PersonalStatusResponse:
    provider = settings.ai_provider.lower()
    ai_configured = provider != "mock" and settings.effective_ai_api_key is not None
    wallet_configured = settings.polymarket_private_key is not None
    base_url = settings.litellm_base_url if provider in {"litellm", "openai_compatible"} else None
    decision = resolve_effective_mode(
        desired_mode if desired_mode is not None else settings.mode,
        live_enabled=settings.personal_live_enabled,
        signer_configured=wallet_configured,
    )
    worker_ready = settings.personal_mode and is_worker_ready()
    real_money = decision.effective in {TradingMode.CANARY, TradingMode.LIVE}
    readiness_fresh = bool(
        binding is not None
        and binding.readiness_checked_at is not None
        and binding.readiness_checked_at >= utc_now() - timedelta(minutes=15)
    )
    live_wallet_ready = bool(
        binding is not None
        and not binding.paused
        and binding.allowances_ready
        and binding.collateral_balance_pusd is not None
        and binding.collateral_balance_pusd > 0
        and readiness_fresh
    )
    credentials_ready = ai_configured and (
        wallet_configured and live_wallet_ready if real_money else True
    )
    return PersonalStatusResponse(
        enabled=settings.personal_mode,
        live_supported=settings.personal_live_enabled,
        mode=decision.effective,
        desired_mode=decision.desired,
        mode_note=decision.note,
        auto_run_enabled=settings.personal_mode
        and settings.personal_auto_run,
        worker_execution_model=settings.worker_execution_model,
        worker_ready=worker_ready,
        ready=worker_ready and credentials_ready,
        cycle_count=cycle_count,
        last_cycle=(
            PersonalCycleStatus.model_validate(last_cycle) if last_cycle is not None else None
        ),
        ai=PersonalAIStatus(
            configured=ai_configured,
            provider=provider,
            base_url=base_url,
            forecast_model=settings.forecast_model,
            critic_model=settings.critic_model,
        ),
        wallet=PersonalWalletStatus(
            configured=wallet_configured,
            address=(
                binding.deposit_wallet_address
                if binding is not None
                else settings.polymarket_deposit_wallet
            ),
            bound=binding is not None,
            signer_address=binding.signer_address if binding is not None else None,
            chain_id=binding.chain_id if binding is not None else None,
            collateral_token=binding.collateral_token if binding is not None else None,
            binding_version=(binding.binding_version if binding is not None else None),
            paused=binding.paused if binding is not None else False,
            collateral_balance_pusd=(
                str(binding.collateral_balance_pusd)
                if binding is not None and binding.collateral_balance_pusd is not None
                else None
            ),
            allowances_ready=(binding.allowances_ready if binding is not None else False),
            readiness_checked_at=(binding.readiness_checked_at if binding is not None else None),
        ),
        reconciliation=reconciliation or PersonalReconciliationStatus(),
    )


def _reconcile_baseline_conflict(message: str) -> str:
    """Translate the durable RPC refusal into operator-actionable Chinese."""

    lowered = message.lower()
    if "armed" in lowered:
        return (
            "当前仍处于实盘 armed 状态，不能重置对账基准。"
            "请在控制台解除武装并等撤单确认完成后再试。"
        )
    if "non-terminal" in lowered:
        return "存在未成交或未确认的订单，不能重置对账基准。请先撤单并等待订单进入终态。"
    if "invalid personal reconcile baseline" in lowered:
        return "对账基准时间无效：必须是不晚于当前时间的时间戳。"
    return "重置对账基准失败：请确认机器人已解除武装且没有未结订单后重试。"


def _record_personal_cycle(
    application: FastAPI,
    snapshot: dict[str, object],
) -> None:
    previous = application.state.personal_last_cycle
    state = snapshot.get("state")
    if state in {"succeeded", "failed"} and (
        not isinstance(previous, dict)
        or previous.get("id") != snapshot.get("id")
        or previous.get("state") == "running"
    ):
        application.state.personal_cycle_count += 1
    application.state.personal_last_cycle = snapshot


def _build_verifier(settings: Settings) -> TokenVerifier | None:
    if not settings.supabase_url:
        return None
    issuer = os.getenv(
        "POLYBOT_SUPABASE_JWT_ISSUER",
        f"{settings.supabase_url.rstrip('/')}/auth/v1",
    )
    audience_values = tuple(
        item.strip()
        for item in os.getenv("POLYBOT_SUPABASE_JWT_AUDIENCE", "authenticated").split(",")
        if item.strip()
    )
    jwks_url = os.getenv("POLYBOT_SUPABASE_JWKS_URL")
    return SupabaseJWTVerifier(
        issuer=issuer,
        audience=audience_values,
        jwks_url=jwks_url,
    )


def _build_repositories(
    settings: Settings,
) -> tuple[JobRepository, CredentialRepository]:
    if settings.uses_supabase:
        assert settings.supabase_url is not None
        assert settings.supabase_service_role_key is not None
        client = create_supabase_client(
            settings.supabase_url,
            settings.supabase_service_role_key.get_secret_value(),
            timeout_seconds=settings.supabase_timeout_seconds,
        )
        return SupabaseJobRepository(client), SupabaseCredentialRepository(client)
    allow_memory = os.getenv("POLYBOT_ALLOW_INMEMORY_CONTROL", "").lower() in {
        "1",
        "true",
        "yes",
    }
    if (
        allow_memory
        and settings.mode is TradingMode.PAPER
        and settings.component == "all"
        and not settings.supabase_url
        and settings.supabase_service_role_key is None
    ):
        return InMemoryJobRepository(), InMemoryCredentialRepository()
    disabled = DisabledControlPlane(
        "durable Supabase control storage is required; in-memory tenancy is disabled"
    )
    return disabled, disabled  # type: ignore[return-value]


def _build_credential_service(
    repository: CredentialRepository,
) -> tuple[CredentialService | None, str | None]:
    try:
        service = CredentialService(
            repository,
            build_api_encryptor_from_environment(),
            fingerprint_key=fingerprint_key_from_environment(),
        )
    except CredentialConfigurationError as exc:
        return None, str(exc)
    return service, None


async def current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthPrincipal:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[7:]
    if not token or token != token.strip():
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    verifier: TokenVerifier | None = request.app.state.auth_verifier
    if verifier is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Supabase authentication is not configured",
        )
    try:
        principal = await verifier.verify(token)
    except JWTVerificationError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    settings: Settings = request.app.state.settings
    if settings.personal_mode and principal.account_id != settings.account_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "this personal deployment is bound to a different Supabase account",
        )
    return principal


PrincipalDep = Annotated[AuthPrincipal, Depends(current_principal)]


async def aal2_principal(principal: PrincipalDep) -> AuthPrincipal:
    if not principal.is_aal2:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "AAL2 or recent MFA verification is required",
        )
    return principal


AAL2PrincipalDep = Annotated[AuthPrincipal, Depends(aal2_principal)]


def job_repository(request: Request) -> JobRepository:
    return request.app.state.job_repository


def credential_service(request: Request) -> CredentialService:
    service: CredentialService | None = request.app.state.credential_service
    if service is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "secure credential ingestion is not configured",
        )
    return service


def credential_repository(request: Request) -> CredentialRepository:
    return request.app.state.credential_repository


JobRepositoryDep = Annotated[JobRepository, Depends(job_repository)]
CredentialServiceDep = Annotated[CredentialService, Depends(credential_service)]
CredentialRepositoryDep = Annotated[
    CredentialRepository,
    Depends(credential_repository),
]
