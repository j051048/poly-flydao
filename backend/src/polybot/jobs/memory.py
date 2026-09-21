"""In-process control-plane repository for paper mode, tests, and CI."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from polybot.config import TradingMode
from polybot.credentials import AIProvider
from polybot.jobs.schemas import (
    AccountNotReadyError,
    AIDiagnosticJob,
    CycleJob,
    CycleJobRequest,
    CycleJobStatus,
    JobConflictError,
    PerformanceSnapshot,
    PortfolioSnapshot,
    RiskPolicySnapshot,
    RuntimeProfile,
    RuntimeProfilePatch,
    WorkerStatusSnapshot,
    WorkerTradingWallet,
    _decimal_or_none,
    _decimal_or_zero,
    _decimal_text,
    _performance_snapshot,
    _ratio_text,
)
from polybot.models import AIUsageRecord, EquityHistoryPoint, RuntimeControl, utc_now


class InMemoryJobRepository:
    """Safe paper-mode test control plane; no signer or provider secrets."""

    def __init__(self):
        self._profiles: dict[str, RuntimeProfile] = {}
        self._jobs: dict[UUID, CycleJob] = {}
        self._controls: dict[str, RuntimeControl] = {}
        self._risks: dict[str, RiskPolicySnapshot] = {}
        self._paper_states: dict[str, dict[str, Any]] = {}
        self._ai_diagnostics: dict[UUID, AIDiagnosticJob] = {}
        self._ai_usage: dict[str, int] = {}
        self._notifications: dict[str, list[dict[str, Any]]] = {}
        self._equity_history: dict[str, list[EquityHistoryPoint]] = {}
        self._ai_usage_records: dict[str, list[AIUsageRecord]] = {}
        self._worker_status = WorkerStatusSnapshot(online=False, ready=False)
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

    async def update_profile(self, account_id: str, patch: RuntimeProfilePatch) -> RuntimeProfile:
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
                if str(job.account_id) == account_id and job.idempotency_key == idempotency_key
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
                raise JobConflictError("cycle idempotency key was reused with different input")
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
            risk_policy_version=(risk.version if risk and risk.id == selected_risk_id else None),
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

    async def get_latest_job(self, *, account_id: str) -> CycleJob | None:
        jobs = [job for job in self._jobs.values() if str(job.account_id) == account_id]
        return max(jobs, key=lambda job: job.created_at) if jobs else None

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

    async def portfolio(
        self,
        account_id: str,
        mode: TradingMode | None = None,
    ) -> PortfolioSnapshot:
        profile = await self.get_or_create_profile(account_id)
        state = self._paper_states.get(account_id, {})
        cash = _decimal_or_none(state.get("cash"))
        paper_positions = state.get("positions", [])
        exposure = sum(
            (
                _decimal_or_zero(item.get("cost"))
                for item in paper_positions
                if isinstance(item, dict)
            ),
            Decimal("0"),
        )
        equity = cash + exposure if cash is not None else None
        return PortfolioSnapshot(
            account_id=account_id,
            positions=[item for item in paper_positions if isinstance(item, dict)],
            open_orders=[],
            orders=[],
            recent_fills=[],
            summary={
                "position_count": len(paper_positions),
                "open_order_count": 0,
                "recent_fill_count": 0,
                "cash_usd": _decimal_text(cash),
                "available_balance_usd": _decimal_text(cash),
                "portfolio_value_usd": _decimal_text(equity),
                "total_equity_usd": _decimal_text(equity),
                "gross_exposure_usd": str(exposure),
                "gross_exposure_pct": _ratio_text(exposure, equity),
                "realized_pnl_usd": state.get("realized_pnl"),
                "unrealized_pnl_usd": "0" if equity is not None else None,
                "pnl_usd": state.get("realized_pnl"),
            },
            mode=mode or profile.desired_mode,
        )

    async def recent_analysis(
        self,
        account_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        del account_id
        if not 1 <= limit <= 50:
            raise ValueError("analysis limit must be between 1 and 50")
        return []

    async def get_active_risk_policy(self, account_id: str) -> RiskPolicySnapshot | None:
        await self.get_or_create_profile(account_id)
        return self._risks.get(account_id)

    async def create_risk_policy_preset(
        self,
        *,
        account_id: str,
        expected_profile_version: int,
        preset: str,
    ) -> RiskPolicySnapshot:
        profile = await self.get_or_create_profile(account_id)
        if profile.version != expected_profile_version:
            raise JobConflictError("runtime profile changed concurrently")
        values = {
            "conservative": ("2", "0.0025", "0.01", "0.025", "0.05", "0.01", "0.04", "0.06"),
            "balanced": ("5", "0.005", "0.02", "0.05", "0.10", "0.02", "0.08", "0.04"),
            "advanced": ("10", "0.01", "0.03", "0.08", "0.15", "0.03", "0.10", "0.03"),
        }
        if preset not in values:
            raise ValueError("unsupported risk preset")
        previous = self._risks[account_id]
        amount, trade, event, bucket, gross, loss, drawdown, edge = values[preset]
        risk = RiskPolicySnapshot(
            id=uuid4(),
            account_id=account_id,
            version=previous.version + 1,
            status="active",
            max_order_usd=Decimal(amount),
            max_trade_risk_pct=Decimal(trade),
            max_event_exposure_pct=Decimal(event),
            max_bucket_exposure_pct=Decimal(bucket),
            max_gross_exposure_pct=Decimal(gross),
            daily_loss_limit_pct=Decimal(loss),
            max_drawdown_pct=Decimal(drawdown),
            min_edge=Decimal(edge),
            created_at=utc_now(),
        )
        self._risks[account_id] = risk
        self._profiles[account_id] = profile.model_copy(
            update={
                "risk_policy_id": risk.id,
                "version": profile.version + 1,
                "updated_at": utc_now(),
            }
        )
        return risk

    async def worker_status(self) -> WorkerStatusSnapshot:
        return self._worker_status

    async def load_paper_state(self, account_id: str) -> dict[str, Any] | None:
        return self._paper_states.get(account_id)

    async def enqueue_ai_diagnostic(self, account_id: str) -> AIDiagnosticJob:
        profile = await self.get_or_create_profile(account_id)
        if profile.ai_credential_id is None or profile.ai_provider in {
            AIProvider.PLATFORM,
            AIProvider.MOCK,
        }:
            raise ValueError("active AI credential is required")
        existing = next(
            (
                job
                for job in self._ai_diagnostics.values()
                if str(job.account_id) == account_id
                and job.status in {"queued", "claimed", "running"}
            ),
            None,
        )
        if existing:
            return existing
        now = utc_now()
        job = AIDiagnosticJob(
            id=uuid4(),
            account_id=account_id,
            credential_id=profile.ai_credential_id,
            provider=profile.ai_provider,
            ai_base_url=profile.ai_base_url,
            model=profile.forecast_model,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        self._ai_diagnostics[job.id] = job
        return job

    async def get_ai_diagnostic(self, *, account_id: str, job_id: UUID) -> AIDiagnosticJob | None:
        job = self._ai_diagnostics.get(job_id)
        return job if job and str(job.account_id) == account_id else None

    async def notifications(self, account_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return list(reversed(self._notifications.get(account_id, [])))[:limit]

    async def mark_notification_read(self, *, account_id: str, notification_id: int) -> bool:
        for item in self._notifications.get(account_id, []):
            if item.get("id") == notification_id:
                item["read_at"] = utc_now().isoformat()
                return True
        return False

    async def performance(self, account_id: str) -> PerformanceSnapshot:
        return _performance_snapshot(
            [],
            ai_usage_used=self._ai_usage.get(account_id, 0),
            ai_usage_limit=self._ai_usage.get(f"{account_id}:limit", 100),
        )

    async def set_ai_budget_limit(self, *, account_id: str, request_limit: int) -> int:
        if not 20 <= request_limit <= 10000:
            raise ValueError("AI request limit out of range")
        self._ai_usage.setdefault(f"{account_id}:limit", request_limit)
        self._ai_usage[f"{account_id}:limit"] = request_limit
        return request_limit

    async def list_equity_history(
        self, account_id: str, *, limit: int = 200
    ) -> list[EquityHistoryPoint]:
        if limit < 1:
            raise ValueError("equity history limit must be positive")
        return self._equity_history.get(account_id, [])[-limit:]

    async def list_ai_usage(
        self, account_id: str, *, limit: int = 50
    ) -> list[AIUsageRecord]:
        if limit < 1:
            raise ValueError("AI usage limit must be positive")
        return self._ai_usage_records.get(account_id, [])[-limit:]

    async def save_paper_state(
        self,
        *,
        job: CycleJob,
        state: dict[str, Any],
    ) -> bool:
        current = self._jobs.get(job.id)
        if (
            current is None
            or current.status is not CycleJobStatus.RUNNING
            or current.claimed_by != job.claimed_by
            or current.fencing_token != job.fencing_token
        ):
            return False
        self._paper_states[str(job.account_id)] = state
        return True

    async def record_worker_heartbeat(
        self,
        *,
        owner_id: str,
        release: str,
        status: str,
        active_jobs: int,
        queue_lag_seconds: Decimal | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        del details
        now = utc_now()
        self._worker_status = WorkerStatusSnapshot(
            online=status != "stopping",
            ready=status == "ready",
            owner_id=owner_id,
            release=release,
            status=status,
            active_jobs=active_jobs,
            queue_lag_seconds=queue_lag_seconds,
            started_at=self._worker_status.started_at or now,
            last_seen_at=now,
        )

    async def claim_ai_diagnostic(
        self, *, claimed_by: str, lease_seconds: int
    ) -> AIDiagnosticJob | None:
        now = utc_now()
        job = next(
            (
                item
                for item in sorted(
                    self._ai_diagnostics.values(), key=lambda value: value.created_at
                )
                if item.status == "queued"
                or (
                    item.status in {"claimed", "running"}
                    and item.lease_expires_at is not None
                    and item.lease_expires_at <= now
                )
            ),
            None,
        )
        if job is None:
            return None
        claimed = job.model_copy(
            update={
                "status": "claimed",
                "claimed_by": claimed_by,
                "fencing_token": job.fencing_token + 1,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "started_at": job.started_at or now,
                "updated_at": now,
            }
        )
        self._ai_diagnostics[job.id] = claimed
        return claimed

    async def finish_ai_diagnostic(
        self,
        *,
        job: AIDiagnosticJob,
        ok: bool,
        result_summary: dict[str, Any],
        error_code: str | None = None,
    ) -> AIDiagnosticJob | None:
        current = self._ai_diagnostics.get(job.id)
        if (
            current is None
            or current.claimed_by != job.claimed_by
            or current.fencing_token != job.fencing_token
        ):
            return None
        saved = current.model_copy(
            update={
                "status": "succeeded" if ok else "failed",
                "result_summary": result_summary,
                "error_code": None if ok else error_code,
                "lease_expires_at": None,
                "completed_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        self._ai_diagnostics[job.id] = saved
        return saved

    async def consume_ai_budget(self, *, account_id: str, units: int = 1) -> tuple[bool, int, int]:
        used = self._ai_usage.get(account_id, 0)
        limit = self._ai_usage.get(f"{account_id}:limit", 100)
        if used + units > limit:
            return False, used, limit
        used += units
        self._ai_usage[account_id] = used
        return True, used, limit

    async def unresolved_market_conditions(self, *, limit: int = 100) -> list[str]:
        del limit
        return []

    async def record_market_resolution(self, *, condition_id: str, outcome: str) -> int:
        del condition_id, outcome
        return 0

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
                            "next_run_at": now + timedelta(seconds=profile.cycle_interval_seconds),
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
                    "next_run_at": now + timedelta(seconds=profile.cycle_interval_seconds),
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
        result_summary: dict[str, Any] | None = None,
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
                "result_summary": result_summary or {},
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
                "status": (CycleJobStatus.QUEUED if will_retry else CycleJobStatus.FAILED),
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
