from fastapi.testclient import TestClient

from polybot.api import create_app
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
        assert health.json()["mode"] == "paper"
        assert health.json()["real_money"] is False

        status = client.get("/v1/status")
        assert status.status_code == 401

        denied = client.post("/v1/cycles/run")
        assert denied.status_code == 401
