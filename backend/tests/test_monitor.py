from __future__ import annotations

import asyncio
from typing import Any

from polybot.monitor import alert_due, alert_payload, monitor_readiness
from polybot.notify import NotificationMessage


class _RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []

    async def send(self, notification: NotificationMessage) -> bool:
        self.messages.append(notification)
        return True


class _FakeRegistry:
    """Replays a fixed sequence of readiness snapshots."""

    def __init__(self, snapshots: list[dict[str, Any]]) -> None:
        self._snapshots = snapshots
        self.calls = 0

    def snapshot(self) -> dict[str, Any]:
        index = min(self.calls, len(self._snapshots) - 1)
        self.calls += 1
        return self._snapshots[index]


def _blocked(seconds: int) -> dict[str, Any]:
    return {
        "ready": False,
        "not_ready_seconds": seconds,
        "blockers": [
            {
                "code": "lease_unavailable",
                "gate": "lease",
                "message": "Worker 租约未持有。",
                "fix": "确认只有一个副本。",
            }
        ],
        "warnings": [{"code": "reconciliation_pending", "message": "等待重置对账基准。"}],
    }


def _ready() -> dict[str, Any]:
    return {"ready": True, "not_ready_seconds": None, "blockers": [], "warnings": []}


def test_alert_due_respects_the_threshold_and_the_repeat_interval() -> None:
    # Below the threshold nothing is ever sent, however long the process has run.
    assert not alert_due(
        blocked_for_seconds=100,
        threshold_seconds=900,
        now_monotonic=1_000.0,
        last_alert_at=None,
    )
    # The first breach raises the alert immediately.
    assert alert_due(
        blocked_for_seconds=901,
        threshold_seconds=900,
        now_monotonic=1_000.0,
        last_alert_at=None,
    )
    # A second alert waits for the repeat interval.
    assert not alert_due(
        blocked_for_seconds=5_000,
        threshold_seconds=900,
        now_monotonic=1_000.0,
        last_alert_at=1_000.0,
    )
    assert alert_due(
        blocked_for_seconds=5_000,
        threshold_seconds=900,
        now_monotonic=1_000.0 + 6 * 3600,
        last_alert_at=1_000.0,
    )


def test_alert_payload_names_the_blockers_and_their_fix() -> None:
    title, message = alert_payload(_blocked(1_800), 1_800)
    assert title
    assert "lease_unavailable" in message
    assert "30 分钟" in message
    assert "Worker 租约未持有。" in message
    assert "确认只有一个副本。" in message
    assert "等待重置对账基准。" in message


def test_alert_payload_stays_readable_without_attribution() -> None:
    _, message = alert_payload({"blockers": [], "warnings": []}, 60)
    assert "unknown" in message


async def test_monitor_sends_one_alert_per_episode_and_announces_recovery() -> None:
    notifier = _RecordingNotifier()
    stop = asyncio.Event()
    clock = {"now": 0.0}
    steps = {"count": 0}
    registry = _FakeRegistry([_blocked(1_800), _blocked(1_860), _ready(), _ready()])

    async def sleep(delay: float) -> None:
        steps["count"] += 1
        clock["now"] += delay
        if steps["count"] >= 4:
            stop.set()

    await monitor_readiness(
        stop=stop,
        threshold_seconds=900,
        poll_seconds=60,
        notifier=notifier,  # type: ignore[arg-type]
        registry=registry,  # type: ignore[arg-type]
        clock=lambda: clock["now"],
        sleep=sleep,
    )

    assert [message.severity for message in notifier.messages] == ["warning", "info"]


async def test_monitor_repeats_a_long_outage_at_a_slow_cadence() -> None:
    notifier = _RecordingNotifier()
    stop = asyncio.Event()
    clock = {"now": 0.0}
    steps = {"count": 0}
    registry = _FakeRegistry([_blocked(3_600)])

    async def sleep(delay: float) -> None:
        steps["count"] += 1
        clock["now"] += delay
        if steps["count"] >= 3:
            stop.set()

    await monitor_readiness(
        stop=stop,
        threshold_seconds=900,
        poll_seconds=60,
        notifier=notifier,  # type: ignore[arg-type]
        registry=registry,  # type: ignore[arg-type]
        repeat_seconds=120,
        clock=lambda: clock["now"],
        sleep=sleep,
    )

    assert len(notifier.messages) == 2


async def test_monitor_stays_silent_while_the_worker_is_healthy() -> None:
    notifier = _RecordingNotifier()
    stop = asyncio.Event()
    registry = _FakeRegistry([_ready()])

    async def sleep(delay: float) -> None:
        stop.set()

    await monitor_readiness(
        stop=stop,
        threshold_seconds=900,
        poll_seconds=60,
        notifier=notifier,  # type: ignore[arg-type]
        registry=registry,  # type: ignore[arg-type]
        sleep=sleep,
    )

    assert notifier.messages == []


async def test_monitor_survives_a_registry_failure() -> None:
    class _Broken:
        def __init__(self) -> None:
            self.calls = 0

        def snapshot(self) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("registry unavailable")
            return _blocked(3_600)

    notifier = _RecordingNotifier()
    stop = asyncio.Event()
    registry = _Broken()
    steps = {"count": 0}

    async def sleep(delay: float) -> None:
        steps["count"] += 1
        if steps["count"] >= 2:
            stop.set()

    await monitor_readiness(
        stop=stop,
        threshold_seconds=900,
        poll_seconds=60,
        notifier=notifier,  # type: ignore[arg-type]
        registry=registry,  # type: ignore[arg-type]
        sleep=sleep,
    )

    assert len(notifier.messages) == 1
