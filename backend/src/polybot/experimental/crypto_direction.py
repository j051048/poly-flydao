from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from polybot.inventory.pair_book import PairInventorySnapshot
from polybot.models import Outcome, utc_now


class CryptoDirectionSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    asset_symbol: str = Field(min_length=1)
    favored_outcome: Outcome
    strength: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    generated_at: datetime


class DirectionalOverlayConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    maximum_directional_shares: Decimal = Field(default=Decimal("2"), ge=0)
    maximum_overlay_notional_usd: Decimal = Field(default=Decimal("1"), ge=0)
    minimum_confidence: Decimal = Field(default=Decimal("0.70"), ge=0, le=1)
    maximum_signal_age: timedelta = Field(default=timedelta(minutes=5), gt=timedelta(0))


class OverlayDecision(BaseModel):
    approved_size: Decimal = Field(ge=0)
    codes: tuple[str, ...] = ()

    @property
    def approved(self) -> bool:
        return self.approved_size > 0 and not self.codes


class DirectionalOverlay:
    """Scale a slow signal while enforcing an absolute directional inventory cap."""

    def __init__(self, config: DirectionalOverlayConfig | None = None):
        self.config = config or DirectionalOverlayConfig()

    def size(
        self,
        *,
        signal: CryptoDirectionSignal,
        requested_size: Decimal,
        limit_price: Decimal,
        inventory: PairInventorySnapshot,
    ) -> OverlayDecision:
        if not self.config.enabled:
            return OverlayDecision(approved_size=Decimal("0"), codes=("overlay_disabled",))
        if signal.confidence < self.config.minimum_confidence:
            return OverlayDecision(approved_size=Decimal("0"), codes=("low_confidence",))
        generated_at = signal.generated_at
        if generated_at.tzinfo is None:
            return OverlayDecision(approved_size=Decimal("0"), codes=("invalid_signal_time",))
        age = utc_now() - generated_at
        if age < timedelta(seconds=-5) or age > self.config.maximum_signal_age:
            return OverlayDecision(approved_size=Decimal("0"), codes=("stale_signal",))
        if requested_size <= 0 or limit_price <= 0 or limit_price >= 1:
            return OverlayDecision(approved_size=Decimal("0"), codes=("invalid_request",))

        current_net_yes = (
            inventory.directional_yes_size - inventory.directional_no_size
        )
        cap = self.config.maximum_directional_shares
        if signal.favored_outcome is Outcome.YES:
            remaining_share_capacity = cap - current_net_yes
        else:
            remaining_share_capacity = cap + current_net_yes
        remaining_share_capacity = max(Decimal("0"), remaining_share_capacity)
        notional_capacity = self.config.maximum_overlay_notional_usd / limit_price
        signal_scaled = requested_size * signal.strength
        approved = min(signal_scaled, remaining_share_capacity, notional_capacity)
        if approved <= 0:
            return OverlayDecision(
                approved_size=Decimal("0"),
                codes=("directional_inventory_cap",),
            )
        return OverlayDecision(approved_size=approved)
