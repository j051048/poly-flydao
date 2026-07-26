from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from polybot.brokers.base import Broker
from polybot.brokers.polymarket import PolymarketBroker
from polybot.config import TradingMode
from polybot.engine import LiveSafetyLatchError
from polybot.runtime import build_runtime
from polybot.stores.base import StateStore


@dataclass(frozen=True)
class ShutdownSafetyResult:
    control_persisted: bool
    cancellation_verified: bool
    cancellation_acknowledged: bool
    lease_released: bool


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
        lease_token is not None
        and lease_confirmed
        and control_persisted
        and cancellation_verified
    )
    if safe_to_acknowledge:
        try:
            lease_confirmed = await store.validate_worker_lease(
                account_id, owner_id, lease_token
            )
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
                    "shutdown cancellation remains pending because an in-flight order "
                    "is unresolved"
                )
        except Exception:
            logger.critical(
                "shutdown cancellation acknowledgement failed; retaining lease for handoff",
                exc_info=True,
            )
    safe_to_release = safe_to_acknowledge and cancellation_acknowledged
    if safe_to_release:
        try:
            lease_released = await store.release_worker_lease(
                account_id, owner_id, lease_token
            )
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


async def run_worker() -> None:
    runtime = build_runtime()
    settings = runtime.settings
    if settings.component == "api":
        await runtime.close()
        raise RuntimeError("POLYBOT_COMPONENT=api cannot run the signer worker")
    logging.basicConfig(level=settings.log_level)
    logger = logging.getLogger("polybot.worker")
    if isinstance(runtime.broker, PolymarketBroker):
        logger.info(
            "Polymarket signer ready: trading_wallet=%s signer=%s wallet_type=%s",
            runtime.broker.client.wallet,
            runtime.broker.client.signer,
            runtime.broker.client.wallet_type,
        )
    owner_id = f"{socket.gethostname()}-{os.getpid()}-{str(uuid4())[:8]}"
    stop = asyncio.Event()
    lease_ok = asyncio.Event()
    lease_token: int | None = None
    loop = asyncio.get_running_loop()
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
        if not lease_ok.is_set() or not reconciliation_ok or stop.is_set() or token is None:
            return None
        valid = await runtime.store.validate_worker_lease(settings.account_id, owner_id, token)
        return token if valid else None

    async def execution_guard() -> bool:
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

    async def watch_runtime_control() -> None:
        last_version: int | None = None
        cancel_pending = False
        while not stop.is_set():
            try:
                control = await runtime.store.get_runtime_control(settings.account_id)
                first_observation = last_version is None
                last_version = control.version
                if settings.mode in {TradingMode.CANARY, TradingMode.LIVE} and (
                    control.cancellation_pending
                    or (
                        first_observation
                        and (control.kill_switch or not control.is_live_armed)
                    )
                ):
                    cancel_pending = True
                if cancel_pending:
                    cancellation_verified = await cancel_all_or_alert(
                        "runtime control disarmed"
                    )
                    if cancellation_verified and control.cancellation_pending:
                        unresolved = await runtime.store.has_unresolved_live_orders(
                            settings.account_id
                        )
                        acknowledged = None
                        if not unresolved:
                            acknowledged = (
                                await runtime.store.acknowledge_runtime_cancellation(
                                    settings.account_id, control.version
                                )
                            )
                        cancel_pending = acknowledged is None
                        if unresolved:
                            logger.warning(
                                "runtime cancellation remains pending while a signed, "
                                "submitting or unknown order is unresolved"
                            )
                        elif acknowledged is None:
                            logger.warning(
                                "runtime cancellation acknowledgement raced a newer control; "
                                "retrying"
                            )
                    elif cancellation_verified:
                        cancel_pending = False
            except Exception:
                logger.exception("runtime-control watch failed")
                if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
                    cancel_pending = not await cancel_all_or_alert("runtime-control watch failed")
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
            try:
                if not lease_ok.is_set():
                    logger.warning("execution skipped until worker lease is healthy")
                elif runtime.reconciler is not None and not runtime.reconciler.healthy.is_set():
                    logger.error("execution skipped until account reconciliation is healthy")
                    await cancel_all_or_alert("account reconciliation unhealthy")
                elif await runtime.store.has_unresolved_live_orders(settings.account_id):
                    logger.critical(
                        "execution blocked: an ambiguous signed/submitting order requires review"
                    )
                    await cancel_all_or_alert("ambiguous durable order state")
                elif not await runtime.store.health():
                    logger.error("state store unhealthy; execution skipped")
                    if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
                        await cancel_all_or_alert("state store unhealthy")
                else:
                    report = await runtime.engine.run_cycle()
                    logger.info(json.dumps(report.model_dump(mode="json"), ensure_ascii=False))
                    safety_latched = any(
                        reason.startswith("live_stop_latched:")
                        for reason in report.skipped
                    )
                    if safety_latched:
                        logger.critical(
                            "real-money safety stop is durably latched; "
                            "manual review and a new arm are required"
                        )
                    else:
                        redeemed = await runtime.broker.redeem_resolved()
                        if redeemed:
                            logger.info(
                                "submitted %s resolved-position redemptions", redeemed
                            )
            except LiveSafetyLatchError:
                logger.critical(
                    "cycle could not complete the durable real-money safety stop; "
                    "terminating into the shutdown safety sequence",
                    exc_info=True,
                )
                if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
                    await cancel_all_or_alert("incomplete real-money safety latch")
                stop.set()
            except Exception:
                logger.exception("cycle failed")
                if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
                    await cancel_all_or_alert("worker cycle failure")

            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.scan_interval_seconds)
            except TimeoutError:
                pass
    finally:
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
                await runtime.store.release_worker_lease(
                    settings.account_id, owner_id, lease_token
                )
        finally:
            await runtime.close()
            if reconcile_task is not None:
                reconcile_task.cancel()
                with suppress(asyncio.CancelledError):
                    await reconcile_task


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
