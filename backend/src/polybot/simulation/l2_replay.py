from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from polybot.models import BookLevel, Side


class L2BookEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_type: Literal["book"] = "book"
    occurred_at: datetime
    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()
    sequence: int = Field(ge=0)


class L2TradeEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_type: Literal["trade"] = "trade"
    occurred_at: datetime
    aggressor_side: Side
    price: Decimal = Field(gt=0, lt=1)
    size: Decimal = Field(gt=0)
    sequence: int = Field(ge=0)


L2Event = Annotated[L2BookEvent | L2TradeEvent, Field(discriminator="event_type")]


class ReplayOrderRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str = Field(min_length=1)
    side: Side
    price: Decimal = Field(gt=0, lt=1)
    size: Decimal = Field(gt=0)
    submitted_at: datetime
    submit_latency: timedelta = Field(default=timedelta(milliseconds=100), ge=timedelta(0))
    cancel_latency: timedelta = Field(default=timedelta(milliseconds=100), ge=timedelta(0))
    post_only: Literal[True] = True


class ReplayCancelRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str = Field(min_length=1)
    requested_at: datetime


class ReplayOrderStatus(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    POST_ONLY_REJECTED = "post_only_rejected"


class ReplayFill(BaseModel):
    order_id: str
    occurred_at: datetime
    price: Decimal
    size: Decimal
    maker: bool = True


class ReplayOrderResult(BaseModel):
    order_id: str
    status: ReplayOrderStatus
    filled_size: Decimal = Field(ge=0)
    remaining_size: Decimal = Field(ge=0)
    queue_ahead_remaining: Decimal = Field(ge=0)
    cancel_requested_at: datetime | None = None
    cancel_effective_at: datetime | None = None


class L2ReplayResult(BaseModel):
    engine: Literal["event_l2_v1"] = "event_l2_v1"
    orders: tuple[ReplayOrderResult, ...]
    fills: tuple[ReplayFill, ...]
    queue_ahead_modelled: Literal[True] = True
    partial_fills_modelled: Literal[True] = True
    cancel_races_modelled: Literal[True] = True
    latency_modelled: Literal[True] = True
    complete_depth: bool = False
    verified_exchange_sequence: bool = False

    @property
    def live_gate_eligible(self) -> bool:
        """Eligible as one input to a live gate, never a profitability guarantee."""

        return self.complete_depth and self.verified_exchange_sequence


class L2ReplayScenario(BaseModel):
    """Portable, timestamped input contract for the P2 research CLI."""

    model_config = ConfigDict(frozen=True)

    events: list[L2Event]
    orders: list[ReplayOrderRequest]
    cancellations: list[ReplayCancelRequest] = Field(default_factory=list)
    complete_depth: bool = False
    verified_exchange_sequence: bool = False

    def replay(self) -> L2ReplayResult:
        return L2ReplayEngine(
            complete_depth=self.complete_depth,
            verified_exchange_sequence=self.verified_exchange_sequence,
        ).run(
            events=self.events,
            orders=self.orders,
            cancellations=self.cancellations,
        )


class _ReplayOrder(BaseModel):
    request: ReplayOrderRequest
    status: ReplayOrderStatus = ReplayOrderStatus.PENDING
    active_at: datetime
    filled_size: Decimal = Decimal("0")
    queue_ahead: Decimal = Decimal("0")
    cancel_requested_at: datetime | None = None
    cancel_effective_at: datetime | None = None

    @property
    def remaining(self) -> Decimal:
        return self.request.size - self.filled_size


class L2ReplayEngine:
    """Deterministic maker replay with price-time queue and cancellation races."""

    def __init__(
        self,
        *,
        complete_depth: bool = False,
        verified_exchange_sequence: bool = False,
    ):
        self.complete_depth = complete_depth
        self.verified_exchange_sequence = verified_exchange_sequence

    def run(
        self,
        *,
        events: list[L2Event],
        orders: list[ReplayOrderRequest],
        cancellations: list[ReplayCancelRequest] | None = None,
    ) -> L2ReplayResult:
        self._validate_sequences(events)
        by_id: dict[str, _ReplayOrder] = {}
        for request in orders:
            if request.order_id in by_id:
                raise ValueError("duplicate replay order id")
            by_id[request.order_id] = _ReplayOrder(
                request=request,
                active_at=request.submitted_at + request.submit_latency,
            )
        for cancellation in cancellations or []:
            order = by_id.get(cancellation.order_id)
            if order is None:
                raise KeyError(f"cancel references unknown order {cancellation.order_id}")
            if cancellation.requested_at < order.request.submitted_at:
                raise ValueError("cancel cannot precede order submission")
            order.cancel_requested_at = cancellation.requested_at
            order.cancel_effective_at = (
                cancellation.requested_at + order.request.cancel_latency
            )

        timeline: list[tuple[datetime, int, int, str, object]] = []
        for index, order in enumerate(by_id.values()):
            timeline.append((order.active_at, 1, index, "activate", order.request.order_id))
            if order.cancel_effective_at is not None:
                timeline.append(
                    (
                        order.cancel_effective_at,
                        2,
                        index,
                        "cancel",
                        order.request.order_id,
                    )
                )
        for index, event in enumerate(events):
            priority = 0 if isinstance(event, L2BookEvent) else 3
            timeline.append((event.occurred_at, priority, index, "market", event))
        timeline.sort(key=lambda item: (item[0], item[1], item[2]))

        bids: dict[Decimal, Decimal] = {}
        asks: dict[Decimal, Decimal] = {}
        fills: list[ReplayFill] = []
        for _occurred_at, _, _, kind, payload in timeline:
            if kind == "activate":
                order = by_id[str(payload)]
                if order.status is not ReplayOrderStatus.PENDING:
                    continue
                if self._would_cross(order.request, bids, asks):
                    order.status = ReplayOrderStatus.POST_ONLY_REJECTED
                    continue
                external_ahead = (
                    bids.get(order.request.price, Decimal("0"))
                    if order.request.side is Side.BUY
                    else asks.get(order.request.price, Decimal("0"))
                )
                own_ahead = sum(
                    (
                        existing.remaining
                        for existing in by_id.values()
                        if existing.status
                        in {ReplayOrderStatus.OPEN, ReplayOrderStatus.PARTIALLY_FILLED}
                        and existing.request.side is order.request.side
                        and existing.request.price == order.request.price
                        and existing.active_at <= order.active_at
                    ),
                    Decimal("0"),
                )
                order.queue_ahead = external_ahead + own_ahead
                order.status = ReplayOrderStatus.OPEN
            elif kind == "cancel":
                order = by_id[str(payload)]
                if order.status in {
                    ReplayOrderStatus.PENDING,
                    ReplayOrderStatus.OPEN,
                    ReplayOrderStatus.PARTIALLY_FILLED,
                }:
                    order.status = ReplayOrderStatus.CANCELLED
            else:
                event = payload
                if isinstance(event, L2BookEvent):
                    bids = {level.price: level.size for level in event.bids}
                    asks = {level.price: level.size for level in event.asks}
                else:
                    assert isinstance(event, L2TradeEvent)
                    self._apply_trade(event, by_id, bids, asks, fills)

        results = tuple(
            ReplayOrderResult(
                order_id=order.request.order_id,
                status=order.status,
                filled_size=order.filled_size,
                remaining_size=order.remaining,
                queue_ahead_remaining=order.queue_ahead,
                cancel_requested_at=order.cancel_requested_at,
                cancel_effective_at=order.cancel_effective_at,
            )
            for order in by_id.values()
        )
        return L2ReplayResult(
            orders=results,
            fills=tuple(fills),
            complete_depth=self.complete_depth,
            verified_exchange_sequence=self.verified_exchange_sequence,
        )

    @staticmethod
    def _apply_trade(
        event: L2TradeEvent,
        orders: dict[str, _ReplayOrder],
        bids: dict[Decimal, Decimal],
        asks: dict[Decimal, Decimal],
        fills: list[ReplayFill],
    ) -> None:
        maker_side = Side.SELL if event.aggressor_side is Side.BUY else Side.BUY
        eligible = [
            order
            for order in orders.values()
            if order.status in {ReplayOrderStatus.OPEN, ReplayOrderStatus.PARTIALLY_FILLED}
            and order.request.side is maker_side
            and order.request.price == event.price
        ]
        eligible.sort(
            key=lambda order: (
                -order.request.price if maker_side is Side.BUY else order.request.price,
                order.active_at,
                order.request.order_id,
            )
        )
        for order in eligible:
            queue_before = order.queue_ahead
            order.queue_ahead = max(Decimal("0"), queue_before - event.size)
            fill_capacity = max(Decimal("0"), event.size - queue_before)
            fill_size = min(order.remaining, fill_capacity)
            if fill_size <= 0:
                continue
            order.filled_size += fill_size
            order.status = (
                ReplayOrderStatus.FILLED
                if order.remaining == 0
                else ReplayOrderStatus.PARTIALLY_FILLED
            )
            fills.append(
                ReplayFill(
                    order_id=order.request.order_id,
                    occurred_at=event.occurred_at,
                    price=order.request.price,
                    size=fill_size,
                )
            )
        book = asks if event.aggressor_side is Side.BUY else bids
        if event.price in book:
            book[event.price] = max(Decimal("0"), book[event.price] - event.size)

    @staticmethod
    def _would_cross(
        request: ReplayOrderRequest,
        bids: dict[Decimal, Decimal],
        asks: dict[Decimal, Decimal],
    ) -> bool:
        if request.side is Side.BUY:
            best_ask = min(asks, default=None)
            return best_ask is not None and request.price >= best_ask
        best_bid = max(bids, default=None)
        return best_bid is not None and request.price <= best_bid

    def _validate_sequences(self, events: list[L2Event]) -> None:
        ordered = sorted(events, key=lambda event: (event.occurred_at, event.sequence))
        sequences = [event.sequence for event in ordered]
        if len(sequences) != len(set(sequences)):
            raise ValueError("duplicate exchange sequence in L2 replay")
        if sequences != sorted(sequences):
            raise ValueError("exchange sequence moves backwards in event time")
        if self.verified_exchange_sequence and (
            not sequences
            or any(
                current != previous + 1
                for previous, current in zip(sequences, sequences[1:], strict=False)
            )
        ):
            raise ValueError("verified L2 replay contains an exchange sequence gap")


def require_event_level_replay(report: object) -> L2ReplayResult:
    """Reject legacy candle/snapshot backtests as live-gate evidence."""

    if not isinstance(report, L2ReplayResult):
        raise ValueError("legacy backtests cannot be used as live-gate evidence")
    if not report.live_gate_eligible:
        raise ValueError(
            "L2 replay lacks complete depth or verified exchange sequence"
        )
    return report
