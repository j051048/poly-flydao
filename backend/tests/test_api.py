from fastapi.testclient import TestClient

from polybot.api import app


def test_control_api_safe_defaults_and_auth_gate() -> None:
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["mode"] == "paper"
        assert health.json()["real_money"] is False

        status = client.get("/v1/status")
        assert status.status_code == 503

        denied = client.post("/v1/cycles/run")
        assert denied.status_code == 503
