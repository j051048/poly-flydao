from __future__ import annotations

from decimal import Decimal
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from polybot.ai.base import ForecastProvider
from polybot.ai.calibration import Calibrator
from polybot.models import Forecast, ForecastRequest


class ForecastState(TypedDict, total=False):
    request: ForecastRequest
    primary: Forecast
    critic: Forecast
    final: Forecast


class ForecastGraph:
    """Two-pass independent forecast plus deterministic disagreement penalty."""

    def __init__(
        self,
        provider: ForecastProvider,
        *,
        primary_model: str,
        critic_model: str,
        calibrator: Calibrator | None = None,
    ):
        self.provider = provider
        self.primary_model = primary_model
        self.critic_model = critic_model
        # Reliability curve driven by the account's own resolved forecasts. It
        # is swapped in place by the worker, so an uncalibrated graph is always
        # the safe default.
        self.calibrator = calibrator
        builder = StateGraph(ForecastState)
        builder.add_node("primary", self._primary)
        builder.add_node("critic", self._critic)
        builder.add_node("aggregate", self._aggregate)
        builder.add_edge(START, "primary")
        builder.add_edge("primary", "critic")
        builder.add_edge("critic", "aggregate")
        builder.add_edge("aggregate", END)
        self.graph = builder.compile()

    def set_calibrator(self, calibrator: Calibrator | None) -> None:
        """Hot-swap the reliability curve used by the next aggregation."""

        self.calibrator = calibrator

    async def _primary(self, state: ForecastState) -> ForecastState:
        request = state["request"].model_copy(
            update={"perspective": "independent base-rate forecaster"}
        )
        result = await self.provider.forecast(request, model=self.primary_model)
        return {"primary": result}

    async def _critic(self, state: ForecastState) -> ForecastState:
        # The critic does not see the primary estimate, reducing anchoring and correlated mistakes.
        request = state["request"].model_copy(
            update={"perspective": "adversarial skeptic seeking base-rate and resolution errors"}
        )
        result = await self.provider.forecast(request, model=self.critic_model)
        return {"critic": result}

    async def _aggregate(self, state: ForecastState) -> ForecastState:
        first = state["primary"]
        second = state["critic"]
        raw_probability = (first.probability_yes + second.probability_yes) / Decimal("2")
        disagreement = abs(first.probability_yes - second.probability_yes)
        confidence = min(first.confidence, second.confidence) * max(
            Decimal("0"), Decimal("1") - disagreement
        )
        calibrator = self.calibrator
        probability = raw_probability
        calibration_note = ""
        if calibrator is not None:
            probability = calibrator.apply(raw_probability)
            if probability != raw_probability:
                calibration_note = (
                    f" Reliability calibration moved the estimate from "
                    f"{raw_probability} to {probability} ({calibrator.describe()})."
                )
        low = min(first.probability_low, second.probability_low, raw_probability, probability)
        high = max(first.probability_high, second.probability_high, raw_probability, probability)
        combined = Forecast(
            market_id=first.market_id,
            probability_yes=probability,
            probability_low=low,
            probability_high=high,
            confidence=confidence,
            evidence_for=list(dict.fromkeys(first.evidence_for + second.evidence_for)),
            evidence_against=list(dict.fromkeys(first.evidence_against + second.evidence_against)),
            assumptions=list(dict.fromkeys(first.assumptions + second.assumptions)),
            invalidation_conditions=list(
                dict.fromkeys(first.invalidation_conditions + second.invalidation_conditions)
            ),
            source_ids=list(dict.fromkeys(first.source_ids + second.source_ids)),
            model=f"ensemble:{first.model}+{second.model}",
            rationale=(
                f"Deterministic two-pass ensemble; absolute disagreement={disagreement}. "
                "Intervals were unioned and confidence was penalized."
                f"{calibration_note}"
            ),
        )
        return {"final": combined}

    async def forecast(self, request: ForecastRequest) -> Forecast:
        result = await self.graph.ainvoke({"request": request})
        return result["final"]

    def usage(self):
        """Return the most recent provider usage record, if the provider reports it."""

        last_usage = getattr(self.provider, "last_usage", None)
        return last_usage() if callable(last_usage) else None

    def drain_usage(self) -> list:
        """Return and clear all usage records produced by the last forecast."""

        drain = getattr(self.provider, "drain_usage", None)
        if callable(drain):
            return drain()
        usage = self.usage()
        return [usage] if usage is not None else []
