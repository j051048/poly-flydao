"""External liveness monitoring for the fail-closed worker.

A blocked worker looks healthy from the outside: the container is up, ``/livez``
answers 200, and no order is ever placed. That mismatch produced several "it
still does not work" reports that only the server logs could explain, so the
worker now escalates its own readiness through the operator's notification
channel.

The escalation rule is deliberately conservative:

* nothing is sent until the process has been not-ready for
  ``POLYBOT_READINESS_ALERT_SECONDS``, so a normal startup or a short
  reconciliation pause stays quiet;
* one alert per episode, re-sent at most once every six hours while the outage
  continues, so a long outage keeps escalating without flooding the operator;
* a short "ready again" message closes the episode, so recovery is never
  silent.

The monitor is a plain coroutine with injected clock and sleep functions: it can
be unit-tested without waiting on wall-clock time, and it can never take the
trading loop down with it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from polybot.notify import NotificationMessage, Notifier
from polybot.readiness import READINESS, ReadinessRegistry

LOGGER = logging.getLogger("polybot.monitor")

# Once an outage is being reported, repeat at most this often.
READINESS_ALERT_REPEAT_SECONDS = 6 * 3600

READY_TITLE = "Polybot 已恢复就绪"
READY_MESSAGE = "Worker 已重新就绪，自动化交易按当前模式继续运行。"
NOT_READY_TITLE = "Polybot 无法交易"


def alert_due(
    *,
    blocked_for_seconds: int,
    threshold_seconds: int,
    now_monotonic: float,
    last_alert_at: float | None,
    repeat_seconds: int = READINESS_ALERT_REPEAT_SECONDS,
) -> bool:
    """Whether the not-ready alert should be raised right now.

    ``last_alert_at`` is a monotonic timestamp for the current outage; it is
    reset to ``None`` as soon as the worker becomes ready again.
    """

    if blocked_for_seconds < max(0, threshold_seconds):
        return False
    if last_alert_at is None:
        return True
    return now_monotonic - last_alert_at >= repeat_seconds


def alert_payload(
    snapshot: dict[str, object],
    blocked_for_seconds: int,
) -> tuple[str, str]:
    """Render the operator-facing title and body for one readiness snapshot."""

    blockers = [
        blocker
        for blocker in (snapshot.get("blockers") or [])
        if isinstance(blocker, dict)
    ]
    codes = ", ".join(str(blocker.get("code")) for blocker in blockers) or "unknown"
    minutes = max(1, blocked_for_seconds // 60)
    lines = [
        f"Worker 已连续 {minutes} 分钟无法进入可交易状态（阻塞项：{codes}）。",
    ]
    for blocker in blockers:
        message = blocker.get("message")
        fix = blocker.get("fix")
        if message and fix:
            lines.append(f"- {message} 处理：{fix}")
        elif message:
            lines.append(f"- {message}")
    warnings = [
        warning
        for warning in (snapshot.get("warnings") or [])
        if isinstance(warning, dict)
    ]
    for warning in warnings:
        message = warning.get("message")
        if message:
            lines.append(f"（提示）{message}")
    return NOT_READY_TITLE, "\n".join(lines)


async def _wait_for_poll(stop: asyncio.Event, delay: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=delay)
    except TimeoutError:
        pass


async def monitor_readiness(
    *,
    stop: asyncio.Event,
    threshold_seconds: int,
    poll_seconds: float,
    notifier: Notifier | None = None,
    registry: ReadinessRegistry | None = None,
    repeat_seconds: int = READINESS_ALERT_REPEAT_SECONDS,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """Publish one alert per not-ready episode, plus one on recovery."""

    registry = registry or READINESS
    now = clock or time.monotonic
    wait = sleep or (lambda delay: _wait_for_poll(stop, delay))
    interval = max(1.0, float(poll_seconds))
    last_alert_at: float | None = None

    while not stop.is_set():
        try:
            snapshot: dict[str, Any] = registry.snapshot()
            if bool(snapshot.get("ready")):
                if last_alert_at is not None:
                    last_alert_at = None
                    LOGGER.info("worker became ready; readiness alert cleared")
                    if notifier is not None:
                        await notifier.send(
                            NotificationMessage(
                                title=READY_TITLE,
                                message=READY_MESSAGE,
                                severity="info",
                            )
                        )
            else:
                blocked_for = int(snapshot.get("not_ready_seconds") or 0)
                if alert_due(
                    blocked_for_seconds=blocked_for,
                    threshold_seconds=threshold_seconds,
                    now_monotonic=now(),
                    last_alert_at=last_alert_at,
                    repeat_seconds=repeat_seconds,
                ):
                    last_alert_at = now()
                    title, message = alert_payload(snapshot, blocked_for)
                    LOGGER.error(
                        "worker has been not ready for %ss; blockers: %s",
                        blocked_for,
                        ", ".join(
                            str(blocker.get("code"))
                            for blocker in (snapshot.get("blockers") or [])
                            if isinstance(blocker, dict)
                        )
                        or "unknown",
                    )
                    if notifier is not None:
                        await notifier.send(
                            NotificationMessage(
                                title=title,
                                message=message,
                                severity="warning",
                            )
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            # The monitor must never be able to stop the trading loop.
            LOGGER.warning("readiness monitor iteration failed", exc_info=True)
        await wait(interval)


__all__ = [
    "NOT_READY_TITLE",
    "READINESS_ALERT_REPEAT_SECONDS",
    "READY_MESSAGE",
    "READY_TITLE",
    "alert_due",
    "alert_payload",
    "monitor_readiness",
]
