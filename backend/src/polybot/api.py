from __future__ import annotations

import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from polybot.ai_endpoint import UnsafeAIBaseURLError, validate_public_ai_base_url
from polybot.auth import (
    AuthPrincipal,
    JWTVerificationError,
    SupabaseJWTVerifier,
    TokenVerifier,
)
from polybot.config import Settings, TradingMode, get_settings
from polybot.credentials import (
    AIProvider,
    CredentialConfigurationError,
    CredentialConflictError,
    CredentialMetadata,
    CredentialRepository,
    CredentialService,
    CredentialStatus,
    InMemoryCredentialRepository,
    SupabaseCredentialRepository,
    TradingWalletMetadata,
    build_api_encryptor_from_environment,
    fingerprint_key_from_environment,
)
from polybot.jobs import (
    AccountNotReadyError,
    AIBudgetRequest,
    AIDiagnosticJob,
    CycleJob,
    CycleJobRequest,
    InMemoryJobRepository,
    JobConflictError,
    JobRepository,
    PerformanceSnapshot,
    PortfolioSnapshot,
    RiskPolicySnapshot,
    RiskPresetRequest,
    RuntimeProfile,
    RuntimeProfilePatch,
    SupabaseJobRepository,
    WorkerStatusSnapshot,
    arm_expiry,
)
from polybot.security_logging import configure_secure_logging, safe_json
from supabase import create_client

LOGGER = logging.getLogger(__name__)
_LEGACY_SECRET_HEADERS = frozenset(
    {
        "x-evm-key",
        "x-api-key",
        "x-base-url",
        "x-forecast-model",
    }
)
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:@+-]{16,128}$")


class RequestRateLimiter:
    """Small per-process abuse brake; Supabase Auth remains the identity authority."""

    def __init__(self) -> None:
        self._windows: dict[str, tuple[float, int]] = {}

    def allow(self, key: str, *, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
        now = time.monotonic()
        started, hits = self._windows.get(key, (0.0, 0))
        if now - started >= window_seconds:
            started, hits = now, 0
        hits += 1
        self._windows[key] = (started, hits)
        if len(self._windows) > 8192:
            cutoff = now - window_seconds
            self._windows = {
                item_key: value for item_key, value in self._windows.items() if value[0] >= cutoff
            }
        retry_after = max(1, int(window_seconds - (now - started)))
        return hits <= limit, retry_after


def _rate_limit_identity(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    source = authorization if authorization.startswith("Bearer ") else ""
    if not source:
        source = request.client.host if request.client else "unknown"
    return sha256(source.encode("utf-8", errors="ignore")).hexdigest()


class AIKeyPutRequest(BaseModel):
    provider: AIProvider
    api_key: SecretStr = Field(min_length=16, max_length=512)
    label: str | None = Field(default=None, min_length=1, max_length=80)


class WalletProvisionRequest(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    owner_address: str | None = None


class WalletImportRequest(WalletProvisionRequest):
    private_key: SecretStr = Field(min_length=64, max_length=66)
    signature_type: int = Field(default=3, ge=0, le=3)
    confirm_standard_allowances: Literal[True]


class ArmRequest(BaseModel):
    mode: TradingMode
    minutes: int = Field(default=5, ge=1, le=15)
    expected_version: int = Field(ge=1)


class MeResponse(BaseModel):
    account_id: UUID
    aal: str
    runtime_profile: RuntimeProfile
    capabilities: dict[str, bool]


class PublicCredentialMetadata(BaseModel):
    id: UUID
    kind: str
    provider: str
    label: str | None = None
    status: str
    version: int
    created_at: datetime
    rotated_at: datetime | None = None
    revoked_at: datetime | None = None


class PublicCredentialStatus(BaseModel):
    ai_credentials: list[PublicCredentialMetadata]
    wallets: list[TradingWalletMetadata]


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
        client = create_client(
            settings.supabase_url,
            settings.supabase_service_role_key.get_secret_value(),
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
        return await verifier.verify(token)
    except JWTVerificationError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


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


def create_app(
    *,
    settings: Settings | None = None,
    auth_verifier: TokenVerifier | None = None,
    jobs: JobRepository | None = None,
    credentials: CredentialRepository | None = None,
    credential_writer: CredentialService | None = None,
) -> FastAPI:
    api_settings = settings or get_settings()
    default_jobs: JobRepository
    default_credentials: CredentialRepository
    if jobs is None or credentials is None:
        default_jobs, default_credentials = _build_repositories(api_settings)
    else:
        default_jobs, default_credentials = jobs, credentials
    selected_jobs = jobs or default_jobs
    selected_credentials = credentials or default_credentials
    credential_error: str | None = None
    if credential_writer is None:
        credential_writer, credential_error = _build_credential_service(selected_credentials)
    selected_verifier = auth_verifier or _build_verifier(api_settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configure_secure_logging(api_settings.log_level)
        forbidden_api_secrets = {
            "polymarket_private_key": api_settings.polymarket_private_key,
            "signed_payload_key": api_settings.signed_payload_key,
            "openai_api_key": api_settings.openai_api_key,
            "litellm_api_key": api_settings.litellm_api_key,
            "credential_private_key": os.getenv("POLYBOT_CREDENTIAL_PRIVATE_KEY_PEM"),
            "credential_private_keyring": os.getenv("POLYBOT_CREDENTIAL_PRIVATE_KEYS_JSON"),
            "credential_symmetric_key": os.getenv("POLYBOT_CREDENTIAL_MASTER_KEY"),
        }
        present = [name for name, value in forbidden_api_secrets.items() if value]
        if present:
            raise RuntimeError(
                "the public API process received worker-only secret material: " + ", ".join(present)
            )
        if credential_error:
            LOGGER.warning(
                safe_json(
                    {
                        "event": "credential_ingestion_disabled",
                        "reason": credential_error,
                    }
                )
            )
        try:
            yield
        finally:
            verifier: TokenVerifier | None = application.state.auth_verifier
            if verifier is not None:
                await verifier.close()

    application = FastAPI(
        title="Polybot Tenant Control API",
        version="0.2.0",
        description="JWT-scoped, queue-only Polymarket automation control plane.",
        lifespan=lifespan,
    )
    application.state.settings = api_settings
    application.state.auth_verifier = selected_verifier
    application.state.job_repository = selected_jobs
    application.state.credential_repository = selected_credentials
    application.state.credential_service = credential_writer
    application.state.rate_limiter = RequestRateLimiter()

    @application.middleware("http")
    async def reject_legacy_secret_transport(
        request: Request,
        call_next: Any,
    ) -> Response:
        request_id = str(uuid4())
        request.state.request_id = request_id
        request_started = time.monotonic()
        if request.method != "OPTIONS":
            sensitive = request.url.path.startswith(
                (
                    "/v1/me/credentials",
                    "/v1/me/wallets",
                    "/v1/me/risk-policy",
                    "/v1/control",
                    "/v1/jobs/cycles",
                )
            )
            limit = 30 if sensitive else 240 if request.url.path.startswith("/v1/") else 120
            identity = _rate_limit_identity(request)
            allowed, retry_after = application.state.rate_limiter.allow(
                f"{identity}:{'sensitive' if sensitive else 'general'}",
                limit=limit,
            )
            if not allowed:
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content={"detail": "request rate limit exceeded"},
                    headers={
                        "Cache-Control": "no-store",
                        "Retry-After": str(retry_after),
                        "X-Request-ID": request_id,
                    },
                )
        if _LEGACY_SECRET_HEADERS.intersection(request.headers.keys()):
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "detail": (
                        "legacy secret headers are forbidden; use one-time "
                        "authenticated credential enrollment"
                    )
                },
                headers={
                    "Cache-Control": "no-store",
                    "X-Request-ID": request_id,
                },
            )
        response = await call_next(request)
        if request.url.path.startswith(
            (
                "/v1/me",
                "/v1/status",
                "/v1/jobs",
                "/v1/control",
                "/v1/worker",
            )
        ):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Request-ID"] = request_id
        LOGGER.info(
            safe_json(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": int((time.monotonic() - request_started) * 1000),
                }
            )
        )
        return response

    if api_settings.allowed_dashboard_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=api_settings.allowed_dashboard_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
            expose_headers=["X-Request-ID", "Retry-After"],
            max_age=600,
        )

    @application.exception_handler(CredentialConflictError)
    @application.exception_handler(JobConflictError)
    async def conflict_handler(request: Request, exc: Exception) -> JSONResponse:
        LOGGER.info(
            safe_json(
                {
                    "event": "tenant_control_conflict",
                    "path": request.url.path,
                    "error_type": type(exc).__name__,
                }
            )
        )
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "the requested state changed concurrently"},
        )

    @application.exception_handler(AccountNotReadyError)
    async def not_ready_handler(request: Request, exc: AccountNotReadyError) -> JSONResponse:
        LOGGER.info(
            safe_json(
                {
                    "event": "tenant_not_ready",
                    "path": request.url.path,
                    "error_type": type(exc).__name__,
                }
            )
        )
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc)},
        )

    @application.exception_handler(ControlPlaneUnavailable)
    async def unavailable_handler(
        request: Request,
        exc: ControlPlaneUnavailable,
    ) -> JSONResponse:
        LOGGER.error(
            safe_json(
                {
                    "event": "control_plane_unavailable",
                    "path": request.url.path,
                    "error_type": type(exc).__name__,
                }
            )
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "durable control storage is unavailable"},
        )

    @application.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        # FastAPI's default error response includes ``errors()[*].input``.
        # Credential endpoints must never reflect submitted key material.
        del exc
        LOGGER.info(
            safe_json(
                {
                    "event": "request_validation_failed",
                    "path": request.url.path,
                }
            )
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": "request validation failed"},
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/livez")
    async def liveness() -> dict[str, bool]:
        return {"ok": True}

    @application.get("/health")
    async def health(repo: JobRepositoryDep) -> JSONResponse:
        store_ok = await repo.health()
        return JSONResponse(
            status_code=200 if store_ok else 503,
            content={
                "ok": store_ok,
                "control_plane": "healthy" if store_ok else "unhealthy",
            },
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/worker-health")
    async def public_worker_health(repo: JobRepositoryDep) -> JSONResponse:
        snapshot = await repo.worker_status()
        ready = snapshot.online and snapshot.ready
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"ok": ready},
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/v1/me", response_model=MeResponse)
    async def me(principal: PrincipalDep, repo: JobRepositoryDep) -> MeResponse:
        profile = await repo.get_or_create_profile(principal.account_id)
        return MeResponse(
            account_id=principal.account_id,
            aal=principal.aal,
            runtime_profile=profile,
            capabilities={
                "wallet_import": True,
                "wallet_provisioning": False,
                "automatic_cycles": True,
            },
        )

    @application.put("/v1/me/runtime-profile", response_model=RuntimeProfile)
    async def update_runtime_profile(
        body: RuntimeProfilePatch,
        principal: PrincipalDep,
        repo: JobRepositoryDep,
    ) -> RuntimeProfile:
        if (
            body.auto_run_enabled
            and body.desired_mode in {TradingMode.CANARY, TradingMode.LIVE}
            and not principal.is_aal2
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "AAL2 is required to enable automatic real-money cycles",
            )
        if body.ai_provider is AIProvider.CUSTOM:
            try:
                safe_base_url = await validate_public_ai_base_url(
                    body.ai_base_url or "",
                    allowed_hosts=api_settings.custom_ai_allowed_hosts,
                )
            except UnsafeAIBaseURLError as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    str(exc),
                ) from exc
            body = body.model_copy(update={"ai_base_url": safe_base_url})
        current = await repo.get_or_create_profile(principal.account_id)
        fields = body.model_fields_set
        effective = body.model_copy(
            update={
                "risk_policy_id": (
                    body.risk_policy_id if "risk_policy_id" in fields else current.risk_policy_id
                ),
                "trading_wallet_id": (
                    body.trading_wallet_id
                    if "trading_wallet_id" in fields
                    else current.trading_wallet_id
                ),
                "ai_credential_id": (
                    body.ai_credential_id
                    if "ai_credential_id" in fields
                    else (
                        None
                        if body.ai_provider in {AIProvider.PLATFORM, AIProvider.MOCK}
                        else current.ai_credential_id
                    )
                ),
            }
        )
        return await repo.update_profile(principal.account_id, effective)

    @application.get("/v1/me/credentials/status", response_model=PublicCredentialStatus)
    async def credentials_status(
        principal: PrincipalDep,
        repository: CredentialRepositoryDep,
    ) -> CredentialStatus:
        return await repository.status(principal.account_id)

    @application.put(
        "/v1/me/credentials/ai",
        response_model=PublicCredentialMetadata,
        status_code=status.HTTP_201_CREATED,
    )
    async def put_ai_credential(
        body: AIKeyPutRequest,
        principal: AAL2PrincipalDep,
        service: CredentialServiceDep,
    ) -> CredentialMetadata:
        try:
            return await service.put_ai(
                account_id=principal.account_id,
                provider=body.provider,
                api_key=body.api_key.get_secret_value(),
                label=body.label,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    @application.delete(
        "/v1/me/credentials/ai",
        response_model=PublicCredentialMetadata,
    )
    async def delete_ai_credential(
        principal: AAL2PrincipalDep,
        repository: CredentialRepositoryDep,
        provider: Annotated[AIProvider, Query()],
    ) -> CredentialMetadata:
        record = await repository.revoke_ai(
            account_id=principal.account_id,
            provider=provider,
        )
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "credential not found")
        return record

    @application.post(
        "/v1/me/credentials/ai/check",
        response_model=AIDiagnosticJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def enqueue_ai_check(
        principal: AAL2PrincipalDep,
        repo: JobRepositoryDep,
    ) -> AIDiagnosticJob:
        try:
            return await repo.enqueue_ai_diagnostic(principal.account_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    @application.get(
        "/v1/me/credentials/ai/check/{job_id}",
        response_model=AIDiagnosticJob,
    )
    async def get_ai_check(
        job_id: UUID,
        principal: PrincipalDep,
        repo: JobRepositoryDep,
    ) -> AIDiagnosticJob:
        job = await repo.get_ai_diagnostic(
            account_id=principal.account_id,
            job_id=job_id,
        )
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "AI diagnostic not found")
        return job

    @application.post("/v1/me/wallets/provision")
    async def provision_wallet(
        body: WalletProvisionRequest,
        principal: AAL2PrincipalDep,
    ) -> None:
        del body, principal
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            "server-side signer provisioning is disabled until a "
            "worker-side KMS flow is configured",
        )

    @application.post(
        "/v1/me/wallets/import",
        response_model=TradingWalletMetadata,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def import_wallet(
        body: WalletImportRequest,
        principal: AAL2PrincipalDep,
        service: CredentialServiceDep,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
    ) -> TradingWalletMetadata:
        if idempotency_key is None or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Idempotency-Key must contain 16-128 safe characters",
            )
        try:
            return await service.import_wallet(
                account_id=principal.account_id,
                private_key=body.private_key.get_secret_value(),
                label=body.label,
                owner_address=body.owner_address,
                signature_type=body.signature_type,
                idempotency_key=idempotency_key,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    @application.delete(
        "/v1/me/wallets/{wallet_id}",
        response_model=TradingWalletMetadata,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def revoke_wallet(
        wallet_id: UUID,
        principal: AAL2PrincipalDep,
        repository: CredentialRepositoryDep,
    ) -> TradingWalletMetadata:
        record = await repository.request_wallet_revoke(
            account_id=principal.account_id,
            wallet_id=wallet_id,
        )
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "wallet not found")
        return record

    @application.get("/v1/me/portfolio", response_model=PortfolioSnapshot)
    async def portfolio(
        principal: PrincipalDep,
        repo: JobRepositoryDep,
    ) -> PortfolioSnapshot:
        return await repo.portfolio(principal.account_id)

    @application.get("/v1/me/notifications")
    async def list_notifications(
        principal: PrincipalDep,
        repo: JobRepositoryDep,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> dict[str, object]:
        return {"items": await repo.notifications(principal.account_id, limit=limit)}

    @application.put("/v1/me/notifications/{notification_id}/read")
    async def read_notification(
        notification_id: int,
        principal: PrincipalDep,
        repo: JobRepositoryDep,
    ) -> dict[str, bool]:
        if notification_id < 1:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found")
        saved = await repo.mark_notification_read(
            account_id=principal.account_id,
            notification_id=notification_id,
        )
        if not saved:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found")
        return {"ok": True}

    @application.put("/v1/me/risk-policy", response_model=RiskPolicySnapshot)
    async def update_risk_policy(
        body: RiskPresetRequest,
        principal: AAL2PrincipalDep,
        repo: JobRepositoryDep,
    ) -> RiskPolicySnapshot:
        return await repo.create_risk_policy_preset(
            account_id=principal.account_id,
            expected_profile_version=body.expected_profile_version,
            preset=body.preset,
        )

    @application.get("/v1/worker/status", response_model=WorkerStatusSnapshot)
    async def worker_status(
        principal: PrincipalDep,
        repo: JobRepositoryDep,
    ) -> WorkerStatusSnapshot:
        del principal
        return await repo.worker_status()

    @application.get("/v1/me/analysis")
    async def recent_analysis(
        principal: PrincipalDep,
        repo: JobRepositoryDep,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
    ) -> dict[str, object]:
        return {
            "items": await repo.recent_analysis(
                principal.account_id,
                limit=limit,
            )
        }

    @application.get("/v1/me/performance", response_model=PerformanceSnapshot)
    async def performance(
        principal: PrincipalDep,
        repo: JobRepositoryDep,
    ) -> PerformanceSnapshot:
        return await repo.performance(principal.account_id)

    @application.put("/v1/me/ai-budget")
    async def update_ai_budget(
        body: AIBudgetRequest,
        principal: AAL2PrincipalDep,
        repo: JobRepositoryDep,
    ) -> dict[str, int]:
        saved = await repo.set_ai_budget_limit(
            account_id=principal.account_id,
            request_limit=body.request_limit,
        )
        return {"request_limit": saved}

    @application.post(
        "/v1/jobs/cycles",
        response_model=CycleJob,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def enqueue_cycle(
        body: CycleJobRequest,
        principal: PrincipalDep,
        repo: JobRepositoryDep,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> CycleJob:
        if idempotency_key is None or not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Idempotency-Key must contain 16-128 safe characters",
            )
        if body.mode in {TradingMode.CANARY, TradingMode.LIVE} and not principal.is_aal2:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "AAL2 is required to queue a real-money cycle",
            )
        return await repo.enqueue(
            account_id=principal.account_id,
            idempotency_key=idempotency_key,
            request=body,
        )

    @application.get("/v1/jobs/{job_id}", response_model=CycleJob)
    async def get_cycle_job(
        job_id: UUID,
        principal: PrincipalDep,
        repo: JobRepositoryDep,
    ) -> CycleJob:
        job = await repo.get_job(account_id=principal.account_id, job_id=job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
        return job

    @application.get("/v1/status")
    async def get_status(
        principal: PrincipalDep,
        repo: JobRepositoryDep,
    ) -> dict[str, object]:
        control = await repo.get_runtime_control(principal.account_id)
        profile = await repo.get_or_create_profile(principal.account_id)
        risk = await repo.get_active_risk_policy(principal.account_id)
        latest_job_getter = getattr(repo, "get_latest_job", None)
        latest_job = (
            await latest_job_getter(account_id=principal.account_id)
            if callable(latest_job_getter)
            else None
        )
        return {
            "account_id": principal.account_id,
            "mode": profile.desired_mode.value,
            "ai_provider": profile.ai_provider.value,
            "forecast_model": profile.forecast_model,
            "control": control.model_dump(mode="json"),
            "runtime_profile": profile.model_dump(mode="json"),
            "risk_limits": (
                {
                    "version": risk.version,
                    "min_edge": str(risk.min_edge),
                    "max_order_usd": str(risk.max_order_usd),
                    "max_trade_risk_pct": str(risk.max_trade_risk_pct),
                    "max_event_exposure_pct": str(risk.max_event_exposure_pct),
                    "max_bucket_exposure_pct": str(risk.max_bucket_exposure_pct),
                    "max_gross_exposure_pct": str(risk.max_gross_exposure_pct),
                    "daily_loss_limit_pct": str(risk.daily_loss_limit_pct),
                    "max_drawdown_pct": str(risk.max_drawdown_pct),
                }
                if risk
                else None
            ),
            "latest_job": (latest_job.model_dump(mode="json") if latest_job is not None else None),
            "execution": "leased_worker_only",
        }

    @application.post("/v1/control/arm")
    async def arm(
        body: ArmRequest,
        principal: AAL2PrincipalDep,
        repo: JobRepositoryDep,
    ) -> dict[str, object]:
        if body.mode not in {TradingMode.CANARY, TradingMode.LIVE}:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "only canary/live controls can be armed",
            )
        saved = await repo.arm(
            account_id=principal.account_id,
            mode=body.mode,
            armed_until=arm_expiry(body.minutes),
            expected_version=body.expected_version,
        )
        if saved is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "runtime control changed concurrently; refresh before arming",
            )
        return saved.model_dump(mode="json")

    @application.post(
        "/v1/control/disarm",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def disarm(
        principal: PrincipalDep,
        repo: JobRepositoryDep,
    ) -> dict[str, object]:
        current = await repo.get_runtime_control(principal.account_id)
        saved = await repo.disarm(
            account_id=principal.account_id,
            mode=current.mode,
        )
        return {
            **saved.model_dump(mode="json"),
            "cancellation_verified": False,
            "cancellation_pending_worker": saved.cancellation_pending,
        }

    @application.post("/v1/cycles/run")
    async def legacy_run_cycle(principal: PrincipalDep) -> None:
        del principal
        raise HTTPException(
            status.HTTP_410_GONE,
            "synchronous HTTP execution was removed; enqueue /v1/jobs/cycles",
        )

    return application


app = create_app()
