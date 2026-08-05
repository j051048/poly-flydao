from __future__ import annotations

import threading
from collections import Counter, defaultdict
from collections.abc import Iterable
from decimal import Decimal


class Metrics:
    """Small single-process Prometheus-style counter registry.

    Personal deployments run exactly one process (WEB_CONCURRENCY=1), so an
    in-process registry is accurate. No account ids or secrets are ever part of
    a metric label; the endpoint is safe to expose without authentication.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, Counter[str]] = defaultdict(Counter)
        self._gauges: dict[str, float] = {}

    def increment(self, name: str, labels: dict[str, str] | None = None, amount: int = 1) -> None:
        if amount <= 0:
            return
        key = _labels(labels)
        with self._lock:
            self._counters[name][key] += amount

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def snapshot(self) -> list[str]:
        lines: list[str] = []
        with self._lock:
            for name, series in sorted(self._counters.items()):
                lines.append(f"# TYPE {name} counter")
                for key, value in sorted(series.items()):
                    lines.append(f"{name}{key} {value}")
            for name, value in sorted(self._gauges.items()):
                lines.append(f"# TYPE {name} gauge")
                lines.append(f"{name} {value}")
        return lines

    def render(self) -> str:
        return "\n".join(self.snapshot()) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()


def _labels(labels: dict[str, str] | None) -> str:
    if not labels:
        return ""
    escaped = ",".join(
        f'{key}="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
        for key, value in sorted(labels.items())
    )
    return f"{{{escaped}}}"


def decimal_cost_usd(value: Decimal | None) -> float:
    return float(value) if value is not None else 0.0


def merged_series(metrics: Iterable[Metrics]) -> list[str]:
    """Merge snapshots from multiple registries (used by tests and multi-runtime)."""

    lines: list[str] = []
    for item in metrics:
        lines.extend(item.snapshot())
    return lines
