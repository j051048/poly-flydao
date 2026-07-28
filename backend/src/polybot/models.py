from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from polybot.config import TradingMode


def utc_now() -> datetime:
    return datetime.now(UTC)


UNCLASSIFIED_EVENT_KEY = "__unclassified_event__"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Outcome(StrEnum):
    YES = "YES"
    NO = "NO"


class MarketSpec(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    condition_id: str | None = None
    event_id: str | None = None
    slug: str | None = None
    event_slug: str | None = None
    question: str = Field(min_length=1)
    description: str = ""
    category: str = "other"
    tags: tuple[str, ...] = ()
    resolution_rules: str = ""
    resolution_source: str | None = None
    yes_token_id: str = Field(min_length=1)
    no_token_id: str = Field(min_length=1)
    yes_label: str = Field(default="Yes", min_length=1)
    no_label: str = Field(default="No", min_length=1)
    active: bool = True
    closed: bool = False
    accepting_orders: bool = True
    neg_risk: bool = False
    liquidity_usd: Decimal = Field(default=Decimal("0"), ge=0)
    volume_24h_usd: Decimal = Field(default=Decimal("0"), ge=0)
    minimum_order_size: Decimal = Field(default=Decimal("1"), gt=0)
    tick_size: Decimal = Field(default=Decimal("0.01"), gt=0)
    fees_enabled: bool = False
    fee_rate: Decimal = Field(default=Decimal("0"), ge=0)
    fee_exponent: Decimal = Field(default=Decimal("1"), gt=0)
    fee_taker_only: bool = True
    maker_rebate_rate: Decimal = Field(default=Decimal("0"), ge=0)
    start_at: datetime | None = None
    end_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_enabled_fee_schedule(self) -> MarketSpec:
        if self.fees_enabled and self.fee_rate <= 0:
            raise ValueError("fee-enabled market is missing a positive fee rate")
        if self.yes_token_id == self.no_token_id:
            raise ValueError("YES and NO token ids must differ")
        if self.yes_label.casefold() == self.no_label.casefold():
            raise ValueError("binary outcome labels must differ")
        if self.start_at and self.end_at and self.start_at >= self.end_at:
            raise ValueError("market start_at must precede end_at")
        return self


class BookLevel(BaseModel):
    price: Decimal = Field(gt=0, lt=1)
    size: Decimal = Field(gt=0)


class OrderBookSnapshot(BaseModel):
    token_id: str = Field(min_length=1)
    market_id: str = Field(min_length=1)
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    tick_size: Decimal = Field(default=Decimal("0.01"), gt=0)
    minimum_order_size: Decimal = Field(default=Decimal("1"), gt=0)
    captured_at: datetime = Field(default_factory=utc_now)

    @property
    def best_bid(self) -> Decimal | None:
        return max((level.price for level in self.bids), default=None)

    @property
    def best_ask(self) -> Decimal | None:
        return min((level.price for level in self.asks), default=None)


class EvidenceItem(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    market_id: str
    title: str
    summary: str
    source_url: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime = Field(default_factory=utc_now)
    reliability: Decimal = Field(default=Decimal("0.5"), ge=0, le=1)
    content_hash: str | None = None

    @model_validator(mode="after")
    def derive_hash(self) -> EvidenceItem:
        if not self.content_hash:
            raw = f"{self.title}\n{self.summary}\n{self.source_url or ''}".encode()
            self.content_hash = hashlib.sha256(raw).hexdigest()
        return self


class Forecast(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    market_id: str
    probability_yes: Decimal = Field(ge=0, le=1)
    probability_low: Decimal = Field(ge=0, le=1)
    probability_high: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    model: str
    prompt_version: str = "forecast-v1"
    rationale: str = ""
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def ordered_interval(self) -> Forecast:
        if not self.probability_low <= self.probability_yes <= self.probability_high:
            raise ValueError("probability interval must contain probability_yes")
        return self


class ForecastRequest(BaseModel):
    market: MarketSpec
    evidence: list[EvidenceItem] = Field(default_factory=list)
    as_of: datetime = Field(default_factory=utc_now)
    perspective: str = "independent base-rate forecaster"


class PortfolioState(BaseModel):
    bankroll_usd: Decimal
    cash_usd: Decimal
    gross_exposure_usd: Decimal = Decimal("0")
    event_exposure_usd: dict[str, Decimal] = Field(default_factory=dict)
    bucket_exposure_usd: dict[str, Decimal] = Field(default_factory=dict)
    token_positions: dict[str, Decimal] = Field(default_factory=dict)
    # The Data API identifies a held outcome by token while Gamma market
    # discovery is keyed by condition. Keep the relationship explicit so a
    # risk exit does not depend on the market appearing in a top-N scan.
    token_condition_ids: dict[str, str] = Field(default_factory=dict)
    token_event_ids: dict[str, str] = Field(default_factory=dict)
    realized_pnl_today_usd: Decimal = Decimal("0")
    peak_equity_usd: Decimal | None = None
    equity_usd: Decimal | None = None
    day_start_equity_usd: Decimal | None = None

    @property
    def daily_equity_pnl_usd(self) -> Decimal:
        if self.equity_usd is None or self.day_start_equity_usd is None:
            return Decimal("0")
        return self.equity_usd - self.day_start_equity_usd

    @property
    def daily_risk_pnl_usd(self) -> Decimal:
        """Conservative daily PnL used by hard risk gates.

        Fill-ledger PnL catches realized execution losses. Equity delta also
        catches mark-to-market and resolution/redemption losses, so the more
        adverse value is authoritative.
        """

        if self.equity_usd is None or self.day_start_equity_usd is None:
            return self.realized_pnl_today_usd
        return min(self.realized_pnl_today_usd, self.daily_equity_pnl_usd)

    @property
    def daily_loss_basis_usd(self) -> Decimal:
        if self.day_start_equity_usd is None or self.day_start_equity_usd <= 0:
            return self.bankroll_usd
        return min(self.bankroll_usd, self.day_start_equity_usd)

    @property
    def drawdown_pct(self) -> Decimal:
        if not self.peak_equity_usd or self.equity_usd is None or self.peak_equity_usd <= 0:
            return Decimal("0")
        return max(
            Decimal("0"),
            (self.peak_equity_usd - self.equity_usd) / self.peak_equity_usd,
        )


class TradeCandidate(BaseModel):
    market_id: str
    event_id: str | None
    bucket: str
    token_id: str
    outcome: Outcome
    side: Side = Side.BUY
    conservative_probability: Decimal = Field(ge=0, le=1)
    expected_price: Decimal = Field(gt=0, lt=1)
    limit_price: Decimal = Field(gt=0, lt=1)
    size: Decimal = Field(gt=0)
    notional_usd: Decimal = Field(gt=0)
    fee_estimate_usd: Decimal = Field(ge=0)
    edge_after_costs: Decimal
    strategy: str = "ai_value_v1"
    forecast_id: str | None
    decision_key: str | None = None
    book_captured_at: datetime


class TradeIntent(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    intent_hash: str
    account_id: str
    run_id: str
    mode: TradingMode
    market_id: str
    event_id: str | None
    bucket: str
    token_id: str
    outcome: Outcome
    side: Side
    price: Decimal
    size: Decimal
    notional_usd: Decimal
    edge_after_costs: Decimal
    forecast_id: str | None
    decision_key: str | None = None
    strategy: str
    post_only: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def from_candidate(
        cls,
        candidate: TradeCandidate,
        *,
        account_id: str,
        run_id: str,
        mode: TradingMode,
        post_only: bool = False,
    ) -> TradeIntent:
        canonical: dict[str, Any] = {
            "account_id": account_id,
            "mode": mode.value,
            "market_id": candidate.market_id,
            "token_id": candidate.token_id,
            "outcome": candidate.outcome.value,
            "side": candidate.side.value,
            "price": str(candidate.limit_price),
            "size": str(candidate.size),
            "strategy": candidate.strategy,
            "forecast_id": candidate.forecast_id,
            "decision_key": candidate.decision_key,
        }
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return cls(
            intent_hash=digest,
            account_id=account_id,
            run_id=run_id,
            mode=mode,
            market_id=candidate.market_id,
            event_id=candidate.event_id,
            bucket=candidate.bucket,
            token_id=candidate.token_id,
            outcome=candidate.outcome,
            side=candidate.side,
            price=candidate.limit_price,
            size=candidate.size,
            notional_usd=candidate.notional_usd,
            edge_after_costs=candidate.edge_after_costs,
            forecast_id=candidate.forecast_id,
            decision_key=candidate.decision_key,
            strategy=candidate.strategy,
            post_only=post_only,
        )


class LiquidityRole(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


class OrderLegPurpose(StrEnum):
    PAIR_ENTRY = "pair_entry"
    IMBALANCE_HEDGE = "imbalance_hedge"
    DIRECTIONAL_OVERLAY = "directional_overlay"


class OrderLegStatus(StrEnum):
    PLANNED = "planned"
    SUBMITTING = "submitting"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


class OrderGroupStatus(StrEnum):
    PLANNED = "planned"
    SUBMITTING = "submitting"
    WORKING = "working"
    IMBALANCED = "imbalanced"
    CANCELLING = "cancelling"
    HEDGING = "hedging"
    PAIRED = "paired"
    CANCELLED = "cancelled"
    FROZEN = "frozen"
    FAILED = "failed"


TERMINAL_ORDER_LEG_STATUSES = frozenset(
    {
        OrderLegStatus.FILLED,
        OrderLegStatus.CANCELLED,
        OrderLegStatus.REJECTED,
        OrderLegStatus.FAILED,
    }
)


class OrderLeg(BaseModel):
    """Durable leg state for a non-atomic pair order group."""

    id: UUID = Field(default_factory=uuid4)
    group_id: UUID
    outcome: Outcome
    token_id: str = Field(min_length=1)
    side: Side = Side.BUY
    purpose: OrderLegPurpose = OrderLegPurpose.PAIR_ENTRY
    liquidity_role: LiquidityRole = LiquidityRole.MAKER
    post_only: bool = True
    price: Decimal = Field(gt=0, lt=1)
    size: Decimal = Field(gt=0)
    filled_size: Decimal = Field(default=Decimal("0"), ge=0)
    average_fill_price: Decimal | None = Field(default=None, gt=0, lt=1)
    fee_paid_usd: Decimal = Field(default=Decimal("0"), ge=0)
    status: OrderLegStatus = OrderLegStatus.PLANNED
    clob_order_id: str | None = None
    deadline_at: datetime
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_execution_shape(self) -> OrderLeg:
        if self.filled_size > self.size:
            raise ValueError("filled_size cannot exceed order leg size")
        if self.filled_size > 0 and self.average_fill_price is None:
            raise ValueError("filled legs require an average fill price")
        if self.filled_size == 0 and self.average_fill_price is not None:
            raise ValueError("unfilled legs cannot have an average fill price")
        if self.liquidity_role is LiquidityRole.MAKER and not self.post_only:
            raise ValueError("maker legs must be post-only")
        if self.liquidity_role is LiquidityRole.TAKER and self.post_only:
            raise ValueError("taker legs cannot be post-only")
        if self.status is OrderLegStatus.FILLED and self.filled_size != self.size:
            raise ValueError("filled status requires the full leg size")
        if self.status in {OrderLegStatus.OPEN, OrderLegStatus.PARTIALLY_FILLED}:
            if not self.clob_order_id:
                raise ValueError("working order legs require a CLOB order id")
        return self

    @property
    def remaining_size(self) -> Decimal:
        return max(Decimal("0"), self.size - self.filled_size)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_ORDER_LEG_STATUSES


class OrderGroup(BaseModel):
    """Durable group state; two CLOB orders are never assumed to be atomic."""

    id: UUID = Field(default_factory=uuid4)
    account_id: str = Field(min_length=1)
    trading_wallet_id: str | None = None
    market_id: str = Field(min_length=1)
    condition_id: str | None = None
    strategy: str = "pair_accumulator_v1"
    target_pair_size: Decimal = Field(gt=0)
    paired_size: Decimal = Field(default=Decimal("0"), ge=0)
    directional_yes_size: Decimal = Field(default=Decimal("0"), ge=0)
    directional_no_size: Decimal = Field(default=Decimal("0"), ge=0)
    expected_net_edge_usd: Decimal
    leg_deadline_at: datetime
    status: OrderGroupStatus = OrderGroupStatus.PLANNED
    research_only: bool = True
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_inventory_shape(self) -> OrderGroup:
        if self.directional_yes_size > 0 and self.directional_no_size > 0:
            raise ValueError("directional inventory cannot point both YES and NO")
        if self.paired_size > self.target_pair_size:
            raise ValueError("paired_size cannot exceed target_pair_size")
        return self


class OrderPlan(BaseModel):
    """Research plan produced before any durable or exchange-side mutation."""

    group: OrderGroup
    legs: tuple[OrderLeg, OrderLeg]
    base_cost_usd: Decimal = Field(ge=0)
    maker_fee_usd: Decimal = Field(default=Decimal("0"), ge=0)
    taker_hedge_fee_buffer_usd: Decimal = Field(default=Decimal("0"), ge=0)
    leg_risk_buffer_usd: Decimal = Field(default=Decimal("0"), ge=0)
    capital_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    expected_payout_usd: Decimal = Field(gt=0)
    enabled: bool = False
    research_only: bool = True
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_pair_plan(self) -> OrderPlan:
        first, second = self.legs
        if first.group_id != self.group.id or second.group_id != self.group.id:
            raise ValueError("all order legs must belong to the plan's group")
        if {first.outcome, second.outcome} != {Outcome.YES, Outcome.NO}:
            raise ValueError("a pair entry plan requires one YES and one NO leg")
        if first.size != second.size or first.size != self.group.target_pair_size:
            raise ValueError("pair entry legs must use identical share sizes")
        if first.purpose is not OrderLegPurpose.PAIR_ENTRY:
            raise ValueError("initial pair legs must use pair_entry purpose")
        if second.purpose is not OrderLegPurpose.PAIR_ENTRY:
            raise ValueError("initial pair legs must use pair_entry purpose")
        if self.group.research_only != self.research_only:
            raise ValueError("plan and group research_only flags must agree")
        return self

    @property
    def net_edge_usd(self) -> Decimal:
        return (
            self.expected_payout_usd
            - self.base_cost_usd
            - self.maker_fee_usd
            - self.taker_hedge_fee_buffer_usd
            - self.leg_risk_buffer_usd
            - self.capital_cost_usd
        )

    @property
    def live_eligible(self) -> bool:
        # P2 remains intentionally disconnected from the production engine.
        return self.enabled and not self.research_only


class RiskDecision(BaseModel):
    approved: bool
    codes: list[str] = Field(default_factory=list)
    details: dict[str, str] = Field(default_factory=dict)


class ExecutionStatus(StrEnum):
    PAPER_FILLED = "paper_filled"
    SHADOWED = "shadowed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    CANCELLED = "cancelled"
    ERROR = "error"


class ExecutionResult(BaseModel):
    intent_hash: str
    status: ExecutionStatus
    order_id: str | None = None
    filled_size: Decimal = Decimal("0")
    average_price: Decimal | None = None
    message: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class RuntimeControl(BaseModel):
    account_id: str
    mode: TradingMode = TradingMode.PAPER
    armed: bool = False
    accept_new_intents: bool = False
    armed_until: datetime | None = None
    kill_switch: bool = True
    cancellation_pending: bool = False
    version: int = 1
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_gate_shape(self) -> RuntimeControl:
        if self.armed:
            if (
                not self.accept_new_intents
                or self.kill_switch
                or self.cancellation_pending
                or self.armed_until is None
                or self.mode not in {TradingMode.CANARY, TradingMode.LIVE}
            ):
                raise ValueError("armed runtime control has an inconsistent gate shape")
        elif self.accept_new_intents or self.armed_until is not None:
            raise ValueError("disarmed runtime control cannot accept intents or retain an expiry")
        return self

    @property
    def is_live_armed(self) -> bool:
        return bool(
            self.armed
            and self.accept_new_intents
            and not self.kill_switch
            and not self.cancellation_pending
            and self.armed_until
            and self.armed_until > utc_now()
            and self.mode in {TradingMode.CANARY, TradingMode.LIVE}
        )


class EquityRiskState(BaseModel):
    account_id: str
    peak_equity_usd: Decimal = Field(ge=0)
    latest_equity_usd: Decimal = Field(ge=0)
    day_start_equity_usd: Decimal = Field(ge=0)
    risk_day: date


class WorkerLease(BaseModel):
    account_id: str
    owner_id: str
    fencing_token: int = Field(gt=0)
    expires_at: datetime


class EngineCycleResult(BaseModel):
    run_id: str
    mode: TradingMode
    markets_scanned: int = 0
    forecasts_created: int = 0
    candidates_created: int = 0
    intents_approved: int = 0
    executions: list[ExecutionResult] = Field(default_factory=list)
    skipped: dict[str, int] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    def skip(self, code: str) -> None:
        self.skipped[code] = self.skipped.get(code, 0) + 1


class ForecastPayload(BaseModel):
    probability_yes: Decimal = Field(ge=0, le=1)
    probability_low: Decimal = Field(ge=0, le=1)
    probability_high: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    evidence_for: list[str]
    evidence_against: list[str]
    assumptions: list[str]
    invalidation_conditions: list[str]
    source_ids: list[str]
    rationale: str

    @field_validator("rationale")
    @classmethod
    def keep_rationale_compact(cls, value: str) -> str:
        return value[:2000]

    @model_validator(mode="after")
    def validate_interval(self) -> ForecastPayload:
        if not self.probability_low <= self.probability_yes <= self.probability_high:
            raise ValueError("probability interval is invalid")
        return self


class EvidenceSearchItem(BaseModel):
    title: str
    summary: str
    source_url: str
    published_at: datetime | None = None
    reliability: Decimal = Field(default=Decimal("0.5"), ge=0, le=1)


class EvidenceSearchPayload(BaseModel):
    items: list[EvidenceSearchItem] = Field(default_factory=list, max_length=12)
    search_summary: str = ""


class UserOrderUpdate(BaseModel):
    clob_order_id: str
    condition_id: str
    token_id: str
    side: Side
    price: Decimal
    original_size: Decimal
    size_matched: Decimal
    status: str
    order_type: str = "GTC"
    outcome: str | None = None
    event_type: str = "RECONCILE"
    occurred_at: datetime = Field(default_factory=utc_now)
    raw: dict[str, Any] = Field(default_factory=dict)


class OrderReconcileTarget(BaseModel):
    clob_order_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    missing_confirmations: int = Field(default=0, ge=0)


class UserTradeUpdate(BaseModel):
    clob_trade_id: str
    candidate_order_ids: list[str] = Field(min_length=1)
    condition_id: str
    token_id: str
    side: Side
    trader_side: str | None = None
    price: Decimal = Field(gt=0, lt=1)
    size: Decimal = Field(gt=0)
    outcome: str | None = None
    status: str
    fee_rate_bps: Decimal = Field(default=Decimal("0"), ge=0)
    transaction_hash: str | None = None
    matched_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    raw: dict[str, Any] = Field(default_factory=dict)


class AccountActivityUpdate(BaseModel):
    activity_key: str = Field(min_length=16)
    activity_type: str = Field(min_length=1)
    condition_id: str | None = None
    amount_usd: Decimal = Field(default=Decimal("0"), ge=0)
    transaction_hash: str | None = None
    occurred_at: datetime = Field(default_factory=utc_now)
    raw: dict[str, Any] = Field(default_factory=dict)


class AccountPositionUpdate(BaseModel):
    condition_id: str
    token_id: str
    outcome: Outcome
    size: Decimal = Decimal("0")
    average_entry_price: Decimal | None = None
    cost_basis_usd: Decimal = Decimal("0")
    realized_pnl_usd: Decimal = Decimal("0")
    mark_price: Decimal | None = None
    unrealized_pnl_usd: Decimal = Decimal("0")
    as_of: datetime = Field(default_factory=utc_now)
