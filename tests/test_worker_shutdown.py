from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import timedelta

from polybot.config import TradingMode
from polybot.models import RuntimeControl, utc_now
from polybot.worker import _shutdown_live_safely


class ShutdownStore:
    def __init__(
        self,
        *,
        disarm_failures: int = 0,
        lease_valid: bool = True,
        acknowledgement_succeeds: bool = True,
        unresolved_orders: bool = False,
        events: list[str] | None = None,
    ) -> None:
        self.disarm_failures = disarm_failures
        self.lease_valid = lease_valid
        self.acknowledgement_succeeds = acknowledgement_succeeds
        self.unresolved_orders = unresolved_orders
        self.events = events
        self.disarm_calls = 0
        self.validate_calls = 0
        self.release_calls = 0
        self.acknowledge_calls = 0

    async def disarm_runtime_control(
        self, account_id: str, mode: TradingMode
    ) -> RuntimeControl:
        if self.events is not None:
            self.events.append("disarm")
        self.disarm_calls += 1
        if self.disarm_calls <= self.disarm_failures:
            raise OSError("store unavailable")
        return RuntimeControl(
            account_id=account_id,
            mode=mode,
            armed=False,
            kill_switch=True,
            cancellation_pending=True,
            version=self.disarm_calls + 1,
            updated_at=utc_now() + timedelta(seconds=self.disarm_calls),
        )

    async def acknowledge_runtime_cancellation(
        self, account_id: str, expected_version: int
    ) -> RuntimeControl | None:
        self.acknowledge_calls += 1
        if not self.acknowledgement_succeeds:
            return None
        return RuntimeControl(
            account_id=account_id,
            mode=TradingMode.CANARY,
            armed=False,
            kill_switch=True,
            cancellation_pending=False,
            version=expected_version + 1,
        )

    async def has_unresolved_live_orders(self, account_id: str) -> bool:
        return self.unresolved_orders

    async def validate_worker_lease(
        self, account_id: str, owner_id: str, fencing_token: int
    ) -> bool:
        self.validate_calls += 1
        return self.lease_valid

    async def release_worker_lease(
        self, account_id: str, owner_id: str, fencing_token: int
    ) -> bool:
        self.release_calls += 1
        return True


class ShutdownBroker:
    def __init__(
        self, outcomes: Iterator[bool | Exception], *, events: list[str] | None = None
    ) -> None:
        self.outcomes = outcomes
        self.events = events
        self.cancel_calls = 0

    async def cancel_all(self, reason: str) -> bool:
        if self.events is not None:
            self.events.append("cancel")
        self.cancel_calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_shutdown_disarms_then_retries_cancel_before_releasing_lease() -> None:
    events: list[str] = []
    store = ShutdownStore(events=events)
    broker = ShutdownBroker(
        iter([False, OSError("exchange unavailable"), True]), events=events
    )

    result = await _shutdown_live_safely(
        store=store,  # type: ignore[arg-type]
        broker=broker,  # type: ignore[arg-type]
        account_id="account",
        mode=TradingMode.CANARY,
        owner_id="worker",
        lease_token=7,
        logger=logging.getLogger("test.worker.shutdown"),
        retry_delay_seconds=0,
    )

    assert result.control_persisted
    assert result.cancellation_verified
    assert result.cancellation_acknowledged
    assert result.lease_released
    assert store.disarm_calls == 1
    assert broker.cancel_calls == 3
    assert store.release_calls == 1
    assert store.acknowledge_calls == 1
    assert events[0] == "disarm"
    assert events[1] == "cancel"


async def test_shutdown_retains_lease_when_cancellation_cannot_be_verified() -> None:
    store = ShutdownStore()
    broker = ShutdownBroker(iter([False, False, False]))

    result = await _shutdown_live_safely(
        store=store,  # type: ignore[arg-type]
        broker=broker,  # type: ignore[arg-type]
        account_id="account",
        mode=TradingMode.LIVE,
        owner_id="worker",
        lease_token=8,
        logger=logging.getLogger("test.worker.shutdown"),
        retry_delay_seconds=0,
    )

    assert result.control_persisted
    assert not result.cancellation_verified
    assert not result.cancellation_acknowledged
    assert not result.lease_released
    assert broker.cancel_calls == 3
    assert store.release_calls == 0


async def test_shutdown_still_cancels_but_retains_lease_when_disarm_fails() -> None:
    store = ShutdownStore(disarm_failures=3)
    broker = ShutdownBroker(iter([True]))

    result = await _shutdown_live_safely(
        store=store,  # type: ignore[arg-type]
        broker=broker,  # type: ignore[arg-type]
        account_id="account",
        mode=TradingMode.CANARY,
        owner_id="worker",
        lease_token=9,
        logger=logging.getLogger("test.worker.shutdown"),
        retry_delay_seconds=0,
    )

    assert not result.control_persisted
    assert result.cancellation_verified
    assert not result.cancellation_acknowledged
    assert not result.lease_released
    assert store.disarm_calls == 3
    assert broker.cancel_calls == 1
    assert store.release_calls == 0


async def test_shutdown_never_releases_an_unconfirmed_lease() -> None:
    store = ShutdownStore(lease_valid=False)
    broker = ShutdownBroker(iter([True]))

    result = await _shutdown_live_safely(
        store=store,  # type: ignore[arg-type]
        broker=broker,  # type: ignore[arg-type]
        account_id="account",
        mode=TradingMode.LIVE,
        owner_id="worker",
        lease_token=10,
        logger=logging.getLogger("test.worker.shutdown"),
        retry_delay_seconds=0,
    )

    assert result.control_persisted
    assert result.cancellation_verified
    assert not result.cancellation_acknowledged
    assert not result.lease_released
    assert store.release_calls == 0


async def test_shutdown_retains_lease_when_acknowledgement_races() -> None:
    store = ShutdownStore(acknowledgement_succeeds=False)
    broker = ShutdownBroker(iter([True]))

    result = await _shutdown_live_safely(
        store=store,  # type: ignore[arg-type]
        broker=broker,  # type: ignore[arg-type]
        account_id="account",
        mode=TradingMode.LIVE,
        owner_id="worker",
        lease_token=11,
        logger=logging.getLogger("test.worker.shutdown"),
        retry_delay_seconds=0,
    )

    assert result.control_persisted
    assert result.cancellation_verified
    assert not result.cancellation_acknowledged
    assert not result.lease_released
    assert store.acknowledge_calls == 1
    assert store.release_calls == 0


async def test_shutdown_does_not_acknowledge_an_inflight_order() -> None:
    store = ShutdownStore(unresolved_orders=True)
    broker = ShutdownBroker(iter([True]))

    result = await _shutdown_live_safely(
        store=store,  # type: ignore[arg-type]
        broker=broker,  # type: ignore[arg-type]
        account_id="account",
        mode=TradingMode.CANARY,
        owner_id="worker",
        lease_token=12,
        logger=logging.getLogger("test.worker.shutdown"),
        retry_delay_seconds=0,
    )

    assert result.cancellation_verified
    assert not result.cancellation_acknowledged
    assert not result.lease_released
    assert store.acknowledge_calls == 0
