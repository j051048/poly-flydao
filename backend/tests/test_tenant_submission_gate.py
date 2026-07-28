from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from polybot.config import TradingMode
from polybot.stores.supabase_store import (
    SupabaseStore,
    TenantExecutionFence,
)


class _Rpc:
    def __init__(self, client: _Client) -> None:
        self.client = client

    def execute(self) -> SimpleNamespace:
        return SimpleNamespace(data=self.client.result)


class _Client:
    def __init__(self, result: Any = True) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def rpc(self, name: str, params: dict[str, Any]) -> _Rpc:
        self.calls.append((name, params))
        return _Rpc(self)


def _fence() -> TenantExecutionFence:
    return TenantExecutionFence(
        job_id="11111111-1111-1111-1111-111111111111",
        claimed_by="worker-12345678",
        job_fencing_token=9,
        profile_version=4,
        risk_policy_id="22222222-2222-2222-2222-222222222222",
        risk_policy_version=3,
        mode=TradingMode.LIVE,
    )


async def test_bound_store_uses_atomic_tenant_submission_rpc() -> None:
    account_id = "33333333-3333-3333-3333-333333333333"
    client = _Client()
    store = SupabaseStore(
        "https://example.supabase.co",
        "service-role",
        account_id=account_id,
        client=client,  # type: ignore[arg-type]
    )
    store.bind_tenant_execution_fence(_fence())

    await store.mark_order_submitting(
        "intent-hash",
        account_id,
        12,
        control_version=8,
    )

    name, params = client.calls[0]
    assert name == "mark_tenant_order_submitting"
    assert params["p_account_id"] == account_id
    assert params["p_control_version"] == 8
    assert params["p_job_fencing_token"] == 9
    assert params["p_profile_version"] == 4
    assert params["p_risk_policy_version"] == 3


async def test_tenant_submission_rejects_missing_control_or_false_scalar() -> None:
    account_id = "33333333-3333-3333-3333-333333333333"
    client = _Client([False])
    store = SupabaseStore(
        "https://example.supabase.co",
        "service-role",
        account_id=account_id,
        client=client,  # type: ignore[arg-type]
    )
    store.bind_tenant_execution_fence(_fence())

    with pytest.raises(RuntimeError, match="runtime-control version"):
        await store.mark_order_submitting("intent-hash", account_id, 12)
    with pytest.raises(RuntimeError, match="tenant job"):
        await store.mark_order_submitting(
            "intent-hash",
            account_id,
            12,
            control_version=8,
        )


def test_tenant_execution_fence_is_one_shot_and_live_only() -> None:
    account_id = "33333333-3333-3333-3333-333333333333"
    store = SupabaseStore(
        "https://example.supabase.co",
        "service-role",
        account_id=account_id,
        client=_Client(),  # type: ignore[arg-type]
    )
    store.bind_tenant_execution_fence(_fence())
    with pytest.raises(RuntimeError, match="already bound"):
        store.bind_tenant_execution_fence(_fence())
    with pytest.raises(ValueError, match="live modes"):
        TenantExecutionFence(
            job_id="job",
            claimed_by="worker",
            job_fencing_token=1,
            profile_version=1,
            risk_policy_id="risk",
            risk_policy_version=1,
            mode=TradingMode.PAPER,
        )
