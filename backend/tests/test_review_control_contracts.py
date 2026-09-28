from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import polybot.api as api_module
from polybot.api import create_app
from polybot.api_dependencies import _personal_status
from polybot.api_models import (
    PersonalCycleRequestRegistry,
    RequestRateLimiter,
    _rate_limit_identity,
)
from polybot.auth import AuthPrincipal, StaticTokenVerifier
from polybot.config import Settings, TradingMode
from polybot.credentials import InMemoryCredentialRepository
from polybot.jobs import InMemoryJobRepository, RiskPolicySnapshot
from polybot.models import EquityHistoryPoint, utc_now
from polybot.personal_configuration import personal_configuration
from polybot.personal_execution import PersonalExecutionRepository

ACCOUNT = "11111111-1111-4111-8111-111111111111"


def policy(**changes) -> RiskPolicySnapshot:
    return RiskPolicySnapshot.model_validate({
        "id": "22222222-2222-4222-8222-222222222222", "account_id": ACCOUNT,
        "version": 2, "status": "active", "max_order_usd": "2",
        "max_trade_risk_pct": "0.01", "max_event_exposure_pct": "0.1",
        "max_bucket_exposure_pct": "0.2", "max_gross_exposure_pct": "0.5",
        "daily_loss_limit_pct": "0.02", "max_drawdown_pct": "0.1",
        "min_edge": "0.03", "created_at": utc_now(), **changes,
    })


def test_risk_policy_changes_the_actual_engine_settings_with_immutable_env_caps():
    settings = Settings(_env_file=None, account_id=ACCOUNT, max_order_usd=17, min_edge="0.04")
    state = personal_configuration(settings)
    state.apply_policy(settings, policy())
    assert settings.max_order_usd == Decimal("2")
    assert settings.min_edge == Decimal("0.04")
    assert state.limits()["version"] == 2
    state.apply_policy(settings, policy(version=3, max_order_usd="100", min_edge="0.05"))
    assert settings.max_order_usd == Decimal("17")
    assert settings.min_edge == Decimal("0.05")
    assert state.ceilings["max_order_usd"] == Decimal("17")
    with pytest.raises(ValueError):
        state.apply_policy(settings, policy(account_id="33333333-3333-4333-8333-333333333333"))
    assert settings.max_order_usd == Decimal("17")


def test_hot_canary_switch_keeps_five_dollar_cap_and_accepts_zero_min_edge():
    settings = Settings(_env_file=None, account_id=ACCOUNT, max_order_usd=17, min_edge=0)
    state = personal_configuration(settings)
    state.apply_policy(settings, policy(max_order_usd="10", min_edge="0"), mode=TradingMode.CANARY)
    assert settings.max_order_usd == Decimal("5")
    assert settings.min_edge == 0
    state.apply_policy(settings, None, mode=TradingMode.LIVE)
    assert settings.max_order_usd == Decimal("17")
    with pytest.raises(ValueError):
        state.apply_policy(settings, policy(max_drawdown_pct="1.1"))
    assert settings.max_order_usd == Decimal("17")


def test_status_never_projects_a_requested_mode_as_a_worker_ack(monkeypatch):
    settings = Settings(
        _env_file=None, personal_mode=True, account_id=ACCOUNT,
        supabase_url="https://example.supabase.co", supabase_service_role_key="test-service-role",
    )
    monkeypatch.setattr("polybot.api_dependencies.is_worker_ready", lambda: True)
    state = personal_configuration(settings)
    snapshot = _personal_status(settings, desired_mode=TradingMode.SHADOW)
    assert snapshot.mode is TradingMode.PAPER
    assert not snapshot.mode_applied
    settings.mode = TradingMode.SHADOW
    assert not _personal_status(settings, desired_mode=TradingMode.SHADOW).mode_applied
    state.confirm(TradingMode.SHADOW)
    assert _personal_status(settings, desired_mode=TradingMode.SHADOW).mode_applied
    assert state.checked_at is None  # Control ticks cannot claim a fresh DB read.
    state.profile_version = 1
    assert not _personal_status(
        settings, desired_mode=TradingMode.SHADOW, desired_profile_version=2,
    ).mode_applied
    state.error = "configuration_unavailable"
    assert not _personal_status(settings, desired_mode=TradingMode.SHADOW).mode_applied


async def test_repository_reads_the_migrated_table_and_scopes_risk_to_the_owner():
    calls = []
    class Query:
        def __init__(self, table):
            assert table in {"account_runtime_profiles", "risk_policies"}
            self.table = table
        def select(self, value):
            return self
        def eq(self, name, value):
            calls.append((self.table, name, value))
            return self
        def limit(self, value):
            return self
        def execute(self):
            row = policy().model_dump(mode="json") if self.table == "risk_policies" else {
                "account_id": ACCOUNT, "desired_mode": "shadow", "version": 9,
                "risk_policy_id": str(policy().id), "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            return SimpleNamespace(data=[row])
    repo = PersonalExecutionRepository(SimpleNamespace(table=Query), ACCOUNT)
    assert await repo.get_desired_mode() is TradingMode.SHADOW
    profile, active = await repo.get_configuration()
    assert profile.version == 9
    assert active.max_order_usd == Decimal("2")
    assert ("risk_policies", "account_id", ACCOUNT) in calls
    assert ("risk_policies", "id", str(policy().id)) in calls


def test_rotating_invalid_tokens_cannot_evade_ip_quota_or_grow_the_cache():
    limiter = RequestRateLimiter(max_entries=64)
    accepted = 0
    for index in range(9000):
        request = Request({
            "type": "http", "client": ("127.0.0.1", 1234),
            "headers": [(b"authorization", f"Bearer invalid-{index}".encode())],
        })
        accepted += limiter.allow(_rate_limit_identity(request), limit=120)[0]
    assert accepted == 120
    assert len(limiter._windows) == 1
    for index in range(9000):
        limiter.allow(f"different-ip-{index}", limit=120)
    assert len(limiter._windows) == 64


async def test_manual_request_is_bound_to_its_own_later_cycle():
    registry = PersonalCycleRequestRegistry()
    registry.observe_cycle({"id": "old", "mode": "paper", "state": "running"})
    entry, _ = await registry.accept(
        account_id=ACCOUNT, idempotency_key="new-manual-request", mode=TradingMode.PAPER,
    )
    registry.observe_cycle({"id": "old", "mode": "paper", "state": "succeeded"})
    assert registry.get(ACCOUNT, entry.request_id)["state"] == "queued"
    registry.observe_cycle({"id": "paper-new", "mode": "paper", "state": "running"})
    registry.observe_cycle({"id": "paper-new", "mode": "paper", "state": "succeeded"})
    result = registry.get(ACCOUNT, entry.request_id)
    assert result["state"] == "succeeded"
    assert result["cycle_id"] == "paper-new"
    assert result["id"] == entry.request_id
    assert registry.get("another-owner", entry.request_id) is None


def _app(repo=None):
    jobs = repo or InMemoryJobRepository()
    app = create_app(
        settings=Settings(_env_file=None, account_id=ACCOUNT, personal_mode=True,
                          personal_auto_run=False, archive_enabled=False,
                          supabase_url="https://example.supabase.co",
                          supabase_service_role_key="test-service-role"),
        jobs=jobs, credentials=InMemoryCredentialRepository(),
        personal_execution=SimpleNamespace(
            get_binding=AsyncMock(return_value=None), list_quarantine=AsyncMock(return_value=[]),
        ),
        auth_verifier=StaticTokenVerifier({"owner": AuthPrincipal(
            account_id=ACCOUNT, aal="aal2", role="authenticated",
            issuer="https://example.test", audience=("authenticated",),
        )}),
    )
    return app, jobs


def test_equity_endpoint_filters_funding_scope_before_limit():
    app, jobs = _app()
    jobs._equity_history[ACCOUNT] = [
        EquityHistoryPoint(recorded_at=utc_now(), equity_usd=Decimal(value), source=source)
        for value, source in [("1000", "paper_cycle"), ("20", "live_cycle"),
                              ("21", "canary_cycle"), ("999", "paper_cycle")]
    ]
    headers = {"Authorization": "Bearer owner"}
    with TestClient(app) as client:
        real = client.get("/v1/me/equity-history?scope=real&limit=2", headers=headers).json()
        paper = client.get("/v1/me/equity-history?limit=2", headers=headers).json()
        invalid = client.get("/v1/me/equity-history?scope=all", headers=headers)
    assert real["scope"] == "real"
    assert [row["equity_usd"] for row in real["items"]] == ["20", "21"]
    assert [row["equity_usd"] for row in paper["items"]] == ["1000", "999"]
    assert invalid.status_code == 422


def test_status_distinguishes_pending_policy_from_effective_limits():
    app, jobs = _app()
    with TestClient(app) as client:
        client.get("/v1/me", headers={"Authorization": "Bearer owner"})
        jobs._profiles[ACCOUNT] = jobs._profiles[ACCOUNT].model_copy(
            update={"risk_policy_id": policy().id},
        )
        jobs._risks[ACCOUNT] = policy()
        pending = client.get("/v1/status", headers={"Authorization": "Bearer owner"})
        assert pending.status_code == 200
        assert pending.json()["risk_policy_pending"]
        state = personal_configuration(app.state.settings)
        state.apply_policy(app.state.settings, policy())
        active = client.get("/v1/status", headers={"Authorization": "Bearer owner"}).json()
    assert not active["risk_policy_pending"]
    assert active["risk_limits"]["max_order_usd"] == "2"
    assert active["risk_limits"]["policy_id"] == str(policy().id)


def test_personal_lifespan_starts_and_stops_resolution_worker(monkeypatch):
    from threading import Event
    started, stopped = Event(), Event()
    async def idle_worker(**kwargs):
        await kwargs["stop_event"].wait()
    class Resolution:
        def __init__(self, **kwargs):
            assert isinstance(kwargs["repository"], InMemoryJobRepository)
        async def serve(self, stop):
            started.set()
            try:
                await stop.wait()
            finally:
                stopped.set()
    monkeypatch.setattr(api_module, "run_worker", idle_worker)
    monkeypatch.setattr(api_module, "MarketResolutionWorker", Resolution)
    app, _ = _app()
    app.state.settings.personal_auto_run = True
    with TestClient(app):
        assert started.wait(3)
    assert stopped.wait(3)


def test_personal_status_does_not_guess_a_mode_when_profile_read_fails():
    app, jobs = _app()
    jobs.get_or_create_profile = AsyncMock(side_effect=RuntimeError("database unavailable"))
    with TestClient(app) as client:
        response = client.get("/v1/personal/status", headers={"Authorization": "Bearer owner"})
    assert response.status_code == 503
    assert "mode" not in response.json()


def test_cycle_status_preserves_the_mode_that_actually_ran():
    from polybot.api_models import PersonalCycleStatus
    snapshot = PersonalCycleStatus.model_validate({
        "id": "paper-cycle", "mode": "paper", "state": "succeeded",
        "started_at": utc_now(), "message": "completed",
    })
    assert snapshot.model_dump(mode="json")["mode"] == "paper"
