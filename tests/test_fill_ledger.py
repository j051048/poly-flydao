from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from polybot.stores.ledger import (
    IncompleteFillLedgerError,
    realized_pnl_since,
    replay_fill_ledger,
)


def _fill(
    *,
    key: str,
    side: str,
    price: str,
    size: str,
    matched_at: datetime,
    fee: str = "0",
    token: str = "yes",
) -> dict[str, object]:
    return {
        "fill_key": key,
        "outcome_token_id": token,
        "condition_id": "condition-1",
        "side": side,
        "price": price,
        "size": size,
        "fee_pusd": fee,
        "settlement_status": "CONFIRMED",
        "matched_at": matched_at.isoformat(),
        "created_at": matched_at.isoformat(),
    }


def test_realized_pnl_uses_full_history_but_only_counts_utc_window() -> None:
    midnight = datetime(2026, 7, 22, tzinfo=UTC)
    rows = [
        _fill(
            key="buy-before",
            side="BUY",
            price="0.40",
            size="10",
            fee="0.10",
            matched_at=midnight - timedelta(days=1),
        ),
        _fill(
            key="sell-before",
            side="SELL",
            price="0.60",
            size="2",
            fee="0.02",
            matched_at=midnight - timedelta(hours=1),
        ),
        _fill(
            key="sell-today",
            side="SELL",
            price="0.70",
            size="3",
            fee="0.03",
            matched_at=midnight + timedelta(hours=1),
        ),
    ]

    # Average cost is 0.41/share, so today's realization is 2.10 - .03 - 1.23.
    assert realized_pnl_since(rows, since=midnight) == Decimal("0.84")


def test_realized_pnl_tracks_each_outcome_token_independently() -> None:
    midnight = datetime(2026, 7, 22, tzinfo=UTC)
    rows = [
        _fill(
            key="yes-buy",
            token="yes",
            side="BUY",
            price="0.20",
            size="5",
            matched_at=midnight - timedelta(days=1),
        ),
        _fill(
            key="no-buy",
            token="no",
            side="BUY",
            price="0.70",
            size="5",
            matched_at=midnight - timedelta(days=1),
        ),
        _fill(
            key="yes-sell",
            token="yes",
            side="SELL",
            price="0.30",
            size="2",
            matched_at=midnight,
        ),
        _fill(
            key="no-sell",
            token="no",
            side="SELL",
            price="0.60",
            size="2",
            matched_at=midnight,
        ),
    ]

    assert realized_pnl_since(rows, since=midnight) == Decimal("0.0")


def test_realized_pnl_fails_closed_when_prior_cost_basis_is_missing() -> None:
    midnight = datetime(2026, 7, 22, tzinfo=UTC)
    rows = [
        _fill(
            key="orphan-sell",
            side="SELL",
            price="0.60",
            size="2",
            matched_at=midnight,
        )
    ]

    with pytest.raises(IncompleteFillLedgerError, match="preceding buy history"):
        realized_pnl_since(rows, since=midnight)


def test_failed_fill_is_not_realized() -> None:
    midnight = datetime(2026, 7, 22, tzinfo=UTC)
    row = _fill(
        key="failed",
        side="SELL",
        price="0.60",
        size="2",
        matched_at=midnight,
    )
    row["settlement_status"] = "FAILED"

    assert realized_pnl_since([row], since=midnight) == Decimal("0")


def test_fill_replay_exposes_remaining_quantity_and_cost_basis() -> None:
    midnight = datetime(2026, 7, 22, tzinfo=UTC)
    rows = [
        _fill(
            key="buy",
            side="BUY",
            price="0.40",
            size="10",
            fee="0.10",
            matched_at=midnight - timedelta(days=1),
        ),
        _fill(
            key="sell",
            side="SELL",
            price="0.60",
            size="2",
            matched_at=midnight,
        ),
    ]

    snapshot = replay_fill_ledger(rows, since=midnight)

    assert snapshot.token_quantities["yes"] == Decimal("8")
    assert snapshot.token_costs_usd["yes"] == Decimal("3.28")
    assert snapshot.realized_pnl_usd == Decimal("0.38")


def test_redeem_activity_realizes_whole_condition_cost_basis() -> None:
    midnight = datetime(2026, 7, 22, tzinfo=UTC)
    rows = [
        _fill(
            key="buy",
            side="BUY",
            price="0.40",
            size="10",
            matched_at=midnight - timedelta(days=1),
        )
    ]
    activities = [
        {
            "activity_key": "redeem-activity",
            "activity_type": "REDEEM",
            "condition_id": "condition-1",
            "amount_usd": "10",
            "occurred_at": midnight,
        }
    ]

    snapshot = replay_fill_ledger(rows, since=midnight, activities=activities)

    assert snapshot.realized_pnl_usd == Decimal("6")
    assert snapshot.token_quantities == {}
    assert snapshot.token_costs_usd == {}


def test_non_trade_token_mutation_fails_closed() -> None:
    midnight = datetime(2026, 7, 22, tzinfo=UTC)
    activity = {
        "activity_key": "split-activity",
        "activity_type": "SPLIT",
        "condition_id": "condition-1",
        "amount_usd": "10",
        "occurred_at": midnight,
    }

    with pytest.raises(IncompleteFillLedgerError, match="unsupported account token activity"):
        replay_fill_ledger([], since=midnight, activities=[activity])
