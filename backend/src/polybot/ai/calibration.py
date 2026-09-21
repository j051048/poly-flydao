"""Reliability calibration for the deterministic forecast ensemble.

The worker keeps a per-account ledger of resolved forecasts. Without using it,
the ensemble would stay systematically over- or under-confident forever. This
module turns that ledger into a reliability curve and applies it to every new
estimate, with three hard properties:

1. **Fail closed.** Missing, thin, or malformed calibration data yields the
   identity function. A correction is never invented from an empty ledger.
2. **Bounded.** A single forecast can only move by ``max_absolute_shift``, and
   the raw disagreement penalty is applied before calibration, so calibration
   can never manufacture conviction that the two passes did not produce.
3. **Shrunk by sample size.** The curve is weighted by
   ``samples / (samples + shrinkage_samples)`` so a handful of outcomes cannot
   swing sizing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from polybot.models import utc_now
from polybot.performance import (
    MIN_CALIBRATION_SAMPLES,
    CalibrationBin,
    PerformanceSnapshot,
    calibration_bins_from_rows,
    decimal_or_none,
)

_QUANTUM = Decimal("0.000001")
_ZERO = Decimal("0")
_ONE = Decimal("1")


class CalibrationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    #: Resolved forecasts required before any correction is applied.
    min_total_samples: int = Field(default=MIN_CALIBRATION_SAMPLES, ge=1)
    #: Resolved forecasts required inside a single decile before it anchors the curve.
    min_bin_samples: int = Field(default=10, ge=1)
    #: Number of hypothetical samples that pull the curve back to the identity.
    shrinkage_samples: int = Field(default=60, ge=1)
    #: Hard cap on how far one forecast may be moved in either direction.
    max_absolute_shift: Decimal = Field(default=Decimal("0.15"), ge=0, le=1)


@dataclass(frozen=True)
class ReliabilityAnchor:
    """One point of the reliability curve: what we said vs what happened."""

    forecast: Decimal
    observed: Decimal
    samples: int


@dataclass(frozen=True)
class CalibrationCurve:
    anchors: tuple[ReliabilityAnchor, ...]
    samples: int

    def interpolate(self, probability: Decimal) -> Decimal:
        """Piecewise-linear read of the curve, clamped at both ends."""

        anchors = self.anchors
        if not anchors:
            return probability
        if probability <= anchors[0].forecast:
            return anchors[0].observed
        if probability >= anchors[-1].forecast:
            return anchors[-1].observed
        for left, right in zip(anchors, anchors[1:], strict=False):
            if left.forecast <= probability <= right.forecast:
                span = right.forecast - left.forecast
                if span <= 0:
                    return right.observed
                weight = (probability - left.forecast) / span
                return left.observed + (right.observed - left.observed) * weight
        return anchors[-1].observed


def build_curve(
    snapshot: PerformanceSnapshot | None,
    config: CalibrationConfig | None = None,
) -> CalibrationCurve | None:
    """Turn a performance snapshot into a usable curve, or ``None``."""

    config = config or CalibrationConfig()
    if not config.enabled or snapshot is None:
        return None
    if snapshot.sample_size < config.min_total_samples:
        return None
    anchors: list[ReliabilityAnchor] = []
    running_max = _ZERO
    for calibration_bin in snapshot.calibration:
        if calibration_bin.samples < config.min_bin_samples:
            continue
        forecast = decimal_or_none(calibration_bin.mean_forecast)
        observed = decimal_or_none(calibration_bin.observed_frequency)
        if forecast is None or observed is None:
            continue
        if not (_ZERO <= forecast <= _ONE and _ZERO <= observed <= _ONE):
            continue
        # Light isotonic pass: a reliability curve must be non-decreasing, so an
        # inverted bin (noise, usually from thin samples) is smoothed upward
        # instead of creating a non-monotone correction.
        observed = max(observed, running_max)
        running_max = observed
        anchors.append(
            ReliabilityAnchor(
                forecast=forecast,
                observed=observed,
                samples=calibration_bin.samples,
            )
        )
    if not anchors:
        return None
    anchors.sort(key=lambda anchor: anchor.forecast)
    deduped: list[ReliabilityAnchor] = []
    for anchor in anchors:
        if deduped and deduped[-1].forecast == anchor.forecast:
            if anchor.samples >= deduped[-1].samples:
                deduped[-1] = anchor
            continue
        deduped.append(anchor)
    return CalibrationCurve(
        anchors=tuple(deduped),
        samples=sum(anchor.samples for anchor in deduped),
    )


class CalibrationSource(Protocol):
    """The slice of a state store the calibration refresh needs."""

    async def calibration_snapshot(self, account_id: str) -> PerformanceSnapshot: ...


class Calibrator:
    """Applies the current reliability curve to raw ensemble probabilities."""

    def __init__(
        self,
        config: CalibrationConfig | None = None,
        *,
        curve: CalibrationCurve | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        self.config = config or CalibrationConfig()
        self._curve = curve
        self._observed_at = observed_at

    @classmethod
    def from_snapshot(
        cls,
        snapshot: PerformanceSnapshot | None,
        config: CalibrationConfig | None = None,
    ) -> Calibrator:
        calibrator = cls(config)
        calibrator.update_from_snapshot(snapshot)
        return calibrator

    @property
    def active(self) -> bool:
        return self._curve is not None

    @property
    def samples(self) -> int:
        return self._curve.samples if self._curve is not None else 0

    @property
    def observed_at(self) -> datetime | None:
        return self._observed_at

    def update_from_snapshot(self, snapshot: PerformanceSnapshot | None) -> bool:
        """Install the curve implied by ``snapshot``; report whether it is usable."""

        curve = build_curve(snapshot, self.config)
        self._curve = curve
        self._observed_at = utc_now() if curve is not None else None
        return curve is not None

    def apply(self, probability: Decimal) -> Decimal:
        """Shrink ``probability`` toward the reliability curve, bounded by config."""

        curve = self._curve
        if curve is None:
            return probability
        target = curve.interpolate(probability)
        total = Decimal(curve.samples + self.config.shrinkage_samples)
        weight = Decimal(curve.samples) / total if total > 0 else _ZERO
        adjusted = probability + (target - probability) * weight
        shift = self.config.max_absolute_shift
        adjusted = min(max(adjusted, probability - shift), probability + shift)
        adjusted = min(max(adjusted, _ZERO), _ONE)
        return adjusted.quantize(_QUANTUM, rounding=ROUND_HALF_UP)

    def describe(self) -> str:
        if self._curve is None:
            return "calibration inactive (insufficient resolved forecasts)"
        return f"reliability curve from {self._curve.samples} resolved forecasts"


async def refresh_calibrator(
    store: Any,
    *,
    account_id: str,
    calibrator: Calibrator,
) -> bool:
    """Reload the reliability curve from the durable ledger.

    Returns whether a usable curve is in force. Any failure leaves the previous
    curve untouched so a transient read error cannot silently disable (or
    fabricate) a correction.
    """

    snapshot = await store.calibration_snapshot(account_id)
    return calibrator.update_from_snapshot(snapshot)


def snapshot_from_bins(
    bins: list[CalibrationBin],
    *,
    sample_size: int | None = None,
) -> PerformanceSnapshot:
    """Build the minimal snapshot the calibrator needs from raw bins."""

    return PerformanceSnapshot(
        sample_size=(
            sample_size if sample_size is not None else sum(item.samples for item in bins)
        ),
        calibration=bins,
    )


__all__ = [
    "CalibrationConfig",
    "CalibrationCurve",
    "CalibrationSource",
    "Calibrator",
    "ReliabilityAnchor",
    "build_curve",
    "calibration_bins_from_rows",
    "refresh_calibrator",
    "snapshot_from_bins",
]
