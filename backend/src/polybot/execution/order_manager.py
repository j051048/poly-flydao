from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from polybot.models import (
    LiquidityRole,
    OrderGroup,
    OrderGroupStatus,
    OrderLeg,
    OrderLegPurpose,
    OrderLegStatus,
    OrderPlan,
    Outcome,
    Side,
    utc_now,
)


class OrderActionType(StrEnum):
    NONE = "none"
    CANCEL_ORDERS = "cancel_orders"
    SUBMIT_HEDGE = "submit_hedge"
    FREEZE = "freeze"


class HedgeQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: Outcome
    token_id: str = Field(min_length=1)
    price: Decimal = Field(gt=0, lt=1)
    available_size: Decimal = Field(gt=0)
    fee_per_share: Decimal = Field(default=Decimal("0"), ge=0)


class OrderManagerAction(BaseModel):
    action: OrderActionType = OrderActionType.NONE
    order_ids: tuple[str, ...] = ()
    hedge_leg: OrderLeg | None = None
    reason: str = ""


class OrderGroupState(BaseModel):
    group: OrderGroup
    legs: tuple[OrderLeg, ...] = Field(min_length=2)
    seen_fill_ids: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def validate_group_membership(self) -> OrderGroupState:
        if any(leg.group_id != self.group.id for leg in self.legs):
            raise ValueError("state contains a leg from another order group")
        return self


class PairOrderManager:
    """Fill-driven cancel → hedge → freeze state machine.

    This component is deliberately deterministic and imports no forecasting or
    AI provider. A batch response is treated as a collection of independent leg
    responses, because Polymarket does not promise pair atomicity.
    """

    def __init__(
        self,
        *,
        maximum_hedge_notional_usd: Decimal = Decimal("5"),
        hedge_deadline: timedelta = timedelta(seconds=4),
    ):
        if maximum_hedge_notional_usd <= 0:
            raise ValueError("maximum_hedge_notional_usd must be positive")
        if hedge_deadline <= timedelta(0):
            raise ValueError("hedge_deadline must be positive")
        self.maximum_hedge_notional_usd = maximum_hedge_notional_usd
        self.hedge_deadline = hedge_deadline

    @staticmethod
    def from_plan(plan: OrderPlan) -> OrderGroupState:
        return OrderGroupState(group=plan.group, legs=plan.legs)

    def record_submission(
        self,
        state: OrderGroupState,
        *,
        accepted_order_ids: dict[UUID, str],
        rejected_leg_ids: frozenset[UUID] = frozenset(),
        now: datetime | None = None,
    ) -> OrderGroupState:
        timestamp = now or utc_now()
        legs: list[OrderLeg] = []
        for leg in state.legs:
            if leg.id in accepted_order_ids:
                legs.append(
                    leg.model_copy(
                        update={
                            "clob_order_id": accepted_order_ids[leg.id],
                            "status": OrderLegStatus.OPEN,
                            "version": leg.version + 1,
                            "updated_at": timestamp,
                        }
                    )
                )
            elif leg.id in rejected_leg_ids:
                legs.append(
                    leg.model_copy(
                        update={
                            "status": OrderLegStatus.REJECTED,
                            "version": leg.version + 1,
                            "updated_at": timestamp,
                        }
                    )
                )
            else:
                legs.append(leg)
        accepted = len(accepted_order_ids)
        if accepted == len(state.legs):
            status = OrderGroupStatus.WORKING
        elif accepted:
            status = OrderGroupStatus.IMBALANCED
        else:
            status = OrderGroupStatus.FAILED
        group = state.group.model_copy(
            update={
                "status": status,
                "version": state.group.version + 1,
                "updated_at": timestamp,
            }
        )
        return self._refresh_inventory(
            state.model_copy(update={"group": group, "legs": tuple(legs)}),
            now=timestamp,
            preserve_phase=True,
        )

    def record_fill(
        self,
        state: OrderGroupState,
        *,
        fill_id: str,
        leg_id: UUID,
        size: Decimal,
        price: Decimal,
        fee_usd: Decimal = Decimal("0"),
        occurred_at: datetime | None = None,
    ) -> OrderGroupState:
        if fill_id in state.seen_fill_ids:
            return state
        if size <= 0 or price <= 0 or price >= 1 or fee_usd < 0:
            raise ValueError("invalid fill")
        timestamp = occurred_at or utc_now()
        found = False
        legs: list[OrderLeg] = []
        for leg in state.legs:
            if leg.id != leg_id:
                legs.append(leg)
                continue
            found = True
            if size > leg.remaining_size:
                raise ValueError("fill exceeds remaining leg size")
            new_filled = leg.filled_size + size
            previous_value = (leg.average_fill_price or Decimal("0")) * leg.filled_size
            average = (previous_value + price * size) / new_filled
            if new_filled == leg.size:
                status = OrderLegStatus.FILLED
            elif leg.status is OrderLegStatus.CANCELLED:
                # A trade can win the race with a cancel acknowledgement. Keep
                # the leg terminal but account for the late partial fill.
                status = OrderLegStatus.CANCELLED
            elif leg.status is OrderLegStatus.CANCEL_PENDING:
                # Preserve the outstanding cancellation transition. The fill is
                # accounted now; the remaining size is terminal only after the
                # exchange confirms the cancel.
                status = OrderLegStatus.CANCEL_PENDING
            else:
                status = OrderLegStatus.PARTIALLY_FILLED
            legs.append(
                leg.model_copy(
                    update={
                        "filled_size": new_filled,
                        "average_fill_price": average,
                        "fee_paid_usd": leg.fee_paid_usd + fee_usd,
                        "status": status,
                        "version": leg.version + 1,
                        "updated_at": timestamp,
                    }
                )
            )
        if not found:
            raise KeyError(f"unknown order leg {leg_id}")
        refreshed = self._refresh_inventory(
            state.model_copy(
                update={
                    "legs": tuple(legs),
                    "seen_fill_ids": state.seen_fill_ids | {fill_id},
                }
            ),
            now=timestamp,
        )
        return refreshed

    def record_hedge_submission(
        self,
        state: OrderGroupState,
        *,
        hedge_leg_id: UUID,
        order_id: str | None,
        now: datetime | None = None,
    ) -> tuple[OrderGroupState, OrderManagerAction]:
        timestamp = now or utc_now()
        found = False
        legs: list[OrderLeg] = []
        for leg in state.legs:
            if leg.id != hedge_leg_id:
                legs.append(leg)
                continue
            found = True
            if leg.purpose is not OrderLegPurpose.IMBALANCE_HEDGE:
                raise ValueError("record_hedge_submission requires a hedge leg")
            if order_id:
                legs.append(
                    leg.model_copy(
                        update={
                            "clob_order_id": order_id,
                            "status": OrderLegStatus.OPEN,
                            "version": leg.version + 1,
                            "updated_at": timestamp,
                        }
                    )
                )
            else:
                legs.append(
                    leg.model_copy(
                        update={
                            "status": OrderLegStatus.REJECTED,
                            "version": leg.version + 1,
                            "updated_at": timestamp,
                        }
                    )
                )
        if not found:
            raise KeyError(f"unknown hedge leg {hedge_leg_id}")
        updated = state.model_copy(update={"legs": tuple(legs)})
        if order_id:
            return updated, OrderManagerAction()
        return self.freeze_after_hedge_failure(
            updated,
            reason="deterministic imbalance hedge was rejected",
            now=timestamp,
        )

    def on_leg_deadline(
        self,
        state: OrderGroupState,
        *,
        now: datetime | None = None,
    ) -> tuple[OrderGroupState, OrderManagerAction]:
        timestamp = now or utc_now()
        if timestamp < state.group.leg_deadline_at or state.group.status in {
            OrderGroupStatus.PAIRED,
            OrderGroupStatus.CANCELLED,
            OrderGroupStatus.FROZEN,
            OrderGroupStatus.FAILED,
        }:
            return state, OrderManagerAction()
        cancel_ids: list[str] = []
        legs: list[OrderLeg] = []
        for leg in state.legs:
            if (
                leg.clob_order_id
                and leg.remaining_size > 0
                and leg.status
                in {
                    OrderLegStatus.OPEN,
                    OrderLegStatus.PARTIALLY_FILLED,
                    OrderLegStatus.SUBMITTING,
                }
            ):
                cancel_ids.append(leg.clob_order_id)
                legs.append(
                    leg.model_copy(
                        update={
                            "status": OrderLegStatus.CANCEL_PENDING,
                            "version": leg.version + 1,
                            "updated_at": timestamp,
                        }
                    )
                )
            else:
                legs.append(leg)
        if cancel_ids:
            group = state.group.model_copy(
                update={
                    "status": OrderGroupStatus.CANCELLING,
                    "version": state.group.version + 1,
                    "updated_at": timestamp,
                }
            )
            return (
                state.model_copy(update={"group": group, "legs": tuple(legs)}),
                OrderManagerAction(
                    action=OrderActionType.CANCEL_ORDERS,
                    order_ids=tuple(cancel_ids),
                    reason="pair leg deadline expired",
                ),
            )
        return self._terminal_without_working_orders(state, timestamp)

    def confirm_cancellations(
        self,
        state: OrderGroupState,
        *,
        cancelled_order_ids: frozenset[str],
        hedge_quote: HedgeQuote | None,
        now: datetime | None = None,
    ) -> tuple[OrderGroupState, OrderManagerAction]:
        timestamp = now or utc_now()
        legs = tuple(
            leg.model_copy(
                update={
                    "status": OrderLegStatus.CANCELLED,
                    "version": leg.version + 1,
                    "updated_at": timestamp,
                }
            )
            if leg.clob_order_id in cancelled_order_ids
            and leg.status is OrderLegStatus.CANCEL_PENDING
            else leg
            for leg in state.legs
        )
        updated = self._refresh_inventory(
            state.model_copy(update={"legs": legs}),
            now=timestamp,
            preserve_phase=True,
        )
        if any(leg.status is OrderLegStatus.CANCEL_PENDING for leg in updated.legs):
            return updated, OrderManagerAction()
        yes_filled, no_filled = self._bought_by_outcome(updated.legs)
        imbalance = abs(yes_filled - no_filled)
        if imbalance == 0:
            return self._terminal_without_working_orders(updated, timestamp)
        needed = Outcome.NO if yes_filled > no_filled else Outcome.YES
        if (
            hedge_quote is None
            or hedge_quote.outcome is not needed
            or hedge_quote.available_size < imbalance
            or imbalance * (hedge_quote.price + hedge_quote.fee_per_share)
            > self.maximum_hedge_notional_usd
        ):
            frozen = updated.group.model_copy(
                update={
                    "status": OrderGroupStatus.FROZEN,
                    "version": updated.group.version + 1,
                    "updated_at": timestamp,
                }
            )
            return (
                updated.model_copy(update={"group": frozen}),
                OrderManagerAction(
                    action=OrderActionType.FREEZE,
                    reason="no bounded deterministic hedge was available",
                ),
            )
        hedge_leg = OrderLeg(
            group_id=updated.group.id,
            outcome=needed,
            token_id=hedge_quote.token_id,
            side=Side.BUY,
            purpose=OrderLegPurpose.IMBALANCE_HEDGE,
            liquidity_role=LiquidityRole.TAKER,
            post_only=False,
            price=hedge_quote.price,
            size=imbalance,
            deadline_at=timestamp + self.hedge_deadline,
            created_at=timestamp,
            updated_at=timestamp,
        )
        hedging = updated.group.model_copy(
            update={
                "status": OrderGroupStatus.HEDGING,
                "version": updated.group.version + 1,
                "updated_at": timestamp,
            }
        )
        next_state = updated.model_copy(
            update={"group": hedging, "legs": (*updated.legs, hedge_leg)}
        )
        return (
            next_state,
            OrderManagerAction(
                action=OrderActionType.SUBMIT_HEDGE,
                hedge_leg=hedge_leg,
                reason="neutralize filled single-leg inventory",
            ),
        )

    def freeze_after_hedge_failure(
        self,
        state: OrderGroupState,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> tuple[OrderGroupState, OrderManagerAction]:
        timestamp = now or utc_now()
        group = state.group.model_copy(
            update={
                "status": OrderGroupStatus.FROZEN,
                "version": state.group.version + 1,
                "updated_at": timestamp,
            }
        )
        return (
            state.model_copy(update={"group": group}),
            OrderManagerAction(action=OrderActionType.FREEZE, reason=reason),
        )

    def _terminal_without_working_orders(
        self,
        state: OrderGroupState,
        timestamp: datetime,
    ) -> tuple[OrderGroupState, OrderManagerAction]:
        refreshed = self._refresh_inventory(state, now=timestamp, preserve_phase=True)
        if refreshed.group.directional_yes_size or refreshed.group.directional_no_size:
            status = OrderGroupStatus.FROZEN
            action = OrderManagerAction(
                action=OrderActionType.FREEZE,
                reason="terminal group retains unhedged directional inventory",
            )
        elif refreshed.group.paired_size > 0:
            status = OrderGroupStatus.PAIRED
            action = OrderManagerAction()
        else:
            status = OrderGroupStatus.CANCELLED
            action = OrderManagerAction()
        group = refreshed.group.model_copy(
            update={
                "status": status,
                "version": refreshed.group.version + 1,
                "updated_at": timestamp,
            }
        )
        return refreshed.model_copy(update={"group": group}), action

    def _refresh_inventory(
        self,
        state: OrderGroupState,
        *,
        now: datetime | None = None,
        preserve_phase: bool = False,
    ) -> OrderGroupState:
        yes_filled, no_filled = self._bought_by_outcome(state.legs)
        paired = min(yes_filled, no_filled)
        directional_yes = max(Decimal("0"), yes_filled - paired)
        directional_no = max(Decimal("0"), no_filled - paired)
        status = state.group.status
        terminal_hedge = False
        terminal_cancel_race = False
        if status is OrderGroupStatus.HEDGING:
            hedge_legs = [
                leg
                for leg in state.legs
                if leg.purpose is OrderLegPurpose.IMBALANCE_HEDGE
            ]
            if hedge_legs and all(leg.is_terminal for leg in hedge_legs):
                terminal_hedge = True
                status = (
                    OrderGroupStatus.PAIRED
                    if paired > 0 and not (directional_yes or directional_no)
                    else OrderGroupStatus.FROZEN
                )
        if status in {OrderGroupStatus.CANCELLED, OrderGroupStatus.PAIRED} and (
            directional_yes or directional_no
        ):
            terminal_cancel_race = True
            status = OrderGroupStatus.FROZEN
        if (
            not preserve_phase
            and not terminal_hedge
            and not terminal_cancel_race
            and status
            not in {
            OrderGroupStatus.CANCELLING,
            OrderGroupStatus.HEDGING,
            OrderGroupStatus.FROZEN,
            OrderGroupStatus.FAILED,
            }
        ):
            if paired >= state.group.target_pair_size and not (
                directional_yes or directional_no
            ):
                status = OrderGroupStatus.PAIRED
            elif directional_yes or directional_no:
                status = OrderGroupStatus.IMBALANCED
            else:
                status = OrderGroupStatus.WORKING
        group = state.group.model_copy(
            update={
                "paired_size": min(paired, state.group.target_pair_size),
                "directional_yes_size": directional_yes,
                "directional_no_size": directional_no,
                "status": status,
                "version": state.group.version + 1,
                "updated_at": now or utc_now(),
            }
        )
        return state.model_copy(update={"group": group})

    @staticmethod
    def _bought_by_outcome(legs: tuple[OrderLeg, ...]) -> tuple[Decimal, Decimal]:
        yes = sum(
            (
                leg.filled_size
                for leg in legs
                if leg.side is Side.BUY and leg.outcome is Outcome.YES
            ),
            Decimal("0"),
        )
        no = sum(
            (
                leg.filled_size
                for leg in legs
                if leg.side is Side.BUY and leg.outcome is Outcome.NO
            ),
            Decimal("0"),
        )
        return yes, no
