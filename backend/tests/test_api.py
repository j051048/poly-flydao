from fastapi.testclient import TestClient

from polybot.api import RequestRateLimiter, create_app
from polybot.config import Settings
from polybot.credentials import InMemoryCredentialRepository
from polybot.jobs import InMemoryJobRepository


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
