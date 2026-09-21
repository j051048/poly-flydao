from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from polybot.api import create_app
from polybot.auth import AuthPrincipal, StaticTokenVerifier
from polybot.config import Settings, TradingMode
from polybot.credentials import InMemoryCredentialRepository
from polybot.jobs import InMemoryJobRepository
from polybot.models import QuarantineRecord, utc_now
from polybot.personal_execution import PersonalRuntimeBinding
from polybot.readiness import GATE_LEASE, GATE_RECONCILIATION, GATE_STORE, READINESS

OWNER = "11111111-1111-4111-8111-111111111111"


def _verifier() -> StaticTokenVerifier:
    return StaticTokenVerifier(
        {
            "owner": AuthPrincipal(
                account_id=OWNER,
                aal="aal2",
                role="authenticated",
                issuer="https://example.supabase.co/auth/v1",
                audience=("authenticated",),
            ),
            "aal1": AuthPrincipal(
                account_id=OWNER,
                aal="aal1",
                role="authenticated",
                issuer="https://example.supabase.co/auth/v1",
                audience=("authenticated",),
            ),
        }
    )


class _BaselineRefused(RuntimeError):
    """Stands in for a PostgREST error raised by the guarded SQL function."""


class _PersonalRepository:
    def __init__(self, *, live_enabled: bool) -> None:
        now = utc_now()
        self.live_enabled = live_enabled
        self.binding = PersonalRuntimeBinding(
            account_id=OWNER,
            signer_address="0x" + ("11" * 20),
            deposit_wallet_address="0x" + ("22" * 20),
            chain_id=137,
            collateral_token="0x" + ("33" * 20),
            binding_version=2,
            paused=False,
            collateral_balance_pusd=Decimal("30"),
            allowances_ready=True,
            readiness_checked_at=now,
            readiness_owner_id="personal-worker-1",
            readiness_fencing_token=3,
            last_seen_at=now,
            updated_at=now,
        )
        self.quarantine: list[QuarantineRecord] = [
            QuarantineRecord(
                kind="trade",
                external_key="manual-trade-1",
                reason="unmapped_account_trade",
                condition_id="0x" + ("44" * 32),
                token_id="99",
                side="BUY",
                size=Decimal("12.5"),
                notional_usd=Decimal("6.25"),
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        ]
        self.baseline_calls: list[datetime] = []
        self.refuse_baseline = False

    async def get_binding(self) -> PersonalRuntimeBinding:
        return self.binding

    async def get_reconcile_baseline(self) -> datetime | None:
        return self.binding.reconcile_baseline_at

    async def list_quarantine(self, limit: int = 200) -> list[QuarantineRecord]:
        return self.quarantine[:limit]

    async def set_reconcile_baseline(self, baseline: datetime) -> datetime:
        if self.refuse_baseline:
            raise _BaselineRefused("refusing to move the reconcile baseline while armed")
        self.baseline_calls.append(baseline)
        self.binding = replace(self.binding, reconcile_baseline_at=baseline)
        return baseline

    async def get_desired_mode(self) -> TradingMode | None:
        return None


def _app(*, live_enabled: bool, mode: str = "paper", repository: _PersonalRepository | None = None):
    personal = repository or _PersonalRepository(live_enabled=live_enabled)
    app = create_app(
        settings=Settings(
            _env_file=None,
            personal_mode=True,
            personal_auto_run=False,
            personal_live_enabled=live_enabled,
            archive_enabled=False,
            component="all",
            account_id=OWNER,
            mode=mode,
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="service-role-test",
            ai_api_key="personal-secret-api-key",
            polymarket_private_key="12" * 32,
            polymarket_deposit_wallet="0x" + ("34" * 20),
        ),
        auth_verifier=_verifier(),
        jobs=InMemoryJobRepository(),
        credentials=InMemoryCredentialRepository(),
        personal_execution=personal,  # type: ignore[arg-type]
    )
    return app, personal


def test_personal_status_exposes_the_quarantine_ledger() -> None:
    app, _ = _app(live_enabled=True)
    with TestClient(app) as client:
        response = client.get(
            "/v1/personal/status",
            headers={"Authorization": "Bearer owner"},
        )
    assert response.status_code == 200
    reconciliation = response.json()["reconciliation"]
    assert reconciliation["quarantined_count"] == 1
    assert reconciliation["baseline_at"] is None
    item = reconciliation["quarantined_items"][0]
    assert item["external_key"] == "manual-trade-1"
    assert item["reason"] == "unmapped_account_trade"
    assert item["notional_usd"] == "6.25"


def test_reconciliation_routes_require_aal2_for_writes() -> None:
    app, repository = _app(live_enabled=True)
    with TestClient(app) as client:
        listed = client.get(
            "/v1/personal/reconciliation",
            headers={"Authorization": "Bearer aal1"},
        )
        denied = client.post(
            "/v1/personal/reconciliation/baseline",
            json={},
            headers={"Authorization": "Bearer aal1"},
        )
    assert listed.status_code == 200
    assert listed.json()["quarantined_count"] == 1
    assert denied.status_code == 403
    assert repository.baseline_calls == []


def test_baseline_reset_adopts_now_and_reports_the_new_ledger() -> None:
    app, repository = _app(live_enabled=True)
    with TestClient(app) as client:
        response = client.post(
            "/v1/personal/reconciliation/baseline",
            json={},
            headers={"Authorization": "Bearer owner"},
        )
    assert response.status_code == 200
    assert len(repository.baseline_calls) == 1
    assert repository.baseline_calls[0].tzinfo is not None
    assert response.json()["baseline_at"] is not None


def test_baseline_reset_accepts_an_explicit_past_instant() -> None:
    app, repository = _app(live_enabled=True)
    requested = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    with TestClient(app) as client:
        response = client.post(
            "/v1/personal/reconciliation/baseline",
            json={"baseline_at": requested.isoformat()},
            headers={"Authorization": "Bearer owner"},
        )
    assert response.status_code == 200
    assert repository.baseline_calls == [requested]


def test_baseline_reset_rejects_a_future_instant() -> None:
    app, repository = _app(live_enabled=True)
    with TestClient(app) as client:
        response = client.post(
            "/v1/personal/reconciliation/baseline",
            json={"baseline_at": (utc_now() + timedelta(hours=2)).isoformat()},
            headers={"Authorization": "Bearer owner"},
        )
    assert response.status_code == 422
    assert repository.baseline_calls == []


def test_baseline_reset_reports_the_durable_refusal_as_conflict() -> None:
    app, repository = _app(live_enabled=True)
    repository.refuse_baseline = True
    with TestClient(app) as client:
        response = client.post(
            "/v1/personal/reconciliation/baseline",
            json={},
            headers={"Authorization": "Bearer owner"},
        )
    assert response.status_code == 409
    assert "armed" in response.json()["detail"]


def test_worker_health_publishes_gates_and_blockers_in_personal_mode() -> None:
    app, _ = _app(live_enabled=True)
    READINESS.reset(role="worker", component="all", mode="canary")
    try:
        with TestClient(app) as client:
            response = client.get("/worker-health")
    finally:
        READINESS.reset_gates()
    assert response.status_code == 503
    payload = response.json()
    assert payload["ready"] is False
    assert set(payload["gates"]) == {GATE_STORE, GATE_LEASE, "runtime_control", GATE_RECONCILIATION}
    assert payload["role"] == "worker"


def test_readyz_mirrors_worker_health_in_every_role() -> None:
    app, _ = _app(live_enabled=True)
    READINESS.reset(role="worker", component="all", mode="paper")
    try:
        READINESS.set_ready(True)
        with TestClient(app) as client:
            ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["ready"] is True
    finally:
        READINESS.reset_gates()
    with TestClient(app) as client:
        not_ready = client.get("/readyz")
    assert not_ready.status_code == 503


def test_personal_mode_switch_is_rejected_when_the_deployment_cannot_honour_it() -> None:
    app, _ = _app(live_enabled=False)
    with TestClient(app) as client:
        profile = client.get("/v1/me", headers={"Authorization": "Bearer owner"}).json()
        response = client.put(
            "/v1/me/runtime-profile",
            json={
                "expected_version": profile["runtime_profile"]["version"],
                "ai_provider": "mock",
                "forecast_model": "gpt-4o-mini",
                "desired_mode": "canary",
                "auto_run_enabled": False,
                "cycle_interval_seconds": 60,
            },
            headers={"Authorization": "Bearer owner"},
        )
    assert response.status_code == 409
    assert "POLYBOT_PERSONAL_LIVE_ENABLED" in response.json()["detail"]


def test_personal_mode_switch_is_durable_when_live_is_enabled() -> None:
    app, _ = _app(live_enabled=True)
    with TestClient(app) as client:
        profile = client.get("/v1/me", headers={"Authorization": "Bearer owner"}).json()
        response = client.put(
            "/v1/me/runtime-profile",
            json={
                "expected_version": profile["runtime_profile"]["version"],
                "ai_provider": "mock",
                "forecast_model": "gpt-4o-mini",
                "desired_mode": "canary",
                "auto_run_enabled": False,
                "cycle_interval_seconds": 60,
            },
            headers={"Authorization": "Bearer owner"},
        )
        assert response.status_code == 200
        status = client.get("/v1/status", headers={"Authorization": "Bearer owner"})
    assert status.status_code == 200
    payload = status.json()
    assert payload["mode"] == "canary"
    assert payload["runtime_profile"]["desired_mode"] == "canary"
