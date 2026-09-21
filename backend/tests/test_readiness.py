from __future__ import annotations

from polybot.readiness import (
    GATE_LEASE,
    GATE_RECONCILIATION,
    GATE_RUNTIME_CONTROL,
    GATE_STORE,
    ReadinessBlocker,
    ReadinessRegistry,
)


def test_registry_starts_fail_closed_with_every_gate_visible() -> None:
    registry = ReadinessRegistry()
    snapshot = registry.snapshot()
    assert snapshot["ready"] is False
    assert snapshot["ok"] is False
    assert set(snapshot["gates"]) == {
        GATE_STORE,
        GATE_LEASE,
        GATE_RUNTIME_CONTROL,
        GATE_RECONCILIATION,
    }
    assert all(value is False for value in snapshot["gates"].values())
    assert snapshot["blockers"] == []
    assert snapshot["not_ready_seconds"] is not None


def test_registry_reports_blockers_in_gate_order() -> None:
    registry = ReadinessRegistry()
    registry.set_gate(
        GATE_RECONCILIATION,
        False,
        (
            ReadinessBlocker(
                code="unmapped_account_trade",
                gate=GATE_RECONCILIATION,
                message="账户里存在无法映射到机器人订单的成交。",
                fix="重置对账基准。",
            ),
        ),
    )
    registry.set_gate(
        GATE_STORE,
        False,
        (
            ReadinessBlocker(
                code="store_unhealthy",
                gate=GATE_STORE,
                message="状态存储不可用。",
                fix="检查 Supabase。",
            ),
        ),
    )
    blockers = registry.snapshot()["blockers"]
    assert [item["code"] for item in blockers] == ["store_unhealthy", "unmapped_account_trade"]
    assert blockers[0]["fix"] == "检查 Supabase。"


def test_registry_becomes_ready_only_when_every_gate_passes() -> None:
    registry = ReadinessRegistry()
    for gate in (GATE_STORE, GATE_LEASE, GATE_RUNTIME_CONTROL):
        registry.set_gate(gate, True)
    assert registry.ready is False
    assert registry.snapshot()["not_ready_seconds"] is not None
    registry.set_gate(GATE_RECONCILIATION, True)
    assert registry.ready is True
    assert registry.snapshot()["not_ready_seconds"] is None


def test_registry_drops_attribution_when_a_gate_recovers() -> None:
    registry = ReadinessRegistry()
    registry.set_gate(GATE_STORE, False, ())
    registry.set_gate(GATE_STORE, True)
    assert registry.snapshot()["blockers"] == []


def test_registry_warnings_are_degraded_but_not_blocking() -> None:
    registry = ReadinessRegistry()
    registry.set_warning("reconciliation_quarantine", "2 笔成交处于隔离区")
    snapshot = registry.snapshot()
    assert snapshot["warnings"] == [
        {"code": "reconciliation_quarantine", "message": "2 笔成交处于隔离区"}
    ]
    registry.clear_warning("reconciliation_quarantine")
    assert registry.snapshot()["warnings"] == []


def test_registry_reset_gates_fails_closed_without_losing_role() -> None:
    registry = ReadinessRegistry()
    registry.reset(role="worker", component="all", mode="canary")
    registry.set_ready(True)
    assert registry.ready is True
    registry.reset_gates()
    snapshot = registry.snapshot()
    assert registry.ready is False
    assert snapshot["role"] == "worker"
    assert snapshot["mode"] == "canary"


def test_registry_set_mode_follows_a_hot_switch() -> None:
    registry = ReadinessRegistry()
    registry.reset(role="worker", component="all", mode="paper")
    registry.set_mode("canary")
    assert registry.snapshot()["mode"] == "canary"


def test_registry_rejects_unknown_gates() -> None:
    registry = ReadinessRegistry()
    try:
        registry.set_gate("made-up", True)
    except ValueError as exc:
        assert "made-up" in str(exc)
    else:  # pragma: no cover - the guard must exist
        raise AssertionError("unknown gates must be rejected")
