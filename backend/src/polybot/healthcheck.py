from __future__ import annotations

import os
import sys
from urllib.error import URLError
from urllib.request import urlopen

# Candidate loopback ports, in priority order. The API/personal role serves
# /livez on POLYBOT_API_PORT (PORT in Zeabur); the tenant-queue worker serves the
# same contract on POLYBOT_WORKER_HEALTH_PORT. One probe covers every role.
PORTS = (
    ("POLYBOT_API_PORT", 8000),
    ("PORT", 8080),
    ("POLYBOT_WORKER_HEALTH_PORT", 8080),
)
DEFAULT_TIMEOUT_SECONDS = 4.0


def _candidate_ports() -> list[int]:
    ports: list[int] = []
    for name, fallback in PORTS:
        raw = os.environ.get(name)
        try:
            port = int(raw) if raw else fallback
        except ValueError:
            port = fallback
        if not 1 <= port <= 65535:
            # A misconfigured value must not hide the documented default.
            port = fallback
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return ports


def probe(
    ports: list[int] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """Return ``(alive, detail)`` for the first responding loopback endpoint.

    Only ``/livez`` is probed on purpose. Readiness is deliberately *not* part
    of the container health check: a deployment that is safe but not yet ready
    (no bound wallet, unfunded account, quarantined manual trades) must stay up
    and keep reporting its blockers instead of being restarted by the platform.
    """

    attempted: list[str] = []
    for port in ports if ports is not None else _candidate_ports():
        url = f"http://127.0.0.1:{port}/livez"
        try:
            with urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback only
                if 200 <= response.status < 300:
                    return True, f"{url} -> {response.status}"
                attempted.append(f"{url} -> {response.status}")
        except (URLError, OSError, ValueError) as exc:
            attempted.append(f"{url} -> {type(exc).__name__}")
    return False, "; ".join(attempted) or "no candidate ports configured"


def main(argv: list[str] | None = None) -> int:
    del argv
    alive, detail = probe()
    print(f"healthcheck {'ok' if alive else 'failed'}: {detail}")  # noqa: T201 - container log
    return 0 if alive else 1


if __name__ == "__main__":
    sys.exit(main())
