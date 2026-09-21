from __future__ import annotations

from polybot.healthcheck import _candidate_ports, main, probe


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_probe_reports_alive_on_the_first_responding_port(monkeypatch) -> None:
    calls: list[str] = []

    def fake_urlopen(url: str, timeout: float) -> _Response:
        calls.append(url)
        return _Response(200)

    monkeypatch.setattr("polybot.healthcheck.urlopen", fake_urlopen)
    alive, detail = probe([8080])
    assert alive is True
    assert "200" in detail
    assert calls == ["http://127.0.0.1:8080/livez"]


def test_probe_reports_failure_without_revealing_credentials(monkeypatch) -> None:
    def fake_urlopen(url: str, timeout: float) -> _Response:
        raise OSError("connection refused")

    monkeypatch.setattr("polybot.healthcheck.urlopen", fake_urlopen)
    alive, detail = probe([8080, 8000])
    assert alive is False
    assert "8080" in detail and "8000" in detail
    assert "service-role" not in detail


def test_probe_treats_a_non_2xx_liveness_answer_as_failed(monkeypatch) -> None:
    monkeypatch.setattr(
        "polybot.healthcheck.urlopen",
        lambda url, timeout: _Response(503),
    )
    alive, detail = probe([8080])
    assert alive is False
    assert "503" in detail


def test_probe_never_touches_readiness(monkeypatch) -> None:
    seen: list[str] = []

    def fake_urlopen(url: str, timeout: float) -> _Response:
        seen.append(url)
        return _Response(200)

    monkeypatch.setattr("polybot.healthcheck.urlopen", fake_urlopen)
    probe([8080])
    assert seen == ["http://127.0.0.1:8080/livez"]


def test_candidate_ports_are_bounded_and_deduplicated(monkeypatch) -> None:
    for name in ("POLYBOT_API_PORT", "PORT", "POLYBOT_WORKER_HEALTH_PORT"):
        monkeypatch.delenv(name, raising=False)
    assert _candidate_ports() == [8000, 8080]
    monkeypatch.setenv("POLYBOT_API_PORT", "not-a-port")
    monkeypatch.setenv("PORT", "99999")
    monkeypatch.setenv("POLYBOT_WORKER_HEALTH_PORT", "8081")
    assert _candidate_ports() == [8000, 8080, 8081]


def test_main_returns_a_shell_usable_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(
        "polybot.healthcheck.urlopen",
        lambda url, timeout: _Response(200),
    )
    assert main([]) == 0
    monkeypatch.setattr(
        "polybot.healthcheck.urlopen",
        lambda url, timeout: (_ for _ in ()).throw(OSError("down")),
    )
    assert main([]) == 1
