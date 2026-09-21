"""HTTP request/response contracts and process-local API helpers.

Split out of :mod:`polybot.api` so the FastAPI wiring — routes, dependencies,
lifespan — can be read without scrolling past the public DTOs first.
``polybot.api`` re-exports every name defined here.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Request
from pydantic import BaseModel, Field, SecretStr

from polybot.config import TradingMode
from polybot.credentials import AIProvider, TradingWalletMetadata
from polybot.jobs import RuntimeProfile


@dataclass(frozen=True, slots=True)
class PersonalCycleAcceptance:
    request_id: str
    mode: TradingMode


class PersonalCycleIdempotencyConflict(ValueError):
    pass


class PersonalCycleRequestRegistry:
    """Bounded process-local idempotency for manual personal worker wake-ups."""

    def __init__(self, *, max_entries: int = 512) -> None:
        if max_entries < 1:
            raise ValueError("personal cycle registry must retain at least one request")
        self._max_entries = max_entries
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
            existing = self._entries.get(cache_key)
            if existing is not None:
                self._entries.move_to_end(cache_key)
                if existing.mode is not mode:
                    raise PersonalCycleIdempotencyConflict(
                        "Idempotency-Key was already used with a different personal cycle input"
                    )
                return existing, False

            accepted = PersonalCycleAcceptance(request_id=str(uuid4()), mode=mode)
            self._entries[cache_key] = accepted
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return accepted, True


class RequestRateLimiter:
    """Small per-process abuse brake; Supabase Auth remains the identity authority."""

    def __init__(self) -> None:
        self._windows: dict[str, tuple[float, int]] = {}

    def allow(self, key: str, *, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
        now = time.monotonic()
        started, hits = self._windows.get(key, (0.0, 0))
        if now - started >= window_seconds:
            started, hits = now, 0
        hits += 1
        self._windows[key] = (started, hits)
        if len(self._windows) > 8192:
            cutoff = now - window_seconds
            self._windows = {
                item_key: value for item_key, value in self._windows.items() if value[0] >= cutoff
            }
        retry_after = max(1, int(window_seconds - (now - started)))
        return hits <= limit, retry_after


def _rate_limit_identity(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    source = authorization if authorization.startswith("Bearer ") else ""
    if not source:
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
