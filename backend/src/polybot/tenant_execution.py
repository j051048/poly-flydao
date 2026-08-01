from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from contextlib import suppress
from datetime import timedelta
from typing import Any
from uuid import uuid4

from pydantic import SecretStr

from polybot import __version__
from polybot.ai_endpoint import UnsafeAIBaseURLError, validate_public_ai_base_url
from polybot.brokers.paper import PaperBroker
from polybot.brokers.polymarket import PolymarketBroker
from polybot.config import Settings, TradingMode
from polybot.credentials import (
    AIProvider,
    EnvelopeDecryptor,
    SecretKind,
    SupabaseCredentialRepository,
    WorkerCredentialRepository,
    build_worker_decryptor_from_environment,
)
from polybot.jobs import (
    CycleJob,
    JobRepository,
    RiskPolicySnapshot,
    SupabaseJobRepository,
    WorkerJobRepository,
)
from polybot.models import EngineCycleResult
from polybot.runtime import Runtime, build_runtime
from polybot.stores.factory import AccountStoreFactory
from polybot.stores.supabase_store import TenantExecutionFence
from polybot.tenant_worker import (
    JobLeaseGuard,
    TenantJobExecutionError,
    TenantJobRunner,
)
from polybot.worker import (
    RuntimeControlWatchState,
    _enforce_runtime_control_once,
    _set_worker_ready,
    _shutdown_live_safely,
)

_PROVIDER_ENDPOINTS = {
    AIProvider.OPENROUTER: "https://openrouter.ai/api/v1",
    AIProvider.ANTHROPIC: "https://api.anthropic.com",
}


class TenantRuntimeExecutor:
    """Build and destroy one account-bound runtime for each fenced cycle job."""

    def __init__(
        self,
        *,
        base_settings: Settings,
        jobs: JobRepository | WorkerJobRepository,
        credentials: WorkerCredentialRepository,
        decryptor: EnvelopeDecryptor,
        stores: AccountStoreFactory,
        logger: logging.Logger | None = None,
    ):
        self.base_settings = base_settings
        self.jobs = jobs
        self.credentials = credentials
        self.decryptor = decryptor
        self.stores = stores
        self.logger = logger or logging.getLogger("polybot.tenant_execution")

    async def __call__(
        self,
        job: CycleJob,
        job_lease: JobLeaseGuard,
    ) -> EngineCycleResult:
        account_id = str(job.account_id)
        profile = await self.jobs.get_or_create_profile(account_id)  # type: ignore[union-attr]
        if profile.status != "active" or profile.desired_mode is not job.mode:
            raise TenantJobExecutionError("runtime_profile_changed", retryable=False)
        if (
            profile.ai_credential_id != job.ai_credential_id
            or profile.trading_wallet_id != job.trading_wallet_id
            or profile.risk_policy_id != job.risk_policy_id
        ):
            raise TenantJobExecutionError("runtime_profile_changed", retryable=False)

        ai_secret: bytearray | None = None
        signer_secret: bytearray | None = None
        runtime: Runtime | None = None
        try:
            if profile.ai_provider is AIProvider.CUSTOM:
                try:
                    safe_base_url = await validate_public_ai_base_url(
                        profile.ai_base_url or "",
                        allowed_hosts=self.base_settings.custom_ai_allowed_hosts,
                    )
                except UnsafeAIBaseURLError as exc:
                    raise TenantJobExecutionError(
                        "custom_provider_endpoint_unsafe",
                        retryable=False,
                    ) from exc
                profile = profile.model_copy(update={"ai_base_url": safe_base_url})
            risk = await self._risk_policy(job)
            ai_secret = await self._ai_secret(job, profile.ai_provider)
            if profile.ai_provider not in {AIProvider.MOCK, AIProvider.PLATFORM}:
                calls_per_market = 3 if profile.ai_provider is AIProvider.OPENAI else 2
                reserved_units = min(
                    10,
                    self.base_settings.max_ai_markets_per_cycle * calls_per_market,
                )
                allowed, _, _ = await self.jobs.consume_ai_budget(  # type: ignore[union-attr]
                    account_id=account_id,
                    units=reserved_units,
                )
                if not allowed:
                    raise TenantJobExecutionError(
                        "ai_daily_budget_exhausted",
                        retryable=False,
                    )
            signer_secret, wallet_address = await self._wallet_secret(job)
            settings = self._job_settings(
                job=job,
                provider=profile.ai_provider,
                model=profile.forecast_model,
                ai_base_url=profile.ai_base_url,
                ai_secret=ai_secret,
                signer_secret=signer_secret,
                wallet_address=wallet_address,
                risk=risk,
            )
            store = self.stores.for_account(account_id)
            if job.mode in {TradingMode.CANARY, TradingMode.LIVE}:
                if (
                    job.claimed_by is None
                    or job.risk_policy_id is None
                    or job.risk_policy_version is None
                ):
                    raise TenantJobExecutionError(
                        "live_job_snapshot_incomplete",
                        retryable=False,
                    )
                store.bind_tenant_execution_fence(
                    TenantExecutionFence(
                        job_id=str(job.id),
                        claimed_by=job.claimed_by,
                        job_fencing_token=job.fencing_token,
                        profile_version=profile.version,
                        risk_policy_id=str(job.risk_policy_id),
                        risk_policy_version=job.risk_policy_version,
                        mode=job.mode,
                    )
                )
            paper_broker: PaperBroker | None = None
            if job.mode is TradingMode.PAPER:
                paper_state = await self.jobs.load_paper_state(account_id)  # type: ignore[union-attr]
                try:
                    paper_broker = PaperBroker.from_state(
                        settings.bankroll_usd,
                        paper_state,
                    )
                except ValueError as exc:
                    raise TenantJobExecutionError(
                        "paper_state_invalid",
                        retryable=False,
                    ) from exc
            runtime = build_runtime(
                settings,
                store_override=store,
                broker_override=paper_broker,
            )
            result = await self._run_runtime(
                runtime=runtime,
                job=job,
                job_lease=job_lease,
                profile_version=profile.version,
                risk=risk,
            )
            if paper_broker is not None:
                saved = await self.jobs.save_paper_state(  # type: ignore[union-attr]
                    job=job,
                    state=paper_broker.export_state(),
                )
                if not saved:
                    raise TenantJobExecutionError(
                        "paper_state_fence_lost",
                        retryable=True,
                    )
            return result
        finally:
            try:
                if runtime is not None:
                    await runtime.close()
            finally:
                # Best effort only: Python libraries may retain immutable
                # string copies internally until their objects are collected.
                _zero(ai_secret)
                _zero(signer_secret)

    async def _risk_policy(self, job: CycleJob) -> RiskPolicySnapshot:
        if job.risk_policy_id is None or job.risk_policy_version is None:
            raise TenantJobExecutionError("risk_policy_missing", retryable=False)
        risk = await self.jobs.get_risk_policy(  # type: ignore[union-attr]
            account_id=str(job.account_id),
            risk_policy_id=job.risk_policy_id,
            expected_version=job.risk_policy_version,
        )
        if risk is None or risk.status != "active" or risk.version != job.risk_policy_version:
            raise TenantJobExecutionError("risk_policy_inactive", retryable=False)
        return risk

    async def _ai_secret(
        self,
        job: CycleJob,
        provider: AIProvider,
    ) -> bytearray | None:
        if provider in {AIProvider.MOCK, AIProvider.PLATFORM}:
            if job.mode in {TradingMode.CANARY, TradingMode.LIVE}:
                raise TenantJobExecutionError("live_ai_provider_invalid", retryable=False)
            return None
        if job.ai_credential_id is None:
            raise TenantJobExecutionError("ai_credential_missing", retryable=False)
        credential = await self.credentials.get_envelope_for_worker(
            account_id=str(job.account_id),
            credential_id=job.ai_credential_id,
            kind=SecretKind.AI_API_KEY,
        )
        if credential is None or credential.provider != provider.value:
            raise TenantJobExecutionError("ai_credential_inactive", retryable=False)
        plaintext = self.decryptor.decrypt(
            credential.envelope,
            account_id=str(job.account_id),
            kind=SecretKind.AI_API_KEY,
            provider=credential.provider,
        )
        return bytearray(plaintext)

    async def _wallet_secret(
        self,
        job: CycleJob,
    ) -> tuple[bytearray | None, str | None]:
        if job.mode not in {TradingMode.CANARY, TradingMode.LIVE}:
            return None, None
        if job.trading_wallet_id is None:
            raise TenantJobExecutionError("trading_wallet_missing", retryable=False)
        wallet = await self.jobs.get_worker_wallet(  # type: ignore[union-attr]
            account_id=str(job.account_id),
            wallet_id=job.trading_wallet_id,
        )
        if wallet is None or wallet.status != "active":
            raise TenantJobExecutionError("trading_wallet_inactive", retryable=False)
        credential = await self.credentials.get_envelope_for_worker(
            account_id=str(job.account_id),
            credential_id=wallet.signer_credential_id,
            kind=SecretKind.EVM_SIGNER_KEY,
        )
        if credential is None or credential.provider != "polymarket":
            raise TenantJobExecutionError("signer_credential_inactive", retryable=False)
        plaintext = self.decryptor.decrypt(
            credential.envelope,
            account_id=str(job.account_id),
            kind=SecretKind.EVM_SIGNER_KEY,
            provider="polymarket",
        )
        if len(plaintext) != 32:
            raise TenantJobExecutionError("signer_credential_invalid", retryable=False)
        return bytearray(plaintext), wallet.deposit_wallet_address

    def _job_settings(
        self,
        *,
        job: CycleJob,
        provider: AIProvider,
        model: str,
        ai_base_url: str | None,
        ai_secret: bytearray | None,
        signer_secret: bytearray | None,
        wallet_address: str | None,
        risk: RiskPolicySnapshot,
    ) -> Settings:
        updates: dict[str, Any] = {
            "account_id": str(job.account_id),
            "mode": job.mode,
            "component": "worker",
            "worker_execution_model": "tenant_queue",
            "forecast_model": model,
            # A tenant currently selects one provider/model contract. Reusing
            # that model for the independent critic keeps custom relays and
            # provider-specific model IDs valid. A future two-model option must
            # store and validate the critic model explicitly per tenant.
            "critic_model": model,
            "max_order_usd": risk.max_order_usd,
            "max_trade_risk_pct": risk.max_trade_risk_pct,
            "max_event_exposure_pct": risk.max_event_exposure_pct,
            "max_bucket_exposure_pct": risk.max_bucket_exposure_pct,
            "max_gross_exposure_pct": risk.max_gross_exposure_pct,
            "daily_loss_limit_pct": risk.daily_loss_limit_pct,
            "max_drawdown_pct": risk.max_drawdown_pct,
            "min_edge": risk.min_edge,
        }
        if provider is AIProvider.OPENAI:
            updates["ai_provider"] = "openai"
            updates["evidence_provider"] = "auto"
            updates["openai_api_key"] = SecretStr(_decode_ai_secret(ai_secret))
            updates["litellm_api_key"] = None
        elif provider in {
            AIProvider.OPENROUTER,
            AIProvider.ANTHROPIC,
            AIProvider.LITELLM,
        }:
            updates["ai_provider"] = "litellm"
            updates["evidence_provider"] = "auto"
            updates["openai_api_key"] = None
            updates["litellm_api_key"] = SecretStr(_decode_ai_secret(ai_secret))
            if provider in _PROVIDER_ENDPOINTS:
                updates["litellm_base_url"] = _PROVIDER_ENDPOINTS[provider]
        elif provider is AIProvider.CUSTOM:
            if not ai_base_url:
                raise TenantJobExecutionError("custom_provider_missing_base_url", retryable=False)
            updates["ai_provider"] = "openai_compatible"
            updates["evidence_provider"] = "auto"
            updates["openai_api_key"] = None
            updates["litellm_api_key"] = SecretStr(_decode_ai_secret(ai_secret))
            updates["litellm_base_url"] = ai_base_url
        elif provider is AIProvider.MOCK and job.mode in {
            TradingMode.PAPER,
            TradingMode.SHADOW,
        }:
            updates["ai_provider"] = "mock"
            updates["evidence_provider"] = "none"
            updates["openai_api_key"] = None
            updates["litellm_api_key"] = None
        else:
            raise TenantJobExecutionError("ai_provider_unsupported", retryable=False)

        if signer_secret is not None:
            updates["polymarket_private_key"] = SecretStr("0x" + signer_secret.hex())
            updates["polymarket_deposit_wallet"] = wallet_address
        else:
            updates["polymarket_private_key"] = None
            updates["polymarket_deposit_wallet"] = None
        return self.base_settings.validated_copy(**updates)

    async def _run_runtime(
        self,
        *,
        runtime: Runtime,
        job: CycleJob,
        job_lease: JobLeaseGuard,
        profile_version: int,
        risk: RiskPolicySnapshot,
    ) -> EngineCycleResult:
        if job.mode not in {TradingMode.CANARY, TradingMode.LIVE}:
            runtime.engine.execution_guard = job_lease.execution_allowed
            return await runtime.engine.run_cycle()
        if not isinstance(runtime.broker, PolymarketBroker) or runtime.reconciler is None:
            raise TenantJobExecutionError("live_runtime_not_isolated", retryable=False)

        account_id = str(job.account_id)
        owner_id = f"{socket.gethostname()}-{str(job.id)[:8]}-{str(uuid4())[:8]}"
        ttl = timedelta(seconds=self.base_settings.tenant_job_lease_seconds)
        lease = await runtime.store.claim_worker_lease(account_id, owner_id, ttl)
        if lease is None:
            raise TenantJobExecutionError("account_worker_lease_busy", retryable=True)
        lease_token = lease.fencing_token
        account_lease_ok = asyncio.Event()
        account_lease_ok.set()
        control_ready = asyncio.Event()
        local_stop = asyncio.Event()
        control_state = RuntimeControlWatchState()
        reconciler_task = asyncio.create_task(
            runtime.reconciler.run(),
            name=f"tenant-reconciler-{job.id}",
        )
        lease_task: asyncio.Task[None] | None = None
        control_task: asyncio.Task[None] | None = None
        successful = False

        async def current_configuration() -> bool:
            if not job_lease.is_valid:
                return False
            if job.claimed_by is None:
                return False
            if not await self.jobs.validate_lease(  # type: ignore[union-attr]
                account_id=account_id,
                job_id=job.id,
                claimed_by=job.claimed_by,
                fencing_token=job.fencing_token,
            ):
                job_lease.invalidate()
                return False
            current_profile = await self.jobs.get_or_create_profile(account_id)  # type: ignore[union-attr]
            if (
                current_profile.version != profile_version
                or current_profile.status != "active"
                or current_profile.desired_mode is not job.mode
                or current_profile.ai_credential_id != job.ai_credential_id
                or current_profile.trading_wallet_id != job.trading_wallet_id
                or current_profile.risk_policy_id != job.risk_policy_id
            ):
                return False
            current_risk = await self.jobs.get_risk_policy(  # type: ignore[union-attr]
                account_id=account_id,
                risk_policy_id=risk.id,
                expected_version=risk.version,
            )
            return (
                current_risk is not None
                and current_risk.status == "active"
                and current_risk.version == risk.version
            )

        async def broker_guard() -> int | None:
            if (
                not account_lease_ok.is_set()
                or not control_ready.is_set()
                or not runtime.reconciler.healthy.is_set()
                or not await current_configuration()
            ):
                return None
            valid = await runtime.store.validate_worker_lease(
                account_id,
                owner_id,
                lease_token,
            )
            return lease_token if valid else None

        async def engine_guard() -> bool:
            return (
                await broker_guard() is not None
                and not await runtime.store.has_unresolved_live_orders(account_id)
            )

        async def maintain_account_lease() -> None:
            interval = max(3.0, ttl.total_seconds() / 3)
            while not local_stop.is_set():
                try:
                    renewed = await runtime.store.claim_worker_lease(
                        account_id,
                        owner_id,
                        ttl,
                    )
                    if renewed is None or renewed.fencing_token != lease_token:
                        account_lease_ok.clear()
                        return
                except Exception:
                    account_lease_ok.clear()
                    self.logger.exception("account worker lease heartbeat failed")
                    return
                try:
                    await asyncio.wait_for(local_stop.wait(), timeout=interval)
                except TimeoutError:
                    pass

        async def watch_control() -> None:
            while not local_stop.is_set():
                try:
                    await _enforce_runtime_control_once(
                        store=runtime.store,
                        broker=runtime.broker,
                        account_id=account_id,
                        mode=job.mode,
                        state=control_state,
                        logger=self.logger,
                    )
                    if control_state.cancellation_pending:
                        control_ready.clear()
                    else:
                        control = await runtime.store.get_runtime_control(account_id)
                        if control.is_live_armed and control.mode is job.mode:
                            control_ready.set()
                        else:
                            control_ready.clear()
                except Exception:
                    control_ready.clear()
                    self.logger.exception("tenant runtime-control watch failed")
                    with suppress(Exception):
                        await runtime.broker.cancel_all("runtime-control watch failed")
                try:
                    await asyncio.wait_for(local_stop.wait(), timeout=2)
                except TimeoutError:
                    pass

        runtime.engine.execution_guard = engine_guard
        runtime.broker.set_execution_guard(broker_guard)
        try:
            await asyncio.wait_for(runtime.reconciler.healthy.wait(), timeout=20)
            await _enforce_runtime_control_once(
                store=runtime.store,
                broker=runtime.broker,
                account_id=account_id,
                mode=job.mode,
                state=control_state,
                logger=self.logger,
            )
            control = await runtime.store.get_runtime_control(account_id)
            if not control.is_live_armed or control.mode is not job.mode:
                raise TenantJobExecutionError("runtime_not_armed", retryable=True)
            control_ready.set()
            lease_task = asyncio.create_task(
                maintain_account_lease(),
                name=f"tenant-account-lease-{job.id}",
            )
            control_task = asyncio.create_task(
                watch_control(),
                name=f"tenant-control-watch-{job.id}",
            )
            collateral_balance, allowances_ready = (
                await runtime.broker.ensure_trading_approvals()
            )
            if job.trading_wallet_id is None:
                raise TenantJobExecutionError("trading_wallet_missing", retryable=False)
            wallet_readiness = await self.credentials.record_wallet_readiness(
                account_id=account_id,
                wallet_id=job.trading_wallet_id,
                collateral_balance_pusd=collateral_balance,
                allowances_ready=allowances_ready,
            )
            if wallet_readiness is None:
                raise TenantJobExecutionError(
                    "wallet_readiness_fence_lost",
                    retryable=True,
                )
            result = await runtime.engine.run_cycle()
            if await broker_guard() is None:
                raise TenantJobExecutionError("execution_fence_lost", retryable=True)
            if not await runtime.broker.cancel_all("tenant cycle boundary"):
                raise TenantJobExecutionError("cycle_boundary_cancel_failed", retryable=True)
            if await runtime.store.has_unresolved_live_orders(account_id):
                raise TenantJobExecutionError("cycle_boundary_order_unresolved", retryable=True)
            successful = True
            return result
        finally:
            local_stop.set()
            for task in (lease_task, control_task, reconciler_task):
                if task is not None:
                    task.cancel()
            for task in (lease_task, control_task, reconciler_task):
                if task is not None:
                    with suppress(asyncio.CancelledError):
                        await task
            if successful:
                released = await runtime.store.release_worker_lease(
                    account_id,
                    owner_id,
                    lease_token,
                )
                if not released:
                    raise TenantJobExecutionError(
                        "account_worker_lease_release_failed",
                        retryable=True,
                    )
            else:
                await _shutdown_live_safely(
                    store=runtime.store,
                    broker=runtime.broker,
                    account_id=account_id,
                    mode=job.mode,
                    owner_id=owner_id,
                    lease_token=lease_token,
                    logger=self.logger,
                )


def _decode_ai_secret(value: bytearray | None) -> str:
    if value is None:
        raise TenantJobExecutionError("ai_credential_missing", retryable=False)
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TenantJobExecutionError("ai_credential_invalid", retryable=False) from exc
    if not decoded:
        raise TenantJobExecutionError("ai_credential_invalid", retryable=False)
    return decoded


def _zero(value: bytearray | None) -> None:
    if value is not None:
        value[:] = b"\x00" * len(value)


async def run_tenant_queue_worker(settings: Settings) -> None:
    """Run the private Zeabur queue consumer; this process owns decryption."""

    if settings.component != "worker" or settings.worker_execution_model != "tenant_queue":
        raise RuntimeError("tenant queue requires a worker-only component")
    if not settings.uses_supabase:
        raise RuntimeError("tenant queue requires Supabase service-role configuration")

    from supabase import create_client

    service_key = settings.supabase_service_role_key.get_secret_value()
    client = create_client(settings.supabase_url, service_key)
    jobs = SupabaseJobRepository(client)
    if not await jobs.health():
        raise RuntimeError("tenant job repository is unavailable; apply migrations through 0015")
    credentials = SupabaseCredentialRepository(client)
    decryptor = build_worker_decryptor_from_environment()
    stores = AccountStoreFactory(
        settings.supabase_url,
        service_key,
        client=client,
    )
    executor = TenantRuntimeExecutor(
        base_settings=settings,
        jobs=jobs,
        credentials=credentials,
        decryptor=decryptor,
        stores=stores,
    )
    owner_id = f"{socket.gethostname()}-{os.getpid()}-{str(uuid4())[:8]}"
    runner = TenantJobRunner(
        repository=jobs,
        executor=executor,
        owner_id=owner_id,
        max_concurrency=settings.tenant_worker_max_concurrency,
        lease_seconds=settings.tenant_job_lease_seconds,
        poll_interval_seconds=settings.tenant_job_poll_seconds,
        schedule_due_jobs=jobs.enqueue_due_jobs,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        with suppress(AttributeError, NotImplementedError):
            loop.add_signal_handler(getattr(signal, signal_name), stop.set)
    from polybot.wallet_worker import WalletLifecycleWorker

    wallet_worker = WalletLifecycleWorker(
        repository=credentials,
        decryptor=decryptor,
        owner_id=owner_id,
        lease_seconds=settings.tenant_job_lease_seconds,
        poll_interval_seconds=settings.tenant_job_poll_seconds,
    )
    wallet_task = asyncio.create_task(
        wallet_worker.serve(stop),
        name="tenant-wallet-lifecycle",
    )
    from polybot.ai_diagnostics import AIDiagnosticWorker

    ai_diagnostic_worker = AIDiagnosticWorker(
        jobs=jobs,
        credentials=credentials,
        decryptor=decryptor,
        settings=settings,
        owner_id=owner_id,
        poll_interval_seconds=settings.tenant_job_poll_seconds,
    )
    ai_diagnostic_task = asyncio.create_task(
        ai_diagnostic_worker.serve(stop),
        name="tenant-ai-diagnostics",
    )
    from polybot.resolution_worker import MarketResolutionWorker

    resolution_worker = MarketResolutionWorker(
        repository=jobs,
        poll_interval_seconds=settings.resolution_poll_seconds,
    )
    resolution_task = asyncio.create_task(
        resolution_worker.serve(stop),
        name="market-resolution-calibration",
    )
    worker_logger = logging.getLogger("polybot.tenant_execution")

    async def publish_worker_heartbeat() -> None:
        while not stop.is_set():
            try:
                await jobs.record_worker_heartbeat(
                    owner_id=owner_id,
                    release=__version__,
                    status="ready",
                    active_jobs=runner.active_job_count,
                    details={
                        "schema_version": 15,
                        "wallet_lifecycle": True,
                        "resolution_calibration": True,
                    },
                )
            except Exception:
                worker_logger.exception("worker heartbeat publication failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                pass

    await jobs.record_worker_heartbeat(
        owner_id=owner_id,
        release=__version__,
        status="starting",
        active_jobs=0,
        details={
            "schema_version": 15,
            "wallet_lifecycle": True,
            "resolution_calibration": True,
        },
    )
    _set_worker_ready(True)
    heartbeat_task = asyncio.create_task(
        publish_worker_heartbeat(),
        name="tenant-worker-heartbeat",
    )
    try:
        await runner.serve(stop)
    finally:
        _set_worker_ready(False)
        stop.set()
        for task in (
            wallet_task,
            ai_diagnostic_task,
            resolution_task,
            heartbeat_task,
        ):
            task.cancel()
        for task in (
            wallet_task,
            ai_diagnostic_task,
            resolution_task,
            heartbeat_task,
        ):
            with suppress(asyncio.CancelledError):
                await task
        with suppress(Exception):
            await jobs.record_worker_heartbeat(
                owner_id=owner_id,
                release=__version__,
                status="stopping",
                active_jobs=0,
                details={"schema_version": 15},
            )
