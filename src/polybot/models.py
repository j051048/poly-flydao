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
    question: str = Field(min_length=1)
    description: str = ""
    category: str = "other"
    resolution_rules: str = ""
    resolution_source: str | None = None
    yes_token_id: str = Field(min_length=1)
    no_token_id: str = Field(min_length=1)
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
    end_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def require_enabled_fee_schedule(self) -> MarketSpec:
        if self.fees_enabled and self.fee_rate <= 0:
            raise ValueError("fee-enabled market is missing a positive fee rate")
        if self.yes_token_id == self.no_token_id:
            raise ValueError("YES and NO token ids must differ")
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
