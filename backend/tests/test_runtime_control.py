from __future__ import annotations

import asyncio
from datetime import timedelta

from polybot.config import TradingMode
from polybot.jobs import InMemoryJobRepository
from polybot.models import utc_now
from polybot.stores.memory import MemoryStore
from polybot.worker import RuntimeControlWatchState, _enforce_runtime_control_once


async def test_memory_arm_is_compare_and_swap() -> None:
    store = MemoryStore()
    account_id = "11111111-1111-1111-1111-111111111111"
    initial = await store.get_runtime_control(account_id)

    armed = await store.arm_runtime_control(
        account_id,
        TradingMode.CANARY,
        utc_now() + timedelta(minutes=2),
        initial.version,
    )
    assert armed is not None
    assert armed.version == initial.version + 1
    assert armed.is_live_armed is True

    stale = await store.arm_runtime_control(
        account_id,
        TradingMode.CANARY,
        utc_now(),
        initial.version,
    )
    assert stale is None
    assert await store.get_runtime_control(account_id) == armed


async def test_disarm_latch_wins_over_a_concurrent_stale_arm() -> None:
    store = MemoryStore()
    account_id = "22222222-2222-2222-2222-222222222222"
    initial = await store.get_runtime_control(account_id)
    first_arm = await store.arm_runtime_control(
        account_id,
        TradingMode.LIVE,
        utc_now() + timedelta(minutes=2),
        initial.version,
    )
    assert first_arm is not None

    stale_version = first_arm.version
    disarmed, stale_arm = await asyncio.gather(
        store.disarm_runtime_control(account_id, TradingMode.LIVE),
        store.arm_runtime_control(
            account_id,
            TradingMode.LIVE,
            utc_now() + timedelta(minutes=2),
            stale_version,
        ),
    )

    assert disarmed.kill_switch is True
    assert stale_arm is None
    current = await store.get_runtime_control(account_id)
    assert current == disarmed
    assert current.armed is False
    assert current.accept_new_intents is False
    assert current.cancellation_pending is True
    assert current.version == stale_version + 1

    blocked = await store.arm_runtime_control(
        account_id,
        TradingMode.LIVE,
        utc_now() + timedelta(minutes=2),
        current.version,
    )
    assert blocked is None

    acknowledged = await store.acknowledge_runtime_cancellation(account_id, current.version)
    assert acknowledged is not None
    assert acknowledged.cancellation_pending is False
    rearmed = await store.arm_runtime_control(
        account_id,
        TradingMode.LIVE,
        utc_now() + timedelta(minutes=2),
        acknowledged.version,
    )
    assert rearmed is not None
    assert rearmed.is_live_armed


async def test_expired_arm_cannot_be_renewed_before_cancellation() -> None:
    store = MemoryStore()
    account_id = "77777777-7777-7777-7777-777777777777"
    initial = await store.get_runtime_control(account_id)
    armed = await store.arm_runtime_control(
        account_id,
        TradingMode.LIVE,
        utc_now() + timedelta(minutes=2),
        initial.version,
    )
    assert armed is not None
    await store.set_runtime_control(
        armed.model_copy(update={"armed_until": utc_now() - timedelta(seconds=1)})
    )

    renewal = await store.arm_runtime_control(
        account_id,
        TradingMode.LIVE,
        utc_now() + timedelta(minutes=2),
        armed.version,
    )
    assert renewal is None

    expired = await store.expire_runtime_control(
        account_id,
        TradingMode.LIVE,
        armed.version,
    )
    assert expired is not None
    assert expired.cancellation_pending
    assert not expired.armed


async def test_cancellation_acknowledgement_waits_for_inflight_orders() -> None:
    store = MemoryStore()
    account_id = "55555555-5555-5555-5555-555555555555"
    disarmed = await store.disarm_runtime_control(account_id, TradingMode.CANARY)
    store.unresolved_live_orders.add("signed-intent")

    blocked = await store.acknowledge_runtime_cancellation(account_id, disarmed.version)

    assert blocked is None
    assert (await store.get_runtime_control(account_id)).cancellation_pending


async def test_tenant_arm_repository_rejects_stale_version() -> None:
    account_id = "33333333-3333-3333-3333-333333333333"
    repository = InMemoryJobRepository()
    repository.live_ready_accounts.add(account_id)
    initial = await repository.get_runtime_control(account_id)
    saved = await repository.arm(
        account_id=account_id,
        mode=TradingMode.CANARY,
        armed_until=utc_now() + timedelta(minutes=5),
        expected_version=initial.version + 1,
    )
    assert saved is None


async def test_tenant_arm_repository_rejects_pending_cancellation() -> None:
    account_id = "44444444-4444-4444-4444-444444444444"
    repository = InMemoryJobRepository()
    repository.live_ready_accounts.add(account_id)
    pending = await repository.disarm(
        account_id=account_id,
        mode=TradingMode.CANARY,
    )
    assert pending.cancellation_pending
    saved = await repository.arm(
        account_id=account_id,
        mode=TradingMode.CANARY,
        armed_until=utc_now() + timedelta(minutes=5),
        expected_version=pending.version,
    )
    assert saved is None


async def test_expired_arm_is_durably_disarmed_and_gtc_orders_are_cancelled() -> None:
    class Broker:
        def __init__(self) -> None:
            self.reasons: list[str] = []

        async def cancel_all(self, reason: str) -> bool:
            self.reasons.append(reason)
            return True

    account_id = "66666666-6666-6666-6666-666666666666"
    store = MemoryStore()
    initial = await store.get_runtime_control(account_id)
    armed = await store.arm_runtime_control(
        account_id,
        TradingMode.CANARY,
        utc_now() + timedelta(minutes=2),
        initial.version,
    )
    assert armed is not None
    state = RuntimeControlWatchState()
    broker = Broker()

    await _enforce_runtime_control_once(
        store=store,
        broker=broker,  # type: ignore[arg-type]
        account_id=account_id,
        mode=TradingMode.CANARY,
        state=state,
        logger=__import__("logging").getLogger("test.runtime-control"),
    )
    assert broker.reasons == []

    await store.set_runtime_control(
        armed.model_copy(update={"armed_until": utc_now() - timedelta(seconds=1)})
    )
    await _enforce_runtime_control_once(
        store=store,
        broker=broker,  # type: ignore[arg-type]
        account_id=account_id,
        mode=TradingMode.CANARY,
        state=state,
        logger=__import__("logging").getLogger("test.runtime-control"),
    )

    current = await store.get_runtime_control(account_id)
    assert broker.reasons == ["runtime control arm expired"]
    assert not current.armed
    assert current.kill_switch
    assert not current.cancellation_pending


async def test_first_real_money_observation_latches_before_cancel_and_blocks_rearm() -> None:
    account_id = "88888888-8888-8888-8888-888888888888"
    store = MemoryStore()

    class FailingBroker:
        def __init__(self) -> None:
            self.pending_during_cancel: list[bool] = []
            self.rearm_results: list[object | None] = []

        async def cancel_all(self, reason: str) -> bool:
            control = await store.get_runtime_control(account_id)
            self.pending_during_cancel.append(control.cancellation_pending)
            self.rearm_results.append(
                await store.arm_runtime_control(
                    account_id,
                    TradingMode.CANARY,
                    utc_now() + timedelta(minutes=2),
                    control.version,
                )
            )
            return False

    broker = FailingBroker()
    state = RuntimeControlWatchState()
    logger = __import__("logging").getLogger("test.runtime-control")

    await _enforce_runtime_control_once(
        store=store,
        broker=broker,  # type: ignore[arg-type]
        account_id=account_id,
        mode=TradingMode.CANARY,
        state=state,
        logger=logger,
    )

    current = await store.get_runtime_control(account_id)
    assert broker.pending_during_cancel == [True]
    assert broker.rearm_results == [None]
    assert current.cancellation_pending
    failed_cancel_version = current.version

    await _enforce_runtime_control_once(
        store=store,
        broker=broker,  # type: ignore[arg-type]
        account_id=account_id,
        mode=TradingMode.CANARY,
        state=state,
        logger=logger,
    )

    assert (await store.get_runtime_control(account_id)).version == failed_cancel_version
    assert broker.pending_during_cancel == [True, True]
    assert broker.rearm_results == [None, None]


async def test_transition_out_of_arm_latches_before_cancel() -> None:
    account_id = "99999999-9999-9999-9999-999999999999"
    store = MemoryStore()
    initial = await store.get_runtime_control(account_id)
    armed = await store.arm_runtime_control(
        account_id,
        TradingMode.CANARY,
        utc_now() + timedelta(minutes=2),
        initial.version,
    )
    assert armed is not None

    class FailingBroker:
        def __init__(self) -> None:
            self.pending_during_cancel: bool | None = None
            self.rearm_result: object | None = None

        async def cancel_all(self, reason: str) -> bool:
            control = await store.get_runtime_control(account_id)
            self.pending_during_cancel = control.cancellation_pending
            self.rearm_result = await store.arm_runtime_control(
                account_id,
                TradingMode.CANARY,
                utc_now() + timedelta(minutes=2),
                control.version,
            )
            return False

    broker = FailingBroker()
    state = RuntimeControlWatchState(
        last_version=armed.version,
        was_live_armed=True,
    )
    await store.set_runtime_control(
        armed.model_copy(
            update={
                "armed": False,
                "accept_new_intents": False,
                "armed_until": None,
                "kill_switch": True,
                "cancellation_pending": False,
                "version": armed.version + 1,
            }
        )
    )

    await _enforce_runtime_control_once(
        store=store,
        broker=broker,  # type: ignore[arg-type]
        account_id=account_id,
        mode=TradingMode.CANARY,
        state=state,
        logger=__import__("logging").getLogger("test.runtime-control"),
    )

    current = await store.get_runtime_control(account_id)
    assert broker.pending_during_cancel is True
    assert broker.rearm_result is None
    assert current.cancellation_pending
