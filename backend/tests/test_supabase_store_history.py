from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from polybot.jobs import SupabaseJobRepository
from polybot.models import AIUsageRecord, utc_now
from polybot.stores.supabase_store import SupabaseStore

ACCOUNT = "11111111-1111-4111-8111-111111111111"


class Query:
    def __init__(self, client: FakeClient, source: str) -> None:
        self.client = client
        self.source = source
        self.payload = None
        self.column = ""
        self.limit_value = None
        self.order_column = None
        self.order_desc = False

    def select(self, column: str):
        self.column = column
        return self

    def eq(self, field: str, value):
        self.client.last_eq = (field, value)
        return self

    def order(self, column: str, desc: bool = False):
        self.order_column = column
        self.order_desc = desc
        return self

    def limit(self, value: int):
        self.limit_value = value
        return self

    def insert(self, payload: dict[str, Any]):
        self.payload = payload
        return self

    def execute(self):
        self.client.executions.append(
            (
                self.source,
                self.column,
                self.payload,
                self.order_column,
                self.order_desc,
                self.limit_value,
            )
        )
        return SimpleNamespace(data=self.client.rows.get(self.source, []))


class FakeClient:
    def __init__(self, rows: dict[str, list[dict[str, Any]]] | None = None):
        self.rows = rows or {}
        self.executions: list[tuple] = []
        self.last_eq = None

    def table(self, name: str) -> Query:
        return Query(self, name)

    def rpc(self, name: str, params: dict) -> Query:
        return Query(self, f"rpc:{name}")


def _equity_row() -> dict[str, Any]:
    return {
        "recorded_at": "2026-08-05T00:00:00+00:00",
        "equity_usd": "123.45",
        "source": "paper_cycle",
    }


def _usage_row() -> dict[str, Any]:
    return {
        "market_id": "m1",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "request_id": "req-1",
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
        "latency_ms": 42,
        "cost_usd": "0.00045",
        "created_at": "2026-08-05T00:00:00+00:00",
    }


async def test_store_records_and_lists_equity_history() -> None:
    client = FakeClient({"equity_history": [_equity_row(), _equity_row()]})
    store = SupabaseStore(
        "https://example.supabase.co",
        "service-role",
        account_id=ACCOUNT,
        client=client,  # type: ignore[arg-type]
    )

    await store.record_equity_history(ACCOUNT, Decimal("123.45"), source="paper_cycle")
    points = await store.list_equity_history(ACCOUNT, limit=10)

    insert = client.executions[0]
    assert insert[0] == "equity_history"
    assert insert[2]["account_id"] == ACCOUNT
    assert insert[2]["equity_usd"] == "123.45"
    assert insert[2]["source"] == "paper_cycle"
    assert len(points) == 2
    assert points[0].equity_usd == Decimal("123.45")
    assert points[0].source == "paper_cycle"


async def test_store_records_and_lists_ai_usage() -> None:
    client = FakeClient({"ai_usage_ledger": [_usage_row(), _usage_row()]})
    store = SupabaseStore(
        "https://example.supabase.co",
        "service-role",
        account_id=ACCOUNT,
        client=client,  # type: ignore[arg-type]
    )
    usage = AIUsageRecord(
        provider="openai",
        model="gpt-4o-mini",
        market_id="m1",
        input_tokens=10,
        output_tokens=20,
        total_tokens=30,
        latency_ms=42,
        cost_usd=Decimal("0.00045"),
        created_at=utc_now(),
    )

    await store.record_ai_usage(ACCOUNT, usage)
    records = await store.list_ai_usage(ACCOUNT, limit=10)

    insert = client.executions[0]
    assert insert[0] == "ai_usage_ledger"
    assert insert[2]["provider"] == "openai"
    assert insert[2]["total_tokens"] == 30
    assert len(records) == 2
    assert records[0].model == "gpt-4o-mini"
    assert records[0].cost_usd == Decimal("0.00045")


async def test_job_repository_lists_history_and_usage() -> None:
    client = FakeClient(
        {
            "equity_history": [_equity_row()],
            "ai_usage_ledger": [_usage_row()],
        }
    )
    repo = SupabaseJobRepository(client)  # type: ignore[arg-type]

    points = await repo.list_equity_history(ACCOUNT, limit=10)
    records = await repo.list_ai_usage(ACCOUNT, limit=10)

    assert len(points) == 1
    assert points[0].source == "paper_cycle"
    assert len(records) == 1
    assert records[0].total_tokens == 30
