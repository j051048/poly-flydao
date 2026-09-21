from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from polybot.models import UserTradeUpdate
from polybot.reconcile import OrderReconciler
from polybot.stores.ledger import IncompleteFillLedgerError


class TraceStore:
    def __init__(self, *, bot_tokens: set[str] | None = None) -> None:
        self.trades: list[str] = []
        self.quarantined: list[object] = []
        self.bot_tokens = bot_tokens or set()

    async def reconcile_trade(self, update: UserTradeUpdate, account_id: str) -> None:
        self.trades.append(update.clob_trade_id)

    async def durable_order_ids(self, candidates: set[str], account_id: str) -> set[str]:
        return set()

    async def durable_token_ids(self, account_id: str) -> set[str]:
        return set(self.bot_tokens)

    async def record_quarantine(self, account_id: str, record: object) -> None:
        self.quarantined.append(record)

    async def pending_trade_ids(self, account_id: str) -> set[str]:
        return set()


class CloseOnlyClient:
    async def close(self) -> None:
        return None


def _reconciler(
    baseline: datetime | None,
    *,
    store: TraceStore | None = None,
    quarantine_enabled: bool = True,
) -> OrderReconciler:
    return OrderReconciler(
        private_key="unused",
        wallet=None,
        account_id="acct",
        store=store or TraceStore(),  # type: ignore[arg-type]
        client=CloseOnlyClient(),
        baseline_utc=baseline,
        quarantine_enabled=quarantine_enabled,
    )


async def test_baseline_ignores_old_trade_in_account_updates() -> None:
    baseline = datetime.now(UTC) - timedelta(minutes=5)
    reconciler = _reconciler(baseline)
    old_trade = SimpleNamespace(matched_at=datetime.now(UTC) - timedelta(days=30))

    assert await reconciler._account_trade_updates(old_trade) == []


async def test_recent_trade_still_requires_durable_mapping() -> None:
    baseline = datetime.now(UTC) - timedelta(minutes=5)
    store = TraceStore()
    reconciler = _reconciler(baseline, store=store)

    assert await reconciler._account_trade_updates(_recent_trade()) == []
    assert len(store.quarantined) == 1
    assert reconciler.quarantined_token_ids == frozenset({"token"})
    assert store.trades == []


async def test_quarantine_is_disabled_for_strict_deployments() -> None:
    baseline = datetime.now(UTC) - timedelta(minutes=5)
    reconciler = _reconciler(baseline, quarantine_enabled=False)

    with pytest.raises(IncompleteFillLedgerError):
        await reconciler._account_trade_updates(_recent_trade())


async def test_trade_touching_bot_inventory_still_fails_closed() -> None:
    """Quarantine must never mask divergence inside the bot's own footprint."""

    baseline = datetime.now(UTC) - timedelta(minutes=5)
    store = TraceStore(bot_tokens={"token"})
    reconciler = _reconciler(baseline, store=store)

    with pytest.raises(IncompleteFillLedgerError):
        await reconciler._account_trade_updates(_recent_trade())
    assert store.quarantined == []


def _recent_trade() -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
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
