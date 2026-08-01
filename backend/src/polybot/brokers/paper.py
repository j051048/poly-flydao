from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from polybot.models import (
    ExecutionResult,
    ExecutionStatus,
    OrderBookSnapshot,
    PortfolioState,
    TradeIntent,
    utc_now,
)


class PaperBroker:
    """Depth-aware paper fills with partial fills and deterministic duplicate protection."""

    def __init__(self, bankroll_usd: Decimal):
        self.initial_bankroll = bankroll_usd
        self.cash = bankroll_usd
        self.positions: dict[str, Decimal] = {}
        self.position_costs: dict[str, Decimal] = {}
        self.event_exposure: dict[str, Decimal] = {}
        self.bucket_exposure: dict[str, Decimal] = {}
        self.token_events: dict[str, str] = {}
        self.token_buckets: dict[str, str] = {}
        self.token_condition_ids: dict[str, str] = {}
        self.token_market_ids: dict[str, str] = {}
        self.token_outcomes: dict[str, str] = {}
        self.seen_intents: set[str] = set()
        self.intent_history: list[str] = []
        self.realized_pnl = Decimal("0")
        self.peak_equity = bankroll_usd
        self.risk_day = utc_now().date()
        self.day_start_equity = bankroll_usd

    @classmethod
    def from_state(
        cls,
        bankroll_usd: Decimal,
        state: Mapping[str, Any] | None,
    ) -> PaperBroker:
        """Restore a bounded durable simulation state for a transient runtime.

        Invalid or incompatible state fails closed instead of silently resetting
        the tenant's paper bankroll and inventing a new performance history.
        """

        broker = cls(bankroll_usd)
        if not state:
            return broker
        if int(state.get("schema_version", 0)) != 1:
            raise ValueError("unsupported paper state schema")
        persisted_bankroll = _money(state.get("initial_bankroll"), "initial bankroll")
        if persisted_bankroll != bankroll_usd:
            raise ValueError("paper bankroll changed without an explicit reset")
        broker.cash = _money(state.get("cash"), "cash")
        broker.realized_pnl = _decimal(state.get("realized_pnl"), "realized pnl")
        broker.peak_equity = _money(state.get("peak_equity"), "peak equity")
        broker.day_start_equity = _money(state.get("day_start_equity"), "day start equity")
        try:
            broker.risk_day = date.fromisoformat(str(state.get("risk_day")))
        except ValueError as exc:
            raise ValueError("paper risk day is invalid") from exc
        positions = state.get("positions", [])
        if not isinstance(positions, list) or len(positions) > 500:
            raise ValueError("paper positions are invalid")
        for raw in positions:
            if not isinstance(raw, Mapping):
                raise ValueError("paper position is invalid")
            token_id = str(raw.get("token_id") or "")
            market_id = str(raw.get("market_id") or "")
            outcome = str(raw.get("outcome") or "")
            if not token_id or not market_id or outcome not in {"YES", "NO"}:
                raise ValueError("paper position identity is invalid")
            shares = _money(raw.get("shares"), "position shares")
            cost = _money(raw.get("cost"), "position cost")
            if shares <= 0:
                continue
            broker.positions[token_id] = shares
            broker.position_costs[token_id] = cost
            broker.token_market_ids[token_id] = market_id
            broker.token_outcomes[token_id] = outcome
            condition_id = str(raw.get("condition_id") or market_id)
            event_id = str(raw.get("event_id") or market_id)
            bucket = str(raw.get("bucket") or "other")
            broker.token_condition_ids[token_id] = condition_id
            broker.token_events[token_id] = event_id
            broker.token_buckets[token_id] = bucket
            broker.event_exposure[event_id] = (
                broker.event_exposure.get(event_id, Decimal("0")) + cost
            )
            broker.bucket_exposure[bucket] = broker.bucket_exposure.get(bucket, Decimal("0")) + cost
        seen_intents = state.get("seen_intents", [])
        if not isinstance(seen_intents, list) or len(seen_intents) > 2000:
            raise ValueError("paper intent history is invalid")
        if any(not isinstance(value, str) or not value for value in seen_intents):
            raise ValueError("paper intent history is invalid")
        broker.seen_intents = set(seen_intents)
        broker.intent_history = list(dict.fromkeys(seen_intents))
        equity = broker.cash + sum(broker.position_costs.values(), Decimal("0"))
        if broker.peak_equity < equity:
            raise ValueError("paper peak equity is below current equity")
        return broker

    def export_state(self) -> dict[str, Any]:
        """Return a secret-free, JSON-compatible state for fenced persistence."""

        positions = []
        for token_id, shares in sorted(self.positions.items()):
            if shares <= 0:
                continue
            positions.append(
                {
                    "token_id": token_id,
                    "market_id": self.token_market_ids[token_id],
                    "outcome": self.token_outcomes[token_id],
                    "shares": str(shares),
                    "cost": str(self.position_costs.get(token_id, Decimal("0"))),
                    "condition_id": self.token_condition_ids.get(token_id),
                    "event_id": self.token_events.get(token_id),
                    "bucket": self.token_buckets.get(token_id),
                }
            )
        return {
            "schema_version": 1,
            "initial_bankroll": str(self.initial_bankroll),
            "cash": str(self.cash),
            "realized_pnl": str(self.realized_pnl),
            "peak_equity": str(self.peak_equity),
            "day_start_equity": str(self.day_start_equity),
            "risk_day": self.risk_day.isoformat(),
            "positions": positions,
            "seen_intents": self.intent_history[-2000:],
        }

    async def portfolio_state(self) -> PortfolioState:
        gross = sum(self.position_costs.values(), Decimal("0"))
        equity = self.cash + gross
        today = utc_now().date()
        if today > self.risk_day:
            self.risk_day = today
            self.day_start_equity = equity
            self.realized_pnl = Decimal("0")
        self.peak_equity = max(self.peak_equity, equity)
        return PortfolioState(
            bankroll_usd=self.initial_bankroll,
            cash_usd=self.cash,
            gross_exposure_usd=gross,
            event_exposure_usd=dict(self.event_exposure),
            bucket_exposure_usd=dict(self.bucket_exposure),
            token_positions=dict(self.positions),
            token_condition_ids=dict(self.token_condition_ids),
            token_event_ids=dict(self.token_events),
            realized_pnl_today_usd=self.realized_pnl,
            peak_equity_usd=self.peak_equity,
            equity_usd=equity,
            day_start_equity_usd=self.day_start_equity,
        )

    async def submit(self, intent: TradeIntent, book: OrderBookSnapshot) -> ExecutionResult:
        if intent.intent_hash in self.seen_intents:
            return ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.DUPLICATE,
                message="paper broker rejected a duplicate intent",
            )
        self.seen_intents.add(intent.intent_hash)
        self.intent_history.append(intent.intent_hash)
        if len(self.intent_history) > 2000:
            expired = self.intent_history.pop(0)
            self.seen_intents.discard(expired)

        remaining = intent.size
        filled = Decimal("0")
        cost = Decimal("0")
        if intent.side.value == "BUY":
            for level in sorted(book.asks, key=lambda item: item.price):
                if level.price > intent.price or remaining <= 0:
                    break
                take = min(level.size, remaining)
                filled += take
                cost += take * level.price
                remaining -= take
        else:
            available = self.positions.get(intent.token_id, Decimal("0"))
            if intent.size > available:
                return ExecutionResult(
                    intent_hash=intent.intent_hash,
                    status=ExecutionStatus.REJECTED,
                    message="paper sell exceeds position",
                )
            for level in sorted(book.bids, key=lambda item: item.price, reverse=True):
                if level.price < intent.price or remaining <= 0:
                    break
                take = min(level.size, remaining)
                filled += take
                cost += take * level.price
                remaining -= take
        if filled <= 0:
            return ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.REJECTED,
                message="limit did not cross available paper depth",
            )

        # Intent notional includes the deterministic fee estimate. Scale it for partial fills.
        all_in_cost = intent.notional_usd * (filled / intent.size)
        if intent.side.value == "BUY" and all_in_cost > self.cash:
            return ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.REJECTED,
                message="paper cash changed before fill",
            )
        event_key = intent.event_id or intent.market_id
        if intent.side.value == "BUY":
            self.cash -= all_in_cost
            self.positions[intent.token_id] = (
                self.positions.get(intent.token_id, Decimal("0")) + filled
            )
            self.position_costs[intent.token_id] = (
                self.position_costs.get(intent.token_id, Decimal("0")) + all_in_cost
            )
            self.token_events[intent.token_id] = event_key
            self.token_buckets[intent.token_id] = intent.bucket
            # TradeIntent carries the Gamma market id rather than condition id.
            # Static/offline market sources accept either identifier, while the
            # live broker supplies the actual condition id from Position.
            self.token_condition_ids[intent.token_id] = intent.market_id
            self.token_market_ids[intent.token_id] = intent.market_id
            self.token_outcomes[intent.token_id] = intent.outcome.value
            self.event_exposure[event_key] = (
                self.event_exposure.get(event_key, Decimal("0")) + all_in_cost
            )
            self.bucket_exposure[intent.bucket] = (
                self.bucket_exposure.get(intent.bucket, Decimal("0")) + all_in_cost
            )
        else:
            before_size = self.positions[intent.token_id]
            basis = self.position_costs.get(intent.token_id, Decimal("0"))
            basis_released = basis * filled / before_size
            self.cash += all_in_cost
            self.realized_pnl += all_in_cost - basis_released
            self.positions[intent.token_id] = before_size - filled
            self.position_costs[intent.token_id] = max(Decimal("0"), basis - basis_released)
            stored_event = self.token_events.get(intent.token_id, event_key)
            stored_bucket = self.token_buckets.get(intent.token_id, intent.bucket)
            self.event_exposure[stored_event] = max(
                Decimal("0"),
                self.event_exposure.get(stored_event, Decimal("0")) - basis_released,
            )
            self.bucket_exposure[stored_bucket] = max(
                Decimal("0"),
                self.bucket_exposure.get(stored_bucket, Decimal("0")) - basis_released,
            )
            if self.positions[intent.token_id] <= 0:
                self.token_condition_ids.pop(intent.token_id, None)
                self.token_events.pop(intent.token_id, None)
                self.token_buckets.pop(intent.token_id, None)
                self.token_market_ids.pop(intent.token_id, None)
                self.token_outcomes.pop(intent.token_id, None)
        return ExecutionResult(
            intent_hash=intent.intent_hash,
            status=ExecutionStatus.PAPER_FILLED,
            order_id=f"paper-{intent.intent_hash[:20]}",
            filled_size=filled,
            average_price=cost / filled,
            message="paper fill; unresolved positions remain valued at cost",
        )

    async def cancel_all(self, reason: str) -> bool:
        # There are no resting paper orders; this keeps kill-switch handling uniform.
        return True

    async def submit_batch(
        self,
        submissions: Sequence[tuple[TradeIntent, OrderBookSnapshot]],
    ) -> list[ExecutionResult]:
        # Sequential fills make the non-atomic semantics explicit in research.
        return [await self.submit(intent, book) for intent, book in submissions]

    async def cancel_order(self, order_id: str, reason: str) -> bool:
        return True

    async def cancel_orders(self, order_ids: Sequence[str], reason: str) -> bool:
        return True

    async def redeem_resolved(self) -> int:
        return 0


def _decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"paper {label} is invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"paper {label} must be finite")
    return parsed


def _money(value: Any, label: str) -> Decimal:
    parsed = _decimal(value, label)
    if parsed < 0:
        raise ValueError(f"paper {label} must be non-negative")
    return parsed


class ShadowBroker:
    def __init__(self, bankroll_usd: Decimal):
        self.bankroll = bankroll_usd

    async def portfolio_state(self) -> PortfolioState:
        return PortfolioState(bankroll_usd=self.bankroll, cash_usd=self.bankroll)

    async def submit(self, intent: TradeIntent, book: OrderBookSnapshot) -> ExecutionResult:
        return ExecutionResult(
            intent_hash=intent.intent_hash,
            status=ExecutionStatus.SHADOWED,
            message="validated intent recorded without an order",
        )

    async def cancel_all(self, reason: str) -> bool:
        return True

    async def submit_batch(
        self,
        submissions: Sequence[tuple[TradeIntent, OrderBookSnapshot]],
    ) -> list[ExecutionResult]:
        return [await self.submit(intent, book) for intent, book in submissions]

    async def cancel_order(self, order_id: str, reason: str) -> bool:
        return True

    async def cancel_orders(self, order_ids: Sequence[str], reason: str) -> bool:
        return True

    async def redeem_resolved(self) -> int:
        return 0


class ControlPlaneBroker:
    """Signer-free live control-plane stub; only the leased worker may trade."""

    def __init__(self, bankroll_usd: Decimal):
        self.bankroll = bankroll_usd

    async def portfolio_state(self) -> PortfolioState:
        raise RuntimeError("the signer-free control plane cannot read a trading portfolio")

    async def submit(self, intent: TradeIntent, book: OrderBookSnapshot) -> ExecutionResult:
        return ExecutionResult(
            intent_hash=intent.intent_hash,
            status=ExecutionStatus.REJECTED,
            message="the signer-free control plane cannot submit orders",
        )

    async def cancel_all(self, reason: str) -> bool:
        # The durable kill switch is watched by the leased signer worker.
        return False

    async def submit_batch(
        self,
        submissions: Sequence[tuple[TradeIntent, OrderBookSnapshot]],
    ) -> list[ExecutionResult]:
        return [await self.submit(intent, book) for intent, book in submissions]

    async def cancel_order(self, order_id: str, reason: str) -> bool:
        return False

    async def cancel_orders(self, order_ids: Sequence[str], reason: str) -> bool:
        return False

    async def redeem_resolved(self) -> int:
        return 0
