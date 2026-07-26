from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from polybot.api import ArmRequest, arm
from polybot.config import TradingMode
from polybot.models import utc_now
from polybot.stores.memory import MemoryStore


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

    acknowledged = await store.acknowledge_runtime_cancellation(
        account_id, current.version
    )
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


async def test_cancellation_acknowledgement_waits_for_inflight_orders() -> None:
    store = MemoryStore()
    account_id = "55555555-5555-5555-5555-555555555555"
    disarmed = await store.disarm_runtime_control(account_id, TradingMode.CANARY)
    store.unresolved_live_orders.add("signed-intent")

    blocked = await store.acknowledge_runtime_cancellation(
        account_id, disarmed.version
    )

    assert blocked is None
    assert (await store.get_runtime_control(account_id)).cancellation_pending


async def test_arm_endpoint_returns_conflict_when_cas_is_stale() -> None:
    class StaleArmStore(MemoryStore):
        async def arm_runtime_control(self, *args, **kwargs):
            return None

    account_id = "33333333-3333-3333-3333-333333333333"
    runtime = SimpleNamespace(
        store=StaleArmStore(),
        settings=SimpleNamespace(
            account_id=account_id,
            mode=TradingMode.CANARY,
            uses_supabase=True,
        ),
    )

    with pytest.raises(HTTPException) as raised:
        await arm(ArmRequest(mode=TradingMode.CANARY, minutes=5), runtime)

    assert raised.value.status_code == 409
    assert "changed concurrently" in str(raised.value.detail)


async def test_arm_endpoint_rejects_pending_cancellation() -> None:
    account_id = "44444444-4444-4444-4444-444444444444"
    store = MemoryStore()
    await store.disarm_runtime_control(account_id, TradingMode.CANARY)
    runtime = SimpleNamespace(
        store=store,
        settings=SimpleNamespace(
            account_id=account_id,
            mode=TradingMode.CANARY,
            uses_supabase=True,
        ),
    )

    with pytest.raises(HTTPException) as raised:
        await arm(ArmRequest(mode=TradingMode.CANARY, minutes=5), runtime)

    assert raised.value.status_code == 409
    assert "zero open orders" in str(raised.value.detail)
