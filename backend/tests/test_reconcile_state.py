from decimal import Decimal
from typing import Any

import pytest

from polybot.models import ExecutionResult, ExecutionStatus, Side, UserTradeUpdate, utc_now
from polybot.stores.ledger import IncompleteFillLedgerError
from polybot.stores.supabase_store import (
    SupabaseStore,
    _aggregate_order_fill_payload,
    _merge_execution_order_payload,
    _merge_fill_payload,
)


def test_duplicate_execution_save_cannot_regress_confirmed_order() -> None:
    existing = {
        "status": "confirmed",
        "clob_order_id": "order-1",
        "original_size": "3",
        "filled_size": "3",
        "remaining_size": "0",
        "average_fill_price": "0.4",
        "attempt_count": 1,
        "response_payload": {"trade_ids": ["trade-1"]},
        "submitted_at": "2026-07-01T00:00:00+00:00",
    }
    duplicate_accepted = {
        "status": "submitted",
        "clob_order_id": "order-1",
        "original_size": "3",
        "filled_size": "0",
        "remaining_size": "3",
        "average_fill_price": None,
        "attempt_count": 1,
        "response_payload": {"message": "accepted"},
        "submitted_at": "2026-07-01T00:00:00+00:00",
        "last_error_detail": None,
    }

    merged = _merge_execution_order_payload(existing, duplicate_accepted)

    assert merged["status"] == "confirmed"
    assert Decimal(str(merged["filled_size"])) == Decimal("3")
    assert Decimal(str(merged["remaining_size"])) == Decimal("0")
    assert merged["response_payload"]["trade_ids"] == ["trade-1"]
    assert merged["response_payload"]["message"] == "accepted"


def test_stale_fill_event_cannot_regress_terminal_settlement() -> None:
    existing = {
        "settlement_status": "CONFIRMED",
        "transaction_hash": "0xtx",
        "mined_at": "2026-07-01T00:00:01+00:00",
        "confirmed_at": "2026-07-01T00:00:02+00:00",
    }
    stale = {
        "settlement_status": "MATCHED",
        "transaction_hash": None,
        "mined_at": None,
        "confirmed_at": None,
    }

    merged = _merge_fill_payload(existing, stale)

    assert merged["settlement_status"] == "CONFIRMED"
    assert merged["transaction_hash"] == "0xtx"
    assert merged["confirmed_at"] == "2026-07-01T00:00:02+00:00"


def test_order_status_is_aggregated_across_all_fills() -> None:
    order = {"status": "live", "original_size": "10"}
    partial = _aggregate_order_fill_payload(
        order,
        [
            {
                "size": "3",
                "price": "0.4",
                "fee_pusd": "0.01",
                "settlement_status": "CONFIRMED",
            }
        ],
    )
    assert partial["status"] == "partially_filled"
    assert Decimal(partial["filled_size"]) == Decimal("3")
    assert Decimal(partial["remaining_size"]) == Decimal("7")

    complete = _aggregate_order_fill_payload(
        order,
        [
            {
                "size": "3",
                "price": "0.4",
                "fee_pusd": "0.01",
                "settlement_status": "CONFIRMED",
            },
            {
                "size": "7",
                "price": "0.5",
                "fee_pusd": "0.02",
                "settlement_status": "CONFIRMED",
            },
        ],
    )
    assert complete["status"] == "confirmed"
    assert Decimal(complete["filled_size"]) == Decimal("10")
    assert Decimal(complete["remaining_size"]) == Decimal("0")
    assert Decimal(complete["average_fill_price"]) == Decimal("0.47")


def test_fill_aggregation_fails_closed_when_fills_exceed_original_size() -> None:
    with pytest.raises(IncompleteFillLedgerError, match="exceed"):
        _aggregate_order_fill_payload(
            {"status": "live", "original_size": "3"},
            [
                {
                    "size": "4",
                    "price": "0.4",
                    "fee_pusd": "0",
                    "settlement_status": "CONFIRMED",
                }
            ],
        )


class FakeResponse:
    def __init__(self, data: list[dict[str, Any]]):
        self.data = data


class FakeQuery:
    def __init__(self, tables: dict[str, list[dict[str, Any]]], table: str):
        self.tables = tables
        self.table = table
        self.operation = "select"
        self.payload: dict[str, Any] | None = None
        self.conflict: str | None = None
        self.filters: list[tuple[str, str, Any]] = []
        self.row_limit: int | None = None
        self.selected_columns = "*"

    def select(self, columns: str):
        self.selected_columns = columns
        return self

    def update(self, payload: dict[str, Any]):
        self.operation = "update"
        self.payload = payload
        return self

    def upsert(self, payload: dict[str, Any], *, on_conflict: str):
        self.operation = "upsert"
        self.payload = payload
        self.conflict = on_conflict
        return self

    def eq(self, field: str, value: Any):
        self.filters.append(("eq", field, value))
        return self

    def in_(self, field: str, values: list[Any]):
        self.filters.append(("in", field, values))
        return self

    def limit(self, value: int):
        self.row_limit = value
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        for kind, field, expected in self.filters:
            if kind == "eq" and row.get(field) != expected:
                return False
            if kind == "in" and row.get(field) not in expected:
                return False
        return True

    def execute(self):
        rows = self.tables.setdefault(self.table, [])
        if self.operation == "select":
            selected = [dict(row) for row in rows if self._matches(row)]
            if self.row_limit is not None:
                selected = selected[: self.row_limit]
            if self.selected_columns != "*":
                columns = [column.strip() for column in self.selected_columns.split(",")]
                selected = [
                    {column: row[column] for column in columns if column in row} for row in selected
                ]
            return FakeResponse(selected)
        if self.operation == "update":
            updated = []
            for row in rows:
                if self._matches(row):
                    row.update(self.payload or {})
                    updated.append(dict(row))
            return FakeResponse(updated)
        assert self.operation == "upsert"
        assert self.payload is not None
        existing = next(
            (
                row
                for row in rows
                if self.conflict is not None
                and row.get(self.conflict) == self.payload.get(self.conflict)
            ),
            None,
        )
        if existing is None:
            rows.append(dict(self.payload))
        else:
            existing.update(self.payload)
        return FakeResponse([dict(self.payload)])


class FakeSupabaseClient:
    def __init__(self, tables: dict[str, list[dict[str, Any]]]):
        self.tables = tables

    def table(self, name: str):
        return FakeQuery(self.tables, name)


async def test_save_execution_never_downgrades_a_persisted_gtd_order() -> None:
    expires_at = "2026-07-01T00:05:00+00:00"
    tables = {
        "order_intents": [
            {
                "id": "intent-1",
                "account_id": "account",
                "intent_hash": "intent-hash",
                "mode": "canary",
                "token_id": "yes-token",
                "side": "BUY",
                "price": "0.4",
                "size": "2",
                "status": "signed",
            }
        ],
        "orders": [
            {
                "id": "db-order",
                "account_id": "account",
                "order_intent_id": "intent-1",
                "client_order_id": "intent-hash",
                "clob_order_id": None,
                "status": "submitting",
                "original_size": "2",
                "filled_size": "0",
                "remaining_size": "2",
                "average_fill_price": None,
                "attempt_count": 1,
                "last_error_detail": None,
                "response_payload": {"signed_order_hash": "signed-hash"},
                "submitted_at": None,
                "order_type": "GTD",
                "expires_at": expires_at,
            }
        ],
    }
    store = SupabaseStore(
        "https://example.supabase.co",
        "service-role",
        account_id="account",
        client=FakeSupabaseClient(tables),
    )

    await store.save_execution(
        ExecutionResult(
            intent_hash="intent-hash",
            status=ExecutionStatus.ERROR,
            message="ambiguous post result",
            raw={},
        ),
        "account",
    )

    assert tables["orders"][0]["order_type"] == "GTD"
    assert tables["orders"][0]["expires_at"] == expires_at


async def test_supabase_reconcile_trade_keeps_partial_and_terminal_states_monotonic() -> None:
    tables = {
        "orders": [
            {
                "id": "db-order",
                "order_intent_id": "intent-1",
                "account_id": "account",
                "clob_order_id": "order-1",
                "status": "live",
                "original_size": "10",
                "filled_size": "0",
                "outcome_token_id": "yes-token",
                "side": "BUY",
                "matched_at": None,
                "mined_at": None,
                "confirmed_at": None,
            }
        ],
        "markets": [{"id": "db-market", "condition_id": "condition-1"}],
        "fills": [],
    }
    store = SupabaseStore(
        "https://example.supabase.co",
        "service-role",
        account_id="account",
        client=FakeSupabaseClient(tables),
    )
    now = utc_now()

    def trade(trade_id: str, size: str, status: str) -> UserTradeUpdate:
        return UserTradeUpdate(
            clob_trade_id=trade_id,
            candidate_order_ids=["order-1"],
            condition_id="condition-1",
            token_id="yes-token",
            side=Side.BUY,
            trader_side="TAKER",
            price=Decimal("0.4"),
            size=Decimal(size),
            status=status,
            transaction_hash="0xtx",
            matched_at=now,
            updated_at=now,
        )

    await store.reconcile_trade(trade("trade-1", "3", "CONFIRMED"), "account")
    assert tables["orders"][0]["status"] == "partially_filled"
    assert Decimal(tables["orders"][0]["filled_size"]) == Decimal("3")

    await store.reconcile_trade(trade("trade-1", "3", "MATCHED"), "account")
    assert tables["fills"][0]["settlement_status"] == "CONFIRMED"
    assert tables["orders"][0]["status"] == "partially_filled"

    await store.reconcile_trade(trade("trade-2", "7", "CONFIRMED"), "account")
    assert tables["orders"][0]["status"] == "confirmed"
    assert Decimal(tables["orders"][0]["filled_size"]) == Decimal("10")
    assert Decimal(tables["orders"][0]["remaining_size"]) == Decimal("0")


async def test_supabase_pending_trade_ids_include_accept_response_and_nonterminal_fills() -> None:
    tables = {
        "orders": [
            {
                "account_id": "account",
                "status": "submitted",
                "response_payload": {"trade_ids": ["accepted-trade"]},
            }
        ],
        "fills": [
            {
                "account_id": "account",
                "clob_trade_id": "mined-trade",
                "settlement_status": "MINED",
            },
            {
                "account_id": "account",
                "clob_trade_id": "terminal-trade",
                "settlement_status": "CONFIRMED",
            },
        ],
    }
    store = SupabaseStore(
        "https://example.supabase.co",
        "service-role",
        account_id="account",
        client=FakeSupabaseClient(tables),
    )

    assert await store.pending_trade_ids("account") == {
        "accepted-trade",
        "mined-trade",
    }

    tables["fills"].append(
        {
            "account_id": "account",
            "clob_trade_id": "accepted-trade",
            "settlement_status": "FAILED",
        }
    )
    assert await store.pending_trade_ids("account") == {"mined-trade"}
