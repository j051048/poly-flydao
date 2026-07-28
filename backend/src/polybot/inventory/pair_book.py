from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, Field

from polybot.models import LiquidityRole, Outcome, Side


class PairFill(BaseModel):
    fill_id: str = Field(min_length=1)
    outcome: Outcome
    side: Side = Side.BUY
    size: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0, lt=1)
    fee_usd: Decimal = Field(default=Decimal("0"), ge=0)
    liquidity_role: LiquidityRole


class PairInventorySnapshot(BaseModel):
    yes_size: Decimal = Field(ge=0)
    no_size: Decimal = Field(ge=0)
    yes_cost_usd: Decimal = Field(ge=0)
    no_cost_usd: Decimal = Field(ge=0)
    paired_size: Decimal = Field(ge=0)
    paired_cost_usd: Decimal = Field(ge=0)
    paired_locked_edge_usd: Decimal
    directional_yes_size: Decimal = Field(ge=0)
    directional_no_size: Decimal = Field(ge=0)
    directional_cost_usd: Decimal = Field(ge=0)
    realized_pnl_usd: Decimal


@dataclass
class _OutcomeInventory:
    size: Decimal = Decimal("0")
    cost_usd: Decimal = Decimal("0")

    @property
    def average_cost(self) -> Decimal:
        if self.size <= 0:
            return Decimal("0")
        return self.cost_usd / self.size


class PairInventoryBook:
    """Average-cost inventory with paired and directional shares separated.

    A complete binary pair is always ``min(YES shares, NO shares)``. Any excess
    remains directional inventory and is never counted as locked-in arbitrage.
    Fill ids are idempotent so websocket/reconciliation duplicates cannot double
    count inventory.
    """

    def __init__(self) -> None:
        self._yes = _OutcomeInventory()
        self._no = _OutcomeInventory()
        self._seen_fills: set[str] = set()
        self._realized_pnl = Decimal("0")

    def apply_fill(self, fill: PairFill) -> bool:
        if fill.fill_id in self._seen_fills:
            return False
        inventory = self._inventory(fill.outcome)
        if fill.side is Side.BUY:
            inventory.size += fill.size
            inventory.cost_usd += fill.size * fill.price + fill.fee_usd
        else:
            if fill.size > inventory.size:
                raise ValueError("sell fill exceeds tracked outcome inventory")
            average_cost = inventory.average_cost
            released_basis = average_cost * fill.size
            net_proceeds = fill.size * fill.price - fill.fee_usd
            inventory.size -= fill.size
            inventory.cost_usd = max(Decimal("0"), inventory.cost_usd - released_basis)
            self._realized_pnl += net_proceeds - released_basis
        self._seen_fills.add(fill.fill_id)
        return True

    def snapshot(self) -> PairInventorySnapshot:
        paired = min(self._yes.size, self._no.size)
        yes_paired_cost = self._yes.average_cost * paired
        no_paired_cost = self._no.average_cost * paired
        paired_cost = yes_paired_cost + no_paired_cost
        directional_yes = max(Decimal("0"), self._yes.size - paired)
        directional_no = max(Decimal("0"), self._no.size - paired)
        directional_cost = (
            directional_yes * self._yes.average_cost
            + directional_no * self._no.average_cost
        )
        return PairInventorySnapshot(
            yes_size=self._yes.size,
            no_size=self._no.size,
            yes_cost_usd=self._yes.cost_usd,
            no_cost_usd=self._no.cost_usd,
            paired_size=paired,
            paired_cost_usd=paired_cost,
            paired_locked_edge_usd=paired - paired_cost,
            directional_yes_size=directional_yes,
            directional_no_size=directional_no,
            directional_cost_usd=directional_cost,
            realized_pnl_usd=self._realized_pnl,
        )

    def _inventory(self, outcome: Outcome) -> _OutcomeInventory:
        return self._yes if outcome is Outcome.YES else self._no
