from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_reconcile_activity_scope import ActivityClient
from test_reconcile_state import FakeQuery, FakeResponse

from polybot.reconcile import OrderReconciler
from polybot.stores.ledger import IncompleteFillLedgerError
from polybot.stores.supabase_store import SupabaseStore


class ScopeQuery(FakeQuery):
    def __init__(self, tables, table):
        super().__init__(tables, table)
        self.bounds = None

    def order(self, column, **kwargs):
        return self

    def range(self, start, end):
        self.bounds = (start, end)
        return self

    def neq(self, column, value):
        self.filters.append(("neq", column, value))
        return self

    def gt(self, column, value):
        self.filters.append(("gt", column, value))
        return self

    def _matches(self, row):
        if not super()._matches(row):
            return False
        for kind, column, value in self.filters:
            if kind == "neq" and row.get(column) == value:
                return False
            if kind == "gt" and row.get(column, 0) <= value:
                return False
        return True

    def execute(self):
        if self.operation == "upsert":
            rows = self.tables.setdefault(self.table, [])
            keys = self.conflict.split(",")
            existing = next(
                (row for row in rows if all(row.get(key) == self.payload.get(key) for key in keys)),
                None,
            )
            if existing is None:
                rows.append(dict(self.payload))
            else:
                existing.update(self.payload)
            return FakeResponse([dict(self.payload)])
        response = super().execute()
        if self.operation == "select" and self.bounds:
            start, end = self.bounds
            response.data = response.data[start : end + 1]
        return response


def store_for(tables):
    return SupabaseStore(
        "https://example.supabase.co", "unused", account_id="account",
        client=SimpleNamespace(table=lambda table: ScopeQuery(tables, table)),
    )


def legacy_row(*, condition="external", kind="REDEEM"):
    return {
        "id": "activity-id", "account_id": "account", "activity_key": "legacy-activity-1",
        "activity_type": kind, "condition_id": condition, "amount_pusd": "10",
        "transaction_hash": "legacy-tx", "raw_payload": {"original": "preserved"},
        "occurred_at": (datetime.now(UTC) - timedelta(days=30)).isoformat(),
        "ledger_scope": "bot", "scope_reason": None,
    }


@pytest.mark.parametrize("kind", ["REDEEM", "SPLIT", "MERGE", "CONVERSION"])
async def test_full_rest_migrates_supabase_legacy_rows_and_keeps_audit(kind):
    row = legacy_row(kind=kind)
    tables = {"account_activities": [row]}
    store = store_for(tables)
    repair = OrderReconciler(
        private_key="unused", wallet=None, account_id="account", store=store,
        client=ActivityClient(), baseline_utc=datetime.now(UTC),
    )

    await repair.reconcile_rest()
    await repair.reconcile_rest()

    assert row["ledger_scope"] == "quarantine"
    assert row["raw_payload"] == {"original": "preserved"}
    assert len(tables["reconciliation_quarantine"]) == 1
    audit = tables["reconciliation_quarantine"][0]
    assert audit["kind"] == "activity"
    assert audit["reason"] == "prebaseline_external_activity"
    assert audit["detail"]["raw_payload"] == row["raw_payload"]
    ledger = await store.fill_ledger_snapshot("account", datetime.now(UTC))
    assert ledger.realized_pnl_usd == 0


async def test_supabase_new_bot_footprint_reactivates_quarantined_row_and_fails_closed():
    row = legacy_row(condition="bot")
    tables = {"account_activities": [row]}
    store = store_for(tables)
    await store.reconcile_activity_scope("account", baseline=None, quarantine_enabled=True)
    assert row["ledger_scope"] == "quarantine"
    tables["order_intents"] = [{"id": "i", "account_id": "account", "market_id": "m"}]
    tables["markets"] = [{"id": "m", "condition_id": "bot"}]

    await store.reconcile_activity_scope(
        "account", baseline=datetime.now(UTC), quarantine_enabled=True
    )

    assert row["ledger_scope"] == "bot"
    assert row["scope_reason"] is None
    assert len(tables["reconciliation_quarantine"]) == 1  # historical audit survives
    with pytest.raises(IncompleteFillLedgerError, match="cost basis"):
        await store.fill_ledger_snapshot("account", datetime.now(UTC))


async def test_supabase_footprint_read_is_complete_and_returns_token_ids():
    tables = {
        "order_intents": [
            {"id": str(n), "account_id": "account", "token_id": f"token-{n}"}
            for n in range(1001)
        ],
        "orders": [{"id": "o", "account_id": "account", "outcome_token_id": "order-token"}],
        "fills": [{"id": "f", "account_id": "account", "outcome_token_id": "fill-token"}],
    }

    tokens = await store_for(tables).durable_token_ids("account")

    assert len(tokens) == 1003
    assert {"token-1000", "order-token", "fill-token"} <= tokens


async def test_supabase_missing_market_mapping_prevents_foreign_classification():
    tables = {
        "account_activities": [legacy_row()],
        "order_intents": [{"id": "i", "account_id": "account", "market_id": "unknown"}],
    }
    with pytest.raises(IncompleteFillLedgerError, match="market definition"):
        await store_for(tables).reconcile_activity_scope(
            "account", baseline=None, quarantine_enabled=True
        )
    assert tables["account_activities"][0]["ledger_scope"] == "bot"


async def test_supabase_control_race_order_never_cancels_on_absence_only():
    row = {
        "id": "o", "clob_order_id": "order", "account_id": "account", "status": "unknown",
        "response_payload": {"reconciliation_required": True},
    }
    store = store_for({"orders": [row]})

    for _ in range(3):
        await store.confirm_orders_absent({"order"}, "account")

    assert row["status"] == "unknown"
