from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from polybot.brokers.base import Broker
from polybot.brokers.paper import PaperBroker
from polybot.brokers.polymarket import PolymarketBroker
from polybot.config import Settings, TradingMode, get_settings
from polybot.engine import LiveSafetyLatchError
from polybot.models import utc_now
from polybot.notify import NotificationMessage, build_notifier
from polybot.personal_execution import PersonalExecutionRepository
from polybot.runtime import build_runtime
from polybot.security_logging import configure_secure_logging, safe_json
from polybot.stores.base import StateStore
from polybot.stores.supabase_store import PersonalExecutionScope, SupabaseStore

_WORKER_READY = False
PersonalCleanupClientFactory = Callable[[str, str | None], Any]


def _set_worker_ready(value: bool) -> None:
    global _WORKER_READY
    _WORKER_READY = value


def is_worker_ready() -> bool:
    """Return process-local readiness without exposing account or credential state."""

    return _WORKER_READY


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
            if _WORKER_READY:
                status = "200 OK"
                body = b'{"ok":true,"role":"worker","ready":true}'
            else:
                status = "503 Service Unavailable"
                body = b'{"ok":false,"role":"worker","ready":false}'
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


async def _prepare_personal_live_cycle(
    *,
    settings: Settings,
    broker: PolymarketBroker,
    repository: PersonalExecutionRepository,
    owner_id: str,
    fencing_token: int,
    logger: logging.Logger,
) -> bool:
    """Refresh the short-lived personal authority without persisting secrets."""

    environment = broker.client.environment
    binding = await repository.bind_wallet(
        owner_id=owner_id,
        fencing_token=fencing_token,
        signer_address=str(broker.client.signer),
        deposit_wallet_address=str(broker.client.wallet),
        chain_id=int(environment.chain_id),
        collateral_token=str(environment.collateral_token),
    )
    if binding is None:
        logger.warning("personal wallet binding lost its active worker lease; retrying")
        return False
    if binding.paused:
        logger.info("personal live execution is paused")
        return False

    now = utc_now()
    control = await repository.get_runtime_control()
    if control.cancellation_pending:
        logger.info("personal live execution is waiting for cancellation acknowledgement")
        return False
    # An expired stored arm must first be expired and cancellation-acknowledged
    # by the runtime-control watcher. Calling the arm RPC in this state is a CAS
    # no-op, so treat it as a normal retry instead of a permanent failure.
    if control.armed and not control.is_live_armed:
        logger.info("expired personal runtime arm is being safely rolled over")
        return False
    renew_before = now + timedelta(minutes=5)
    needs_arm = (
        not control.is_live_armed
        or control.mode is not settings.mode
        or control.armed_until is None
        or control.armed_until <= renew_before
    )
    if needs_arm:
        control = await repository.arm(
            owner_id=owner_id,
            fencing_token=fencing_token,
            mode=settings.mode,
            armed_until=now + timedelta(minutes=10),
            expected_version=control.version,
        )
        if control is None:
            logger.info("personal runtime arm raced a control or lease update; retrying")
            return False

    collateral, allowances_ready = await broker.ensure_trading_approvals()
    ready_binding = await repository.record_wallet_readiness(
        owner_id=owner_id,
        fencing_token=fencing_token,
        binding_version=binding.binding_version,
        collateral_balance_pusd=collateral,
        allowances_ready=allowances_ready,
    )
    if ready_binding is None:
        logger.warning("personal wallet readiness lost its active fence; retrying")
        return False
    return bool(
        not ready_binding.paused
        and ready_binding.allowances_ready
        and ready_binding.collateral_balance_pusd is not None
        and ready_binding.collateral_balance_pusd > 0
    )


def _personal_cleanup_client(private_key: str, wallet: str | None) -> Any:
    from polymarket import SecureClient

    return SecureClient.create(private_key=private_key, wallet=wallet)


async def _clear_personal_live_downgrade(
    *,
    settings: Settings,
    store: StateStore,
    owner_id: str,
    fencing_token: int,
    logger: logging.Logger,
    client_factory: PersonalCleanupClientFactory = _personal_cleanup_client,
) -> bool:
    """Fail closed before a former live deployment may run paper/shadow cycles."""

    if settings.mode not in {TradingMode.PAPER, TradingMode.SHADOW}:
        return True
    try:
        control = await store.get_runtime_control(settings.account_id)
        unresolved = await store.has_unresolved_live_orders(settings.account_id)
        historical_live = bool(
            control.mode in {TradingMode.CANARY, TradingMode.LIVE}
            or control.armed
            or control.accept_new_intents
            or not control.kill_switch
            or control.cancellation_pending
            or unresolved
        )
        lease_valid = await store.validate_worker_lease(
            settings.account_id,
            owner_id,
            fencing_token,
        )
        if not lease_valid:
            logger.warning("personal downgrade cleanup is waiting for the active worker lease")
            return False
        if not historical_live:
            if control.mode is settings.mode:
                return True
            aligned = await store.disarm_runtime_control(
                settings.account_id,
                settings.mode,
            )
            return bool(
                aligned.mode is settings.mode
                and not aligned.armed
                and aligned.kill_switch
                and await store.validate_worker_lease(
                    settings.account_id,
                    owner_id,
                    fencing_token,
                )
            )

        historical_mode = (
            control.mode
            if control.mode in {TradingMode.CANARY, TradingMode.LIVE}
            else TradingMode.LIVE
        )
        if (
            control.cancellation_pending
            and not control.armed
            and not control.accept_new_intents
            and control.kill_switch
            and control.mode is historical_mode
        ):
            disarmed = control
        else:
            disarmed = await store.disarm_runtime_control(
                settings.account_id,
                historical_mode,
            )

        key = settings.polymarket_private_key
        if key is None:
            logger.critical(
                "POLYMARKET_PRIVATE_KEY is required to clear the former live wallet "
                "before paper/shadow execution"
            )
            return False

        client: Any | None = None
        try:
            client = await asyncio.to_thread(
                client_factory,
                key.get_secret_value(),
                settings.polymarket_deposit_wallet,
            )
            await asyncio.to_thread(client.cancel_all)
            open_orders: list[Any] = []
            for delay in (0.0, 0.2, 0.5):
                if delay:
                    await asyncio.sleep(delay)
                open_orders = await asyncio.to_thread(
                    lambda: list(client.list_open_orders().iter_items())
                )
                if not open_orders:
                    break
            if open_orders:
                logger.critical(
                    "personal downgrade cleanup could not verify zero exchange open orders"
                )
                return False
        finally:
            if client is not None:
                close = getattr(client, "close", None)
                if callable(close):
                    with suppress(Exception):
                        await asyncio.to_thread(close)

        if not await store.validate_worker_lease(
            settings.account_id,
            owner_id,
            fencing_token,
        ):
            logger.critical("personal downgrade cleanup lost its worker lease")
            return False
        if await store.has_unresolved_live_orders(settings.account_id):
            logger.critical(
                "personal downgrade cleanup is blocked by a signed, submitting, "
                "or unknown durable order"
            )
            return False
        acknowledged = await store.acknowledge_runtime_cancellation(
            settings.account_id,
            disarmed.version,
        )
        if acknowledged is None:
            logger.warning("personal downgrade cancellation acknowledgement raced; retrying")
            return False
        if not await store.validate_worker_lease(
            settings.account_id,
            owner_id,
            fencing_token,
        ):
            logger.critical("personal downgrade cleanup lost its lease before mode alignment")
            return False
        aligned = await store.disarm_runtime_control(
            settings.account_id,
            settings.mode,
        )
        if not await store.validate_worker_lease(
            settings.account_id,
            owner_id,
            fencing_token,
        ):
            logger.critical("personal downgrade cleanup lost its lease after mode alignment")
            return False
        return bool(
            aligned.mode is settings.mode
            and not aligned.armed
            and not aligned.accept_new_intents
            and aligned.kill_switch
            and not aligned.cancellation_pending
        )
    except Exception:
        logger.critical(
            "personal downgrade cleanup failed; paper/shadow execution remains blocked",
            exc_info=True,
        )
        return False


async def _load_personal_paper_broker(
    *,
    settings: Settings,
    repository: PersonalExecutionRepository,
    owner_id: str,
    fencing_token: int,
) -> PaperBroker:
    state = await repository.load_paper_state(
        owner_id=owner_id,
        fencing_token=fencing_token,
    )
    return PaperBroker.from_state(
        settings.bankroll_usd,
        state.state if state is not None else None,
    )


async def _save_personal_paper_broker(
    *,
    repository: PersonalExecutionRepository,
    broker: PaperBroker,
    owner_id: str,
    fencing_token: int,
) -> bool:
    saved = await repository.save_paper_state(
        owner_id=owner_id,
        fencing_token=fencing_token,
        state=broker.export_state(),
    )
    return saved is not None


async def run_worker(
    *,
    settings: Settings | None = None,
    stop_event: asyncio.Event | None = None,
    cycle_trigger: asyncio.Event | None = None,
    cycle_observer: Callable[[dict[str, object]], None] | None = None,
    install_signal_handlers: bool = True,
) -> None:
    settings = settings or get_settings()
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
            "account UUID, migrations through 0018, URL, and service-role key"
        )
    if isinstance(runtime.broker, PolymarketBroker):
        logger.info(
            "Polymarket signer ready: trading_wallet=%s signer=%s wallet_type=%s",
            runtime.broker.client.wallet,
            runtime.broker.client.signer,
            runtime.broker.client.wallet_type,
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
    if isinstance(runtime.broker, PolymarketBroker):
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
        runtime.broker = paper_broker
        runtime.engine.broker = paper_broker
        paper_state_fencing_token = token
        logger.info("personal paper account state restored under worker fence %s", token)
        return True

    async def save_personal_paper_state(token: int) -> bool:
        nonlocal paper_state_fencing_token
        if personal_execution is None or settings.mode is not TradingMode.PAPER:
            return True
        if not isinstance(runtime.broker, PaperBroker):
            raise RuntimeError("personal paper runtime has an incompatible broker")
        saved = await _save_personal_paper_broker(
            repository=personal_execution,
            broker=runtime.broker,
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

    def refresh_worker_readiness() -> None:
        reconciliation_ok = runtime.reconciler is None or runtime.reconciler.healthy.is_set()
        _set_worker_ready(
            not stop.is_set()
            and lease_ok.is_set()
            and runtime_control_ready.is_set()
            and reconciliation_ok
        )

    async def watch_runtime_control() -> None:
        state = RuntimeControlWatchState()
        downgrade_mode = bool(
            settings.personal_mode and settings.mode in {TradingMode.PAPER, TradingMode.SHADOW}
        )
        while not stop.is_set():
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

    try:
        while not stop.is_set():
            refresh_worker_readiness()
            try:
                if not lease_ok.is_set():
                    logger.warning("execution skipped until worker lease is healthy")
                elif not runtime_control_ready.is_set():
                    logger.error(
                        "execution skipped until runtime control and cancellation state are healthy"
                    )
                elif runtime.reconciler is not None and not runtime.reconciler.healthy.is_set():
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
                    logger.error("state store unhealthy; execution skipped")
                    if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
                        if not await cancel_all_or_alert("state store unhealthy"):
                            await pause_personal_live("state-store cancel-all failed")
                else:
                    current_fence = lease_token
                    cycle_ready = current_fence is not None
                    if cycle_ready and settings.mode is TradingMode.PAPER:
                        cycle_ready = await restore_personal_paper_state(current_fence)
                    if (
                        cycle_ready
                        and personal_execution is not None
                        and settings.mode in {TradingMode.CANARY, TradingMode.LIVE}
                    ):
                        if not isinstance(runtime.broker, PolymarketBroker):
                            raise RuntimeError(
                                "personal real-money runtime requires PolymarketBroker"
                            )
                        try:
                            cycle_ready = await _prepare_personal_live_cycle(
                                settings=settings,
                                broker=runtime.broker,
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
