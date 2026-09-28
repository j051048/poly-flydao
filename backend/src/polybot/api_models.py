"""HTTP request/response contracts and process-local API helpers.

Split out of :mod:`polybot.api` so the FastAPI wiring — routes, dependencies,
lifespan — can be read without scrolling past the public DTOs first.
``polybot.api`` re-exports every name defined here.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Request
from pydantic import BaseModel, Field, SecretStr

from polybot.config import TradingMode
from polybot.credentials import AIProvider, TradingWalletMetadata
from polybot.jobs import RuntimeProfile
from polybot.models import utc_now


@dataclass(slots=True)
class PersonalCycleAcceptance:
    request_id: str
    mode: TradingMode
    created_at: datetime = field(default_factory=utc_now)
    state: str = "queued"
    cycle: dict[str, object] = field(default_factory=dict)
    created_tick: float = field(default_factory=time.monotonic)
    started_tick: float | None = None


class PersonalCycleIdempotencyConflict(ValueError):
    pass


class PersonalCycleCapacityExceeded(RuntimeError):
    pass


class PersonalCycleRequestRegistry:
    """Bounded process-local idempotency for manual personal worker wake-ups."""

    def __init__(
        self, *, max_entries: int = 512,
        queue_timeout_seconds: float = 300, run_timeout_seconds: float = 3600,
    ) -> None:
        if max_entries < 1:
            raise ValueError("personal cycle registry must retain at least one request")
        self._max_entries = max_entries
        if queue_timeout_seconds <= 0 or run_timeout_seconds <= 0:
            raise ValueError("personal cycle request timeouts must be positive")
        self._queue_timeout_seconds = queue_timeout_seconds
        self._run_timeout_seconds = run_timeout_seconds
        self._entries: OrderedDict[tuple[str, str], PersonalCycleAcceptance] = OrderedDict()
        self._lock = asyncio.Lock()

    async def accept(
        self,
        *,
        account_id: str,
        idempotency_key: str,
        mode: TradingMode,
    ) -> tuple[PersonalCycleAcceptance, bool]:
        cache_key = (account_id, idempotency_key)
        async with self._lock:
            self._expire()
            existing = self._entries.get(cache_key)
            if existing is not None:
                self._entries.move_to_end(cache_key)
                if existing.mode is not mode:
                    raise PersonalCycleIdempotencyConflict(
                        "Idempotency-Key was already used with a different personal cycle input"
                    )
                return existing, False

            if len(self._entries) >= self._max_entries:
                terminal = next((
                    key for key, entry in self._entries.items()
                    if entry.state not in {"queued", "running"}
                ), None)
                if terminal is None:
                    raise PersonalCycleCapacityExceeded(
                        "too many pending personal cycle requests; "
                        "wait for an existing request before submitting another"
                    )
                del self._entries[terminal]
            accepted = PersonalCycleAcceptance(request_id=str(uuid4()), mode=mode)
            self._entries[cache_key] = accepted
            return accepted, True

    def observe_cycle(self, snapshot: dict[str, object]) -> None:
        self._expire()
        if not snapshot.get("id"):
            return
        for entry in self._entries.values():
            if entry.state == "queued" and snapshot.get("state") == "running":
                if snapshot.get("mode") == entry.mode.value:
                    entry.cycle = dict(snapshot)
                    entry.state = "running"
                    entry.started_tick = time.monotonic()
                elif snapshot.get("mode") in {mode.value for mode in TradingMode}:
                    self._fail(entry, "mode_superseded", "运行模式已变化，本次排队请求未执行。")
            elif entry.state == "running" and entry.cycle.get("id") == snapshot.get("id"):
                entry.cycle = dict(snapshot)
                entry.state = str(snapshot["state"])

    def supersede_queued(self, account_id: str, mode: TradingMode) -> None:
        self._expire()
        for (owner, _), entry in self._entries.items():
            if owner == account_id and entry.state == "queued" and entry.mode is not mode:
                self._fail(entry, "mode_superseded", "运行模式已变化，本次排队请求未执行。")

    def fail_active(self, reason: str, message: str) -> None:
        for entry in self._entries.values():
            if entry.state in {"queued", "running"}:
                self._fail(entry, reason, message, outcome_unknown=entry.state == "running")

    @staticmethod
    def _fail(
        entry: PersonalCycleAcceptance, reason: str, message: str, *, outcome_unknown: bool = False,
    ) -> None:
        entry.state = "failed"
        entry.cycle = {
            **entry.cycle, "state": "failed", "reason": reason, "message": message,
            "completed_at": utc_now().isoformat(), "outcome_unknown": outcome_unknown,
        }

    def _expire(self) -> None:
        now = time.monotonic()
        for entry in self._entries.values():
            if entry.state == "queued" and now - entry.created_tick >= self._queue_timeout_seconds:
                self._fail(entry, "queue_timeout", "等待运行超时，请检查 Worker 状态后重新提交。")
            elif (
                entry.state == "running" and entry.started_tick is not None
                and now - entry.started_tick >= self._run_timeout_seconds
            ):
                self._fail(
                    entry, "result_timeout", "未能及时确认周期结果；请查看最新运行状态。"
                    "此提示不表示交易已停止。", outcome_unknown=True,
                )

    def get(self, account_id: str, request_id: str) -> dict[str, object] | None:
        self._expire()
        for (owner, _), entry in self._entries.items():
            if owner == account_id and entry.request_id == request_id:
                return {
                    **entry.cycle,
                    "id": entry.request_id,
                    "request_id": entry.request_id,
                    "mode": entry.mode.value,
                    "state": entry.state,
                    "cycle_id": entry.cycle.get("id"),
                    "created_at": entry.created_at.isoformat(),
                }
        return None


class RequestRateLimiter:
    """Small per-process abuse brake; Supabase Auth remains the identity authority."""

    def __init__(self, *, max_entries: int = 8192) -> None:
        if max_entries < 1:
            raise ValueError("rate limiter capacity must be positive")
        self._max_entries = max_entries
        self._windows: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def allow(self, key: str, *, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
        now = time.monotonic()
        # Fixed windows are inserted in start-time order. Expiry is amortized
        # O(1), and a full cache refuses new identities rather than growing.
        while self._windows:
            first, (started_at, _) = next(iter(self._windows.items()))
            if now - started_at < window_seconds:
                break
            del self._windows[first]
        if key not in self._windows and len(self._windows) >= self._max_entries:
            return False, window_seconds
        started, hits = self._windows.get(key, (0.0, 0))
        if now - started >= window_seconds:
            started, hits = now, 0
        hits += 1
        self._windows[key] = (started, hits)
        retry_after = max(1, int(window_seconds - (now - started)))
        return hits <= limit, retry_after


def _rate_limit_identity(request: Request) -> str:
    # Before authentication only the connection's trusted client address is an
    # identity. Raw Authorization and forwarded headers are attacker-controlled.
    source = request.client.host if request.client else "unknown"
    return sha256(source.encode("utf-8", errors="ignore")).hexdigest()


class AIKeyPutRequest(BaseModel):
    provider: AIProvider
    api_key: SecretStr = Field(min_length=16, max_length=512)
    label: str | None = Field(default=None, min_length=1, max_length=80)


class WalletProvisionRequest(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    owner_address: str | None = None


class WalletImportRequest(WalletProvisionRequest):
    private_key: SecretStr = Field(min_length=64, max_length=66)
    signature_type: int = Field(default=3, ge=0, le=3)
    confirm_standard_allowances: Literal[True]


class ArmRequest(BaseModel):
    mode: TradingMode
    minutes: int = Field(default=5, ge=1, le=15)
    expected_version: int = Field(ge=1)


class PersonalCycleRequest(BaseModel):
    mode: TradingMode


class ReconcileBaselineRequest(BaseModel):
    """Optional explicit baseline; omitting it adopts "now"."""

    baseline_at: datetime | None = None


class MeResponse(BaseModel):
    account_id: UUID
    aal: str
    runtime_profile: RuntimeProfile
    capabilities: dict[str, bool]


class PublicCredentialMetadata(BaseModel):
    id: UUID
    kind: str
    provider: str
    label: str | None = None
    status: str
    version: int
    created_at: datetime
    rotated_at: datetime | None = None
    revoked_at: datetime | None = None


class PublicCredentialStatus(BaseModel):
    ai_credentials: list[PublicCredentialMetadata]
    wallets: list[TradingWalletMetadata]


class PersonalAIStatus(BaseModel):
    configured: bool
    provider: str
    base_url: str | None
    forecast_model: str
    critic_model: str


class PersonalWalletStatus(BaseModel):
    configured: bool
    address: str | None
    bound: bool = False
    signer_address: str | None = None
    chain_id: int | None = None
    collateral_token: str | None = None
    binding_version: int | None = None
    paused: bool = False
    collateral_balance_pusd: str | None = None
    allowances_ready: bool = False
    readiness_checked_at: datetime | None = None


class PersonalCycleStatus(BaseModel):
    id: str
    state: Literal["running", "succeeded", "failed"]
    mode: TradingMode | None = None
    started_at: datetime
    completed_at: datetime | None = None
    message: str
    result_summary: dict[str, object] | None = None


class PersonalQuarantineItem(BaseModel):
    kind: str
    external_key: str
    reason: str
    condition_id: str | None = None
    token_id: str | None = None
    side: str | None = None
    size: str | None = None
    notional_usd: str | None = None


class PersonalReconciliationStatus(BaseModel):
    """Public, non-secret view of what reconciliation is ignoring and why."""

    baseline_at: datetime | None = None
    quarantined_count: int = 0
    quarantined_items: list[PersonalQuarantineItem] = Field(default_factory=list)
    reason: str | None = None


class PersonalStatusResponse(BaseModel):
    enabled: bool
    live_supported: bool
    # ``mode`` is what the worker is actually allowed to run; ``desired_mode`` is
    # the durable dashboard request, which the worker adopts at a cycle boundary.
    # They only differ for the seconds in between, or when a request was clamped.
    mode: TradingMode
    desired_mode: TradingMode | None = None
    mode_note: str | None = None
    mode_applied: bool = False
    configuration_error: str | None = None
    configuration_checked_at: datetime | None = None
    risk_policy_id: str | None = None
    risk_policy_version: int | None = None
    auto_run_enabled: bool
    worker_execution_model: str
    worker_ready: bool
    ready: bool
    cycle_count: int
    last_cycle: PersonalCycleStatus | None = None
    ai: PersonalAIStatus
    wallet: PersonalWalletStatus
    reconciliation: PersonalReconciliationStatus = Field(
        default_factory=PersonalReconciliationStatus
    )
