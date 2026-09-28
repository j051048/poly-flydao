from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import polybot.api as api_module
from polybot.api import create_app
from polybot.api_models import PersonalCycleCapacityExceeded, PersonalCycleRequestRegistry
from polybot.auth import AuthPrincipal, StaticTokenVerifier
from polybot.config import Settings, TradingMode
from polybot.credentials import InMemoryCredentialRepository
from polybot.jobs import InMemoryJobRepository

ACCOUNT = "11111111-1111-4111-8111-111111111111"
HEADERS = {"Authorization": "Bearer owner"}


async def _accept(registry, key="request-one", mode=TradingMode.PAPER):
    return await registry.accept(account_id=ACCOUNT, idempotency_key=key, mode=mode)


async def test_full_registry_keeps_active_requests_and_reuses_only_terminal_slots():
    registry = PersonalCycleRequestRegistry(max_entries=2)
    first, _ = await _accept(registry)
    registry.observe_cycle({"id": "cycle-one", "mode": "paper", "state": "running"})
    second, _ = await _accept(registry, "request-two")
    with pytest.raises(PersonalCycleCapacityExceeded):
        await _accept(registry, "request-three")
    duplicate, is_new = await _accept(registry)
    assert not is_new
    assert duplicate.request_id == first.request_id
    assert registry.get(ACCOUNT, first.request_id)["state"] == "running"
    assert registry.get(ACCOUNT, second.request_id)["state"] == "queued"

    registry.observe_cycle({"id": "cycle-one", "mode": "paper", "state": "succeeded"})
    third, _ = await _accept(registry, "request-three")
    assert registry.get(ACCOUNT, second.request_id)["state"] == "queued"
    assert registry.get(ACCOUNT, third.request_id)["state"] == "queued"
    assert registry.get(ACCOUNT, first.request_id) is None
    assert len(registry._entries) == 2


@pytest.mark.parametrize("running", [False, True])
async def test_timeout_is_terminal_and_idempotent_without_claiming_running_cycle_stopped(running):
    registry = PersonalCycleRequestRegistry(queue_timeout_seconds=1, run_timeout_seconds=1)
    entry, _ = await _accept(registry)
    if running:
        registry.observe_cycle({"id": "slow", "mode": "paper", "state": "running"})
        entry.started_tick -= 2
    else:
        entry.created_tick -= 2
    result = registry.get(ACCOUNT, entry.request_id)
    assert result["state"] == "failed"
    assert result["reason"] == ("result_timeout" if running else "queue_timeout")
    assert result["outcome_unknown"] is running
    assert result["completed_at"]
    duplicate, is_new = await _accept(registry)
    assert not is_new
    assert duplicate.request_id == entry.request_id
    registry.observe_cycle({"id": "slow", "mode": "paper", "state": "running"})
    assert registry.get(ACCOUNT, entry.request_id)["state"] == "failed"


async def test_mode_change_supersedes_queued_requests_without_touching_running_requests():
    registry = PersonalCycleRequestRegistry()
    running, _ = await _accept(registry)
    registry.observe_cycle({"id": "running", "mode": "paper", "state": "running"})
    queued, _ = await _accept(registry, "queued")
    registry.supersede_queued("different-owner", TradingMode.SHADOW)
    assert registry.get(ACCOUNT, queued.request_id)["state"] == "queued"
    registry.supersede_queued(ACCOUNT, TradingMode.SHADOW)
    assert registry.get(ACCOUNT, queued.request_id)["reason"] == "mode_superseded"
    assert registry.get(ACCOUNT, running.request_id)["state"] == "running"
    registry.observe_cycle({"id": "running", "mode": "paper", "state": "succeeded"})
    assert registry.get(ACCOUNT, running.request_id)["state"] == "succeeded"


async def test_worker_observed_mode_change_supersedes_waiting_old_mode():
    registry = PersonalCycleRequestRegistry()
    entry, _ = await _accept(registry)
    registry.observe_cycle({"id": "new-mode", "mode": "shadow", "state": "running"})
    assert registry.get(ACCOUNT, entry.request_id)["reason"] == "mode_superseded"


def _app(monkeypatch, worker):
    async def idle_resolution(self, stop):
        await stop.wait()

    monkeypatch.setattr(api_module, "run_worker", worker)
    monkeypatch.setattr(api_module, "is_worker_ready", lambda: True)
    monkeypatch.setattr(api_module.MarketResolutionWorker, "serve", idle_resolution)
    return create_app(
        settings=Settings(
            _env_file=None, personal_mode=True, archive_enabled=False, component="all",
            account_id=ACCOUNT, supabase_url="https://example.supabase.co",
            supabase_service_role_key="test-service-role",
        ),
        jobs=InMemoryJobRepository(), credentials=InMemoryCredentialRepository(),
        personal_execution=SimpleNamespace(),
        auth_verifier=StaticTokenVerifier({"owner": AuthPrincipal(
            account_id=ACCOUNT, aal="aal2", role="authenticated",
            issuer="https://example.test", audience=("authenticated",),
        )}),
    )


def _submit(client, key):
    return client.post(
        "/v1/personal/cycles/run", json={"mode": "paper"},
        headers={**HEADERS, "Idempotency-Key": key},
    )


def test_api_capacity_error_does_not_make_accepted_request_disappear(monkeypatch):
    async def idle_worker(**kwargs):
        await kwargs["stop_event"].wait()

    app = _app(monkeypatch, idle_worker)
    with TestClient(app) as client:
        app.state.personal_cycle_requests = PersonalCycleRequestRegistry(max_entries=1)
        first = _submit(client, "personal-request-first")
        assert first.status_code == 202
        full = _submit(client, "personal-request-overflow")
        assert full.status_code == 429
        assert full.headers["retry-after"] == "30"
        assert "pending personal cycle" in full.json()["detail"]
        duplicate = _submit(client, "personal-request-first")
        assert duplicate.status_code == 202
        assert duplicate.json()["id"] == first.json()["id"]
        status = client.get(f"/v1/personal/cycles/{first.json()['id']}", headers=HEADERS)
        assert status.status_code == 200
        assert status.json()["state"] == "queued"


def test_saved_mode_change_ends_queued_request_before_another_cycle_starts(monkeypatch):
    async def idle_worker(**kwargs):
        await kwargs["stop_event"].wait()

    app = _app(monkeypatch, idle_worker)
    with TestClient(app) as client:
        accepted = _submit(client, "personal-request-old-mode")
        assert accepted.status_code == 202
        profile = app.state.job_repository._profiles[ACCOUNT]
        changed = client.put("/v1/me/runtime-profile", headers=HEADERS, json={
            "expected_version": profile.version,
            "ai_provider": profile.ai_provider.value,
            "forecast_model": profile.forecast_model,
            "desired_mode": "shadow",
        })
        assert changed.status_code == 200
        response = client.get(f"/v1/personal/cycles/{accepted.json()['id']}", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["state"] == "failed"
        assert response.json()["reason"] == "mode_superseded"
        assert response.json()["cycle_id"] is None


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("exit_kind", ["error", "cancelled", "completed"])
def test_worker_exit_terminates_queued_and_running_request_tracking(
    monkeypatch, running, exit_kind,
):
    async def exiting_worker(**kwargs):
        await kwargs["cycle_trigger"].wait()
        if running:
            kwargs["cycle_observer"]({
                "id": "interrupted-cycle", "state": "running", "mode": "paper",
                "started_at": "2026-09-28T00:00:00Z", "message": "running",
            })
        if exit_kind == "error":
            raise RuntimeError("synthetic worker failure")
        if exit_kind == "cancelled":
            raise asyncio.CancelledError

    app = _app(monkeypatch, exiting_worker)
    with TestClient(app) as client:
        accepted = _submit(client, "personal-request-stop")
        assert accepted.status_code == 202
        path = f"/v1/personal/cycles/{accepted.json()['id']}"
        for _ in range(10):
            response = client.get(path, headers=HEADERS)
            if response.json()["state"] == "failed":
                break
        result = response.json()
        assert result["state"] == "failed"
        assert result["reason"] == "worker_stopped"
        assert result["outcome_unknown"] is running
        assert result["cycle_id"] == ("interrupted-cycle" if running else None)
        assert _submit(client, "personal-request-after-stop").status_code == 503
