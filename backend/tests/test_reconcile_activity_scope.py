from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from polybot.models import ExecutionResult, ExecutionStatus, MarketSpec, QuarantineKind
from polybot.reconcile import OrderReconciler
from polybot.stores.ledger import IncompleteFillLedgerError
from polybot.stores.memory import MemoryStore


class Items:
    def __init__(self, items):
        self.items = items

    async def iter_items(self):
        for item in self.items:
            yield item


class ActivityClient:
    def __init__(self, *, activities=(), trades=(), positions=()):
        self.activities = list(activities)
        self.trades = list(trades)
        self.positions = list(positions)

    def list_open_orders(self):
        return Items([])

    def list_account_trades(self, **kwargs):
        return Items(self.trades)

    def list_activity(self, **kwargs):
        return Items(self.activities)

    def list_positions(self, **kwargs):
        return Items(self.positions)

    async def get_order(self, *, order_id):
        trade = next(trade for trade in self.trades if trade.taker_order_id == order_id)
        return SimpleNamespace(
            id=order_id, condition_id=trade.condition_id, token_id=trade.token_id,
            side=trade.side, price=trade.price, original_size=trade.size,
            size_matched=trade.size, status="MATCHED", order_type="GTC",
        )


def activity(kind="REDEEM", *, condition="external", occurred_at=None):
    return SimpleNamespace(
        type=kind,
        condition_id=condition,
        transaction_hash=f"tx-{kind}-{condition}",
        timestamp=occurred_at or datetime.now(UTC),
        amount=Decimal("10"),
    )


def reconciler(store, client, *, baseline=None, quarantine_enabled=True):
    return OrderReconciler(
        private_key="unused",
        wallet=None,
        account_id="account",
        store=store,
        client=client,
        baseline_utc=baseline,
        quarantine_enabled=quarantine_enabled,
    )


@pytest.mark.parametrize("kind", ["REDEEM", "SPLIT", "MERGE", "CONVERSION"])
async def test_rest_quarantines_prebaseline_external_lifecycle_and_preserves_audit(kind):
    store = MemoryStore()
    event = activity(kind, occurred_at=datetime.now(UTC) - timedelta(days=30))
    repair = reconciler(
        store, ActivityClient(activities=[event]), baseline=datetime.now(UTC) - timedelta(minutes=5)
    )

    await repair.reconcile_rest()
    repair.set_baseline(datetime.now(UTC))
    await repair.reconcile_rest()

    records = await store.list_quarantine("account")
    assert len(records) == 1
    assert records[0].kind == QuarantineKind.ACTIVITY
    assert records[0].reason == "prebaseline_external_activity"
    assert records[0].detail["activity_type"] == kind
    assert len(store.account_activity_updates) == 1
    ledger = await store.fill_ledger_snapshot("account", datetime.now(UTC) - timedelta(days=31))
    assert ledger.realized_pnl_usd == 0
    assert ledger.token_quantities == {}


async def test_rest_repairs_legacy_contamination_even_when_rest_does_not_return_it():
    store = MemoryStore()
    old = activity(occurred_at=datetime.now(UTC) - timedelta(days=30))
    await store.reconcile_account_activity(OrderReconciler._activity_update(old), "account")
    with pytest.raises(IncompleteFillLedgerError, match="cost basis"):
        await store.fill_ledger_snapshot("account", datetime.now(UTC))

    await reconciler(store, ActivityClient(), baseline=datetime.now(UTC)).reconcile_rest()

    assert len(store.account_activity_updates) == 1
    assert len(store.quarantined_activity_keys) == 1
    assert (await store.fill_ledger_snapshot("account", datetime.now(UTC))).token_quantities == {}


async def test_foreign_position_can_be_redeemed_after_quarantine():
    store = MemoryStore()
    await store.save_market(
        MarketSpec(
            id="external-market",
            condition_id="external",
            question="External position?",
            yes_token_id="external-yes",
            no_token_id="external-no",
        )
    )
    client = ActivityClient(
        positions=[
            SimpleNamespace(
                condition_id="external",
                token_id="external-yes",
                size=Decimal("10"),
                initial_value=Decimal("4"),
                current_value=Decimal("10"),
                avg_price=Decimal("0.4"),
                realized_pnl=0,
                cur_price=Decimal("1"),
            )
        ]
    )
    repair = reconciler(store, client)
    await repair.reconcile_rest()
    assert not store.position_updates
    client.positions = []
    client.activities = [activity()]

    await repair.reconcile_rest()

    assert {record.kind for record in await store.list_quarantine("account")} == {
        QuarantineKind.POSITION, QuarantineKind.ACTIVITY
    }
    assert (await store.fill_ledger_snapshot("account", datetime.now(UTC))).realized_pnl_usd == 0


async def seed_bot_trade(store, *, occurred_at):
    await store.save_market(
        MarketSpec(
            id="bot-market", condition_id="bot", question="Bot position?",
            yes_token_id="yes", no_token_id="no",
        )
    )
    await store.save_execution(
        ExecutionResult(
            intent_hash="intent", status=ExecutionStatus.ACCEPTED, order_id="bot-order"
        ),
        "account",
    )
    return SimpleNamespace(
        id="bot-trade", matched_at=occurred_at, updated_at=occurred_at, status="CONFIRMED",
        condition_id="bot", maker_orders=[], taker_order_id="bot-order", token_id="yes",
        side="BUY", price=Decimal("0.4"), size=Decimal("10"), fee_rate_bps=0,
        transaction_hash="buy-tx", trader_side="TAKER",
    )


async def test_rest_keeps_bot_buy_and_redeem_cost_basis_across_baseline_reset():
    store = MemoryStore()
    now = datetime.now(UTC)
    trade = await seed_bot_trade(store, occurred_at=now - timedelta(days=30))
    client = ActivityClient(
        trades=[trade], activities=[activity(condition="bot", occurred_at=now - timedelta(days=1))]
    )
    repair = reconciler(store, client, baseline=now)

    await repair.reconcile_rest()

    ledger = await store.fill_ledger_snapshot("account", now - timedelta(days=31))
    assert ledger.realized_pnl_usd == Decimal("6")
    assert ledger.token_quantities == {}
    assert ledger.redeemed_condition_ids == {"bot"}
    assert not store.quarantined_activity_keys


@pytest.mark.parametrize("kind", ["SPLIT", "MERGE", "CONVERSION"])
async def test_rest_bot_condition_unsupported_activity_still_fails_closed(kind):
    store = MemoryStore()
    now = datetime.now(UTC)
    trade = await seed_bot_trade(store, occurred_at=now - timedelta(days=30))
    client = ActivityClient(
        trades=[trade],
        activities=[activity(kind, condition="bot", occurred_at=now - timedelta(days=1))],
    )

    with pytest.raises(IncompleteFillLedgerError, match="unsupported account token activity"):
        await reconciler(store, client, baseline=now).reconcile_rest()
    assert not store.quarantined_activity_keys


async def test_strict_mode_keeps_postbaseline_external_activity_in_strict_ledger():
    store = MemoryStore()
    with pytest.raises(IncompleteFillLedgerError, match="cost basis"):
        await reconciler(
            store, ActivityClient(activities=[activity()]), quarantine_enabled=False
        ).reconcile_rest()


async def test_missing_activity_condition_never_assumed_external():
    store = MemoryStore()
    with pytest.raises(IncompleteFillLedgerError, match="invalid economics"):
        await reconciler(
            store, ActivityClient(activities=[activity(condition=None)]), baseline=datetime.now(UTC)
        ).reconcile_rest()
