from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from polybot.config import Settings
from polybot.models import (
    AIUsageRecord,
    BookLevel,
    EquityHistoryPoint,
    Forecast,
    OrderBookSnapshot,
    utc_now,
)
from polybot.retention import (
    MIN_AI_USAGE_DAYS,
    MIN_EQUITY_DAYS,
    MIN_SNAPSHOT_DAYS,
    RetentionPolicy,
    prune_history,
)
from polybot.stores.memory import MemoryStore
from polybot.stores.supabase_store import SupabaseStore

ACCOUNT = "11111111-1111-4111-8111-111111111111"


class _Query:
    def __init__(self, client: _FakeClient, source: str) -> None:
        self.client = client
        self.source = source

    def execute(self) -> Any:
        return SimpleNamespace(data=self.client.responses.get(self.source))


class _FakeClient:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def rpc(self, name: str, params: dict[str, Any]) -> _Query:
        self.calls.append((name, params))
        return _Query(self, f"rpc:{name}")

    def table(self, name: str) -> _Query:
        return _Query(self, name)


def _usage(*, age_days: int) -> AIUsageRecord:
    return AIUsageRecord(
        provider="openai",
        model="gpt-4o-mini",
        total_tokens=10,
        created_at=utc_now() - timedelta(days=age_days),
    )


def _equity_point(*, age_days: int) -> EquityHistoryPoint:
    return EquityHistoryPoint(
        recorded_at=utc_now() - timedelta(days=age_days),
        equity_usd=Decimal("100"),
        source="paper_cycle",
    )


def _snapshot(*, age_days: int) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id="token",
        market_id="market",
        bids=[BookLevel(price=Decimal("0.4"), size=Decimal("10"))],
        asks=[BookLevel(price=Decimal("0.6"), size=Decimal("10"))],
        captured_at=utc_now() - timedelta(days=age_days),
    )


def test_policy_enforces_hard_minimums() -> None:
    for kwargs in (
        {"ai_usage_days": MIN_AI_USAGE_DAYS - 1},
        {"equity_history_days": MIN_EQUITY_DAYS - 1},
        {"snapshot_days": MIN_SNAPSHOT_DAYS - 1},
        {"batch_limit": 99},
        {"interval_seconds": 3_599},
    ):
        with pytest.raises(ValueError):
            RetentionPolicy(**kwargs)  # type: ignore[arg-type]


def test_policy_defaults_are_generous_enough_to_be_safe() -> None:
    policy = RetentionPolicy()
    assert policy.enabled is True
    assert policy.ai_usage_days >= MIN_AI_USAGE_DAYS
    assert policy.equity_history_days >= MIN_EQUITY_DAYS
    assert policy.snapshot_days >= MIN_SNAPSHOT_DAYS
    # The database function takes its own argument names.
    assert policy.prune_kwargs() == {
        "ai_usage_days": policy.ai_usage_days,
        "equity_days": policy.equity_history_days,
        "snapshot_days": policy.snapshot_days,
        "batch_limit": policy.batch_limit,
    }


def test_policy_reads_settings() -> None:
    settings = Settings(
        _env_file=None,
        personal_mode=True,
        account_id=ACCOUNT,
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
        ai_api_key="test-key",
        retention_enabled=False,
        retention_ai_usage_days=30,
        retention_equity_history_days=60,
        retention_snapshot_days=7,
        retention_interval_seconds=7_200,
    )
    policy = RetentionPolicy.from_settings(settings)
    assert policy.enabled is False
    assert policy.ai_usage_days == 30
    assert policy.equity_history_days == 60
    assert policy.snapshot_days == 7
    assert policy.interval_seconds == 7_200


async def test_disabled_policy_never_touches_the_store() -> None:
    class _Store:
        calls = 0

        async def prune_history(self, **kwargs: Any) -> dict[str, int]:
            _Store.calls += 1
            return {}

    assert await prune_history(_Store(), RetentionPolicy(enabled=False)) == {}
    assert _Store.calls == 0


async def test_memory_store_prunes_only_expired_history(forecast: Forecast) -> None:
    store = MemoryStore()
    store.ai_usage[ACCOUNT] = [_usage(age_days=400), _usage(age_days=1)]
    store.equity_history[ACCOUNT] = [_equity_point(age_days=500), _equity_point(age_days=2)]
    store.snapshots = [_snapshot(age_days=90), _snapshot(age_days=0)]
    await store.save_forecast(forecast, ACCOUNT)

    removed = await store.prune_history(
        ai_usage_days=90,
        equity_days=365,
        snapshot_days=30,
    )

    assert removed == {"ai_usage_ledger": 1, "equity_history": 1, "snapshots": 1}
    assert [record.total_tokens for record in store.ai_usage[ACCOUNT]] == [10]
    assert [point.source for point in store.equity_history[ACCOUNT]] == ["paper_cycle"]
    assert len(store.snapshots) == 1
    # Forecasts are audit evidence for real decisions and must survive.
    assert store.forecasts


async def test_memory_store_respects_the_batch_limit() -> None:
    store = MemoryStore()
    store.ai_usage[ACCOUNT] = [_usage(age_days=400) for _ in range(5)]

    removed = await store.prune_history(
        ai_usage_days=90,
        equity_days=365,
        snapshot_days=30,
        batch_limit=100,
    )

    assert removed["ai_usage_ledger"] == 5
    assert store.ai_usage[ACCOUNT] == []


async def test_memory_store_rejects_windows_below_the_minimum() -> None:
    store = MemoryStore()
    with pytest.raises(ValueError):
        await store.prune_history(ai_usage_days=1, equity_days=365, snapshot_days=30)


async def test_supabase_store_calls_the_bounded_retention_rpc() -> None:
    client = _FakeClient(
        {
            "rpc:prune_polybot_history": {
                "ai_usage_ledger": 3,
                "equity_history": 1,
                "snapshots": 7,
            }
        }
    )
    store = SupabaseStore(
        "https://example.supabase.co",
        "service-role",
        account_id=ACCOUNT,
        client=client,  # type: ignore[arg-type]
    )

    removed = await store.prune_history(
        ai_usage_days=90,
        equity_days=365,
        snapshot_days=30,
        batch_limit=5_000,
    )

    assert removed == {"ai_usage_ledger": 3, "equity_history": 1, "snapshots": 7}
    name, params = client.calls[0]
    assert name == "prune_polybot_history"
    assert params == {
        "p_ai_usage_days": 90,
        "p_equity_days": 365,
        "p_snapshot_days": 30,
        "p_batch_limit": 5_000,
    }


async def test_supabase_store_fails_closed_on_bad_arguments_and_empty_results() -> None:
    store = SupabaseStore(
        "https://example.supabase.co",
        "service-role",
        account_id=ACCOUNT,
        client=_FakeClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError):
        await store.prune_history(ai_usage_days=1, equity_days=365, snapshot_days=30)
    with pytest.raises(ValueError):
        await store.prune_history(
            ai_usage_days=90,
            equity_days=365,
            snapshot_days=30,
            batch_limit=10,
        )
    assert await store.prune_history(
        ai_usage_days=90,
        equity_days=365,
        snapshot_days=30,
    ) == {"ai_usage_ledger": 0, "equity_history": 0, "snapshots": 0}
