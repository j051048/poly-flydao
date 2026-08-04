from dataclasses import replace
from decimal import Decimal
from threading import Event

import pytest
from fastapi.testclient import TestClient

import polybot.api as api_module
from polybot.api import (
    PersonalCycleIdempotencyConflict,
    PersonalCycleRequestRegistry,
    RequestRateLimiter,
    create_app,
)
from polybot.auth import AuthPrincipal, StaticTokenVerifier
from polybot.config import Settings, TradingMode
from polybot.credentials import InMemoryCredentialRepository
from polybot.jobs import InMemoryJobRepository
from polybot.models import RuntimeControl, utc_now
from polybot.personal_execution import PersonalRuntimeBinding
from polybot.worker import _set_worker_ready, is_worker_ready


class _UnboundPersonalRepository:
    async def get_binding(self) -> None:
        return None


def test_control_api_safe_defaults_and_auth_gate() -> None:
    app = create_app(
        settings=Settings(),
        jobs=InMemoryJobRepository(),
        credentials=InMemoryCredentialRepository(),
    )
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"ok": True, "control_plane": "healthy"}
        assert health.headers["cache-control"] == "no-store"

        status = client.get("/v1/status")
        assert status.status_code == 401

        denied = client.post("/v1/cycles/run")
        assert denied.status_code == 401


def test_request_rate_limiter_blocks_after_the_bounded_window_quota() -> None:
    limiter = RequestRateLimiter()
    assert limiter.allow("tenant:sensitive", limit=2)[0]
    assert limiter.allow("tenant:sensitive", limit=2)[0]
    allowed, retry_after = limiter.allow("tenant:sensitive", limit=2)
    assert not allowed
    assert retry_after >= 1


async def test_personal_cycle_registry_coalesces_and_rejects_key_reuse() -> None:
    registry = PersonalCycleRequestRegistry(max_entries=2)
    first, is_new = await registry.accept(
        account_id="11111111-1111-4111-8111-111111111111",
        idempotency_key="personal-cycle-request-0001",
        mode=TradingMode.PAPER,
    )
    duplicate, duplicate_is_new = await registry.accept(
        account_id="11111111-1111-4111-8111-111111111111",
        idempotency_key="personal-cycle-request-0001",
        mode=TradingMode.PAPER,
    )

    assert is_new
    assert not duplicate_is_new
    assert duplicate.request_id == first.request_id
    with pytest.raises(PersonalCycleIdempotencyConflict):
        await registry.accept(
            account_id="11111111-1111-4111-8111-111111111111",
            idempotency_key="personal-cycle-request-0001",
            mode=TradingMode.LIVE,
        )


def test_personal_status_is_owner_scoped_and_never_returns_secrets() -> None:
    owner = "11111111-1111-4111-8111-111111111111"
    other = "22222222-2222-4222-8222-222222222222"
    verifier = StaticTokenVerifier(
        {
            "owner": AuthPrincipal(
                account_id=owner,
                aal="aal1",
                role="authenticated",
                issuer="https://example.supabase.co/auth/v1",
                audience=("authenticated",),
            ),
            "other": AuthPrincipal(
                account_id=other,
                aal="aal2",
                role="authenticated",
                issuer="https://example.supabase.co/auth/v1",
                audience=("authenticated",),
            ),
        }
    )
    settings = Settings(
        _env_file=None,
        personal_mode=True,
        personal_auto_run=False,
        component="all",
        account_id=owner,
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
        ai_api_key="personal-secret-api-key",
        ai_base_url="https://relay.example.com/v1",
        ai_model="relay-model-v1",
        polymarket_private_key="12" * 32,
        polymarket_deposit_wallet="0x" + ("34" * 20),
    )
    app = create_app(
        settings=settings,
        auth_verifier=verifier,
        jobs=InMemoryJobRepository(),
        credentials=InMemoryCredentialRepository(),
        personal_execution=_UnboundPersonalRepository(),  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        response = client.get(
            "/v1/personal/status",
            headers={"Authorization": "Bearer owner"},
        )
        forbidden = client.get(
            "/v1/personal/status",
            headers={"Authorization": "Bearer other"},
        )
        old_queue = client.post(
            "/v1/jobs/cycles",
            json={"mode": "paper"},
            headers={
                "Authorization": "Bearer owner",
                "Idempotency-Key": "personal-cycle-123456",
            },
        )
        disabled_worker = client.post(
            "/v1/personal/cycles/run",
            headers={
                "Authorization": "Bearer owner",
                "Idempotency-Key": "personal-disabled-0001",
            },
            json={"mode": "paper"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "enabled": True,
        "live_supported": False,
        "mode": "paper",
        "auto_run_enabled": False,
        "worker_execution_model": "single_account",
        "worker_ready": False,
        "ready": False,
        "cycle_count": 0,
        "last_cycle": None,
        "ai": {
            "configured": True,
            "provider": "openai_compatible",
            "base_url": "https://relay.example.com/v1",
            "forecast_model": "relay-model-v1",
            "critic_model": "relay-model-v1",
        },
        "wallet": {
            "configured": True,
            "address": "0x" + ("34" * 20),
            "bound": False,
            "signer_address": None,
            "chain_id": None,
            "collateral_token": None,
            "binding_version": None,
            "paused": False,
            "collateral_balance_pusd": None,
            "allowances_ready": False,
            "readiness_checked_at": None,
        },
    }
    serialized = response.text
    assert "personal-secret-api-key" not in serialized
    assert "12" * 32 not in serialized
    assert forbidden.status_code == 403
    assert old_queue.status_code == 409
    assert disabled_worker.status_code == 409


def test_personal_lifespan_runs_one_worker_and_manual_trigger(
    monkeypatch,
) -> None:
    owner = "11111111-1111-4111-8111-111111111111"
    triggered = Event()
    stopped = Event()
    starts = 0

    async def fake_worker(
        *,
        settings,
        stop_event,
        cycle_trigger,
        cycle_observer,
        install_signal_handlers,
    ) -> None:
        nonlocal starts
        starts += 1
        assert settings.personal_mode is True
        assert install_signal_handlers is False
        _set_worker_ready(True)
        try:
            await cycle_trigger.wait()
            cycle_observer(
                {
                    "id": "personal-test-cycle",
                    "state": "succeeded",
                    "started_at": "2026-08-04T00:00:00Z",
                    "completed_at": "2026-08-04T00:00:01Z",
                    "message": "cycle completed",
                    "result_summary": {"markets_scanned": 3},
                }
            )
            triggered.set()
            await stop_event.wait()
        finally:
            _set_worker_ready(False)
            stopped.set()

    monkeypatch.setattr(api_module, "run_worker", fake_worker)
    verifier = StaticTokenVerifier(
        {
            "owner": AuthPrincipal(
                account_id=owner,
                aal="aal1",
                role="authenticated",
                issuer="https://example.supabase.co/auth/v1",
                audience=("authenticated",),
            )
        }
    )
    app = create_app(
        settings=Settings(
            _env_file=None,
            personal_mode=True,
            component="all",
            account_id=owner,
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="service-role-test",
            ai_api_key="personal-api-key",
        ),
        auth_verifier=verifier,
        jobs=InMemoryJobRepository(),
        credentials=InMemoryCredentialRepository(),
        personal_execution=_UnboundPersonalRepository(),  # type: ignore[arg-type]
    )

    for _lifespan_run in range(2):
        triggered.clear()
        stopped.clear()
        with TestClient(app) as client:
            # Yield to the lifespan-owned worker task before reading readiness.
            for _ in range(10):
                response = client.get(
                    "/v1/personal/status",
                    headers={"Authorization": "Bearer owner"},
                )
                if response.json()["worker_ready"]:
                    break
            assert response.json()["worker_ready"] is True

            accepted = client.post(
                "/v1/personal/cycles/run",
                headers={
                    "Authorization": "Bearer owner",
                    "Idempotency-Key": f"personal-cycle-request-{_lifespan_run:02d}",
                },
                json={"mode": "paper"},
            )
            assert accepted.status_code == 202
            duplicate = client.post(
                "/v1/personal/cycles/run",
                headers={
                    "Authorization": "Bearer owner",
                    "Idempotency-Key": f"personal-cycle-request-{_lifespan_run:02d}",
                },
                json={"mode": "paper"},
            )
            assert duplicate.status_code == 202
            assert duplicate.json()["id"] == accepted.json()["id"]
            assert accepted.json()["coalesced"] is False
            assert duplicate.json()["coalesced"] is True
            assert triggered.wait(timeout=1)

            status_response = client.get(
                "/v1/status",
                headers={"Authorization": "Bearer owner"},
            )
            assert status_response.status_code == 200
            status_payload = status_response.json()
            assert status_payload["latest_job"]["status"] == "succeeded"
            assert status_payload["personal"]["cycle_count"] == 1
            assert status_payload["personal"]["last_cycle"]["result_summary"] == {
                "markets_scanned": 3
            }

        assert stopped.wait(timeout=1)
    assert starts == 2
    assert is_worker_ready() is False


class _PersonalControlRepository:
    def __init__(self, owner: str) -> None:
        now = utc_now()
        self.binding = PersonalRuntimeBinding(
            account_id=owner,
            signer_address="0x" + ("11" * 20),
            deposit_wallet_address="0x" + ("22" * 20),
            chain_id=137,
            collateral_token="0x" + ("33" * 20),
            binding_version=1,
            paused=True,
            collateral_balance_pusd=Decimal("20"),
            allowances_ready=True,
            readiness_checked_at=now,
            readiness_owner_id="personal-worker-1",
            readiness_fencing_token=4,
            last_seen_at=now,
            updated_at=now,
        )
        self.control = RuntimeControl(account_id=owner, version=1)
        self.pause_calls: list[bool] = []
        self.resume_calls: list[int] = []

    async def get_binding(self) -> PersonalRuntimeBinding:
        return self.binding

    async def get_runtime_control(self) -> RuntimeControl:
        return self.control

    async def set_paused(self, paused: bool) -> PersonalRuntimeBinding:
        assert paused is True
        self.pause_calls.append(paused)
        self.binding = replace(self.binding, paused=paused, updated_at=utc_now())
        self.control = RuntimeControl(
            account_id=self.control.account_id,
            mode=TradingMode.CANARY,
            armed=False,
            kill_switch=True,
            cancellation_pending=True,
            version=self.control.version + 1,
        )
        return self.binding

    async def resume(self, *, expected_version: int) -> RuntimeControl | None:
        self.resume_calls.append(expected_version)
        if self.control.version != expected_version:
            return None
        self.binding = replace(self.binding, paused=False, updated_at=utc_now())
        return self.control


def test_personal_live_resume_and_pause_accept_owner_aal1() -> None:
    owner = "11111111-1111-4111-8111-111111111111"
    verifier = StaticTokenVerifier(
        {
            "owner": AuthPrincipal(
                account_id=owner,
                aal="aal1",
                role="authenticated",
                issuer="https://example.supabase.co/auth/v1",
                audience=("authenticated",),
            )
        }
    )
    personal = _PersonalControlRepository(owner)
    settings = Settings(
        _env_file=None,
        personal_mode=True,
        personal_auto_run=False,
        personal_live_enabled=True,
        mode="canary",
        component="all",
        account_id=owner,
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
        ai_api_key="personal-ai-key",
        polymarket_private_key="12" * 32,
    )
    app = create_app(
        settings=settings,
        auth_verifier=verifier,
        jobs=InMemoryJobRepository(),
        credentials=InMemoryCredentialRepository(),
        personal_execution=personal,  # type: ignore[arg-type]
    )

    with TestClient(app) as client:
        mismatch = client.post(
            "/v1/control/arm",
            headers={"Authorization": "Bearer owner"},
            json={"mode": "live", "minutes": 10, "expected_version": 1},
        )
        resumed = client.post(
            "/v1/control/arm",
            headers={"Authorization": "Bearer owner"},
            json={"mode": "canary", "minutes": 10, "expected_version": 1},
        )
        raced = client.post(
            "/v1/control/arm",
            headers={"Authorization": "Bearer owner"},
            json={"mode": "canary", "minutes": 10, "expected_version": 2},
        )
        paused = client.post(
            "/v1/control/disarm",
            headers={"Authorization": "Bearer owner"},
        )

    assert mismatch.status_code == 409
    assert resumed.status_code == 200
    assert resumed.json()["resume_requested"] is True
    assert resumed.json()["version"] == 1
    assert raced.status_code == 409
    assert paused.status_code == 202
    assert paused.json()["paused"] is True
    assert personal.resume_calls == [1, 2]
    assert personal.pause_calls == [True]
