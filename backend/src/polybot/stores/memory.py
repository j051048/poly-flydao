from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from polybot.config import TradingMode
from polybot.fees import matched_taker_fee_usd
from polybot.models import (
    AccountActivityUpdate,
    AccountPositionUpdate,
    AIUsageRecord,
    EquityHistoryPoint,
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
    utc_now,
)
from polybot.stores.ledger import FillLedgerSnapshot, replay_fill_ledger


class MemoryStore:
    def __init__(self) -> None:
        self.markets: dict[str, MarketSpec] = {}
        self.snapshots: list[OrderBookSnapshot] = []
        self.evidence: dict[str, EvidenceItem] = {}
        self.forecasts: dict[str, Forecast] = {}
        self.intents: dict[str, TradeIntent] = {}
        self.risk_events: list[tuple[TradeIntent, RiskDecision]] = []
        self.executions: list[ExecutionResult] = []
        self.controls: dict[str, RuntimeControl] = {}
        self.leases: dict[str, WorkerLease] = {}
        self.order_updates: list[UserOrderUpdate] = []
        self.trade_updates: list[UserTradeUpdate] = []
        self.account_activity_updates: dict[str, AccountActivityUpdate] = {}
        self.position_updates: dict[str, AccountPositionUpdate] = {}
        self.signed_orders: dict[str, tuple[str, bytes, int, str, int]] = {}
        self.unresolved_live_orders: set[str] = set()
        self.durable_orders: dict[str, str] = {}
        self.order_missing_confirmations: dict[str, int] = {}
        self.equity_states: dict[str, EquityRiskState] = {}
        self.equity_history: dict[str, list[EquityHistoryPoint]] = {}
        self.ai_usage: dict[str, list[AIUsageRecord]] = {}
        self._lock = asyncio.Lock()

    async def health(self) -> bool:
        return True

    async def save_market(self, market: MarketSpec) -> None:
        self.markets[market.id] = market

    async def save_snapshot(self, snapshot: OrderBookSnapshot) -> None:
        self.snapshots.append(snapshot)

    async def save_evidence(self, evidence: list[EvidenceItem]) -> None:
        self.evidence.update({item.id: item for item in evidence})

    async def save_forecast(self, forecast: Forecast, account_id: str) -> None:
        self.forecasts[forecast.id] = forecast

    async def forecast_is_fresh(self, market_id: str, account_id: str, max_age: timedelta) -> bool:
        cutoff = utc_now() - max_age
        return any(
            forecast.market_id == market_id and forecast.created_at >= cutoff
            for forecast in self.forecasts.values()
        )

    async def save_risk_decision(
        self, intent: TradeIntent, decision: RiskDecision, account_id: str
    ) -> None:
        self.risk_events.append((intent, decision))

    async def reserve_intent(self, intent: TradeIntent) -> bool:
        async with self._lock:
            if intent.intent_hash in self.intents:
                return False
            self.intents[intent.intent_hash] = intent
            return True

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
    ) -> None:
        existing = self.signed_orders.get(intent.intent_hash)
        if existing is not None and existing[0] != signed_order_hash:
            raise RuntimeError("intent already has a different signed order")
        self.signed_orders[intent.intent_hash] = (
            signed_order_hash,
            payload_ciphertext,
            key_version,
            intent.account_id,
            fencing_token,
            order_type,
            expires_at,
        )
        self.unresolved_live_orders.add(intent.intent_hash)

    async def mark_order_submitting(
        self,
        intent_hash: str,
        account_id: str,
        fencing_token: int,
        *,
        control_version: int | None = None,
    ) -> None:
        async with self._lock:
            signed = self.signed_orders.get(intent_hash)
            if signed is None:
                raise RuntimeError("cannot submit an order before its signed payload is durable")
            if signed[3] != account_id or signed[4] != fencing_token:
                raise RuntimeError("signed order does not belong to the active fencing token")
            lease = self.leases.get(account_id)
            if (
                lease is None
                or lease.fencing_token != fencing_token
                or lease.expires_at <= utc_now()
            ):
                raise RuntimeError("worker lease expired before submission transition")
            control = self.controls.get(account_id)
            if (
                control_version is not None
                and (control is None or control.version != control_version)
            ):
                raise RuntimeError("runtime control changed before submission transition")
            self.unresolved_live_orders.add(intent_hash)

    async def save_execution(self, result: ExecutionResult, account_id: str) -> None:
        self.executions.append(result)
        if result.order_id:
            status = {
                "accepted": "submitted",
                "cancelled": "cancelled",
                "rejected": "rejected",
                "error": "unknown",
            }.get(result.status.value, result.status.value)
            current = self.durable_orders.get(result.order_id)
            if current not in {
                "matched",
                "mined",
                "confirmed",
                "cancelled",
                "expired",
                "failed",
            }:
                self.durable_orders[result.order_id] = status
        if result.order_id or result.status.value in {"rejected", "cancelled"}:
            self.unresolved_live_orders.discard(result.intent_hash)

    async def get_runtime_control(self, account_id: str) -> RuntimeControl:
        return self.controls.get(
            account_id,
            RuntimeControl(
                account_id=account_id,
                mode=TradingMode.PAPER,
                armed=False,
                kill_switch=True,
            ),
        )

    async def set_runtime_control(self, control: RuntimeControl) -> RuntimeControl:
        self.controls[control.account_id] = control
        return control

    async def arm_runtime_control(
        self,
        account_id: str,
        mode: str,
        armed_until: datetime,
        expected_version: int,
    ) -> RuntimeControl | None:
        async with self._lock:
            current = self.controls.get(
                account_id,
                RuntimeControl(
                    account_id=account_id,
                    mode=TradingMode.PAPER,
                    armed=False,
                    kill_switch=True,
                ),
            )
            now = utc_now()
            eligible_disarmed = bool(
                not current.armed
                and not current.accept_new_intents
                and current.kill_switch
                and current.armed_until is None
            )
            eligible_renewal = bool(
                current.armed
                and current.is_live_armed
                and current.mode is TradingMode(mode)
            )
            if (
                current.version != expected_version
                or current.cancellation_pending
                or armed_until <= now
                or armed_until > now + timedelta(minutes=15)
                or not (eligible_disarmed or eligible_renewal)
            ):
                return None
            control = RuntimeControl(
                account_id=account_id,
                mode=TradingMode(mode),
                armed=True,
                accept_new_intents=True,
                armed_until=armed_until,
                kill_switch=False,
                version=current.version + 1,
            )
            self.controls[account_id] = control
            return control

    async def expire_runtime_control(
        self,
        account_id: str,
        mode: str,
        expected_version: int,
    ) -> RuntimeControl | None:
        async with self._lock:
            current = self.controls.get(account_id)
            if (
                current is None
                or current.version != expected_version
                or current.mode is not TradingMode(mode)
                or not current.armed
                or current.armed_until is None
                or current.armed_until > utc_now()
            ):
                return None
            expired = RuntimeControl(
                account_id=account_id,
                mode=TradingMode(mode),
                armed=False,
                accept_new_intents=False,
                armed_until=None,
                kill_switch=True,
                cancellation_pending=True,
                version=current.version + 1,
            )
            self.controls[account_id] = expired
            return expired

    async def disarm_runtime_control(self, account_id: str, mode: str) -> RuntimeControl:
        async with self._lock:
            current = self.controls.get(
                account_id,
                RuntimeControl(
                    account_id=account_id,
                    mode=TradingMode.PAPER,
                    armed=False,
                    kill_switch=True,
                ),
            )
            control = RuntimeControl(
                account_id=account_id,
                mode=TradingMode(mode),
                armed=False,
                accept_new_intents=False,
                armed_until=None,
                kill_switch=True,
                cancellation_pending=TradingMode(mode) in {TradingMode.CANARY, TradingMode.LIVE},
                version=current.version + 1,
            )
            self.controls[account_id] = control
            return control

    async def acknowledge_runtime_cancellation(
        self, account_id: str, expected_version: int
    ) -> RuntimeControl | None:
        async with self._lock:
            current = self.controls.get(account_id)
            if (
                current is None
                or current.version != expected_version
                or not current.cancellation_pending
                or current.armed
                or not current.kill_switch
                or self.unresolved_live_orders
            ):
                return None
            acknowledged = current.model_copy(
                update={
                    "cancellation_pending": False,
                    "version": current.version + 1,
                    "updated_at": utc_now(),
                }
            )
            self.controls[account_id] = acknowledged
            return acknowledged

    async def claim_worker_lease(
        self, account_id: str, owner_id: str, ttl: timedelta
    ) -> WorkerLease | None:
        async with self._lock:
            now = utc_now()
            current = self.leases.get(account_id)
            if current is not None:
                if current.expires_at > now and current.owner_id != owner_id:
                    return None
            token = (
                current.fencing_token
                if current is not None and current.owner_id == owner_id
                else (current.fencing_token + 1 if current is not None else 1)
            )
            lease = WorkerLease(
                account_id=account_id,
                owner_id=owner_id,
                fencing_token=token,
                expires_at=now + ttl,
            )
            self.leases[account_id] = lease
            return lease

    async def validate_worker_lease(
        self, account_id: str, owner_id: str, fencing_token: int
    ) -> bool:
        lease = self.leases.get(account_id)
        return bool(
            lease
            and lease.owner_id == owner_id
            and lease.fencing_token == fencing_token
            and lease.expires_at > utc_now()
        )

    async def release_worker_lease(
        self, account_id: str, owner_id: str, fencing_token: int
    ) -> bool:
        async with self._lock:
            if not await self.validate_worker_lease(account_id, owner_id, fencing_token):
                return False
            self.leases.pop(account_id, None)
            return True

    async def has_unresolved_live_orders(self, account_id: str) -> bool:
        return bool(
            self.unresolved_live_orders
            or any(
                status in {"signed", "submitting", "unknown"}
                for status in self.durable_orders.values()
            )
        )

    async def record_equity_state(
        self,
        account_id: str,
        equity_usd: Decimal,
    ) -> EquityRiskState:
        today = utc_now().date()
        current = self.equity_states.get(account_id)
        if current is None:
            state = EquityRiskState(
                account_id=account_id,
                peak_equity_usd=equity_usd,
                latest_equity_usd=equity_usd,
                day_start_equity_usd=equity_usd,
                risk_day=today,
            )
        else:
            day_start = (
                current.latest_equity_usd
                if current.risk_day < today
                else current.day_start_equity_usd
            )
            state = EquityRiskState(
                account_id=account_id,
                peak_equity_usd=max(current.peak_equity_usd, equity_usd),
                latest_equity_usd=equity_usd,
                day_start_equity_usd=day_start,
                risk_day=today,
            )
        self.equity_states[account_id] = state
        return state

    async def record_equity_history(
        self,
        account_id: str,
        equity_usd: Decimal,
        source: str,
    ) -> None:
        if equity_usd < 0:
            raise ValueError("equity history must be non-negative")
        if not source.strip():
            raise ValueError("equity history source must be non-empty")
        self.equity_history.setdefault(account_id, []).append(
            EquityHistoryPoint(
                recorded_at=utc_now(),
                equity_usd=equity_usd,
                source=source,
            )
        )

    async def list_equity_history(
        self,
        account_id: str,
        limit: int,
    ) -> list[EquityHistoryPoint]:
        if limit < 1:
            raise ValueError("equity history limit must be positive")
        points = self.equity_history.get(account_id, [])
        return points[-limit:]

    async def record_ai_usage(self, account_id: str, usage: AIUsageRecord) -> None:
        if usage.total_tokens < 0:
            raise ValueError("AI usage tokens must be non-negative")
        self.ai_usage.setdefault(account_id, []).append(usage)

    async def list_ai_usage(
        self, account_id: str, limit: int
    ) -> list[AIUsageRecord]:
        if limit < 1:
            raise ValueError("AI usage limit must be positive")
        return self.ai_usage.get(account_id, [])[-limit:]

    async def realized_pnl_since(self, account_id: str, since: datetime) -> Decimal:
        return (await self.fill_ledger_snapshot(account_id, since)).realized_pnl_usd

    async def fill_ledger_snapshot(self, account_id: str, since: datetime) -> FillLedgerSnapshot:
        rows = [
            {
                "fill_key": (
                    f"{update.clob_trade_id}:"
                    f"{update.candidate_order_ids[0] if update.candidate_order_ids else ''}"
                ),
                "outcome_token_id": update.token_id,
                "condition_id": update.condition_id,
                "side": update.side.value,
                "price": update.price,
                "size": update.size,
                "fee_pusd": matched_taker_fee_usd(
                    size=update.size,
                    price=update.price,
                    fee_rate_bps=update.fee_rate_bps,
                    trader_side=update.trader_side,
                ),
                "settlement_status": update.status.removeprefix("TRADE_STATUS_"),
                "matched_at": update.matched_at,
                "created_at": update.updated_at,
            }
            for update in self.trade_updates
        ]
        activities = [
            {
                "activity_key": update.activity_key,
                "activity_type": update.activity_type,
                "condition_id": update.condition_id,
                "amount_usd": update.amount_usd,
                "occurred_at": update.occurred_at,
            }
            for update in self.account_activity_updates.values()
        ]
        return replay_fill_ledger(rows, since=since, activities=activities)

    async def event_ids_for_conditions(self, condition_ids: set[str]) -> dict[str, str]:
        wanted = {str(condition_id) for condition_id in condition_ids if condition_id}
        return {
            str(market.condition_id or market.id): str(market.event_id)
            for market in self.markets.values()
            if str(market.condition_id or market.id) in wanted and market.event_id
        }

    async def position_outcomes(
        self, positions: set[tuple[str, str]]
    ) -> dict[tuple[str, str], Outcome]:
        outcomes: dict[tuple[str, str], Outcome] = {}
        for market in self.markets.values():
            condition_id = str(market.condition_id or market.id)
            yes_key = (condition_id, market.yes_token_id)
            no_key = (condition_id, market.no_token_id)
            if yes_key in positions:
                outcomes[yes_key] = Outcome.YES
            if no_key in positions:
                outcomes[no_key] = Outcome.NO
        return outcomes

    async def durable_order_ids(self, candidate_order_ids: set[str], account_id: str) -> set[str]:
        return set(candidate_order_ids).intersection(self.durable_orders)

    async def pending_trade_ids(self, account_id: str) -> set[str]:
        response_ids = {
            str(trade_id)
            for execution in self.executions
            for trade_id in execution.raw.get("trade_ids", ())
            if trade_id
        }
        nonterminal = {
            update.clob_trade_id
            for update in self.trade_updates
            if update.status.removeprefix("TRADE_STATUS_")
            in {"MATCHED", "MATCHED_NOT_BROADCASTED", "MINED", "RETRYING"}
        }
        terminal = {
            update.clob_trade_id
            for update in self.trade_updates
            if update.status.removeprefix("TRADE_STATUS_") in {"CONFIRMED", "FAILED"}
        }
        return (response_ids | nonterminal) - terminal

    async def reconcile_order(self, update: UserOrderUpdate, account_id: str) -> None:
        self.order_updates.append(update)
        if update.clob_order_id in self.durable_orders:
            status = update.status.removeprefix("ORDER_STATUS_")
            mapped = {
                "LIVE": "live",
                "UNMATCHED": "live",
                "DELAYED": "submitted",
                "MATCHED": "matched",
                "CANCELED": "cancelled",
                "CANCELLED": "cancelled",
            }.get(status, "unknown")
            if Decimal("0") < update.size_matched < update.original_size:
                mapped = "partially_filled"
            current = self.durable_orders[update.clob_order_id]
            if current not in {"mined", "confirmed", "cancelled", "expired", "failed"}:
                self.durable_orders[update.clob_order_id] = mapped
            self.order_missing_confirmations.pop(update.clob_order_id, None)

    async def reconcile_open_order_snapshot(
        self, open_order_ids: set[str], account_id: str
    ) -> list[OrderReconcileTarget]:
        unresolved = {
            "submitted",
            "live",
            "partially_filled",
            "unknown",
            "cancel_pending",
        }
        return [
            OrderReconcileTarget(
                clob_order_id=order_id,
                status=status,
                missing_confirmations=self.order_missing_confirmations.get(order_id, 0),
            )
            for order_id, status in self.durable_orders.items()
            if status in unresolved and order_id not in open_order_ids
        ]

    async def confirm_orders_absent(self, clob_order_ids: set[str], account_id: str) -> None:
        unresolved = {
            "submitted",
            "live",
            "partially_filled",
            "unknown",
            "cancel_pending",
        }
        for order_id in clob_order_ids:
            status = self.durable_orders.get(order_id)
            if status not in unresolved:
                continue
            misses = self.order_missing_confirmations.get(order_id, 0) + 1
            self.order_missing_confirmations[order_id] = misses
            if status == "cancel_pending" or misses >= 2:
                self.durable_orders[order_id] = "cancelled"
                self.order_missing_confirmations.pop(order_id, None)
            else:
                self.durable_orders[order_id] = "unknown"

    async def reconcile_trade(self, update: UserTradeUpdate, account_id: str) -> None:
        key = (
            update.clob_trade_id,
            update.candidate_order_ids[0] if update.candidate_order_ids else "",
            update.token_id,
        )
        async with self._lock:
            for index, current in enumerate(self.trade_updates):
                current_key = (
                    current.clob_trade_id,
                    current.candidate_order_ids[0] if current.candidate_order_ids else "",
                    current.token_id,
                )
                if current_key == key:
                    current_status = current.status.removeprefix("TRADE_STATUS_")
                    incoming_status = update.status.removeprefix("TRADE_STATUS_")
                    ranks = {
                        "MATCHED_NOT_BROADCASTED": 0,
                        "MATCHED": 0,
                        "RETRYING": 1,
                        "MINED": 2,
                        "CONFIRMED": 3,
                        "FAILED": 3,
                    }
                    if current_status in {"CONFIRMED", "FAILED"} or ranks.get(
                        current_status, 0
                    ) > ranks.get(incoming_status, 0):
                        update = update.model_copy(
                            update={
                                "status": current.status,
                                "transaction_hash": (
                                    current.transaction_hash or update.transaction_hash
                                ),
                                "updated_at": max(current.updated_at, update.updated_at),
                            }
                        )
                    self.trade_updates[index] = update
                    break
            else:
                self.trade_updates.append(update)
            for order_id in update.candidate_order_ids:
                if order_id not in self.durable_orders:
                    continue
                status = update.status.removeprefix("TRADE_STATUS_")
                mapped = {
                    "MATCHED_NOT_BROADCASTED": "matched",
                    "MATCHED": "matched",
                    "RETRYING": "matched",
                    "MINED": "mined",
                    "CONFIRMED": "confirmed",
                    "FAILED": "failed",
                }.get(status, "matched")
                self.durable_orders[order_id] = mapped
                if mapped in {"confirmed", "failed"}:
                    self.order_missing_confirmations.pop(order_id, None)

    async def reconcile_account_activity(
        self, update: AccountActivityUpdate, account_id: str
    ) -> None:
        self.account_activity_updates[update.activity_key] = update

    async def reconcile_positions(
        self, positions: list[AccountPositionUpdate], account_id: str
    ) -> None:
        active_tokens = {position.token_id for position in positions}
        for token_id, previous in list(self.position_updates.items()):
            if token_id not in active_tokens and previous.size > 0:
                self.position_updates[token_id] = previous.model_copy(
                    update={"size": Decimal("0"), "cost_basis_usd": Decimal("0")}
                )
        for position in positions:
            self.position_updates[position.token_id] = position
