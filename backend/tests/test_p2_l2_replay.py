from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from polybot.models import BookLevel, Side, utc_now
from polybot.simulation.l2_replay import (
    L2BookEvent,
    L2ReplayEngine,
    L2ReplayScenario,
    L2TradeEvent,
    ReplayCancelRequest,
    ReplayOrderRequest,
    ReplayOrderStatus,
    require_event_level_replay,
)


def _book(at, sequence: int = 1) -> L2BookEvent:
    return L2BookEvent(
        occurred_at=at,
        sequence=sequence,
        bids=(BookLevel(price=Decimal("0.40"), size=Decimal("5")),),
        asks=(BookLevel(price=Decimal("0.60"), size=Decimal("5")),),
    )


def test_l2_replay_models_queue_partial_fill_and_cancel_race() -> None:
    start = utc_now()
    events = [
        _book(start),
        L2TradeEvent(
            occurred_at=start + timedelta(seconds=1),
            sequence=2,
            aggressor_side=Side.SELL,
            price=Decimal("0.40"),
            size=Decimal("3"),
        ),
        L2TradeEvent(
            occurred_at=start + timedelta(seconds=2),
            sequence=3,
            aggressor_side=Side.SELL,
            price=Decimal("0.40"),
            size=Decimal("4"),
        ),
    ]
    result = L2ReplayEngine().run(
        events=events,
        orders=[
            ReplayOrderRequest(
                order_id="maker",
                side=Side.BUY,
                price=Decimal("0.40"),
                size=Decimal("5"),
                submitted_at=start,
                submit_latency=timedelta(0),
                cancel_latency=timedelta(seconds=1),
            )
        ],
        cancellations=[
            ReplayCancelRequest(
                order_id="maker",
                requested_at=start + timedelta(seconds=1, milliseconds=500),
            )
        ],
    )
    order = result.orders[0]
    assert order.status is ReplayOrderStatus.CANCELLED
    assert order.filled_size == Decimal("2")
    assert order.remaining_size == Decimal("3")
    assert order.queue_ahead_remaining == 0
    assert result.fills[0].size == Decimal("2")


def test_cancel_effective_at_trade_timestamp_wins_deterministically() -> None:
    start = utc_now()
    result = L2ReplayEngine().run(
        events=[
            _book(start),
            L2TradeEvent(
                occurred_at=start + timedelta(seconds=2),
                sequence=2,
                aggressor_side=Side.SELL,
                price=Decimal("0.40"),
                size=Decimal("10"),
            ),
        ],
        orders=[
            ReplayOrderRequest(
                order_id="maker",
                side=Side.BUY,
                price=Decimal("0.40"),
                size=Decimal("2"),
                submitted_at=start,
                submit_latency=timedelta(0),
                cancel_latency=timedelta(seconds=1),
            )
        ],
        cancellations=[
            ReplayCancelRequest(
                order_id="maker",
                requested_at=start + timedelta(seconds=1),
            )
        ],
    )
    assert result.orders[0].status is ReplayOrderStatus.CANCELLED
    assert result.orders[0].filled_size == 0


def test_replay_rejects_post_only_order_that_would_cross_on_activation() -> None:
    start = utc_now()
    result = L2ReplayEngine().run(
        events=[_book(start)],
        orders=[
            ReplayOrderRequest(
                order_id="crossing",
                side=Side.BUY,
                price=Decimal("0.60"),
                size=Decimal("1"),
                submitted_at=start,
                submit_latency=timedelta(0),
            )
        ],
    )
    assert result.orders[0].status is ReplayOrderStatus.POST_ONLY_REJECTED


def test_only_complete_sequenced_l2_replay_can_enter_live_gate_evidence() -> None:
    start = utc_now()
    incomplete = L2ReplayEngine().run(events=[_book(start)], orders=[])
    with pytest.raises(ValueError, match="complete depth"):
        require_event_level_replay(incomplete)
    with pytest.raises(ValueError, match="legacy backtests"):
        require_event_level_replay(object())

    complete = L2ReplayEngine(
        complete_depth=True,
        verified_exchange_sequence=True,
    ).run(events=[_book(start)], orders=[])
    assert require_event_level_replay(complete) is complete


def test_replay_rejects_duplicate_or_time_reversed_exchange_sequences() -> None:
    start = utc_now()
    duplicate = [
        _book(start, sequence=1),
        L2TradeEvent(
            occurred_at=start + timedelta(seconds=1),
            sequence=1,
            aggressor_side=Side.SELL,
            price=Decimal("0.40"),
            size=Decimal("1"),
        ),
    ]
    with pytest.raises(ValueError, match="duplicate"):
        L2ReplayEngine().run(events=duplicate, orders=[])

    reversed_sequence = [
        _book(start, sequence=2),
        L2TradeEvent(
            occurred_at=start + timedelta(seconds=1),
            sequence=1,
            aggressor_side=Side.SELL,
            price=Decimal("0.40"),
            size=Decimal("1"),
        ),
    ]
    with pytest.raises(ValueError, match="backwards"):
        L2ReplayEngine().run(events=reversed_sequence, orders=[])

    sequence_gap = [
        _book(start, sequence=1),
        L2TradeEvent(
            occurred_at=start + timedelta(seconds=1),
            sequence=3,
            aggressor_side=Side.SELL,
            price=Decimal("0.40"),
            size=Decimal("1"),
        ),
    ]
    with pytest.raises(ValueError, match="sequence gap"):
        L2ReplayEngine(verified_exchange_sequence=True).run(
            events=sequence_gap,
            orders=[],
        )


def test_replay_scenario_is_a_portable_research_only_contract() -> None:
    start = utc_now()
    scenario = L2ReplayScenario(
        events=[_book(start)],
        orders=[
            ReplayOrderRequest(
                order_id="maker",
                side=Side.BUY,
                price=Decimal("0.40"),
                size=Decimal("1"),
                submitted_at=start,
                submit_latency=timedelta(0),
            )
        ],
    )

    result = scenario.replay()

    assert result.orders[0].status is ReplayOrderStatus.OPEN
    assert not result.live_gate_eligible
