"""Resolved-forecast scoring and reliability bins.

``PerformanceSnapshot`` is the single source of truth for "how has this account
actually done". It feeds the dashboard, the strategy-validation gate, and the
AI reliability curve consumed by the forecast graph, so the row-to-snapshot
maths lives here instead of inside the job repository.

Nothing in this module invents data: malformed, non-finite, or out-of-range
rows are dropped, and an empty ledger yields an explicit empty snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Deciles of predicted probability. The same bin count is assumed by the
#: reliability curve, so bin edges and curve anchors always line up.
BIN_COUNT = 10

#: Minimum resolved forecasts before a calibration curve is trusted, and the
#: point at which the dashboard stops calling the sample "insufficient".
MIN_CALIBRATION_SAMPLES = 30

#: Matches the database default for ``ai_usage_daily.request_limit``.
DEFAULT_AI_BUDGET_LIMIT = 100


class CalibrationBin(BaseModel):
    model_config = ConfigDict(frozen=True)

    lower: Decimal
    upper: Decimal
    samples: int
    mean_forecast: Decimal | None = None
    observed_frequency: Decimal | None = None


class PerformanceSnapshot(BaseModel):
    sample_size: int = 0
    resolved_markets: int = 0
    brier_score: Decimal | None = None
    log_loss: Decimal | None = None
    calibration: list[CalibrationBin] = Field(default_factory=list)
    ai_usage_used: int = 0
    ai_usage_limit: int = 0
    strategy_validation: str = "insufficient_data"
    research_only: bool = True
    warning: str = "校准样本和历史结果不保证未来盈利；P2 微结构策略仍保持 research-only。"


def decimal_or_none(value: Any) -> Decimal | None:
    """Parse a finite ``Decimal`` from a DB/JSON value, or return ``None``."""

    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


@dataclass(frozen=True)
class ResolvedForecast:
    probability: Decimal
    brier_score: Decimal
    log_loss: Decimal
    resolved_yes: bool
    market_id: str


def resolved_forecasts_from_rows(rows: list[dict[str, Any]]) -> list[ResolvedForecast]:
    points: list[ResolvedForecast] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        probability = decimal_or_none(row.get("probability_yes"))
        brier = decimal_or_none(row.get("brier_score"))
        log_loss = decimal_or_none(row.get("log_loss"))
        if probability is None or brier is None or log_loss is None:
            continue
        if not 0 <= probability <= 1 or brier < 0 or log_loss < 0:
            continue
        points.append(
            ResolvedForecast(
                probability=probability,
                brier_score=brier,
                log_loss=log_loss,
                resolved_yes=bool(row.get("resolved_yes")),
                market_id=str(row.get("market_id") or ""),
            )
        )
    return points


def calibration_bins(points: list[ResolvedForecast]) -> list[CalibrationBin]:
    """Bucket resolved forecasts into deciles of predicted probability."""

    bins: list[CalibrationBin] = []
    for index in range(BIN_COUNT):
        lower = Decimal(index) / Decimal(BIN_COUNT)
        upper = Decimal(index + 1) / Decimal(BIN_COUNT)
        selected = [
            point
            for point in points
            if lower <= point.probability <= upper
            and (index == BIN_COUNT - 1 or point.probability < upper)
        ]
        bins.append(
            CalibrationBin(
                lower=lower,
                upper=upper,
                samples=len(selected),
                mean_forecast=(
                    sum((point.probability for point in selected), Decimal("0"))
                    / Decimal(len(selected))
                    if selected
                    else None
                ),
                observed_frequency=(
                    Decimal(sum(1 for point in selected if point.resolved_yes))
                    / Decimal(len(selected))
                    if selected
                    else None
                ),
            )
        )
    return bins


def calibration_bins_from_rows(rows: list[dict[str, Any]]) -> list[CalibrationBin]:
    return calibration_bins(resolved_forecasts_from_rows(rows))


def performance_snapshot_from_rows(
    rows: list[dict[str, Any]],
    *,
    ai_usage_used: int,
    ai_usage_limit: int,
) -> PerformanceSnapshot:
    points = resolved_forecasts_from_rows(rows)
    count = len(points)
    return PerformanceSnapshot(
        sample_size=count,
        resolved_markets=len({point.market_id for point in points if point.market_id}),
        brier_score=(
            sum((point.brier_score for point in points), Decimal("0")) / Decimal(count)
            if count
            else None
        ),
        log_loss=(
            sum((point.log_loss for point in points), Decimal("0")) / Decimal(count)
            if count
            else None
        ),
        calibration=calibration_bins(points),
        ai_usage_used=ai_usage_used,
        ai_usage_limit=ai_usage_limit,
        strategy_validation=(
            "calibrating" if count >= MIN_CALIBRATION_SAMPLES else "insufficient_data"
        ),
    )
