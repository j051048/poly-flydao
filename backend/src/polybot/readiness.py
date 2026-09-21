from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from polybot.models import utc_now

GATE_STORE = "store"
GATE_LEASE = "lease"
GATE_RUNTIME_CONTROL = "runtime_control"
GATE_RECONCILIATION = "reconciliation"

# Evaluation order matters for operators: the first unsatisfied gate is almost
# always the actual reason a deployment never becomes usable.
GATE_ORDER: tuple[str, ...] = (
    GATE_STORE,
    GATE_LEASE,
    GATE_RUNTIME_CONTROL,
    GATE_RECONCILIATION,
)


@dataclass(frozen=True)
class ReadinessBlocker:
    """A single, operator-actionable reason a gate is unsatisfied.

    ``fix`` is deliberately prescriptive: every blocker must tell the operator
    what to change, not merely that something is wrong.
    """

    code: str
    gate: str
    message: str
    fix: str

    def public(self) -> dict[str, str]:
        return {
            "code": self.code,
            "gate": self.gate,
            "message": self.message,
            "fix": self.fix,
        }


class ReadinessRegistry:
    """Process-local readiness with per-gate attribution.

    The worker refuses to run real-money cycles until every gate is satisfied.
    Without attribution this fail-closed design is indistinguishable from a
    crash, so the registry is the single source of truth for *why* the process
    is not ready. It never stores account identifiers, credentials, or order
    details: only gate names, stable codes, and operator guidance.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._role = "worker"
        self._component = "worker"
        self._mode = "unknown"
        self._gates: dict[str, bool] = {name: False for name in GATE_ORDER}
        self._blockers: dict[str, tuple[ReadinessBlocker, ...]] = {}
        self._warnings: dict[str, str] = {}
        self._not_ready_since: datetime | None = utc_now()

    def reset(self, *, role: str, component: str, mode: str) -> None:
        with self._lock:
            self._role = role
            self._component = component
            self._mode = mode
            self._gates = {name: False for name in GATE_ORDER}
            self._blockers = {}
            self._warnings = {}
            self._not_ready_since = utc_now()

    def set_gate(
        self,
        gate: str,
        ok: bool,
        blockers: tuple[ReadinessBlocker, ...] = (),
    ) -> None:
        if gate not in self._gates:
            raise ValueError(f"unknown readiness gate: {gate}")
        with self._lock:
            self._gates[gate] = bool(ok)
            if ok or not blockers:
                self._blockers.pop(gate, None)
            else:
                self._blockers[gate] = tuple(blockers)
            if all(self._gates.values()):
                self._not_ready_since = None
            elif self._not_ready_since is None:
                self._not_ready_since = utc_now()

    def set_warning(self, code: str, message: str) -> None:
        """Record a degraded-but-not-blocking condition (for example quarantine)."""

        with self._lock:
            self._warnings[code] = message

    def set_mode(self, mode: str) -> None:
        """Publish the currently effective trading mode.

        A personal deployment can switch modes at a cycle boundary, so the
        readiness payload has to follow the process rather than the boot-time
        environment value.
        """

        with self._lock:
            self._mode = mode

    def clear_warning(self, code: str) -> None:
        with self._lock:
            self._warnings.pop(code, None)

    def set_ready(self, value: bool) -> None:
        """Force the overall verdict, used by the signer-free control plane."""

        with self._lock:
            for gate in self._gates:
                self._gates[gate] = bool(value)
            if value:
                self._not_ready_since = None

    def reset_gates(self) -> None:
        """Fail closed: drop every gate and its attribution without a verdict."""

        with self._lock:
            self._gates = {name: False for name in GATE_ORDER}
            self._blockers = {}
            if self._not_ready_since is None:
                self._not_ready_since = utc_now()

    @property
    def ready(self) -> bool:
        with self._lock:
            return all(self._gates.values())

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            gates = dict(self._gates)
            blockers = [
                blocker.public()
                for gate in GATE_ORDER
                for blocker in self._blockers.get(gate, ())
            ]
            warnings = [{"code": code, "message": text} for code, text in self._warnings.items()]
            since = self._not_ready_since
            ready = all(gates.values())
            payload: dict[str, object] = {
                "ok": ready,
                "ready": ready,
                "role": self._role,
                "component": self._component,
                "mode": self._mode,
                "checked_at": utc_now().isoformat(),
                "gates": {gate: gates[gate] for gate in GATE_ORDER},
                "blockers": blockers,
                "warnings": warnings,
                "not_ready_seconds": (
                    None if since is None else max(0, int((utc_now() - since).total_seconds()))
                ),
            }
            return payload


READINESS = ReadinessRegistry()
