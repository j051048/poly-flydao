from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient

from polybot.api import create_app
from polybot.auth import AuthPrincipal, StaticTokenVerifier
from polybot.config import Settings
from polybot.credentials import (
    AesGcmEnvelopeEncryptor,
    CredentialService,
    InMemoryCredentialRepository,
)
from polybot.jobs import InMemoryJobRepository

ACCOUNT_A = "11111111-1111-4111-8111-111111111111"
ACCOUNT_B = "22222222-2222-4222-8222-222222222222"
AI_SECRET = "sk-test-tenant-a-super-secret-value"
EVM_SECRET = "0x" + ("12" * 32)


def _principal(account_id: str, *, aal: str) -> AuthPrincipal:
    return AuthPrincipal(
        account_id=account_id,
        aal=aal,
        role="authenticated",
        issuer="https://example.supabase.co/auth/v1",
        audience=("authenticated",),
    )


def _test_app() -> tuple[object, InMemoryCredentialRepository, InMemoryJobRepository]:
    credentials = InMemoryCredentialRepository()
    jobs = InMemoryJobRepository()
    writer = CredentialService(
        credentials,
        AesGcmEnvelopeEncryptor(b"e" * 32),
        fingerprint_key=b"f" * 32,
    )
    verifier = StaticTokenVerifier(
        {
            "a-aal1": _principal(ACCOUNT_A, aal="aal1"),
            "a-aal2": _principal(ACCOUNT_A, aal="aal2"),
            "b-aal2": _principal(ACCOUNT_B, aal="aal2"),
        }
    )
    settings = Settings(
        mode="paper",
        component="all",
        openai_api_key=None,
        litellm_api_key=None,
        polymarket_private_key=None,
        signed_payload_key=None,
    )
    return (
        create_app(
            settings=settings,
            auth_verifier=verifier,
            jobs=jobs,
            credentials=credentials,
            credential_writer=writer,
        ),
        credentials,
        jobs,
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_secret_enrollment_never_reflects_or_transports_plaintext() -> None:
    app, repository, _ = _test_app()
    with TestClient(app) as client:
        response = client.put(
            "/v1/me/credentials/ai",
            headers=_auth("a-aal2"),
            json={"provider": "openai", "api_key": AI_SECRET, "label": "primary"},
        )
        assert response.status_code == 201
        assert AI_SECRET not in response.text
        assert response.json()["last_four"] == "alue"
        assert response.headers["cache-control"] == "no-store"

        stored = next(iter(repository._credentials.values()))
        assert AI_SECRET not in str(stored)
        assert stored["account_id"] == ACCOUNT_A
        assert stored["envelope"]["ciphertext"]

        rejected = client.put(
            "/v1/me/credentials/ai",
            headers=_auth("a-aal2"),
            json={"provider": "openai", "api_key": "topsecret"},
        )
        assert rejected.status_code == 422
        assert "topsecret" not in rejected.text
        assert rejected.json() == {"detail": "request validation failed"}

        legacy = client.post(
            "/v1/cycles/run",
            headers={**_auth("a-aal1"), "X-API-Key": AI_SECRET},
        )
        assert legacy.status_code == 400
        assert AI_SECRET not in legacy.text


def test_wallet_import_requires_aal2_ack_and_enters_verification_queue() -> None:
    app, _, _ = _test_app()
    payload = {
        "private_key": EVM_SECRET,
        "signature_type": 3,
        "confirm_standard_allowances": True,
        "label": "small bot wallet",
    }
    aal2_headers = {
        **_auth("a-aal2"),
        "Idempotency-Key": "wallet:test:00000001",
    }
    with TestClient(app) as client:
        aal1 = client.post(
            "/v1/me/wallets/import",
            headers=_auth("a-aal1"),
            json=payload,
        )
        assert aal1.status_code == 403
        assert EVM_SECRET not in aal1.text

        missing_ack_payload = {
            key: value
            for key, value in payload.items()
            if key != "confirm_standard_allowances"
        }
        missing_ack = client.post(
            "/v1/me/wallets/import",
            headers=aal2_headers,
            json=missing_ack_payload,
        )
        assert missing_ack.status_code == 422
        assert EVM_SECRET not in missing_ack.text

        accepted = client.post(
            "/v1/me/wallets/import",
            headers=aal2_headers,
            json=payload,
        )
        assert accepted.status_code == 202
        assert accepted.json()["status"] == "pending_verification"
        assert EVM_SECRET not in accepted.text

        provision = client.post(
            "/v1/me/wallets/provision",
            headers=_auth("a-aal2"),
            json={"label": "not yet supported"},
        )
        assert provision.status_code == 501


def test_jobs_are_jwt_scoped_idempotent_and_never_execute_over_http() -> None:
    app, _, _ = _test_app()
    headers = {**_auth("a-aal1"), "Idempotency-Key": "manual:test:00000001"}
    with TestClient(app) as client:
        first = client.post("/v1/jobs/cycles", headers=headers, json={"mode": "paper"})
        second = client.post("/v1/jobs/cycles", headers=headers, json={"mode": "paper"})
        assert first.status_code == second.status_code == 202
        assert first.json()["id"] == second.json()["id"]
        assert second.json()["deduplicated"] is True

        job_id = UUID(first.json()["id"])
        own = client.get(f"/v1/jobs/{job_id}", headers=_auth("a-aal1"))
        other = client.get(f"/v1/jobs/{job_id}", headers=_auth("b-aal2"))
        assert own.status_code == 200
        assert other.status_code == 404
        assert own.json()["account_id"] == ACCOUNT_A

        removed = client.post("/v1/cycles/run", headers=_auth("a-aal1"))
        assert removed.status_code == 410


def test_status_disarm_and_portfolio_contracts_are_tenant_scoped() -> None:
    app, _, _ = _test_app()
    with TestClient(app) as client:
        me = client.get("/v1/me", headers=_auth("a-aal1"))
        assert me.status_code == 200
        assert me.json()["account_id"] == ACCOUNT_A
        assert me.json()["capabilities"]["wallet_provisioning"] is False

        status_response = client.get("/v1/status", headers=_auth("a-aal1"))
        assert status_response.status_code == 200
        body = status_response.json()
        assert body["mode"] == "paper"
        assert body["ai_provider"] == "platform"
        assert body["risk_limits"]["max_bucket_exposure_pct"] == "0.05"

        portfolio = client.get("/v1/me/portfolio", headers=_auth("a-aal1"))
        assert portfolio.status_code == 200
        assert portfolio.json()["orders"] == []
        assert portfolio.json()["summary"]["portfolio_value_usd"] is None

        disarmed = client.post("/v1/control/disarm", headers=_auth("a-aal1"))
        assert disarmed.status_code == 202
        assert disarmed.json()["cancellation_verified"] is False
