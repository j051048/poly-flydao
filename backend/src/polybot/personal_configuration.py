"""Worker-confirmed personal configuration and immutable deployment risk ceilings."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from polybot.config import Settings, TradingMode
from polybot.jobs import RiskPolicySnapshot

RISK_FIELDS = (
    "max_order_usd", "max_trade_risk_pct", "max_event_exposure_pct",
    "max_bucket_exposure_pct", "max_gross_exposure_pct", "daily_loss_limit_pct",
    "max_drawdown_pct", "min_edge",
)


@dataclass
class PersonalConfiguration:
    ceilings: dict[str, Decimal]
    policy_id: str | None = None
    policy_version: int | None = None
    profile_version: int | None = None
    confirmed_mode: TradingMode | None = None
    checked_at: datetime | None = None
    error: str | None = None
    policy_loaded: bool = False
    effective: dict[str, Decimal] = field(default_factory=dict)

    def policy_values(
        self, settings: Settings, policy: RiskPolicySnapshot | None,
        *, mode: TradingMode | None = None,
    ) -> dict[str, Decimal]:
        values = dict(self.ceilings)
        if policy is not None:
            if str(policy.account_id) != settings.account_id or policy.status != "active":
                raise ValueError("risk policy is not active for this account")
            for name, ceiling in values.items():
                requested = getattr(policy, name)
                if (
                    not requested.is_finite()
                    or (requested < 0 if name == "min_edge" else requested <= 0)
                    or (name != "max_order_usd" and requested > 1)
                ):
                    raise ValueError("risk policy contains an invalid limit")
                values[name] = (
                    max(ceiling, requested) if name == "min_edge" else min(ceiling, requested)
                )
        if (mode or settings.mode) is TradingMode.CANARY:
            values["max_order_usd"] = min(values["max_order_usd"], Decimal("5"))
        return values

    def apply_policy(
        self, settings: Settings, policy: RiskPolicySnapshot | None,
        *, mode: TradingMode | None = None,
    ) -> None:
        values = self.policy_values(settings, policy, mode=mode)
        # No awaits between assignments: no half-applied policy is observable.
        for name, value in values.items():
            setattr(settings, name, value)
        self.effective = values
        self.policy_id = str(policy.id) if policy is not None else None
        self.policy_version = policy.version if policy is not None else None
        self.policy_loaded = True

    def confirm(self, mode: TradingMode) -> None:
        self.confirmed_mode = mode

    def limits(self) -> dict[str, object]:
        return {
            **{key: str(value) for key, value in self.effective.items()},
            "policy_id": self.policy_id,
            "version": self.policy_version,
            "applied": self.policy_loaded and self.error is None,
        }


def personal_configuration(settings: Settings) -> PersonalConfiguration:
    state = settings._runtime_configuration
    if state is None:
        values = {name: getattr(settings, name) for name in RISK_FIELDS}
        state = PersonalConfiguration(ceilings=values, effective=dict(values))
        settings._runtime_configuration = state
    return state
