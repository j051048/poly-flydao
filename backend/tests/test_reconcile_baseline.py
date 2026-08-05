from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from polybot.models import UserTradeUpdate
from polybot.reconcile import OrderReconciler
from polybot.stores.ledger import IncompleteFillLedgerError


class TraceStore:
    def __init__(self) -> None:
        self.trades: list[str] = []

    async def reconcile_trade(self, update: UserTradeUpdate, account_id: str) -> None:
        self.trades.append(update.clob_trade_id)

    async def durable_order_ids(self, candidates: set[str], account_id: str) -> set[str]:
        return set()

    async def pending_trade_ids(self, account_id: str) -> set[str]:
        return set()


class CloseOnlyClient:
    async def close(self) -> None:
        return None


def _reconciler(baseline: datetime | None) -> OrderReconciler:
    return OrderReconciler(
        private_key="unused",
        wallet=None,
        account_id="acct",
        store=TraceStore(),  # type: ignore[arg-type]
        client=CloseOnlyClient(),
        baseline_utc=baseline,
    )


async def test_baseline_ignores_old_trade_in_account_updates() -> None:
    baseline = datetime.now(UTC) - timedelta(minutes=5)
    reconciler = _reconciler(baseline)
    old_trade = SimpleNamespace(matched_at=datetime.now(UTC) - timedelta(days=30))

    assert await reconciler._account_trade_updates(old_trade) == []


async def test_recent_trade_still_requires_durable_mapping() -> None:
    baseline = datetime.now(UTC) - timedelta(minutes=5)
    reconciler = _reconciler(baseline)
    now = datetime.now(UTC)
    trade = SimpleNamespace(
        id="trade-new",
        matched_at=now,
        status="MATCHED",
        condition_id="c1",
        maker_orders=[],
        taker_order_id="order-new",
        token_id="token",
        side="BUY",
        price=Decimal("0.5"),
        matched_amount=Decimal("1"),
        size=Decimal("1"),
        fee_rate_bps=0,
        transaction_hash=None,
        updated_at=now,
        trader_side="TAKER",
    )

    with pytest.raises(IncompleteFillLedgerError):
        await reconciler._account_trade_updates(trade)


async def test_reconcile_trade_and_notify_skips_old_update() -> None:
    baseline = datetime.now(UTC) - timedelta(minutes=5)
    store = TraceStore()
    reconciler = OrderReconciler(
        private_key="unused",
        wallet=None,
        account_id="acct",
        store=store,  # type: ignore[arg-type]
        client=CloseOnlyClient(),
        baseline_utc=baseline,
    )
    old = UserTradeUpdate(
        clob_trade_id="old-1",
        candidate_order_ids=["order-old"],
        condition_id="c1",
        token_id="token",
        side="BUY",
        price=Decimal("0.5"),
        size=Decimal("1"),
        status="MATCHED",
        matched_at=datetime.now(UTC) - timedelta(days=1),
    )

    await reconciler._reconcile_trade_and_notify(old)

    assert store.trades == []


def test_baseline_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _reconciler(datetime.now())
