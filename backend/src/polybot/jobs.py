from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from postgrest.exceptions import APIError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from polybot.ai_endpoint import UnsafeAIBaseURLError, normalize_ai_base_url
from polybot.config import TradingMode
from polybot.credentials import AIProvider
from polybot.models import RuntimeControl, utc_now


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
    summary: dict[str, int | str | None]


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

    async def portfolio(self, account_id: str) -> PortfolioSnapshot: ...

    async def get_active_risk_policy(
        self, account_id: str
    ) -> RiskPolicySnapshot | None: ...


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


def _profile_from_row(row: dict[str, Any]) -> RuntimeProfile:
    return RuntimeProfile.model_validate(row)


def _job_from_row(row: dict[str, Any]) -> CycleJob:
    return CycleJob.model_validate(row)


class SupabaseJobRepository:
    """Service-role control-plane repository with explicit per-call tenant scope."""

    def __init__(self, client: Any):
        self._client = client

    async def _execute(self, builder: Any) -> Any:
        try:
            return await asyncio.to_thread(builder.execute)
        except APIError as exc:
            if str(exc.code) in {"23505", "40001"}:
                raise JobConflictError("optimistic concurrency conflict") from exc
            raise

    @staticmethod
    def _first(response: Any) -> dict[str, Any] | None:
        data = getattr(response, "data", None)
        if isinstance(data, list):
            return data[0] if data and isinstance(data[0], dict) else None
        return data if isinstance(data, dict) else None

    async def health(self) -> bool:
        try:
            await self._execute(self._client.table("cycle_jobs").select("id").limit(1))
            return True
        except Exception:
            return False

    async def get_or_create_profile(self, account_id: str) -> RuntimeProfile:
        response = await self._execute(
            self._client.rpc(
                "ensure_account_runtime_profile",
                {"p_account_id": account_id},
            )
        )
        row = self._first(response)
        if row is None:
            raise AccountNotReadyError("runtime profile is unavailable")
        return _profile_from_row(row)

    async def update_profile(
        self, account_id: str, patch: RuntimeProfilePatch
    ) -> RuntimeProfile:
        response = await self._execute(
            self._client.rpc(
                "update_account_runtime_profile",
                {
                    "p_account_id": account_id,
                    "p_expected_version": patch.expected_version,
                    "p_ai_provider": patch.ai_provider.value,
                    "p_ai_base_url": patch.ai_base_url,
                    "p_forecast_model": patch.forecast_model,
                    "p_ai_credential_id": (
                        str(patch.ai_credential_id) if patch.ai_credential_id else None
                    ),
                    "p_trading_wallet_id": (
                        str(patch.trading_wallet_id) if patch.trading_wallet_id else None
                    ),
                    "p_risk_policy_id": (
                        str(patch.risk_policy_id) if patch.risk_policy_id else None
                    ),
                    "p_desired_mode": patch.desired_mode.value,
                    "p_auto_run_enabled": patch.auto_run_enabled,
                    "p_cycle_interval_seconds": patch.cycle_interval_seconds,
                },
            )
        )
        row = self._first(response)
        if row is None:
            raise JobConflictError("runtime profile changed concurrently")
        return _profile_from_row(row)

    async def enqueue(
        self,
        *,
        account_id: str,
        idempotency_key: str,
        request: CycleJobRequest,
    ) -> CycleJob:
        response = await self._execute(
            self._client.rpc(
                "enqueue_cycle_job",
                {
                    "p_account_id": account_id,
                    "p_idempotency_key": idempotency_key,
                    "p_mode": request.mode.value,
                    "p_trading_wallet_id": (
                        str(request.trading_wallet_id) if request.trading_wallet_id else None
                    ),
                    "p_ai_credential_id": (
                        str(request.ai_credential_id) if request.ai_credential_id else None
                    ),
                    "p_risk_policy_id": (
                        str(request.risk_policy_id) if request.risk_policy_id else None
                    ),
                    "p_run_after": (
                        request.run_after.isoformat() if request.run_after else None
                    ),
                },
            )
        )
        row = self._first(response)
        if row is None:
            raise JobConflictError("cycle job could not be queued")
        return _job_from_row(row)

    async def get_job(self, *, account_id: str, job_id: UUID) -> CycleJob | None:
        response = await self._execute(
            self._client.table("cycle_jobs")
            .select(
                "id,account_id,trading_wallet_id,ai_credential_id,risk_policy_id,mode,"
                "risk_policy_version,idempotency_key,status,attempt_count,max_attempts,"
                "claimed_by,fencing_token,heartbeat_at,lease_expires_at,run_after,error_code,"
                "requested_run_after,created_at,updated_at,started_at,completed_at"
            )
            .eq("account_id", account_id)
            .eq("id", str(job_id))
            .limit(1)
        )
        row = self._first(response)
        return _job_from_row(row) if row else None

    async def get_runtime_control(self, account_id: str) -> RuntimeControl:
        await self.get_or_create_profile(account_id)
        response = await self._execute(
            self._client.table("runtime_controls")
            .select("*")
            .eq("account_id", account_id)
            .limit(1)
        )
        row = self._first(response)
        if row is None:
            return RuntimeControl(account_id=account_id)
        return RuntimeControl.model_validate(row)

    async def arm(
        self,
        *,
        account_id: str,
        mode: TradingMode,
        armed_until: datetime,
        expected_version: int,
    ) -> RuntimeControl | None:
        await self.assert_live_ready(account_id)
        response = await self._execute(
            self._client.rpc(
                "arm_runtime_control",
                {
                    "p_account_id": account_id,
                    "p_mode": mode.value,
                    "p_armed_until": armed_until.isoformat(),
                    "p_expected_version": expected_version,
                },
            )
        )
        row = self._first(response)
        return RuntimeControl.model_validate(row) if row else None

    async def disarm(self, *, account_id: str, mode: TradingMode) -> RuntimeControl:
        response = await self._execute(
            self._client.rpc(
                "disarm_runtime_control",
                {"p_account_id": account_id, "p_mode": mode.value},
            )
        )
        row = self._first(response)
        if row is None:
            raise JobConflictError("runtime could not be disarmed")
        return RuntimeControl.model_validate(row)

    async def assert_live_ready(self, account_id: str) -> None:
        profile = await self.get_or_create_profile(account_id)
        if (
            profile.ai_provider in {AIProvider.PLATFORM, AIProvider.MOCK}
            or profile.ai_credential_id is None
            or profile.trading_wallet_id is None
            or profile.risk_policy_id is None
        ):
            raise AccountNotReadyError(
                "active tenant AI credential, verified wallet, and risk policy are required"
            )
        credential_response, wallet_response, risk_response = await asyncio.gather(
            self._execute(
                self._client.table("credential_refs")
                .select("id")
                .eq("account_id", account_id)
                .eq("id", str(profile.ai_credential_id))
                .eq("status", "active")
                .limit(1)
            ),
            self._execute(
                self._client.table("trading_wallets")
                .select("id")
                .eq("account_id", account_id)
                .eq("id", str(profile.trading_wallet_id))
                .eq("status", "active")
                .limit(1)
            ),
            self._execute(
                self._client.table("risk_policies")
                .select("id")
                .eq("account_id", account_id)
                .eq("id", str(profile.risk_policy_id))
                .eq("status", "active")
                .limit(1)
            ),
        )
        if not all(
            self._first(response)
            for response in (credential_response, wallet_response, risk_response)
        ):
            raise AccountNotReadyError(
                "active tenant AI credential, verified wallet, and risk policy are required"
            )

    async def portfolio(self, account_id: str) -> PortfolioSnapshot:
        positions_response, orders_response, fills_response = await asyncio.gather(
            self._execute(
                self._client.table("positions")
                .select(
                    "id,market_id,outcome_token_id,outcome,shares,average_entry_price,"
                    "cost_basis_pusd,realized_pnl_pusd,mark_price,unrealized_pnl_pusd,"
                    "as_of,version,updated_at"
                )
                .eq("account_id", account_id)
                .order("updated_at", desc=True)
                .limit(250)
            ),
            self._execute(
                self._client.table("orders")
                .select(
                    "id,order_intent_id,clob_order_id,outcome_token_id,side,order_type,"
                    "limit_price,original_size,filled_size,remaining_size,status,"
                    "submitted_at,updated_at"
                )
                .eq("account_id", account_id)
                .in_(
                    "status",
                    [
                        "created",
                        "signed",
                        "submitting",
                        "submitted",
                        "live",
                        "unknown",
                        "partially_filled",
                        "cancel_pending",
                        "matched",
                        "mined",
                    ],
                )
                .order("updated_at", desc=True)
                .limit(250)
            ),
            self._execute(
                self._client.table("fills")
                .select(
                    "id,order_id,market_id,clob_trade_id,outcome_token_id,side,"
                    "liquidity_role,price,size,fee_pusd,settlement_status,"
                    "transaction_hash,matched_at,confirmed_at"
                )
                .eq("account_id", account_id)
                .order("matched_at", desc=True)
                .limit(100)
            ),
        )
        positions = [
            row
            for row in (getattr(positions_response, "data", None) or [])
            if isinstance(row, dict)
        ]
        open_orders = [
            row
            for row in (getattr(orders_response, "data", None) or [])
            if isinstance(row, dict)
        ]
        recent_fills = [
            row
            for row in (getattr(fills_response, "data", None) or [])
            if isinstance(row, dict)
        ]
        return PortfolioSnapshot(
            account_id=account_id,
            positions=positions,
            open_orders=open_orders,
            orders=open_orders,
            recent_fills=recent_fills,
            summary={
                "position_count": len(positions),
                "open_order_count": len(open_orders),
                "recent_fill_count": len(recent_fills),
                "cash_usd": None,
                "portfolio_value_usd": None,
                "total_equity_usd": None,
                "gross_exposure_usd": None,
                "realized_pnl_usd": None,
                "unrealized_pnl_usd": None,
                "pnl_usd": None,
            },
        )

    async def get_active_risk_policy(
        self, account_id: str
    ) -> RiskPolicySnapshot | None:
        profile = await self.get_or_create_profile(account_id)
        if profile.risk_policy_id is None:
            return None
        response = await self._execute(
            self._client.table("risk_policies")
            .select(
                "id,account_id,version,status,max_order_usd,max_trade_risk_pct,"
                "max_event_exposure_pct,max_bucket_exposure_pct,max_gross_exposure_pct,"
                "daily_loss_limit_pct,max_drawdown_pct,min_edge,created_at"
            )
            .eq("account_id", account_id)
            .eq("id", str(profile.risk_policy_id))
            .eq("status", "active")
            .limit(1)
        )
        row = self._first(response)
        return RiskPolicySnapshot.model_validate(row) if row else None

    async def enqueue_due_jobs(self, *, limit: int = 100) -> int:
        response = await self._execute(
            self._client.rpc("enqueue_due_cycle_jobs", {"p_limit": limit})
        )
        data = getattr(response, "data", 0)
        if isinstance(data, list):
            data = data[0] if data else 0
        if isinstance(data, dict):
            data = data.get("enqueue_due_cycle_jobs", data.get("count", 0))
        return int(data or 0)

    async def claim_next_job(
        self,
        *,
        claimed_by: str,
        lease_seconds: int,
        account_id: str | None = None,
    ) -> CycleJob | None:
        response = await self._execute(
            self._client.rpc(
                "claim_next_cycle_job",
                {
                    "p_claimed_by": claimed_by,
                    "p_lease_seconds": lease_seconds,
                    "p_account_id": account_id,
                },
            )
        )
        row = self._first(response)
        return _job_from_row(row) if row else None

    async def heartbeat(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
        lease_seconds: int,
    ) -> bool:
        response = await self._execute(
            self._client.rpc(
                "heartbeat_cycle_job",
                {
                    "p_account_id": account_id,
                    "p_job_id": str(job_id),
                    "p_claimed_by": claimed_by,
                    "p_fencing_token": fencing_token,
                    "p_lease_seconds": lease_seconds,
                },
            )
        )
        data = getattr(response, "data", False)
        if isinstance(data, list):
            data = data[0] if data else False
        if isinstance(data, dict):
            data = data.get("heartbeat_cycle_job", data.get("ok", False))
        return data is True

    async def validate_lease(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> bool:
        response = await self._execute(
            self._client.rpc(
                "validate_cycle_job_lease",
                {
                    "p_account_id": account_id,
                    "p_job_id": str(job_id),
                    "p_claimed_by": claimed_by,
                    "p_fencing_token": fencing_token,
                },
            )
        )
        data = getattr(response, "data", False)
        if isinstance(data, list):
            data = data[0] if data else False
        if isinstance(data, dict):
            data = data.get("validate_cycle_job_lease", data.get("valid", False))
        return data is True

    async def mark_running(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> CycleJob | None:
        return await self._transition(
            "mark_cycle_job_running",
            account_id=account_id,
            job_id=job_id,
            claimed_by=claimed_by,
            fencing_token=fencing_token,
        )

    async def complete(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> CycleJob | None:
        return await self._transition(
            "complete_cycle_job",
            account_id=account_id,
            job_id=job_id,
            claimed_by=claimed_by,
            fencing_token=fencing_token,
        )

    async def fail(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
        error_code: str,
        retryable: bool,
    ) -> CycleJob | None:
        response = await self._execute(
            self._client.rpc(
                "fail_cycle_job",
                {
                    "p_account_id": account_id,
                    "p_job_id": str(job_id),
                    "p_claimed_by": claimed_by,
                    "p_fencing_token": fencing_token,
                    "p_error_code": error_code,
                    "p_retryable": retryable,
                },
            )
        )
        row = self._first(response)
        return _job_from_row(row) if row else None

    async def _transition(
        self,
        function_name: str,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> CycleJob | None:
        response = await self._execute(
            self._client.rpc(
                function_name,
                {
                    "p_account_id": account_id,
                    "p_job_id": str(job_id),
                    "p_claimed_by": claimed_by,
                    "p_fencing_token": fencing_token,
                },
            )
        )
        row = self._first(response)
        return _job_from_row(row) if row else None

    async def get_worker_wallet(
        self, *, account_id: str, wallet_id: UUID
    ) -> WorkerTradingWallet | None:
        response = await self._execute(
            self._client.table("trading_wallets")
            .select(
                "id,account_id,deposit_wallet_address,signer_address,"
                "signer_credential_id,signature_type,status,version"
            )
            .eq("account_id", account_id)
            .eq("id", str(wallet_id))
            .eq("status", "active")
            .limit(1)
        )
        row = self._first(response)
        return WorkerTradingWallet.model_validate(row) if row else None

    async def get_risk_policy(
        self,
        *,
        account_id: str,
        risk_policy_id: UUID,
        expected_version: int,
    ) -> RiskPolicySnapshot | None:
        response = await self._execute(
            self._client.table("risk_policies")
            .select(
                "id,account_id,version,status,max_order_usd,max_trade_risk_pct,"
                "max_event_exposure_pct,max_bucket_exposure_pct,max_gross_exposure_pct,"
                "daily_loss_limit_pct,max_drawdown_pct,min_edge,created_at"
            )
            .eq("account_id", account_id)
            .eq("id", str(risk_policy_id))
            .eq("version", expected_version)
            .eq("status", "active")
            .limit(1)
        )
        row = self._first(response)
        return RiskPolicySnapshot.model_validate(row) if row else None


class InMemoryJobRepository:
    """Safe paper-mode test control plane; no signer or provider secrets."""

    def __init__(self):
        self._profiles: dict[str, RuntimeProfile] = {}
        self._jobs: dict[UUID, CycleJob] = {}
        self._controls: dict[str, RuntimeControl] = {}
        self._risks: dict[str, RiskPolicySnapshot] = {}
        self.live_ready_accounts: set[str] = set()

    async def health(self) -> bool:
        return True

    async def get_or_create_profile(self, account_id: str) -> RuntimeProfile:
        profile = self._profiles.get(account_id)
        if profile is None:
            now = utc_now()
            risk = RiskPolicySnapshot(
                id=uuid4(),
                account_id=account_id,
                version=1,
                status="active",
                max_order_usd=Decimal("5"),
                max_trade_risk_pct=Decimal("0.005"),
                max_event_exposure_pct=Decimal("0.02"),
                max_bucket_exposure_pct=Decimal("0.05"),
                max_gross_exposure_pct=Decimal("0.10"),
                daily_loss_limit_pct=Decimal("0.02"),
                max_drawdown_pct=Decimal("0.08"),
                min_edge=Decimal("0.04"),
                created_at=now,
            )
            profile = RuntimeProfile(
                account_id=account_id,
                risk_policy_id=risk.id,
                created_at=now,
                updated_at=now,
            )
            self._risks[account_id] = risk
            self._profiles[account_id] = profile
            self._controls[account_id] = RuntimeControl(account_id=account_id)
        return profile

    async def update_profile(
        self, account_id: str, patch: RuntimeProfilePatch
    ) -> RuntimeProfile:
        previous = await self.get_or_create_profile(account_id)
        if previous.version != patch.expected_version:
            raise JobConflictError("runtime profile changed concurrently")
        updated = RuntimeProfile(
            account_id=account_id,
            ai_provider=patch.ai_provider,
            ai_base_url=patch.ai_base_url,
            forecast_model=patch.forecast_model,
            ai_credential_id=patch.ai_credential_id,
            trading_wallet_id=patch.trading_wallet_id,
            risk_policy_id=patch.risk_policy_id,
            desired_mode=patch.desired_mode,
            auto_run_enabled=patch.auto_run_enabled,
            cycle_interval_seconds=patch.cycle_interval_seconds,
            next_run_at=(
                utc_now()
                if patch.auto_run_enabled and not previous.auto_run_enabled
                else previous.next_run_at
            ),
            status=previous.status,
            version=previous.version + 1,
            created_at=previous.created_at,
            updated_at=utc_now(),
        )
        self._profiles[account_id] = updated
        return updated

    async def enqueue(
        self,
        *,
        account_id: str,
        idempotency_key: str,
        request: CycleJobRequest,
    ) -> CycleJob:
        existing = next(
            (
                job
                for job in self._jobs.values()
                if str(job.account_id) == account_id
                and job.idempotency_key == idempotency_key
            ),
            None,
        )
        if existing:
            profile = await self.get_or_create_profile(account_id)
            expected_wallet = request.trading_wallet_id or profile.trading_wallet_id
            expected_ai = request.ai_credential_id or profile.ai_credential_id
            expected_risk = request.risk_policy_id or profile.risk_policy_id
            if (
                existing.mode != request.mode
                or existing.trading_wallet_id != expected_wallet
                or existing.ai_credential_id != expected_ai
                or existing.risk_policy_id != expected_risk
                or existing.requested_run_after != request.run_after
            ):
                raise JobConflictError(
                    "cycle idempotency key was reused with different input"
                )
            return existing.model_copy(update={"deduplicated": True})
        if request.mode in {TradingMode.CANARY, TradingMode.LIVE}:
            await self.assert_live_ready(account_id)
        profile = await self.get_or_create_profile(account_id)
        selected_risk_id = request.risk_policy_id or profile.risk_policy_id
        risk = self._risks.get(account_id)
        now = utc_now()
        job = CycleJob(
            id=uuid4(),
            account_id=account_id,
            trading_wallet_id=request.trading_wallet_id or profile.trading_wallet_id,
            ai_credential_id=request.ai_credential_id or profile.ai_credential_id,
            risk_policy_id=selected_risk_id,
            risk_policy_version=(
                risk.version if risk and risk.id == selected_risk_id else None
            ),
            mode=request.mode,
            idempotency_key=idempotency_key,
            status=CycleJobStatus.QUEUED,
            run_after=request.run_after or now,
            requested_run_after=request.run_after,
            created_at=now,
            updated_at=now,
        )
        self._jobs[job.id] = job
        return job

    async def get_job(self, *, account_id: str, job_id: UUID) -> CycleJob | None:
        job = self._jobs.get(job_id)
        if job is None or str(job.account_id) != account_id:
            return None
        return job

    async def get_runtime_control(self, account_id: str) -> RuntimeControl:
        await self.get_or_create_profile(account_id)
        return self._controls[account_id]

    async def arm(
        self,
        *,
        account_id: str,
        mode: TradingMode,
        armed_until: datetime,
        expected_version: int,
    ) -> RuntimeControl | None:
        await self.assert_live_ready(account_id)
        current = await self.get_runtime_control(account_id)
        if current.version != expected_version or current.cancellation_pending:
            return None
        saved = RuntimeControl(
            account_id=account_id,
            mode=mode,
            armed=True,
            accept_new_intents=True,
            armed_until=armed_until,
            kill_switch=False,
            cancellation_pending=False,
            version=current.version + 1,
        )
        self._controls[account_id] = saved
        return saved

    async def disarm(self, *, account_id: str, mode: TradingMode) -> RuntimeControl:
        current = await self.get_runtime_control(account_id)
        saved = RuntimeControl(
            account_id=account_id,
            mode=mode,
            armed=False,
            accept_new_intents=False,
            armed_until=None,
            kill_switch=True,
            cancellation_pending=mode in {TradingMode.CANARY, TradingMode.LIVE},
            version=current.version + 1,
        )
        self._controls[account_id] = saved
        return saved

    async def assert_live_ready(self, account_id: str) -> None:
        if account_id not in self.live_ready_accounts:
            raise AccountNotReadyError(
                "active tenant AI credential, verified wallet, and risk policy are required"
            )

    async def portfolio(self, account_id: str) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            account_id=account_id,
            positions=[],
            open_orders=[],
            orders=[],
            recent_fills=[],
            summary={
                "position_count": 0,
                "open_order_count": 0,
                "recent_fill_count": 0,
                "cash_usd": None,
                "portfolio_value_usd": None,
                "total_equity_usd": None,
                "gross_exposure_usd": None,
                "realized_pnl_usd": None,
                "unrealized_pnl_usd": None,
                "pnl_usd": None,
            },
        )

    async def get_active_risk_policy(
        self, account_id: str
    ) -> RiskPolicySnapshot | None:
        await self.get_or_create_profile(account_id)
        return self._risks.get(account_id)

    async def enqueue_due_jobs(self, *, limit: int = 100) -> int:
        if not 1 <= limit <= 500:
            raise ValueError("invalid enqueue limit")
        now = utc_now()
        count = 0
        for account_id, profile in sorted(
            self._profiles.items(),
            key=lambda item: item[1].next_run_at or now,
        ):
            if count >= limit:
                break
            if (
                not profile.auto_run_enabled
                or profile.next_run_at is None
                or profile.next_run_at > now
                or any(
                    str(job.account_id) == account_id
                    and job.status
                    in {
                        CycleJobStatus.QUEUED,
                        CycleJobStatus.CLAIMED,
                        CycleJobStatus.RUNNING,
                    }
                    for job in self._jobs.values()
                )
            ):
                continue
            if profile.desired_mode in {TradingMode.CANARY, TradingMode.LIVE}:
                control = await self.get_runtime_control(account_id)
                if not control.is_live_armed:
                    self._profiles[account_id] = profile.model_copy(
                        update={
                            "next_run_at": now
                            + timedelta(seconds=profile.cycle_interval_seconds),
                            "updated_at": now,
                        }
                    )
                    continue
            request = CycleJobRequest(
                mode=profile.desired_mode,
                trading_wallet_id=profile.trading_wallet_id,
                ai_credential_id=profile.ai_credential_id,
                risk_policy_id=profile.risk_policy_id,
            )
            await self.enqueue(
                account_id=account_id,
                idempotency_key=f"auto:{profile.next_run_at.isoformat()}",
                request=request,
            )
            self._profiles[account_id] = profile.model_copy(
                update={
                    "next_run_at": now
                    + timedelta(seconds=profile.cycle_interval_seconds),
                    "updated_at": now,
                }
            )
            count += 1
        return count

    async def get_worker_wallet(
        self, *, account_id: str, wallet_id: UUID
    ) -> WorkerTradingWallet | None:
        return None

    async def get_risk_policy(
        self,
        *,
        account_id: str,
        risk_policy_id: UUID,
        expected_version: int,
    ) -> RiskPolicySnapshot | None:
        return None

    async def claim_next_job(
        self,
        *,
        claimed_by: str,
        lease_seconds: int,
        account_id: str | None = None,
    ) -> CycleJob | None:
        if not claimed_by or not 10 <= lease_seconds <= 300:
            raise ValueError("invalid worker lease")
        now = utc_now()
        eligible = sorted(
            (
                job
                for job in self._jobs.values()
                if (account_id is None or str(job.account_id) == account_id)
                and job.run_after <= now
                and (
                    job.status == CycleJobStatus.QUEUED
                    or (
                        job.status in {CycleJobStatus.CLAIMED, CycleJobStatus.RUNNING}
                        and job.lease_expires_at is not None
                        and job.lease_expires_at <= now
                        and job.attempt_count < job.max_attempts
                    )
                )
            ),
            key=lambda item: (item.run_after, item.created_at),
        )
        if not eligible:
            return None
        job = eligible[0]
        claimed = job.model_copy(
            update={
                "status": CycleJobStatus.CLAIMED,
                "claimed_by": claimed_by,
                "fencing_token": job.fencing_token + 1,
                "attempt_count": job.attempt_count + 1,
                "heartbeat_at": now,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "updated_at": now,
            }
        )
        self._jobs[job.id] = claimed
        return claimed

    async def heartbeat(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
        lease_seconds: int,
    ) -> bool:
        job = self._worker_job(account_id, job_id, claimed_by, fencing_token)
        if job is None or not 10 <= lease_seconds <= 300:
            return False
        now = utc_now()
        if job.lease_expires_at is None or job.lease_expires_at <= now:
            return False
        self._jobs[job.id] = job.model_copy(
            update={
                "heartbeat_at": now,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "updated_at": now,
            }
        )
        return True

    async def validate_lease(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> bool:
        job = self._worker_job(account_id, job_id, claimed_by, fencing_token)
        return bool(
            job
            and job.status in {CycleJobStatus.CLAIMED, CycleJobStatus.RUNNING}
            and job.lease_expires_at
            and job.lease_expires_at > utc_now()
        )

    async def mark_running(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> CycleJob | None:
        job = self._worker_job(account_id, job_id, claimed_by, fencing_token)
        if job is None or job.status != CycleJobStatus.CLAIMED:
            return None
        now = utc_now()
        saved = job.model_copy(
            update={
                "status": CycleJobStatus.RUNNING,
                "started_at": job.started_at or now,
                "updated_at": now,
            }
        )
        self._jobs[job.id] = saved
        return saved

    async def complete(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> CycleJob | None:
        job = self._worker_job(account_id, job_id, claimed_by, fencing_token)
        if job is None or job.status not in {
            CycleJobStatus.CLAIMED,
            CycleJobStatus.RUNNING,
        }:
            return None
        now = utc_now()
        saved = job.model_copy(
            update={
                "status": CycleJobStatus.SUCCEEDED,
                "completed_at": now,
                "lease_expires_at": None,
                "updated_at": now,
            }
        )
        self._jobs[job.id] = saved
        return saved

    async def fail(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
        error_code: str,
        retryable: bool,
    ) -> CycleJob | None:
        job = self._worker_job(account_id, job_id, claimed_by, fencing_token)
        if job is None or not error_code or len(error_code) > 64:
            return None
        now = utc_now()
        will_retry = retryable and job.attempt_count < job.max_attempts
        saved = job.model_copy(
            update={
                "status": (
                    CycleJobStatus.QUEUED if will_retry else CycleJobStatus.FAILED
                ),
                "claimed_by": None if will_retry else job.claimed_by,
                "lease_expires_at": None,
                "run_after": now + timedelta(seconds=30) if will_retry else job.run_after,
                "error_code": error_code,
                "completed_at": None if will_retry else now,
                "updated_at": now,
            }
        )
        self._jobs[job.id] = saved
        return saved

    def _worker_job(
        self,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> CycleJob | None:
        job = self._jobs.get(job_id)
        if (
            job is None
            or str(job.account_id) != account_id
            or job.claimed_by != claimed_by
            or job.fencing_token != fencing_token
        ):
            return None
        return job


def arm_expiry(minutes: int) -> datetime:
    return utc_now() + timedelta(minutes=minutes)
