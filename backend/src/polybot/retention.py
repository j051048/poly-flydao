"""Bounded retention for the append-only history tables.

Every cycle writes AI usage rows, equity points, and order-book snapshots. None
of those are needed forever, but nothing else in the system would ever remove
them, so a long-running deployment grows without bound. This module owns the
only sanctioned deletion policy.

Two rules keep it safe:

* **Only history is pruned.** Orders, fills, forecasts, positions, and the
  reconciliation ledger are the audit trail for real money and are never
  touched here.
* **Windows have hard minimums** enforced both here and in the database
  function, so a mistyped environment variable cannot wipe recent data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

MIN_AI_USAGE_DAYS = 7
MIN_EQUITY_DAYS = 30
MIN_SNAPSHOT_DAYS = 1
DEFAULT_BATCH_LIMIT = 20_000


class PrunableStore(Protocol):
    async def prune_history(
        self,
        *,
        ai_usage_days: int,
        equity_days: int,
        snapshot_days: int,
        batch_limit: int = DEFAULT_BATCH_LIMIT,
    ) -> dict[str, int]: ...


@dataclass(frozen=True)
class RetentionPolicy:
    enabled: bool = True
    ai_usage_days: int = 90
    equity_history_days: int = 365
    snapshot_days: int = 30
    batch_limit: int = DEFAULT_BATCH_LIMIT
    interval_seconds: int = 86_400

    def __post_init__(self) -> None:
        if self.ai_usage_days < MIN_AI_USAGE_DAYS:
            raise ValueError(
                f"ai_usage_days must be at least {MIN_AI_USAGE_DAYS}"
            )
        if self.equity_history_days < MIN_EQUITY_DAYS:
            raise ValueError(
                f"equity_history_days must be at least {MIN_EQUITY_DAYS}"
            )
        if self.snapshot_days < MIN_SNAPSHOT_DAYS:
            raise ValueError(f"snapshot_days must be at least {MIN_SNAPSHOT_DAYS}")
        if self.batch_limit < 100:
            raise ValueError("batch_limit must be at least 100")
        if self.interval_seconds < 3600:
            raise ValueError("interval_seconds must be at least one hour")

    @classmethod
    def from_settings(cls, settings: Any) -> RetentionPolicy:
        return cls(
            enabled=bool(getattr(settings, "retention_enabled", True)),
            ai_usage_days=int(getattr(settings, "retention_ai_usage_days", 90)),
            equity_history_days=int(
                getattr(settings, "retention_equity_history_days", 365)
            ),
            snapshot_days=int(getattr(settings, "retention_snapshot_days", 30)),
            interval_seconds=int(
                getattr(settings, "retention_interval_seconds", 86_400)
            ),
        )

    def prune_kwargs(self) -> dict[str, int]:
        return {
            "ai_usage_days": self.ai_usage_days,
            "equity_days": self.equity_history_days,
            "snapshot_days": self.snapshot_days,
            "batch_limit": self.batch_limit,
        }


async def prune_history(
    store: PrunableStore,
    policy: RetentionPolicy,
) -> dict[str, int]:
    """Run one bounded prune pass; returns the per-table deleted row counts."""

    if not policy.enabled:
        return {}
    return await store.prune_history(**policy.prune_kwargs())


__all__ = [
    "DEFAULT_BATCH_LIMIT",
    "MIN_AI_USAGE_DAYS",
    "MIN_EQUITY_DAYS",
    "MIN_SNAPSHOT_DAYS",
    "PrunableStore",
    "RetentionPolicy",
    "prune_history",
]
