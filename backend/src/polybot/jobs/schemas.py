"""Job and control-plane contracts shared by the API and the workers.

This is the contract half of the module that used to be ``polybot.jobs``: the
request/response models, the repository protocols, and the row parsers the two
repository implementations share. The public import path stays
``polybot.jobs``, which re-exports everything here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from polybot.ai_endpoint import UnsafeAIBaseURLError, normalize_ai_base_url
from polybot.config import TradingMode
from polybot.credentials import AIProvider
from polybot.models import AIUsageRecord, EquityHistoryPoint, RuntimeControl, utc_now
from polybot.performance import PerformanceSnapshot, performance_snapshot_from_rows


class JobConflictError(RuntimeError):
    """A cycle is already active or an optimistic transition lost a race."""


class JobNotFoundError(LookupError):
    """A tenant-scoped job was not found."""


class AccountNotReadyError(RuntimeError):
    """The tenant has not completed the prerequisites for the requested mode."""


class CycleJobStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RuntimeProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account_id: UUID
    ai_provider: AIProvider = AIProvider.PLATFORM
    ai_base_url: str | None = None
    forecast_model: str = "gpt-5.6-terra"
    ai_credential_id: UUID | None = None
    trading_wallet_id: UUID | None = None
    risk_policy_id: UUID | None = None
    desired_mode: TradingMode = TradingMode.PAPER
    auto_run_enabled: bool = False
    cycle_interval_seconds: int = 60
    next_run_at: datetime | None = None
    status: str = "active"
    version: int = 1
    created_at: datetime
    updated_at: datetime


class RuntimeProfilePatch(BaseModel):
    expected_version: int = Field(ge=1)
    ai_provider: AIProvider
    ai_base_url: str | None = Field(
        default=None,
        max_length=256,
    )
    forecast_model: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )
    ai_credential_id: UUID | None = None
    trading_wallet_id: UUID | None = None
    risk_policy_id: UUID | None = None
    desired_mode: TradingMode = TradingMode.PAPER
    auto_run_enabled: bool = False
    cycle_interval_seconds: int = Field(default=60, ge=30, le=3600)

    @model_validator(mode="after")
    def validate_custom_provider_endpoint(self) -> RuntimeProfilePatch:
        if self.ai_provider is AIProvider.CUSTOM:
            if self.ai_base_url is None:
                raise ValueError("custom AI provider requires ai_base_url")
            try:
                self.ai_base_url = normalize_ai_base_url(self.ai_base_url)
            except UnsafeAIBaseURLError as exc:
                raise ValueError(str(exc)) from exc
        elif self.ai_base_url is not None:
            raise ValueError("ai_base_url is only allowed for the custom AI provider")
        return self


class CycleJobRequest(BaseModel):
    mode: TradingMode = TradingMode.PAPER
    trading_wallet_id: UUID | None = None
    ai_credential_id: UUID | None = None
    risk_policy_id: UUID | None = None
    run_after: datetime | None = None


class CycleJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: UUID
    account_id: UUID
    trading_wallet_id: UUID | None = None
    ai_credential_id: UUID | None = None
    risk_policy_id: UUID | None = None
    risk_policy_version: int | None = None
    mode: TradingMode
    idempotency_key: str
    status: CycleJobStatus
    attempt_count: int = 0
    max_attempts: int = 3
    claimed_by: str | None = None
    fencing_token: int = 0
    heartbeat_at: datetime | None = None
    lease_expires_at: datetime | None = None
    run_after: datetime
    requested_run_after: datetime | None = None
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result_summary: dict[str, Any] | None = None
    deduplicated: bool = False


class WorkerTradingWallet(BaseModel):
    id: UUID
    account_id: UUID
    deposit_wallet_address: str
    signer_address: str | None = None
    signer_credential_id: UUID
    signature_type: int
    status: str
    version: int


class RiskPolicySnapshot(BaseModel):
    id: UUID
    account_id: UUID
    version: int
    status: str
    max_order_usd: Decimal
    max_trade_risk_pct: Decimal
    max_event_exposure_pct: Decimal
    max_bucket_exposure_pct: Decimal
    max_gross_exposure_pct: Decimal
    daily_loss_limit_pct: Decimal
    max_drawdown_pct: Decimal
    min_edge: Decimal
    created_at: datetime


class PortfolioSnapshot(BaseModel):
    account_id: UUID
    positions: list[dict[str, Any]]
    open_orders: list[dict[str, Any]]
    orders: list[dict[str, Any]]
    recent_fills: list[dict[str, Any]]
    summary: dict[str, Any]
    wallet: dict[str, Any] | None = None
    mode: TradingMode = TradingMode.PAPER


class WorkerStatusSnapshot(BaseModel):
    online: bool
    ready: bool
    owner_id: str | None = None
    release: str | None = None
    status: str = "offline"
    active_jobs: int = 0
    queue_lag_seconds: Decimal | None = None
    started_at: datetime | None = None
    last_seen_at: datetime | None = None


class RiskPresetRequest(BaseModel):
    expected_profile_version: int = Field(ge=1)
    preset: str = Field(pattern=r"^(conservative|balanced|advanced)$")


class AIBudgetRequest(BaseModel):
    request_limit: int = Field(ge=20, le=10000)


class AIDiagnosticJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: UUID
    account_id: UUID
    credential_id: UUID
    provider: AIProvider
    ai_base_url: str | None = None
    model: str
    status: str
    claimed_by: str | None = None
    fencing_token: int = 0
    lease_expires_at: datetime | None = None
    result_summary: dict[str, Any] | None = None
    error_code: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime


class JobRepository(Protocol):
    async def health(self) -> bool: ...

    async def get_or_create_profile(self, account_id: str) -> RuntimeProfile: ...

    async def update_profile(
        self, account_id: str, patch: RuntimeProfilePatch
    ) -> RuntimeProfile: ...

    async def enqueue(
        self,
        *,
        account_id: str,
        idempotency_key: str,
        request: CycleJobRequest,
    ) -> CycleJob: ...

    async def get_job(self, *, account_id: str, job_id: UUID) -> CycleJob | None: ...

    async def get_latest_job(self, *, account_id: str) -> CycleJob | None: ...

    async def get_runtime_control(self, account_id: str) -> RuntimeControl: ...

    async def arm(
        self,
        *,
        account_id: str,
        mode: TradingMode,
        armed_until: datetime,
        expected_version: int,
    ) -> RuntimeControl | None: ...

    async def disarm(self, *, account_id: str, mode: TradingMode) -> RuntimeControl: ...

    async def assert_live_ready(self, account_id: str) -> None: ...

    async def portfolio(
        self,
        account_id: str,
        mode: TradingMode | None = None,
    ) -> PortfolioSnapshot: ...

    async def recent_analysis(
        self,
        account_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]: ...

    async def get_active_risk_policy(self, account_id: str) -> RiskPolicySnapshot | None: ...

    async def create_risk_policy_preset(
        self,
        *,
        account_id: str,
        expected_profile_version: int,
        preset: str,
    ) -> RiskPolicySnapshot: ...

    async def worker_status(self) -> WorkerStatusSnapshot: ...

    async def load_paper_state(self, account_id: str) -> dict[str, Any] | None: ...

    async def enqueue_ai_diagnostic(self, account_id: str) -> AIDiagnosticJob: ...

    async def get_ai_diagnostic(
        self, *, account_id: str, job_id: UUID
    ) -> AIDiagnosticJob | None: ...

    async def notifications(self, account_id: str, *, limit: int = 20) -> list[dict[str, Any]]: ...

    async def mark_notification_read(self, *, account_id: str, notification_id: int) -> bool: ...

    async def performance(self, account_id: str) -> PerformanceSnapshot: ...

    async def set_ai_budget_limit(self, *, account_id: str, request_limit: int) -> int: ...

    async def list_equity_history(
        self, account_id: str, *, limit: int = 200
    ) -> list[EquityHistoryPoint]: ...

    async def list_ai_usage(
        self, account_id: str, *, limit: int = 50
    ) -> list[AIUsageRecord]: ...


class WorkerJobRepository(Protocol):
    async def enqueue_due_jobs(self, *, limit: int = 100) -> int: ...

    async def claim_next_job(
        self,
        *,
        claimed_by: str,
        lease_seconds: int,
        account_id: str | None = None,
    ) -> CycleJob | None: ...

    async def heartbeat(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
        lease_seconds: int,
    ) -> bool: ...

    async def validate_lease(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> bool: ...

    async def mark_running(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> CycleJob | None: ...

    async def complete(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
        result_summary: dict[str, Any] | None = None,
    ) -> CycleJob | None: ...

    async def fail(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
        error_code: str,
        retryable: bool,
    ) -> CycleJob | None: ...

    async def get_worker_wallet(
        self, *, account_id: str, wallet_id: UUID
    ) -> WorkerTradingWallet | None: ...

    async def get_risk_policy(
        self,
        *,
        account_id: str,
        risk_policy_id: UUID,
        expected_version: int,
    ) -> RiskPolicySnapshot | None: ...

    async def save_paper_state(
        self,
        *,
        job: CycleJob,
        state: dict[str, Any],
    ) -> bool: ...

    async def record_worker_heartbeat(
        self,
        *,
        owner_id: str,
        release: str,
        status: str,
        active_jobs: int,
        queue_lag_seconds: Decimal | None = None,
        details: dict[str, Any] | None = None,
    ) -> None: ...

    async def claim_ai_diagnostic(
        self, *, claimed_by: str, lease_seconds: int
    ) -> AIDiagnosticJob | None: ...

    async def finish_ai_diagnostic(
        self,
        *,
        job: AIDiagnosticJob,
        ok: bool,
        result_summary: dict[str, Any],
        error_code: str | None = None,
    ) -> AIDiagnosticJob | None: ...

    async def consume_ai_budget(
        self, *, account_id: str, units: int = 1
    ) -> tuple[bool, int, int]: ...

    async def unresolved_market_conditions(self, *, limit: int = 100) -> list[str]: ...

    async def record_market_resolution(self, *, condition_id: str, outcome: str) -> int: ...


def _profile_from_row(row: dict[str, Any]) -> RuntimeProfile:
    return RuntimeProfile.model_validate(row)


def _job_from_row(row: dict[str, Any]) -> CycleJob:
    return CycleJob.model_validate(row)


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() else None


def _decimal_or_zero(value: Any) -> Decimal:
    return _decimal_or_none(value) or Decimal("0")


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _ratio_text(numerator: Decimal, denominator: Decimal | None) -> str | None:
    if denominator is None or denominator <= 0:
        return None
    return str(numerator / denominator)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _inside_window(
    value: Any,
    start: datetime | None,
    end: datetime | None,
) -> bool:
    candidate = _parse_datetime(value)
    return bool(candidate and start and end and start <= candidate <= end)


def _performance_snapshot(
    rows: list[dict[str, Any]],
    *,
    ai_usage_used: int,
    ai_usage_limit: int,
) -> PerformanceSnapshot:
    """Deprecated alias; the maths now lives in :mod:`polybot.performance`."""

    return performance_snapshot_from_rows(
        rows,
        ai_usage_used=ai_usage_used,
        ai_usage_limit=ai_usage_limit,
    )


def arm_expiry(minutes: int) -> datetime:
    return utc_now() + timedelta(minutes=minutes)
