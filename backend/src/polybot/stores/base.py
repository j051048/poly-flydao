from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from polybot.models import (
    AccountActivityUpdate,
    AccountPositionUpdate,
    EquityRiskState,
    EvidenceItem,
    ExecutionResult,
    Forecast,
    MarketSpec,
    OrderBookSnapshot,
    OrderReconcileTarget,
    Outcome,
    RiskDecision,
    RuntimeControl,
    TradeIntent,
    UserOrderUpdate,
    UserTradeUpdate,
    WorkerLease,
)
from polybot.stores.ledger import FillLedgerSnapshot


class StateStore(Protocol):
    async def health(self) -> bool: ...

    async def save_market(self, market: MarketSpec) -> None: ...

    async def save_snapshot(self, snapshot: OrderBookSnapshot) -> None: ...

    async def save_evidence(self, evidence: list[EvidenceItem]) -> None: ...

    async def save_forecast(self, forecast: Forecast, account_id: str) -> None: ...

    async def forecast_is_fresh(
        self, market_id: str, account_id: str, max_age: timedelta
    ) -> bool: ...

    async def save_risk_decision(
        self, intent: TradeIntent, decision: RiskDecision, account_id: str
    ) -> None: ...

    async def reserve_intent(self, intent: TradeIntent) -> bool: ...

    async def prepare_signed_order(
        self,
        intent: TradeIntent,
        *,
        signed_order_hash: str,
        payload_ciphertext: bytes,
        key_version: int,
        fencing_token: int,
        order_type: str = "GTC",
        expires_at: datetime | None = None,
    ) -> None: ...

    async def mark_order_submitting(
        self,
        intent_hash: str,
        account_id: str,
        fencing_token: int,
        *,
        control_version: int | None = None,
    ) -> None: ...

    async def save_execution(self, result: ExecutionResult, account_id: str) -> None: ...

    async def get_runtime_control(self, account_id: str) -> RuntimeControl: ...

    async def set_runtime_control(self, control: RuntimeControl) -> RuntimeControl: ...

    async def arm_runtime_control(
        self,
        account_id: str,
        mode: str,
        armed_until: datetime,
        expected_version: int,
    ) -> RuntimeControl | None: ...

    async def expire_runtime_control(
        self,
        account_id: str,
        mode: str,
        expected_version: int,
    ) -> RuntimeControl | None: ...

    async def disarm_runtime_control(self, account_id: str, mode: str) -> RuntimeControl: ...

    async def acknowledge_runtime_cancellation(
        self, account_id: str, expected_version: int
    ) -> RuntimeControl | None: ...

    async def claim_worker_lease(
        self, account_id: str, owner_id: str, ttl: timedelta
    ) -> WorkerLease | None: ...

    async def validate_worker_lease(
        self, account_id: str, owner_id: str, fencing_token: int
    ) -> bool: ...

    async def release_worker_lease(
        self, account_id: str, owner_id: str, fencing_token: int
    ) -> bool: ...

    async def has_unresolved_live_orders(self, account_id: str) -> bool: ...

    async def record_equity_state(
        self,
        account_id: str,
        equity_usd: Decimal,
    ) -> EquityRiskState: ...

    async def realized_pnl_since(self, account_id: str, since: datetime) -> Decimal: ...

    async def fill_ledger_snapshot(
        self, account_id: str, since: datetime
    ) -> FillLedgerSnapshot: ...

    async def event_ids_for_conditions(self, condition_ids: set[str]) -> dict[str, str]: ...

    async def position_outcomes(
        self, positions: set[tuple[str, str]]
    ) -> dict[tuple[str, str], Outcome]: ...

    async def durable_order_ids(
        self, candidate_order_ids: set[str], account_id: str
    ) -> set[str]: ...

    async def pending_trade_ids(self, account_id: str) -> set[str]: ...

    async def reconcile_order(self, update: UserOrderUpdate, account_id: str) -> None: ...

    async def reconcile_open_order_snapshot(
        self, open_order_ids: set[str], account_id: str
    ) -> list[OrderReconcileTarget]: ...

    async def confirm_orders_absent(self, clob_order_ids: set[str], account_id: str) -> None: ...

    async def reconcile_trade(self, update: UserTradeUpdate, account_id: str) -> None: ...

    async def reconcile_account_activity(
        self, update: AccountActivityUpdate, account_id: str
    ) -> None: ...

    async def reconcile_positions(
        self, positions: list[AccountPositionUpdate], account_id: str
    ) -> None: ...
