from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from polybot.execution.order_manager import (
    HedgeQuote,
    OrderActionType,
    OrderGroupState,
    PairOrderManager,
)
from polybot.experimental.crypto_direction import (
    CryptoDirectionSignal,
    DirectionalOverlay,
    DirectionalOverlayConfig,
)
from polybot.inventory.pair_book import PairFill, PairInventoryBook
from polybot.models import (
    LiquidityRole,
    OrderGroup,
    OrderGroupStatus,
    OrderLeg,
    OrderLegPurpose,
    OrderLegStatus,
    Outcome,
    utc_now,
)


def _working_state() -> OrderGroupState:
    now = utc_now()
    group = OrderGroup(
        account_id="account",
        market_id="market",
        target_pair_size=Decimal("5"),
        expected_net_edge_usd=Decimal("0.10"),
        leg_deadline_at=now + timedelta(seconds=2),
    )
    yes = OrderLeg(
        group_id=group.id,
        outcome=Outcome.YES,
        token_id="yes",
        purpose=OrderLegPurpose.PAIR_ENTRY,
        liquidity_role=LiquidityRole.MAKER,
        post_only=True,
        price=Decimal("0.45"),
        size=Decimal("5"),
        clob_order_id="order-yes",
        status=OrderLegStatus.OPEN,
        deadline_at=group.leg_deadline_at,
    )
    no = OrderLeg(
        group_id=group.id,
        outcome=Outcome.NO,
        token_id="no",
        purpose=OrderLegPurpose.PAIR_ENTRY,
        liquidity_role=LiquidityRole.MAKER,
        post_only=True,
        price=Decimal("0.49"),
        size=Decimal("5"),
        clob_order_id="order-no",
        status=OrderLegStatus.OPEN,
        deadline_at=group.leg_deadline_at,
    )
    return OrderGroupState(
        group=group.model_copy(update={"status": OrderGroupStatus.WORKING}),
        legs=(yes, no),
    )


def test_pair_inventory_is_minimum_fill_and_directional_remainder_is_separate() -> None:
    # Exercise a grid of fill sizes as a lightweight property-style invariant test.
    for yes_size in range(1, 6):
        for no_size in range(1, 6):
            book = PairInventoryBook()
            assert book.apply_fill(
                PairFill(
                    fill_id="yes",
                    outcome=Outcome.YES,
                    size=Decimal(yes_size),
                    price=Decimal("0.45"),
                    fee_usd=Decimal("0.01"),
                    liquidity_role=LiquidityRole.TAKER,
                )
            )
            assert book.apply_fill(
                PairFill(
                    fill_id="no",
                    outcome=Outcome.NO,
                    size=Decimal(no_size),
                    price=Decimal("0.49"),
                    liquidity_role=LiquidityRole.MAKER,
                )
            )
            assert not book.apply_fill(
                PairFill(
                    fill_id="yes",
                    outcome=Outcome.YES,
                    size=Decimal(yes_size),
                    price=Decimal("0.45"),
                    liquidity_role=LiquidityRole.TAKER,
                )
            )
            snapshot = book.snapshot()
            assert snapshot.paired_size == Decimal(min(yes_size, no_size))
            assert snapshot.directional_yes_size == Decimal(max(yes_size - no_size, 0))
            assert snapshot.directional_no_size == Decimal(max(no_size - yes_size, 0))
            assert not (
                snapshot.directional_yes_size > 0
                and snapshot.directional_no_size > 0
            )


def test_single_leg_deadline_cancels_then_hedges_without_ai() -> None:
    manager = PairOrderManager(maximum_hedge_notional_usd=Decimal("5"))
    state = _working_state()
    yes_leg, no_leg = state.legs
    state = manager.record_fill(
        state,
        fill_id="fill-yes",
        leg_id=yes_leg.id,
        size=Decimal("3"),
        price=Decimal("0.45"),
    )
    assert state.group.status is OrderGroupStatus.IMBALANCED
    assert state.group.directional_yes_size == Decimal("3")

    state, cancel = manager.on_leg_deadline(
        state,
        now=state.group.leg_deadline_at + timedelta(milliseconds=1),
    )
    assert cancel.action is OrderActionType.CANCEL_ORDERS
    assert set(cancel.order_ids) == {"order-yes", "order-no"}
    assert state.group.status is OrderGroupStatus.CANCELLING

    state, hedge = manager.confirm_cancellations(
        state,
        cancelled_order_ids=frozenset(cancel.order_ids),
        hedge_quote=HedgeQuote(
            outcome=Outcome.NO,
            token_id="no",
            price=Decimal("0.50"),
            available_size=Decimal("10"),
            fee_per_share=Decimal("0.001"),
        ),
    )
    assert hedge.action is OrderActionType.SUBMIT_HEDGE
    assert hedge.hedge_leg is not None
    assert hedge.hedge_leg.outcome is Outcome.NO
    assert hedge.hedge_leg.size == Decimal("3")
    assert hedge.hedge_leg.liquidity_role is LiquidityRole.TAKER

    state, action = manager.record_hedge_submission(
        state,
        hedge_leg_id=hedge.hedge_leg.id,
        order_id="hedge-order",
    )
    assert action.action is OrderActionType.NONE
    state = manager.record_fill(
        state,
        fill_id="fill-hedge",
        leg_id=hedge.hedge_leg.id,
        size=Decimal("3"),
        price=Decimal("0.50"),
        fee_usd=Decimal("0.003"),
    )
    assert state.group.status is OrderGroupStatus.PAIRED
    assert state.group.paired_size == Decimal("3")
    assert state.group.directional_yes_size == 0


def test_unavailable_or_oversized_hedge_freezes_group() -> None:
    manager = PairOrderManager(maximum_hedge_notional_usd=Decimal("1"))
    state = _working_state()
    state = manager.record_fill(
        state,
        fill_id="fill-yes",
        leg_id=state.legs[0].id,
        size=Decimal("3"),
        price=Decimal("0.45"),
    )
    state, cancel = manager.on_leg_deadline(
        state,
        now=state.group.leg_deadline_at + timedelta(milliseconds=1),
    )
    state, action = manager.confirm_cancellations(
        state,
        cancelled_order_ids=frozenset(cancel.order_ids),
        hedge_quote=HedgeQuote(
            outcome=Outcome.NO,
            token_id="no",
            price=Decimal("0.50"),
            available_size=Decimal("3"),
        ),
    )
    assert state.group.status is OrderGroupStatus.FROZEN
    assert action.action is OrderActionType.FREEZE


def test_fill_winning_cancel_race_is_accounted_and_duplicate_safe() -> None:
    manager = PairOrderManager()
    state = _working_state()
    state, cancel = manager.on_leg_deadline(
        state,
        now=state.group.leg_deadline_at + timedelta(milliseconds=1),
    )
    state, _ = manager.confirm_cancellations(
        state,
        cancelled_order_ids=frozenset(cancel.order_ids),
        hedge_quote=None,
    )
    cancelled_leg = state.legs[0]
    state = manager.record_fill(
        state,
        fill_id="late-fill",
        leg_id=cancelled_leg.id,
        size=Decimal("1"),
        price=Decimal("0.45"),
    )
    duplicate = manager.record_fill(
        state,
        fill_id="late-fill",
        leg_id=cancelled_leg.id,
        size=Decimal("1"),
        price=Decimal("0.45"),
    )
    assert state.legs[0].filled_size == Decimal("1")
    assert state.legs[0].status is OrderLegStatus.CANCELLED
    assert duplicate == state
    assert state.group.status is OrderGroupStatus.FROZEN


def test_partial_fill_during_cancel_pending_preserves_cancel_then_hedge_ordering() -> None:
    manager = PairOrderManager()
    state = _working_state()
    state, cancel = manager.on_leg_deadline(
        state,
        now=state.group.leg_deadline_at + timedelta(milliseconds=1),
    )
    state = manager.record_fill(
        state,
        fill_id="race-before-ack",
        leg_id=state.legs[0].id,
        size=Decimal("1"),
        price=Decimal("0.45"),
    )
    assert state.legs[0].status is OrderLegStatus.CANCEL_PENDING
    state, action = manager.confirm_cancellations(
        state,
        cancelled_order_ids=frozenset(cancel.order_ids),
        hedge_quote=HedgeQuote(
            outcome=Outcome.NO,
            token_id="no",
            price=Decimal("0.50"),
            available_size=Decimal("1"),
        ),
    )
    assert action.action is OrderActionType.SUBMIT_HEDGE
    assert action.hedge_leg is not None
    assert action.hedge_leg.size == Decimal("1")
    assert all(
        leg.status is not OrderLegStatus.CANCEL_PENDING
        for leg in state.legs
        if leg.purpose is OrderLegPurpose.PAIR_ENTRY
    )


def test_directional_overlay_cannot_exceed_hard_inventory_or_notional_caps() -> None:
    inventory = PairInventoryBook()
    inventory.apply_fill(
        PairFill(
            fill_id="directional",
            outcome=Outcome.YES,
            size=Decimal("1.5"),
            price=Decimal("0.45"),
            liquidity_role=LiquidityRole.MAKER,
        )
    )
    overlay = DirectionalOverlay(
        DirectionalOverlayConfig(
            enabled=True,
            maximum_directional_shares=Decimal("2"),
            maximum_overlay_notional_usd=Decimal("10"),
            minimum_confidence=Decimal("0.7"),
        )
    )
    decision = overlay.size(
        signal=CryptoDirectionSignal(
            asset_symbol="BTC",
            favored_outcome=Outcome.YES,
            strength=Decimal("1"),
            confidence=Decimal("0.9"),
            generated_at=utc_now(),
        ),
        requested_size=Decimal("100"),
        limit_price=Decimal("0.50"),
        inventory=inventory.snapshot(),
    )
    assert decision.approved
    assert decision.approved_size == Decimal("0.5")


def test_directional_overlay_is_disabled_by_default() -> None:
    empty = PairInventoryBook().snapshot()
    decision = DirectionalOverlay().size(
        signal=CryptoDirectionSignal(
            asset_symbol="BTC",
            favored_outcome=Outcome.YES,
            strength=Decimal("1"),
            confidence=Decimal("1"),
            generated_at=utc_now(),
        ),
        requested_size=Decimal("1"),
        limit_price=Decimal("0.5"),
        inventory=empty,
    )
    assert not decision.approved
    assert decision.codes == ("overlay_disabled",)
