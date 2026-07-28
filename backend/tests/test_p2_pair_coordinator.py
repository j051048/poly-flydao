from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from polybot.config import TradingMode
from polybot.execution.coordinator import (
    AtomicFillCommit,
    CoordinatorFill,
    CoordinatorStatus,
    HedgePackage,
    PairCoordinatorConfig,
    PairExecutionCoordinator,
)
from polybot.execution.order_manager import HedgeQuote, OrderGroupState, PairOrderManager
from polybot.models import (
    BookLevel,
    ExecutionResult,
    ExecutionStatus,
    LiquidityRole,
    OrderBookSnapshot,
    OrderGroup,
    OrderGroupStatus,
    OrderLeg,
    OrderLegPurpose,
    OrderPlan,
    Outcome,
    PortfolioState,
    TradeIntent,
    utc_now,
)


def _plan_and_books() -> tuple[OrderPlan, dict[str, OrderBookSnapshot]]:
    now = utc_now()
    group = OrderGroup(
        account_id="account",
        market_id="market",
        target_pair_size=Decimal("3"),
        expected_net_edge_usd=Decimal("0.15"),
        leg_deadline_at=now + timedelta(seconds=2),
        research_only=True,
    )
    yes = OrderLeg(
        group_id=group.id,
        outcome=Outcome.YES,
        token_id="yes",
        purpose=OrderLegPurpose.PAIR_ENTRY,
        liquidity_role=LiquidityRole.MAKER,
        post_only=True,
        price=Decimal("0.45"),
        size=Decimal("3"),
        deadline_at=group.leg_deadline_at,
    )
    no = OrderLeg(
        group_id=group.id,
        outcome=Outcome.NO,
        token_id="no",
        purpose=OrderLegPurpose.PAIR_ENTRY,
        liquidity_role=LiquidityRole.MAKER,
        post_only=True,
        price=Decimal("0.49"),
        size=Decimal("3"),
        deadline_at=group.leg_deadline_at,
    )
    plan = OrderPlan(
        group=group,
        legs=(yes, no),
        base_cost_usd=Decimal("2.82"),
        taker_hedge_fee_buffer_usd=Decimal("0.01"),
        leg_risk_buffer_usd=Decimal("0.01"),
        capital_cost_usd=Decimal("0.01"),
        expected_payout_usd=Decimal("3"),
        enabled=True,
        research_only=True,
    )
    books = {
        "yes": OrderBookSnapshot(
            market_id="market",
            token_id="yes",
            bids=[BookLevel(price=Decimal("0.44"), size=Decimal("10"))],
            asks=[BookLevel(price=Decimal("0.46"), size=Decimal("10"))],
        ),
        "no": OrderBookSnapshot(
            market_id="market",
            token_id="no",
            bids=[BookLevel(price=Decimal("0.48"), size=Decimal("10"))],
            asks=[BookLevel(price=Decimal("0.50"), size=Decimal("10"))],
        ),
    }
    return plan, books


class MemoryPairExecutionStore:
    def __init__(self, log: list[str]):
        self.log = log
        self.states: dict[UUID, OrderGroupState] = {}
        self.fill_ids: set[str] = set()
        self.fail_next_transition = False

    async def persist_plan_atomic(self, plan: OrderPlan) -> bool:
        self.log.append("persist_plan")
        if plan.group.id in self.states:
            return False
        self.states[plan.group.id] = PairOrderManager.from_plan(plan)
        return True

    async def load_group_state(
        self,
        *,
        account_id: str,
        group_id: UUID,
    ) -> OrderGroupState:
        state = self.states[group_id]
        if state.group.account_id != account_id:
            raise PermissionError("cross-account state load")
        return state

    async def commit_transition_atomic(
        self,
        *,
        expected_group_version: int,
        state: OrderGroupState,
        reason: str,
    ) -> bool:
        self.log.append(f"transition:{reason}")
        if self.fail_next_transition:
            self.fail_next_transition = False
            return False
        current = self.states[state.group.id]
        if current.group.version != expected_group_version:
            return False
        self.states[state.group.id] = state
        return True

    async def commit_fill_atomic(
        self,
        *,
        expected_group_version: int,
        fill: CoordinatorFill,
        state: OrderGroupState,
    ) -> AtomicFillCommit:
        self.log.append(f"fill:{fill.fill_id}")
        if fill.fill_id in self.fill_ids:
            return AtomicFillCommit.DUPLICATE
        current = self.states[fill.group_id]
        if current.group.version != expected_group_version:
            return AtomicFillCommit.CONFLICT
        self.fill_ids.add(fill.fill_id)
        self.states[fill.group_id] = state
        return AtomicFillCommit.APPLIED

    async def load_after_cancel_reconciliation(
        self,
        *,
        account_id: str,
        group_id: UUID,
        order_ids: tuple[str, ...],
    ) -> OrderGroupState:
        self.log.append("cancel_reconciliation_barrier")
        return await self.load_group_state(account_id=account_id, group_id=group_id)


class ResearchIntentFactory:
    def __init__(self, mode: TradingMode = TradingMode.PAPER):
        self.mode = mode

    def build_intent(
        self,
        *,
        group: OrderGroup,
        leg: OrderLeg,
        book: OrderBookSnapshot,
        run_id: str,
    ) -> TradeIntent:
        return TradeIntent(
            intent_hash=leg.id.hex,
            account_id=group.account_id,
            run_id=run_id,
            mode=self.mode,
            market_id=group.market_id,
            event_id=None,
            bucket="crypto",
            token_id=leg.token_id,
            outcome=leg.outcome,
            side=leg.side,
            price=leg.price,
            size=leg.size,
            notional_usd=leg.price * leg.size,
            edge_after_costs=group.expected_net_edge_usd / group.target_pair_size,
            forecast_id=None,
            strategy=group.strategy,
            post_only=leg.post_only,
        )


class FakePairBroker:
    def __init__(self, log: list[str]):
        self.log = log
        self.cancel_succeeds = True
        self.batch_results: list[ExecutionResult] | None = None
        self.hedge_result: ExecutionResult | None = None

    async def portfolio_state(self) -> PortfolioState:
        return PortfolioState(bankroll_usd=Decimal("100"), cash_usd=Decimal("100"))

    async def submit(
        self,
        intent: TradeIntent,
        book: OrderBookSnapshot,
    ) -> ExecutionResult:
        self.log.append("submit_hedge")
        return self.hedge_result or ExecutionResult(
            intent_hash=intent.intent_hash,
            status=ExecutionStatus.ACCEPTED,
            order_id="hedge-order",
        )

    async def submit_batch(
        self,
        submissions,
    ) -> list[ExecutionResult]:
        self.log.append("submit_batch")
        if self.batch_results is not None:
            return self.batch_results
        return [
            ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.ACCEPTED,
                order_id=f"order-{index}",
            )
            for index, (intent, _) in enumerate(submissions)
        ]

    async def cancel_order(self, order_id: str, reason: str) -> bool:
        self.log.append(f"cancel_order:{order_id}")
        return self.cancel_succeeds

    async def cancel_orders(self, order_ids, reason: str) -> bool:
        self.log.append("cancel_orders:" + ",".join(order_ids))
        return self.cancel_succeeds

    async def cancel_all(self, reason: str) -> bool:
        self.log.append("cancel_all")
        return self.cancel_succeeds

    async def redeem_resolved(self) -> int:
        return 0


class FixedHedgeProvider:
    def __init__(
        self,
        log: list[str],
        books: dict[str, OrderBookSnapshot],
    ):
        self.log = log
        self.books = books
        self.enabled = True

    async def quote(self, state: OrderGroupState) -> HedgePackage | None:
        self.log.append("hedge_quote")
        if not self.enabled:
            return None
        outcome = (
            Outcome.NO
            if state.group.directional_yes_size > 0
            else Outcome.YES
        )
        token_id = "no" if outcome is Outcome.NO else "yes"
        return HedgePackage(
            quote=HedgeQuote(
                outcome=outcome,
                token_id=token_id,
                price=Decimal("0.50"),
                available_size=Decimal("10"),
            ),
            book=self.books[token_id],
        )


def _coordinator(
    *,
    enabled: bool,
    log: list[str],
    store: MemoryPairExecutionStore,
    broker: FakePairBroker,
    books: dict[str, OrderBookSnapshot],
    mode: TradingMode = TradingMode.PAPER,
) -> PairExecutionCoordinator:
    return PairExecutionCoordinator(
        store=store,
        broker=broker,
        intent_factory=ResearchIntentFactory(mode),
        hedge_provider=FixedHedgeProvider(log, books),
        config=PairCoordinatorConfig(enabled=enabled, research_only=True),
    )


async def test_coordinator_is_disabled_before_any_persistence_or_broker_call() -> None:
    plan, books = _plan_and_books()
    log: list[str] = []
    store = MemoryPairExecutionStore(log)
    broker = FakePairBroker(log)
    result = await _coordinator(
        enabled=False,
        log=log,
        store=store,
        broker=broker,
        books=books,
    ).submit_plan(plan=plan, books=books, run_id="run")
    assert result.status is CoordinatorStatus.DISABLED
    assert log == []
    assert store.states == {}


async def test_plan_is_atomically_persisted_before_non_atomic_batch_submission() -> None:
    plan, books = _plan_and_books()
    log: list[str] = []
    store = MemoryPairExecutionStore(log)
    broker = FakePairBroker(log)
    result = await _coordinator(
        enabled=True,
        log=log,
        store=store,
        broker=broker,
        books=books,
    ).submit_plan(plan=plan, books=books, run_id="run")
    assert result.status is CoordinatorStatus.WORKING
    assert log[:3] == [
        "persist_plan",
        "submit_batch",
        "transition:batch_leg_results",
    ]
    assert result.state is not None
    assert result.state.group.status is OrderGroupStatus.WORKING


async def test_research_coordinator_rejects_live_intents_before_exchange() -> None:
    plan, books = _plan_and_books()
    log: list[str] = []
    store = MemoryPairExecutionStore(log)
    broker = FakePairBroker(log)
    result = await _coordinator(
        enabled=True,
        log=log,
        store=store,
        broker=broker,
        books=books,
        mode=TradingMode.CANARY,
    ).submit_plan(plan=plan, books=books, run_id="run")
    assert result.status is CoordinatorStatus.FROZEN
    assert "submit_batch" not in log
    assert log == ["persist_plan", "transition:freeze"]


async def test_fill_commit_is_idempotent_and_drives_pair_completion() -> None:
    plan, books = _plan_and_books()
    log: list[str] = []
    store = MemoryPairExecutionStore(log)
    broker = FakePairBroker(log)
    coordinator = _coordinator(
        enabled=True,
        log=log,
        store=store,
        broker=broker,
        books=books,
    )
    await coordinator.submit_plan(plan=plan, books=books, run_id="run")
    for fill_id, leg in zip(("yes-fill", "no-fill"), plan.legs, strict=True):
        result = await coordinator.on_fill(
            CoordinatorFill(
                fill_id=fill_id,
                account_id=plan.group.account_id,
                group_id=plan.group.id,
                leg_id=leg.id,
                size=Decimal("3"),
                price=leg.price,
            )
        )
    assert result.status is CoordinatorStatus.PAIRED
    assert result.state is not None
    assert result.state.group.paired_size == Decimal("3")

    duplicate = await coordinator.on_fill(
        CoordinatorFill(
            fill_id="yes-fill",
            account_id=plan.group.account_id,
            group_id=plan.group.id,
            leg_id=plan.legs[0].id,
            size=Decimal("3"),
            price=plan.legs[0].price,
        )
    )
    assert duplicate.status is CoordinatorStatus.DUPLICATE
    assert store.fill_ids == {"yes-fill", "no-fill"}


async def test_deadline_orders_cancel_reconciliation_before_deterministic_hedge() -> None:
    plan, books = _plan_and_books()
    log: list[str] = []
    store = MemoryPairExecutionStore(log)
    broker = FakePairBroker(log)
    coordinator = _coordinator(
        enabled=True,
        log=log,
        store=store,
        broker=broker,
        books=books,
    )
    await coordinator.submit_plan(plan=plan, books=books, run_id="run")
    await coordinator.on_fill(
        CoordinatorFill(
            fill_id="single-leg",
            account_id=plan.group.account_id,
            group_id=plan.group.id,
            leg_id=plan.legs[0].id,
            size=Decimal("3"),
            price=plan.legs[0].price,
        )
    )
    result = await coordinator.process_deadline(
        account_id=plan.group.account_id,
        group_id=plan.group.id,
        run_id="deadline",
        now=plan.group.leg_deadline_at + timedelta(milliseconds=1),
    )
    assert result.status is CoordinatorStatus.WORKING
    assert result.state is not None
    assert result.state.group.status is OrderGroupStatus.HEDGING
    assert log.index("cancel_reconciliation_barrier") < log.index("hedge_quote")
    assert log.index("hedge_quote") < log.index("submit_hedge")

    hedge_leg = next(
        leg
        for leg in result.state.legs
        if leg.purpose is OrderLegPurpose.IMBALANCE_HEDGE
    )
    completed = await coordinator.on_fill(
        CoordinatorFill(
            fill_id="hedge-fill",
            account_id=plan.group.account_id,
            group_id=plan.group.id,
            leg_id=hedge_leg.id,
            size=Decimal("3"),
            price=Decimal("0.50"),
        )
    )
    assert completed.status is CoordinatorStatus.PAIRED
    assert completed.state is not None
    assert completed.state.group.directional_yes_size == 0


async def test_unverified_cancel_freezes_without_requesting_a_hedge() -> None:
    plan, books = _plan_and_books()
    log: list[str] = []
    store = MemoryPairExecutionStore(log)
    broker = FakePairBroker(log)
    broker.cancel_succeeds = False
    coordinator = _coordinator(
        enabled=True,
        log=log,
        store=store,
        broker=broker,
        books=books,
    )
    await coordinator.submit_plan(plan=plan, books=books, run_id="run")
    await coordinator.on_fill(
        CoordinatorFill(
            fill_id="single-leg",
            account_id=plan.group.account_id,
            group_id=plan.group.id,
            leg_id=plan.legs[0].id,
            size=Decimal("1"),
            price=plan.legs[0].price,
        )
    )
    result = await coordinator.process_deadline(
        account_id=plan.group.account_id,
        group_id=plan.group.id,
        run_id="deadline",
        now=plan.group.leg_deadline_at + timedelta(milliseconds=1),
    )
    assert result.status is CoordinatorStatus.FROZEN
    assert "hedge_quote" not in log
    assert "submit_hedge" not in log


async def test_batch_state_cas_conflict_triggers_targeted_cancel() -> None:
    plan, books = _plan_and_books()
    log: list[str] = []
    store = MemoryPairExecutionStore(log)
    store.fail_next_transition = True
    broker = FakePairBroker(log)
    result = await _coordinator(
        enabled=True,
        log=log,
        store=store,
        broker=broker,
        books=books,
    ).submit_plan(plan=plan, books=books, run_id="run")
    assert result.status is CoordinatorStatus.CONFLICT
    assert any(item.startswith("cancel_orders:order-0,order-1") for item in log)


async def test_partial_batch_acceptance_is_cancelled_and_frozen() -> None:
    plan, books = _plan_and_books()
    log: list[str] = []
    store = MemoryPairExecutionStore(log)
    broker = FakePairBroker(log)
    broker.batch_results = [
        ExecutionResult(
            intent_hash="accepted",
            status=ExecutionStatus.ACCEPTED,
            order_id="single-accepted-leg",
        ),
        ExecutionResult(
            intent_hash="rejected",
            status=ExecutionStatus.REJECTED,
            message="post-only rejected",
        ),
    ]
    result = await _coordinator(
        enabled=True,
        log=log,
        store=store,
        broker=broker,
        books=books,
    ).submit_plan(plan=plan, books=books, run_id="run")
    assert result.status is CoordinatorStatus.FROZEN
    assert "cancel_orders:single-accepted-leg" in log
    assert result.state is not None
    assert result.state.group.status is OrderGroupStatus.FROZEN
