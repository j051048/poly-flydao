from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from polybot.ai.calibration import CalibrationConfig, Calibrator, refresh_calibrator
from polybot.brokers.base import Broker
from polybot.brokers.polymarket import PolymarketBroker
from polybot.config import Settings, TradingMode, get_settings
from polybot.engine import LiveSafetyLatchError
from polybot.models import utc_now
from polybot.monitor import monitor_readiness
from polybot.notify import NotificationMessage, build_notifier
from polybot.personal_cycle import (
    _clear_personal_live_downgrade,
    _load_personal_paper_broker,
    _prepare_personal_live_cycle,
    _save_personal_paper_broker,
)
from polybot.personal_execution import PersonalExecutionRepository
from polybot.readiness import (
    GATE_LEASE,
    GATE_RECONCILIATION,
    GATE_RUNTIME_CONTROL,
    GATE_STORE,
    READINESS,
    ReadinessBlocker,
)
from polybot.retention import RetentionPolicy, prune_history
from polybot.runtime import build_runtime
from polybot.runtime_mode import resolve_effective_mode
from polybot.security_logging import configure_secure_logging, safe_json
from polybot.stores.base import StateStore
from polybot.stores.supabase_store import PersonalExecutionScope, SupabaseStore

# Stable, operator-facing explanations for every way reconciliation can fail.
# The worker is fail-closed, so an unattributed failure is indistinguishable
# from a crash; these codes are what makes the gate actionable in the dashboard.
_RECONCILIATION_BLOCKERS: dict[str, tuple[str, str]] = {
    "unmapped_account_trade": (
        "账户里存在无法映射到机器人订单的成交，成本基础无法证明。",
        "若该钱包曾用于手动交易：在 Polymarket 官网清仓后于「诊断」页重置对账基准；"
        "或为该机器人单独使用一个专用钱包。",
    ),
    "fill_ledger_position_mismatch": (
        "链上持仓与机器人成交台账不一致，无法计算可信的成本基础。",
        "确认没有手动买入且未清仓的仓位；有则在官网清仓后重置对账基准。",
    ),
    "fill_ledger_incomplete": (
        "成交台账不完整（存在无法解释的卖出或赎回）。",
        "在当前钱包上不要混用手动与机器人交易；确认后重置对账基准。",
    ),
    "reconciliation_error": (
        "对账过程本身失败（SDK、网络或凭证错误）。",
        "检查 Zeabur 日志中的 polybot.reconcile 记录，并确认 Polymarket 凭证与网络可达。",
    ),
}


def _set_worker_ready(value: bool) -> None:
    """Force the overall verdict.

    Gates are the source of truth while the worker is running; this helper only
    exists for the two fail-closed boundaries (startup and shutdown) where every
    gate must be dropped regardless of what the loop last observed.
    """

    if not value:
        READINESS.reset_gates()
    else:
        READINESS.set_ready(True)


def is_worker_ready() -> bool:
    """Return process-local readiness without exposing account or credential state."""

    return READINESS.ready


def worker_readiness() -> dict[str, object]:
    """Public readiness contract: gates plus operator-actionable blockers."""

    return READINESS.snapshot()


def _reconciliation_blocker(reason_code: str) -> ReadinessBlocker:
    message, fix = _RECONCILIATION_BLOCKERS.get(
        reason_code, _RECONCILIATION_BLOCKERS["reconciliation_error"]
    )
    return ReadinessBlocker(
        code=f"reconciliation_{reason_code}",
        gate=GATE_RECONCILIATION,
        message=message,
        fix=fix,
    )


@dataclass(frozen=True)
class ShutdownSafetyResult:
    control_persisted: bool
    cancellation_verified: bool
    cancellation_acknowledged: bool
    lease_released: bool


@dataclass
class RuntimeControlWatchState:
    last_version: int | None = None
    was_live_armed: bool = False
    cancellation_pending: bool = False


async def _handle_worker_health_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    """Serve the non-secret liveness contract required by Zeabur.

    The worker must never expose queue, tenant, or credential state. A TCP
    listener plus a fixed HTTP response is sufficient to distinguish a live
    process from a crashed container.
    """

    status = "404 Not Found"
    body = b'{"ok":false}'
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        parts = request_line.decode("ascii", errors="replace").strip().split()
        if len(parts) == 3 and parts[0] in {"GET", "HEAD"} and parts[1] == "/livez":
            status = "200 OK"
            body = b'{"ok":true,"role":"worker"}'
        elif len(parts) == 3 and parts[0] in {"GET", "HEAD"} and parts[1] == "/readyz":
            payload = READINESS.snapshot()
            status = "200 OK" if payload["ready"] else "503 Service Unavailable"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(parts) == 3 and parts[0] == "HEAD":
            body = b""
    except (TimeoutError, ValueError):
        status = "400 Bad Request"
        body = b'{"ok":false}'

    response = (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    writer.write(response + body)
    with suppress(ConnectionError):
        await writer.drain()
    writer.close()
    with suppress(ConnectionError):
        await writer.wait_closed()


async def _start_worker_health_server(
    *,
    host: str,
    port: int,
) -> asyncio.AbstractServer:
    return await asyncio.start_server(
        _handle_worker_health_request,
        host=host,
        port=port,
        limit=4096,
    )


async def _wait_for_store_startup(
    store: StateStore,
    logger: logging.Logger,
    *,
    attempts: int = 3,
    retry_delay_seconds: float = 2.0,
) -> bool:
    """Bound startup retries so permanent schema/account mistakes fail visibly."""

    for attempt in range(1, attempts + 1):
        if await store.health():
            return True
        logger.error("state store startup preflight failed (attempt %s/%s)", attempt, attempts)
        if retry_delay_seconds > 0 and attempt < attempts:
            await asyncio.sleep(retry_delay_seconds)
    return False


async def _enforce_runtime_control_once(
    *,
    store: StateStore,
    broker: Broker,
    account_id: str,
    mode: TradingMode,
    state: RuntimeControlWatchState,
    logger: logging.Logger,
) -> None:
    """Persist every required cancellation latch before verifying zero open orders."""

    control = await store.get_runtime_control(account_id)
    first_observation = state.last_version is None
    previous_version = state.last_version
    real_money = mode in {TradingMode.CANARY, TradingMode.LIVE}
    armed_for_worker = control.is_live_armed and control.mode is mode
    reason = "runtime control disarmed"

    # Let PostgreSQL's clock and a version CAS decide expiry. This prevents a
    # concurrent API renewal from skipping the required cancel-all transition.
    unsafe_stored_arm = False
    if real_money and control.armed and control.mode is mode:
        expired = await store.expire_runtime_control(
            account_id,
            mode,
            control.version,
        )
        if expired is not None:
            control = expired
            armed_for_worker = False
            unsafe_stored_arm = True
            reason = "runtime control arm expired"
    elif real_money and control.armed:
        control = await store.disarm_runtime_control(account_id, mode)
        armed_for_worker = False
        unsafe_stored_arm = True
        reason = "runtime control mode mismatched"

    transitioned_out_of_arm = state.was_live_armed and not armed_for_worker
    transition_requires_cancel = real_money and (
        not armed_for_worker and (first_observation or transitioned_out_of_arm or unsafe_stored_arm)
    )

    # Another worker may have completed and acknowledged the same cancellation
    # while this worker was retrying. A newer durable state supersedes only the
    # local retry flag; an unchanged state must still be latched below.
    if (
        state.cancellation_pending
        and previous_version is not None
        and control.version != previous_version
        and not control.cancellation_pending
    ):
        state.cancellation_pending = False

    cancellation_required = (
        state.cancellation_pending or control.cancellation_pending or transition_requires_cancel
    )
    if real_money and cancellation_required and not control.cancellation_pending:
        # Establish the database latch before touching the exchange. This
        # serializes against arm_runtime_control, so a failed or slow cancel-all
        # can never leave a window in which the API can re-arm execution.
        control = await store.disarm_runtime_control(account_id, mode)
        armed_for_worker = False
        if not control.cancellation_pending:
            raise RuntimeError("real-money disarm did not establish cancellation_pending")

    if real_money and (
        control.cancellation_pending or transition_requires_cancel or state.cancellation_pending
    ):
        state.cancellation_pending = True

    state.last_version = control.version
    state.was_live_armed = armed_for_worker
    if not state.cancellation_pending:
        return

    cancellation_verified = await broker.cancel_all(reason)
    if not cancellation_verified:
        logger.critical("cancel-all did not verify zero open orders: %s", reason)
        return
    if not control.cancellation_pending:
        state.cancellation_pending = False
        return

    if await store.has_unresolved_live_orders(account_id):
        logger.warning(
            "runtime cancellation remains pending while a signed, submitting "
            "or unknown order is unresolved"
        )
        return
    acknowledged = await store.acknowledge_runtime_cancellation(account_id, control.version)
    if acknowledged is None:
        logger.warning("runtime cancellation acknowledgement raced a newer control; retrying")
        return
    state.last_version = acknowledged.version
    state.cancellation_pending = False


async def _shutdown_live_safely(
    *,
    store: StateStore,
    broker: Broker,
    account_id: str,
    mode: TradingMode,
    owner_id: str,
    lease_token: int | None,
    logger: logging.Logger,
    attempts: int = 3,
    retry_delay_seconds: float = 1.0,
) -> ShutdownSafetyResult:
    """Fail closed during a real-money worker shutdown.

    The durable kill switch is written before exchange cancellation. A failed
    shutdown deliberately retains the lease so a replacement cannot trade
    until the lease expires and observes the persisted disarm.
    """

    if attempts < 1:
        raise ValueError("shutdown safety requires at least one attempt")

    control_persisted = False
    disarm_version: int | None = None
    cancellation_ack_required = True
    for attempt in range(1, attempts + 1):
        try:
            control = await store.disarm_runtime_control(account_id, mode)
            control_persisted = True
            disarm_version = control.version
            cancellation_ack_required = control.cancellation_pending
            break
        except Exception:
            logger.critical(
                "failed to persist shutdown disarm (attempt %s/%s)",
                attempt,
                attempts,
                exc_info=True,
            )
        if retry_delay_seconds > 0 and attempt < attempts:
            await asyncio.sleep(retry_delay_seconds)

    lease_confirmed = lease_token is not None
    cancellation_verified = False
    for attempt in range(1, attempts + 1):
        if lease_token is not None:
            try:
                if not await store.validate_worker_lease(account_id, owner_id, lease_token):
                    lease_confirmed = False
                    logger.critical(
                        "shutdown worker lease is no longer valid; issuing cancel-all "
                        "only as a fail-safe handoff"
                    )
            except Exception:
                lease_confirmed = False
                logger.critical(
                    "shutdown worker lease could not be validated; issuing cancel-all "
                    "only as a fail-safe handoff",
                    exc_info=True,
                )

        try:
            cancellation_verified = await broker.cancel_all("worker shutdown")
        except Exception:
            logger.critical(
                "shutdown cancel-all failed or could not be verified (attempt %s/%s)",
                attempt,
                attempts,
                exc_info=True,
            )
        if cancellation_verified:
            break
        logger.critical(
            "shutdown cancel-all did not verify zero open orders (attempt %s/%s)",
            attempt,
            attempts,
        )
        if retry_delay_seconds > 0 and attempt < attempts:
            await asyncio.sleep(retry_delay_seconds)

    lease_released = False
    safe_to_acknowledge = (
        lease_token is not None and lease_confirmed and control_persisted and cancellation_verified
    )
    if safe_to_acknowledge:
        try:
            lease_confirmed = await store.validate_worker_lease(account_id, owner_id, lease_token)
        except Exception:
            lease_confirmed = False
            logger.critical(
                "shutdown worker lease final validation failed; retaining lease for handoff",
                exc_info=True,
            )
        safe_to_acknowledge = lease_confirmed

    cancellation_acknowledged = not cancellation_ack_required
    if safe_to_acknowledge and cancellation_ack_required and disarm_version is not None:
        try:
            unresolved = await store.has_unresolved_live_orders(account_id)
            if not unresolved:
                acknowledged = await store.acknowledge_runtime_cancellation(
                    account_id, disarm_version
                )
                cancellation_acknowledged = acknowledged is not None
            else:
                logger.critical(
                    "shutdown cancellation remains pending because an in-flight order is unresolved"
                )
        except Exception:
            logger.critical(
                "shutdown cancellation acknowledgement failed; retaining lease for handoff",
                exc_info=True,
            )
    safe_to_release = safe_to_acknowledge and cancellation_acknowledged
    if safe_to_release:
        try:
            lease_released = await store.release_worker_lease(account_id, owner_id, lease_token)
        except Exception:
            logger.critical("shutdown worker lease release failed", exc_info=True)

    if lease_token is not None and not lease_released:
        logger.critical(
            "shutdown safety handoff active: worker lease was not released and must "
            "expire before a replacement can take over "
            "(control_persisted=%s cancellation_verified=%s lease_confirmed=%s)",
            control_persisted,
            cancellation_verified,
            lease_confirmed,
        )

    return ShutdownSafetyResult(
        control_persisted=control_persisted,
        cancellation_verified=cancellation_verified,
        cancellation_acknowledged=cancellation_acknowledged,
        lease_released=lease_released,
    )


async def _wait_for_next_cycle(
    *,
    stop: asyncio.Event,
    cycle_trigger: asyncio.Event | None,
    timeout_seconds: float,
) -> None:
    if cycle_trigger is None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=timeout_seconds)
        except TimeoutError:
            pass
        return

    stop_task = asyncio.create_task(stop.wait())
    trigger_task = asyncio.create_task(cycle_trigger.wait())
    tasks = {stop_task, trigger_task}
    try:
        await asyncio.wait(tasks, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED)
        if trigger_task.done() and cycle_trigger.is_set():
            cycle_trigger.clear()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task




async def run_worker(
    *,
    settings: Settings | None = None,
    stop_event: asyncio.Event | None = None,
    cycle_trigger: asyncio.Event | None = None,
    cycle_observer: Callable[[dict[str, object]], None] | None = None,
    install_signal_handlers: bool = True,
) -> None:
    settings = settings or get_settings()
    READINESS.reset(
        role="worker",
        component=settings.component,
        mode=settings.mode.value,
    )
    _set_worker_ready(False)
    if settings.component == "api":
        raise RuntimeError("POLYBOT_COMPONENT=api cannot run the signer worker")
    configure_secure_logging(settings.log_level)
    notifier = build_notifier(settings.notify_webhook_url)

    async def notify_cycle(snapshot: dict[str, object]) -> None:
        if notifier is None:
            return
        state = str(snapshot.get("state") or "unknown")
        summary = snapshot.get("result_summary")
        detail = ""
        if isinstance(summary, dict):
            detail = (
                f"markets={summary.get('markets_scanned')} "
                f"forecasts={summary.get('forecasts_created')} "
                f"intents={summary.get('intents_approved')} "
                f"executions={summary.get('executions')} "
                f"skips={len(summary.get('skipped') or {})}"
            )
        await notifier.send(
            NotificationMessage(
                title=f"Polybot cycle {state}",
                message=f"{snapshot.get('message') or ''} {detail}".strip(),
                severity="error" if state == "failed" else "info",
            )
        )
    if settings.worker_execution_model == "tenant_queue":
        from polybot.tenant_execution import run_tenant_queue_worker

        health_server = await _start_worker_health_server(
            host=settings.api_host,
            port=settings.worker_health_port,
        )
        try:
            await run_tenant_queue_worker(settings)
        finally:
            health_server.close()
            await health_server.wait_closed()
        return

    runtime = build_runtime(settings)
    logger = logging.getLogger("polybot.worker")

    def publish_cycle(snapshot: dict[str, object]) -> None:
        if cycle_observer is None:
            return
        try:
            cycle_observer(snapshot)
        except Exception:
            logger.exception("personal cycle status observer failed")
        if snapshot.get("state") in {"succeeded", "failed"}:
            asyncio.get_running_loop().create_task(notify_cycle(snapshot))

    if not await _wait_for_store_startup(runtime.store, logger):
        await runtime.close()
        raise RuntimeError(
            "state store startup preflight failed; verify the Supabase Auth user, "
            "account UUID, migrations through 0020, URL, and service-role key"
        )
    READINESS.set_gate(GATE_STORE, True)
    if runtime.live_broker is not None:
        logger.info(
            "Polymarket signer ready: trading_wallet=%s signer=%s wallet_type=%s",
            runtime.live_broker.client.wallet,
            runtime.live_broker.client.signer,
            runtime.live_broker.client.wallet_type,
        )
    owner_id = f"{socket.gethostname()}-{os.getpid()}-{str(uuid4())[:8]}"
    personal_execution: PersonalExecutionRepository | None = None
    if settings.personal_mode and settings.uses_supabase:
        if not isinstance(runtime.store, SupabaseStore):
            await runtime.close()
            raise RuntimeError("personal Supabase runtime requires SupabaseStore")
        personal_execution = PersonalExecutionRepository(
            runtime.store.client,
            settings.account_id,
        )
        if not await personal_execution.schema_ready():
            await runtime.close()
            raise RuntimeError("personal runtime requires Supabase migration 0018")
        if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
            runtime.store.bind_personal_execution_scope(
                PersonalExecutionScope(owner_id=owner_id, mode=settings.mode)
            )
            bound_scope_mode: TradingMode | None = settings.mode
        else:
            bound_scope_mode = None
    stop = stop_event or asyncio.Event()
    lease_ok = asyncio.Event()
    runtime_control_ready = asyncio.Event()
    lease_token: int | None = None
    paper_state_fencing_token: int | None = None
    loop = asyncio.get_running_loop()
    if install_signal_handlers:
        for signal_name in ("SIGINT", "SIGTERM"):
            with suppress(AttributeError, NotImplementedError):
                import signal

                loop.add_signal_handler(getattr(signal, signal_name), stop.set)

    lease_ttl = timedelta(seconds=min(300, max(30, settings.scan_interval_seconds * 2)))
    reconcile_task: asyncio.Task[None] | None = None
    if runtime.reconciler is not None:
        reconcile_task = asyncio.create_task(
            runtime.reconciler.run(), name="polymarket-user-reconciler"
        )

    async def lease_guard() -> int | None:
        reconciliation_ok = runtime.reconciler is None or runtime.reconciler.healthy.is_set()
        token = lease_token
        if (
            not lease_ok.is_set()
            or not runtime_control_ready.is_set()
            or not reconciliation_ok
            or stop.is_set()
            or token is None
        ):
            return None
        valid = await runtime.store.validate_worker_lease(settings.account_id, owner_id, token)
        return token if valid else None

    async def execution_guard() -> bool:
        if not runtime_control_ready.is_set():
            return False
        if await runtime.store.has_unresolved_live_orders(settings.account_id):
            return False
        return await lease_guard() is not None

    runtime.engine.execution_guard = execution_guard
    if runtime.mode_aware is not None:
        runtime.mode_aware.set_execution_guard(lease_guard)
    elif isinstance(runtime.broker, PolymarketBroker):
        runtime.broker.set_execution_guard(lease_guard)

    async def cancel_all_or_alert(reason: str) -> bool:
        try:
            return await runtime.broker.cancel_all(reason)
        except Exception:
            logger.critical("cancel-all failed or could not be verified: %s", reason, exc_info=True)
            return False

    async def pause_personal_live(reason: str) -> bool:
        if personal_execution is None or settings.mode not in {
            TradingMode.CANARY,
            TradingMode.LIVE,
        }:
            return False
        try:
            saved = await personal_execution.set_paused(True)
        except Exception:
            logger.critical(
                "failed to persist the personal live pause latch: %s",
                reason,
                exc_info=True,
            )
            stop.set()
            return False
        if saved is None:
            logger.critical("personal live pause latch has no wallet binding: %s", reason)
            stop.set()
            return False
        logger.critical("personal live execution paused: %s", reason)
        return True

    async def restore_personal_paper_state(token: int) -> bool:
        nonlocal paper_state_fencing_token
        if personal_execution is None or settings.mode is not TradingMode.PAPER:
            return True
        if paper_state_fencing_token == token:
            return True
        paper_broker = await _load_personal_paper_broker(
            settings=settings,
            repository=personal_execution,
            owner_id=owner_id,
            fencing_token=token,
        )
        runtime.replace_paper_broker(paper_broker)
        paper_state_fencing_token = token
        logger.info("personal paper account state restored under worker fence %s", token)
        return True

    async def save_personal_paper_state(token: int) -> bool:
        nonlocal paper_state_fencing_token
        if personal_execution is None or settings.mode is not TradingMode.PAPER:
            return True
        paper_broker = runtime.paper_broker
        if paper_broker is None:
            raise RuntimeError("personal paper runtime has an incompatible broker")
        saved = await _save_personal_paper_broker(
            repository=personal_execution,
            broker=paper_broker,
            owner_id=owner_id,
            fencing_token=token,
        )
        if saved:
            return True
        # Never keep trading from a state that failed its fenced durable save.
        # The next iteration must reload the last committed state.
        paper_state_fencing_token = None
        logger.critical("personal paper state save lost its worker fence; cycle halted")
        return False

    async def align_effective_mode() -> None:
        """Adopt the durable ``desired_mode`` at a cycle boundary.

        The dashboard writes ``runtime_profiles.desired_mode``. A personal
        deployment has to act on that without a redeploy, otherwise the mode
        selector is a fake switch: the API reports success while nothing changes
        until someone edits Zeabur. Only the capability layer (may this
        deployment touch real funds at all) stays deployment-level, and any
        downgrade is published as a readiness warning instead of being silent.
        """

        nonlocal paper_state_fencing_token
        if personal_execution is None or not settings.personal_mode:
            return
        try:
            desired = await personal_execution.get_desired_mode()
        except Exception:
            logger.warning("desired mode lookup failed; keeping the current mode")
            return
        if not isinstance(desired, TradingMode) or desired is settings.mode:
            await bind_live_scope_if_needed()
            return
        decision = resolve_effective_mode(
            desired,
            live_enabled=settings.personal_live_enabled,
            signer_configured=runtime.live_broker is not None,
        )
        previous = settings.mode
        if decision.effective is previous:
            # The request was clamped (for example canary without a live
            # deployment). Surface the reason once and keep running safely.
            if decision.note:
                READINESS.set_warning("mode_downgraded", decision.note)
            await bind_live_scope_if_needed()
            return
        settings.mode = decision.effective
        READINESS.set_mode(decision.effective.value)
        # The simulator must be reloaded under the new mode before it trades.
        paper_state_fencing_token = None
        if decision.effective in {TradingMode.CANARY, TradingMode.LIVE}:
            if runtime.live_broker is None:
                settings.mode = previous
                READINESS.set_mode(previous.value)
                logger.critical("refusing a live mode switch without a live broker")
                return
        await bind_live_scope_if_needed()
        if decision.note:
            READINESS.set_warning("mode_downgraded", decision.note)
        else:
            READINESS.clear_warning("mode_downgraded")
        logger.warning(
            "effective trading mode changed %s -> %s (desired %s)",
            previous.value,
            decision.effective.value,
            decision.desired.value,
        )

    async def bind_live_scope_if_needed() -> None:
        """Bind (or re-bind) the durable live submission gate for the current mode.

        The scope is live-only, and the lease may not be held yet on the first
        loop iteration, so this retries until the gate matches the effective mode.
        """

        nonlocal bound_scope_mode
        mode = settings.mode
        if (
            mode not in {TradingMode.CANARY, TradingMode.LIVE}
            or bound_scope_mode is mode
            or runtime.live_broker is None
            or lease_token is None
        ):
            return
        runtime.store.bind_personal_execution_scope(
            PersonalExecutionScope(owner_id=owner_id, mode=mode)
        )
        bound_scope_mode = mode

    calibrator = Calibrator(CalibrationConfig())
    attach_calibrator = getattr(
        getattr(runtime.engine, "forecaster", None), "set_calibrator", None
    )
    if callable(attach_calibrator):
        attach_calibrator(calibrator)
    calibration_refreshed_at: float | None = None

    async def maybe_refresh_calibration() -> None:
        """Keep the AI reliability curve in step with the account's own results.

        The curve is rebuilt from resolved forecasts at most once per
        ``POLYBOT_CALIBRATION_REFRESH_SECONDS`` so a long campaign slowly
        corrects a systematically over-confident model instead of trusting it
        forever.
        """

        nonlocal calibration_refreshed_at
        now_monotonic = loop.time()
        if (
            calibration_refreshed_at is not None
            and now_monotonic - calibration_refreshed_at < settings.calibration_refresh_seconds
        ):
            return
        try:
            active = await refresh_calibrator(
                runtime.store,
                account_id=settings.account_id,
                calibrator=calibrator,
            )
        except Exception:
            logger.warning(
                "reliability calibration refresh failed; keeping the current curve",
                exc_info=True,
            )
            calibration_refreshed_at = now_monotonic
            return
        calibration_refreshed_at = now_monotonic
        if active:
            logger.info("AI reliability calibration active: %s", calibrator.describe())
        else:
            logger.info("AI reliability calibration inactive (insufficient history)")

    retention_policy = RetentionPolicy.from_settings(settings)
    retention_pruned_at: float | None = None

    async def maybe_prune_history() -> None:
        """Keep the append-only history tables bounded without a manual job.

        A long deployment writes AI usage, equity points, and snapshots on every
        cycle and would otherwise grow forever. Pruning is bounded per call and
        only ever touches those three tables, so running it on a daily timer is
        safe while the worker keeps trading.
        """

        nonlocal retention_pruned_at
        if not retention_policy.enabled:
            return
        now_monotonic = loop.time()
        if (
            retention_pruned_at is not None
            and now_monotonic - retention_pruned_at < retention_policy.interval_seconds
        ):
            return
        retention_pruned_at = now_monotonic
        prune = getattr(runtime.store, "prune_history", None)
        if not callable(prune):
            logger.debug("state store has no retention support; skipping the prune pass")
            return
        try:
            removed = await prune_history(runtime.store, retention_policy)
        except Exception:
            logger.warning("history pruning failed; retrying on the next interval", exc_info=True)
            return
        if any(removed.values()):
            logger.info("pruned history rows: %s", safe_json(removed))

    ai_budget_alerted = False
    ai_spend_alerted_at: float | None = None

    async def review_ai_spend(report: object) -> None:
        """Surface AI budget exhaustion and unusually expensive cycles."""

        nonlocal ai_budget_alerted, ai_spend_alerted_at
        skipped = getattr(report, "skipped", {}) or {}
        exhausted = "ai_budget_exhausted" in skipped
        if exhausted and not ai_budget_alerted:
            ai_budget_alerted = True
            READINESS.set_warning(
                "ai_budget_exhausted",
                "当日 AI 预算已用尽：本轮不再产生新预测，已持仓仍会按风控退出；"
                "UTC 零点后自动恢复，或在控制台提高预算上限。",
            )
            logger.warning("daily AI budget exhausted; new forecasts paused")
            if notifier is not None:
                await notifier.send(
                    NotificationMessage(
                        title="Polybot AI budget exhausted",
                        message=(
                            "The daily AI request budget is exhausted. New forecasts are "
                            "paused until UTC midnight; risk exits still run."
                        ),
                        severity="warning",
                    )
                )
        elif not exhausted and ai_budget_alerted:
            ai_budget_alerted = False
            READINESS.clear_warning("ai_budget_exhausted")

        units = int(getattr(report, "ai_units_reserved", 0) or 0)
        cost = getattr(report, "ai_cost_usd", None) or Decimal("0")
        if units < settings.ai_cycle_units_alert and cost < settings.ai_cycle_cost_alert_usd:
            return
        now_monotonic = loop.time()
        if ai_spend_alerted_at is not None and now_monotonic - ai_spend_alerted_at < 3600:
            return
        ai_spend_alerted_at = now_monotonic
        logger.warning("cycle AI spend is high: units=%s cost_usd=%s", units, cost)
        if notifier is not None:
            await notifier.send(
                NotificationMessage(
                    title="Polybot AI spend alert",
                    message=(
                        f"One cycle reserved {units} AI requests and an estimated "
                        f"{cost} USD. Check the AI usage page and the market universe."
                    ),
                    severity="warning",
                )
            )

    def refresh_worker_readiness() -> None:
        stopped = stop.is_set()
        if runtime.reconciler is not None and runtime.reconciler.quarantine_count:
            READINESS.set_warning(
                "reconciliation_quarantine",
                f"{runtime.reconciler.quarantine_count} 笔成交或持仓处于隔离区："
                "它们不计入机器人权益，相关市场也不会被交易。",
            )
        else:
            READINESS.clear_warning("reconciliation_quarantine")
        READINESS.set_gate(
            GATE_LEASE,
            not stopped and lease_ok.is_set(),
            (
                ReadinessBlocker(
                    code="lease_unavailable",
                    gate=GATE_LEASE,
                    message="Worker 租约未持有：另一个副本占用了该账户，或租约心跳失败。",
                    fix="确认 Zeabur 个人服务只有一个副本（WEB_CONCURRENCY=1），"
                    "且 Supabase 可达。",
                ),
            ),
        )
        READINESS.set_gate(
            GATE_RUNTIME_CONTROL,
            not stopped and runtime_control_ready.is_set(),
            (
                ReadinessBlocker(
                    code="runtime_control_blocked",
                    gate=GATE_RUNTIME_CONTROL,
                    message="运行控制未就绪：暂停中、撤单进行中，或存在未确认的撤单。",
                    fix="在控制台解除暂停，等撤单确认完成后自动恢复。",
                ),
            ),
        )
        reconciliation_ok = runtime.reconciler is None or runtime.reconciler.healthy.is_set()
        failure = getattr(runtime.reconciler, "last_failure", None)
        blocker = _reconciliation_blocker(getattr(failure, "code", "reconciliation_error"))
        if reconciliation_ok:
            READINESS.set_gate(GATE_RECONCILIATION, not stopped)
            READINESS.clear_warning("reconciliation_pending")
        elif settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
            READINESS.set_gate(GATE_RECONCILIATION, False, (blocker,))
            READINESS.clear_warning("reconciliation_pending")
        else:
            # Paper and shadow cannot touch funds, so leftover manual activity must
            # not block a first successful run the way it did before. It blocks the
            # switch into a real-money mode instead, and the reason is published so
            # the dashboard can offer the one-click baseline reset.
            READINESS.set_gate(GATE_RECONCILIATION, not stopped)
            READINESS.set_warning(
                "reconciliation_pending",
                f"{blocker.message}{blocker.fix}（切换到 canary/live 前必须解决）",
            )

    async def watch_runtime_control() -> None:
        state = RuntimeControlWatchState()
        while not stop.is_set():
            # Recomputed every tick: a dashboard switch out of a live mode has to
            # arm the downgrade cleanup (cancel + disarm) without a restart.
            downgrade_mode = bool(
                settings.personal_mode
                and settings.mode in {TradingMode.PAPER, TradingMode.SHADOW}
            )
            try:
                token = lease_token
                if downgrade_mode:
                    cleanup_ready = bool(
                        lease_ok.is_set()
                        and token is not None
                        and await _clear_personal_live_downgrade(
                            settings=settings,
                            store=runtime.store,
                            owner_id=owner_id,
                            fencing_token=token,
                            logger=logger,
                        )
                    )
                    if cleanup_ready:
                        runtime_control_ready.set()
                    else:
                        runtime_control_ready.clear()
                else:
                    await _enforce_runtime_control_once(
                        store=runtime.store,
                        broker=runtime.broker,
                        account_id=settings.account_id,
                        mode=settings.mode,
                        state=state,
                        logger=logger,
                    )
                    if state.cancellation_pending:
                        runtime_control_ready.clear()
                    else:
                        runtime_control_ready.set()
            except Exception:
                runtime_control_ready.clear()
                logger.exception("runtime-control watch failed")
                if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
                    state.cancellation_pending = not await cancel_all_or_alert(
                        "runtime-control watch failed"
                    )
            refresh_worker_readiness()
            try:
                await asyncio.wait_for(stop.wait(), timeout=2.0)
            except TimeoutError:
                pass

    async def maintain_lease() -> None:
        nonlocal lease_token
        interval = max(5.0, lease_ttl.total_seconds() / 3)
        while not stop.is_set():
            try:
                lease = await runtime.store.claim_worker_lease(
                    settings.account_id, owner_id, lease_ttl
                )
                if lease is not None:
                    lease_token = lease.fencing_token
                    lease_ok.set()
                else:
                    lease_token = None
                    lease_ok.clear()
                    logger.warning("worker lease held by another replica")
                    if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
                        await cancel_all_or_alert("worker lease lost")
            except Exception:
                lease_token = None
                lease_ok.clear()
                logger.exception("worker lease heartbeat failed")
                if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
                    await cancel_all_or_alert("worker lease heartbeat failed")
            refresh_worker_readiness()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    lease_task = asyncio.create_task(maintain_lease(), name="polybot-lease-heartbeat")
    control_task = asyncio.create_task(
        watch_runtime_control(), name="polybot-runtime-control-watch"
    )
    readiness_task = asyncio.create_task(
        monitor_readiness(
            stop=stop,
            notifier=notifier,
            threshold_seconds=settings.readiness_alert_seconds,
            poll_seconds=max(15.0, min(60.0, settings.readiness_alert_seconds / 4)),
        ),
        name="polybot-readiness-monitor",
    )

    async def sync_reconcile_baseline() -> None:
        """Adopt a durable baseline change without a redeploy.

        An operator resets the baseline from the dashboard when a dedicated
        wallet carries pre-existing manual activity. The worker has to pick
        that up on its own: the whole point is to avoid a Zeabur redeploy.
        """

        if runtime.reconciler is None or personal_execution is None:
            return
        try:
            stored = await personal_execution.get_reconcile_baseline()
        except Exception:
            logger.warning("reconcile baseline lookup failed; keeping the current value")
            return
        if stored is None or stored == runtime.reconciler.baseline_utc:
            return
        runtime.reconciler.set_baseline(stored)
        logger.warning("adopted the durable reconcile baseline; replaying account history")

    try:
        while not stop.is_set():
            await align_effective_mode()
            await sync_reconcile_baseline()
            await maybe_refresh_calibration()
            await maybe_prune_history()
            refresh_worker_readiness()
            try:
                if not lease_ok.is_set():
                    logger.warning("execution skipped until worker lease is healthy")
                elif not runtime_control_ready.is_set():
                    logger.error(
                        "execution skipped until runtime control and cancellation state are healthy"
                    )
                elif (
                    runtime.reconciler is not None
                    and not runtime.reconciler.healthy.is_set()
                    and settings.mode in {TradingMode.CANARY, TradingMode.LIVE}
                ):
                    logger.error("execution skipped until account reconciliation is healthy")
                    if not await cancel_all_or_alert("account reconciliation unhealthy"):
                        await pause_personal_live("account reconciliation cancel-all failed")
                elif await runtime.store.has_unresolved_live_orders(settings.account_id):
                    logger.critical(
                        "execution blocked: an ambiguous signed/submitting order requires review"
                    )
                    await cancel_all_or_alert("ambiguous durable order state")
                    await pause_personal_live("ambiguous durable order state")
                elif not await runtime.store.health():
                    READINESS.set_gate(
                        GATE_STORE,
                        False,
                        (
                            ReadinessBlocker(
                                code="store_unhealthy",
                                gate=GATE_STORE,
                                message="状态存储不可用：无法读写数据库。",
                                fix="检查 Supabase 项目状态、service-role key 与出网连接。",
                            ),
                        ),
                    )
                    logger.error("state store unhealthy; execution skipped")
                    if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
                        if not await cancel_all_or_alert("state store unhealthy"):
                            await pause_personal_live("state-store cancel-all failed")
                else:
                    READINESS.set_gate(GATE_STORE, True)
                    current_fence = lease_token
                    cycle_ready = current_fence is not None
                    if cycle_ready and settings.mode is TradingMode.PAPER:
                        cycle_ready = await restore_personal_paper_state(current_fence)
                    if (
                        cycle_ready
                        and personal_execution is not None
                        and settings.mode in {TradingMode.CANARY, TradingMode.LIVE}
                    ):
                        if runtime.live_broker is None:
                            raise RuntimeError(
                                "personal real-money runtime requires PolymarketBroker"
                            )
                        try:
                            cycle_ready = await _prepare_personal_live_cycle(
                                settings=settings,
                                broker=runtime.live_broker,
                                repository=personal_execution,
                                owner_id=owner_id,
                                fencing_token=current_fence,
                                logger=logger,
                            )
                        except Exception as exc:
                            # No collateral, an approval transport failure, or a
                            # short lease/control race is recoverable. Keep the
                            # durable manual pause unchanged and retry later.
                            logger.warning(
                                "personal live readiness is not ready (%s); retrying",
                                type(exc).__name__,
                            )
                            cycle_ready = False
                    if not cycle_ready:
                        logger.info("personal cycle prerequisites are not ready; execution skipped")
                    else:
                        cycle_id = str(uuid4())
                        started_at = utc_now()
                        publish_cycle(
                            {
                                "id": cycle_id,
                                "state": "running",
                                "started_at": started_at.isoformat(),
                                "completed_at": None,
                                "message": "cycle is running",
                                "result_summary": None,
                            }
                        )
                        try:
                            report = await runtime.engine.run_cycle()
                            if (
                                settings.mode is TradingMode.PAPER
                                and not await save_personal_paper_state(current_fence)
                            ):
                                raise RuntimeError("personal paper state was not durably committed")
                        except Exception as exc:
                            if (
                                settings.mode is TradingMode.PAPER
                                and personal_execution is not None
                            ):
                                # The broker may have mutated cash/positions before
                                # the cycle raised. Force the next attempt to discard
                                # that uncommitted in-memory state and reload the last
                                # fenced Supabase snapshot.
                                paper_state_fencing_token = None
                            publish_cycle(
                                {
                                    "id": cycle_id,
                                    "state": "failed",
                                    "started_at": started_at.isoformat(),
                                    "completed_at": utc_now().isoformat(),
                                    "message": f"cycle failed ({type(exc).__name__})",
                                    "result_summary": None,
                                }
                            )
                            raise
                        publish_cycle(
                            {
                                "id": cycle_id,
                                "state": "succeeded",
                                "started_at": started_at.isoformat(),
                                "completed_at": (report.completed_at or utc_now()).isoformat(),
                                "message": "cycle completed",
                                "result_summary": {
                                    "run_id": report.run_id,
                                    "markets_scanned": report.markets_scanned,
                                    "forecasts_created": report.forecasts_created,
                                    "candidates_created": report.candidates_created,
                                    "intents_approved": report.intents_approved,
                                    "executions": len(report.executions),
                                    "skipped": report.skipped,
                                },
                            }
                        )
                        logger.info(safe_json(report.model_dump(mode="json")))
                        await review_ai_spend(report)
                        safety_latched = any(
                            reason.startswith("live_stop_latched:") for reason in report.skipped
                        )
                        if safety_latched:
                            logger.critical(
                                "real-money safety stop is durably latched; "
                                "manual review and a resume are required"
                            )
                            await pause_personal_live("deterministic live safety latch")
                            if notifier is not None:
                                asyncio.get_running_loop().create_task(
                                    notifier.send(
                                        NotificationMessage(
                                            title="Polybot live safety latch",
                                            message=(
                                                "real-money execution stopped by "
                                                "deterministic safety latch"
                                            ),
                                            severity="critical",
                                        )
                                    )
                                )
                        else:
                            redeemed = await runtime.broker.redeem_resolved()
                            if redeemed:
                                logger.info(
                                    "submitted %s resolved-position redemptions",
                                    redeemed,
                                )
            except LiveSafetyLatchError:
                logger.critical(
                    "cycle could not complete the durable real-money safety stop; "
                    "terminating into the shutdown safety sequence",
                    exc_info=True,
                )
                if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
                    await pause_personal_live("incomplete deterministic live safety latch")
                    await cancel_all_or_alert("incomplete real-money safety latch")
                stop.set()
            except Exception:
                logger.exception("cycle failed")
                if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
                    cancellation_verified = await cancel_all_or_alert("worker cycle failure")
                    unresolved = True
                    try:
                        unresolved = await runtime.store.has_unresolved_live_orders(
                            settings.account_id
                        )
                    except Exception:
                        logger.critical(
                            "failed to verify durable order state after cycle failure",
                            exc_info=True,
                        )
                    if not cancellation_verified or unresolved:
                        await pause_personal_live(
                            "cycle failure left cancellation or order state ambiguous"
                        )

            await _wait_for_next_cycle(
                stop=stop,
                cycle_trigger=cycle_trigger,
                timeout_seconds=settings.scan_interval_seconds,
            )
    finally:
        _set_worker_ready(False)
        stop.set()
        # Freeze lease/control state before the bounded shutdown sequence. The lease
        # remains valid for at least 30 seconds, while shutdown retries are bounded to
        # a few seconds.
        lease_task.cancel()
        with suppress(asyncio.CancelledError):
            await lease_task
        control_task.cancel()
        with suppress(asyncio.CancelledError):
            await control_task
        readiness_task.cancel()
        with suppress(asyncio.CancelledError):
            await readiness_task

        try:
            if reconcile_task is not None:
                reconcile_task.cancel()
                with suppress(asyncio.CancelledError):
                    await reconcile_task
            if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
                await _shutdown_live_safely(
                    store=runtime.store,
                    broker=runtime.broker,
                    account_id=settings.account_id,
                    mode=settings.mode,
                    owner_id=owner_id,
                    lease_token=lease_token,
                    logger=logger,
                )
            elif lease_token is not None:
                await runtime.store.release_worker_lease(settings.account_id, owner_id, lease_token)
        finally:
            await runtime.close()


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
