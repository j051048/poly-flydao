"""Personal-mode cycle preparation, downgrade cleanup, and paper state.

Split out of :mod:`polybot.worker`. These helpers own the two things that make
a personal deployment safe to switch between paper and real money: arming a
live cycle only when the wallet, approvals, and lease all agree, and cleaning
up (cancel + disarm + acknowledge) when the operator switches back down.
``polybot.worker`` re-exports them.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from typing import Any

from polybot.brokers.paper import PaperBroker
from polybot.brokers.polymarket import PolymarketBroker
from polybot.config import Settings, TradingMode
from polybot.models import utc_now
from polybot.personal_execution import PersonalExecutionRepository
from polybot.stores.base import StateStore

PersonalCleanupClientFactory = Callable[[str, str | None], Any]
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
