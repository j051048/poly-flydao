from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from polybot.stores.supabase_store import SupabaseStore


class Query:
    def __init__(self, client: FakeClient, source: str) -> None:
        self.client = client
        self.source = source
        self.column = ""

    def select(self, column: str):
        self.column = column
        return self

    def eq(self, field: str, value: Any):
        return self

    def limit(self, value: int):
        return self

    def execute(self):
        self.client.executions.append((self.source, self.column))
        return SimpleNamespace(data=[])


class Admin:
    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.calls = 0

    def get_user_by_id(self, account_id: str):
        self.calls += 1
        return SimpleNamespace(user=SimpleNamespace(id=self.account_id))


class FakeClient:
    def __init__(self, account_id: str) -> None:
        self.executions: list[tuple[str, str]] = []
        self.admin = Admin(account_id)
        self.auth = SimpleNamespace(admin=self.admin)

    def table(self, name: str) -> Query:
        return Query(self, name)

    def rpc(self, name: str, params: dict[str, Any]) -> Query:
        assert params["p_expected_version"] == 0
        return Query(self, f"rpc:{name}")


async def test_supabase_health_preflights_account_migrations_and_expiry_rpc_once() -> None:
    account_id = "11111111-1111-1111-1111-111111111111"
    client = FakeClient(account_id)
    store = SupabaseStore(
        "https://example.supabase.co",
        "service-role",
        account_id=account_id,
        client=client,  # type: ignore[arg-type]
    )

    assert await store.health()
    assert await store.health()
    assert client.admin.calls == 1
    assert ("account_risk_state", "risk_day") in client.executions
    assert ("account_activities", "activity_key") in client.executions
    assert ("orders", "open_snapshot_miss_count,expires_at") in client.executions
    assert ("rpc:expire_runtime_control", "") in client.executions


async def test_supabase_health_rejects_a_different_auth_user() -> None:
    account_id = "22222222-2222-2222-2222-222222222222"
    client = FakeClient("33333333-3333-3333-3333-333333333333")
    store = SupabaseStore(
        "https://example.supabase.co",
        "service-role",
        account_id=account_id,
        client=client,  # type: ignore[arg-type]
    )

    assert not await store.health()
