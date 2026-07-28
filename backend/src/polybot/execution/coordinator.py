from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from polybot.brokers.base import Broker
from polybot.config import TradingMode
from polybot.execution.order_manager import (
    HedgeQuote,
    OrderActionType,
    OrderGroupState,
    PairOrderManager,
)
from polybot.models import (
    ExecutionResult,
    ExecutionStatus,
    OrderBookSnapshot,
    OrderGroup,
    OrderGroupStatus,
    OrderLeg,
    OrderPlan,
    Side,
    TradeIntent,
    utc_now,
)


class AtomicFillCommit(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


class PairExecutionStore(Protocol):
    """Persistence boundary required by the coordinator.

    Implementations must write the plan's group and both legs in one database
    transaction. State transitions use group version as a CAS token. Fill
    commits must insert the unique fill id and update leg/group/inventory in the
    same transaction.
    """

    async def persist_plan_atomic(self, plan: OrderPlan) -> bool: ...

    async def load_group_state(
        self,
        *,
        account_id: str,
        group_id: UUID,
    ) -> OrderGroupState: ...

    async def commit_transition_atomic(
        self,
        *,
        expected_group_version: int,
        state: OrderGroupState,
        reason: str,
    ) -> bool: ...

    async def commit_fill_atomic(
        self,
        *,
        expected_group_version: int,
        fill: CoordinatorFill,
        state: OrderGroupState,
    ) -> AtomicFillCommit: ...

    async def load_after_cancel_reconciliation(
        self,
        *,
        account_id: str,
        group_id: UUID,
        order_ids: tuple[str, ...],
    ) -> OrderGroupState:
        """Return state only after REST/user-stream fill reconciliation."""
        ...


class PairIntentFactory(Protocol):
    def build_intent(
        self,
        *,
        group: OrderGroup,
        leg: OrderLeg,
        book: OrderBookSnapshot,
        run_id: str,
    ) -> TradeIntent: ...


class DeterministicHedgeProvider(Protocol):
    async def quote(
        self,
        state: OrderGroupState,
    ) -> HedgePackage | None:
        """Return a bounded L2 quote; implementations must not call an AI model."""
        ...


class HedgePackage(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: Literal["deterministic_l2"] = "deterministic_l2"
    quote: HedgeQuote
    book: OrderBookSnapshot


class CoordinatorFill(BaseModel):
    model_config = ConfigDict(frozen=True)

    fill_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    group_id: UUID
    leg_id: UUID
    size: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0, lt=1)
    fee_usd: Decimal = Field(default=Decimal("0"), ge=0)
    occurred_at: datetime = Field(default_factory=utc_now)


class PairCoordinatorConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    research_only: bool = True
    maximum_cas_retries: int = Field(default=3, ge=1, le=10)


class CoordinatorStatus(StrEnum):
    DISABLED = "disabled"
    DUPLICATE = "duplicate"
    SUBMITTED = "submitted"
    WORKING = "working"
    PAIRED = "paired"
    FROZEN = "frozen"
    FAILED = "failed"
    CONFLICT = "conflict"
    NOOP = "noop"


class CoordinatorResult(BaseModel):
    status: CoordinatorStatus
    state: OrderGroupState | None = None
    executions: tuple[ExecutionResult, ...] = ()
    message: str = ""


class PairExecutionCoordinator:
    """Protocol-driven orchestration for the research-only pair lifecycle."""

    _accepted_statuses = frozenset(
        {ExecutionStatus.ACCEPTED, ExecutionStatus.PAPER_FILLED}
    )

    def __init__(
        self,
        *,
        store: PairExecutionStore,
        broker: Broker,
        intent_factory: PairIntentFactory,
        hedge_provider: DeterministicHedgeProvider,
        order_manager: PairOrderManager | None = None,
        config: PairCoordinatorConfig | None = None,
    ):
        self.store = store
        self.broker = broker
        self.intent_factory = intent_factory
        self.hedge_provider = hedge_provider
        self.order_manager = order_manager or PairOrderManager()
        self.config = config or PairCoordinatorConfig()

    async def submit_plan(
        self,
        *,
        plan: OrderPlan,
        books: Mapping[str, OrderBookSnapshot],
        run_id: str,
    ) -> CoordinatorResult:
        disabled = self._disabled_result(plan)
        if disabled is not None:
            return disabled
        if not await self.store.persist_plan_atomic(plan):
            state = await self.store.load_group_state(
                account_id=plan.group.account_id,
                group_id=plan.group.id,
            )
            return CoordinatorResult(
                status=CoordinatorStatus.DUPLICATE,
                state=state,
                message="order plan already exists",
            )
        state = self.order_manager.from_plan(plan)
        try:
            submissions: list[tuple[TradeIntent, OrderBookSnapshot]] = []
            for leg in plan.legs:
                book = books[leg.token_id]
                intent = self.intent_factory.build_intent(
                    group=plan.group,
                    leg=leg,
                    book=book,
                    run_id=run_id,
                )
                self._validate_intent(plan.group, leg, intent, book)
                submissions.append((intent, book))
        except Exception as exc:
            return await self._freeze_before_exchange(
                state,
                f"intent construction failed: {type(exc).__name__}",
            )

        try:
            executions = await self.broker.submit_batch(submissions)
        except Exception as exc:
            try:
                await self.broker.cancel_all("pair batch raised without a complete result")
            except Exception:
                pass
            return await self._freeze_before_exchange(
                state,
                f"batch submission was ambiguous: {type(exc).__name__}",
            )
        if len(executions) != len(plan.legs):
            return await self._freeze_after_ambiguous_batch(
                state,
                executions,
                "broker returned a malformed batch result count",
            )

        accepted_order_ids: dict[UUID, str] = {}
        rejected_leg_ids: set[UUID] = set()
        for leg, execution in zip(plan.legs, executions, strict=True):
            if execution.status in self._accepted_statuses and execution.order_id:
                accepted_order_ids[leg.id] = execution.order_id
            else:
                rejected_leg_ids.add(leg.id)
        partially_accepted = bool(accepted_order_ids) and len(accepted_order_ids) != len(
            plan.legs
        )
        ambiguous_ids = [
            execution.order_id
            for execution in executions
            if execution.order_id and execution.status is ExecutionStatus.ERROR
        ]
        if partially_accepted or ambiguous_ids:
            await self._cancel_accepted_best_effort(
                [*accepted_order_ids.values(), *ambiguous_ids]
            )
        next_state = self.order_manager.record_submission(
            state,
            accepted_order_ids=accepted_order_ids,
            rejected_leg_ids=frozenset(rejected_leg_ids),
        )
        if partially_accepted or any(
            execution.status is ExecutionStatus.ERROR for execution in executions
        ):
            next_state, _ = self.order_manager.freeze_after_hedge_failure(
                next_state,
                reason="non-atomic batch legs did not all reach an accepted state",
            )
        if not await self.store.commit_transition_atomic(
            expected_group_version=state.group.version,
            state=next_state,
            reason="batch_leg_results",
        ):
            await self._cancel_accepted_best_effort(accepted_order_ids.values())
            return CoordinatorResult(
                status=CoordinatorStatus.CONFLICT,
                executions=tuple(executions),
                message="state CAS failed after batch response; accepted legs were cancelled",
            )

        for leg, execution in zip(plan.legs, executions, strict=True):
            if (
                execution.status is ExecutionStatus.PAPER_FILLED
                and execution.filled_size > 0
                and execution.average_price is not None
            ):
                fill_result = await self.on_fill(
                    CoordinatorFill(
                        fill_id=f"paper:{execution.intent_hash}",
                        account_id=plan.group.account_id,
                        group_id=plan.group.id,
                        leg_id=leg.id,
                        size=execution.filled_size,
                        price=execution.average_price,
                    )
                )
                if fill_result.status is CoordinatorStatus.CONFLICT:
                    return fill_result.model_copy(
                        update={"executions": tuple(executions)}
                    )
        latest = await self.store.load_group_state(
            account_id=plan.group.account_id,
            group_id=plan.group.id,
        )
        return CoordinatorResult(
            status=self._status_for_state(latest),
            state=latest,
            executions=tuple(executions),
        )

    async def on_fill(self, fill: CoordinatorFill) -> CoordinatorResult:
        if not self.config.enabled:
            return CoordinatorResult(
                status=CoordinatorStatus.DISABLED,
                message="pair execution coordinator is disabled",
            )
        for _ in range(self.config.maximum_cas_retries):
            before = await self.store.load_group_state(
                account_id=fill.account_id,
                group_id=fill.group_id,
            )
            if fill.fill_id in before.seen_fill_ids:
                return CoordinatorResult(
                    status=CoordinatorStatus.DUPLICATE,
                    state=before,
                    message="fill was already applied",
                )
            try:
                after = self.order_manager.record_fill(
                    before,
                    fill_id=fill.fill_id,
                    leg_id=fill.leg_id,
                    size=fill.size,
                    price=fill.price,
                    fee_usd=fill.fee_usd,
                    occurred_at=fill.occurred_at,
                )
            except (KeyError, ValueError) as exc:
                return CoordinatorResult(
                    status=CoordinatorStatus.FAILED,
                    state=before,
                    message=str(exc),
                )
            committed = await self.store.commit_fill_atomic(
                expected_group_version=before.group.version,
                fill=fill,
                state=after,
            )
            if committed is AtomicFillCommit.APPLIED:
                return CoordinatorResult(
                    status=self._status_for_state(after),
                    state=after,
                )
            if committed is AtomicFillCommit.DUPLICATE:
                latest = await self.store.load_group_state(
                    account_id=fill.account_id,
                    group_id=fill.group_id,
                )
                return CoordinatorResult(
                    status=CoordinatorStatus.DUPLICATE,
                    state=latest,
                    message="fill id uniqueness rejected a duplicate",
                )
        return CoordinatorResult(
            status=CoordinatorStatus.CONFLICT,
            message="fill CAS retries were exhausted",
        )

    async def process_deadline(
        self,
        *,
        account_id: str,
        group_id: UUID,
        run_id: str,
        now: datetime | None = None,
    ) -> CoordinatorResult:
        if not self.config.enabled:
            return CoordinatorResult(
                status=CoordinatorStatus.DISABLED,
                message="pair execution coordinator is disabled",
            )
        before = await self.store.load_group_state(
            account_id=account_id,
            group_id=group_id,
        )
        cancelling, action = self.order_manager.on_leg_deadline(before, now=now)
        if action.action is OrderActionType.NONE:
            return CoordinatorResult(
                status=CoordinatorStatus.NOOP,
                state=before,
                message="group has no expired working legs",
            )
        if action.action is not OrderActionType.CANCEL_ORDERS:
            if not await self.store.commit_transition_atomic(
                expected_group_version=before.group.version,
                state=cancelling,
                reason="leg_deadline_terminal",
            ):
                return CoordinatorResult(
                    status=CoordinatorStatus.CONFLICT,
                    message="deadline terminal transition lost a state CAS race",
                )
            return CoordinatorResult(
                status=self._status_for_state(cancelling),
                state=cancelling,
                message=action.reason,
            )
        if not await self.store.commit_transition_atomic(
            expected_group_version=before.group.version,
            state=cancelling,
            reason="leg_deadline_cancel_pending",
        ):
            return CoordinatorResult(
                status=CoordinatorStatus.CONFLICT,
                message="deadline transition lost a state CAS race",
            )

        try:
            cancellation_verified = await self.broker.cancel_orders(
                action.order_ids,
                "pair leg deadline",
            )
        except Exception:
            cancellation_verified = False
        if not cancellation_verified:
            return await self._freeze_state(
                cancelling,
                "targeted cancellation was not verifiable",
            )
        try:
            reconciled = await self.store.load_after_cancel_reconciliation(
                account_id=account_id,
                group_id=group_id,
                order_ids=action.order_ids,
            )
        except Exception as exc:
            return await self._freeze_state(
                cancelling,
                f"cancel reconciliation barrier failed: {type(exc).__name__}",
            )
        try:
            hedge_package = await self.hedge_provider.quote(reconciled)
        except Exception:
            hedge_package = None
        after_cancel, next_action = self.order_manager.confirm_cancellations(
            reconciled,
            cancelled_order_ids=frozenset(action.order_ids),
            hedge_quote=hedge_package.quote if hedge_package else None,
            now=now,
        )
        if not await self.store.commit_transition_atomic(
            expected_group_version=reconciled.group.version,
            state=after_cancel,
            reason="cancel_reconciled",
        ):
            return CoordinatorResult(
                status=CoordinatorStatus.CONFLICT,
                message="cancel reconciliation transition lost a state CAS race",
            )
        if next_action.action is not OrderActionType.SUBMIT_HEDGE:
            return CoordinatorResult(
                status=self._status_for_state(after_cancel),
                state=after_cancel,
                message=next_action.reason,
            )
        assert next_action.hedge_leg is not None
        assert hedge_package is not None
        hedge_leg = next_action.hedge_leg
        try:
            hedge_intent = self.intent_factory.build_intent(
                group=after_cancel.group,
                leg=hedge_leg,
                book=hedge_package.book,
                run_id=run_id,
            )
            self._validate_intent(
                after_cancel.group,
                hedge_leg,
                hedge_intent,
                hedge_package.book,
            )
            execution = await self.broker.submit(hedge_intent, hedge_package.book)
        except Exception as exc:
            failed, _ = self.order_manager.record_hedge_submission(
                after_cancel,
                hedge_leg_id=hedge_leg.id,
                order_id=None,
                now=now,
            )
            if not await self.store.commit_transition_atomic(
                expected_group_version=after_cancel.group.version,
                state=failed,
                reason="hedge_submission_exception",
            ):
                return CoordinatorResult(
                    status=CoordinatorStatus.CONFLICT,
                    message="hedge failed and freeze CAS also conflicted",
                )
            return CoordinatorResult(
                status=CoordinatorStatus.FROZEN,
                state=failed,
                message=f"hedge submission failed: {type(exc).__name__}",
            )

        accepted = execution.status in self._accepted_statuses and execution.order_id
        if execution.status is ExecutionStatus.ERROR and execution.order_id:
            try:
                await self.broker.cancel_order(
                    execution.order_id,
                    "ambiguous deterministic hedge result",
                )
            except Exception:
                pass
        hedge_state, hedge_action = self.order_manager.record_hedge_submission(
            after_cancel,
            hedge_leg_id=hedge_leg.id,
            order_id=execution.order_id if accepted else None,
            now=now,
        )
        if not await self.store.commit_transition_atomic(
            expected_group_version=after_cancel.group.version,
            state=hedge_state,
            reason="hedge_submission_result",
        ):
            if accepted and execution.order_id:
                try:
                    await self.broker.cancel_order(
                        execution.order_id,
                        "hedge state CAS conflict",
                    )
                except Exception:
                    pass
            return CoordinatorResult(
                status=CoordinatorStatus.CONFLICT,
                executions=(execution,),
                message="hedge result CAS failed; targeted cancellation attempted",
            )
        if (
            execution.status is ExecutionStatus.PAPER_FILLED
            and execution.filled_size > 0
            and execution.average_price is not None
        ):
            filled = await self.on_fill(
                CoordinatorFill(
                    fill_id=f"paper:{execution.intent_hash}",
                    account_id=account_id,
                    group_id=group_id,
                    leg_id=hedge_leg.id,
                    size=execution.filled_size,
                    price=execution.average_price,
                )
            )
            return filled.model_copy(update={"executions": (execution,)})
        return CoordinatorResult(
            status=self._status_for_state(hedge_state),
            state=hedge_state,
            executions=(execution,),
            message=hedge_action.reason,
        )

    def _disabled_result(self, plan: OrderPlan) -> CoordinatorResult | None:
        if not self.config.enabled:
            return CoordinatorResult(
                status=CoordinatorStatus.DISABLED,
                message="pair execution coordinator is disabled",
            )
        if not plan.enabled:
            return CoordinatorResult(
                status=CoordinatorStatus.DISABLED,
                message="order plan is disabled",
            )
        if self.config.research_only and not plan.research_only:
            return CoordinatorResult(
                status=CoordinatorStatus.FAILED,
                message="research-only coordinator rejected a live-eligible plan",
            )
        return None

    def _validate_intent(
        self,
        group: OrderGroup,
        leg: OrderLeg,
        intent: TradeIntent,
        book: OrderBookSnapshot,
    ) -> None:
        if intent.account_id != group.account_id:
            raise ValueError("intent account differs from durable order group")
        if (
            intent.market_id != group.market_id
            or intent.market_id != book.market_id
            or intent.token_id != leg.token_id
            or intent.token_id != book.token_id
            or intent.outcome is not leg.outcome
        ):
            raise ValueError("intent, leg, and book identities differ")
        if intent.price != leg.price or intent.size != leg.size or intent.side is not leg.side:
            raise ValueError("intent changed the durable leg economics")
        if intent.post_only != leg.post_only:
            raise ValueError("intent changed the durable maker/taker constraint")
        if intent.strategy != group.strategy:
            raise ValueError("intent changed the durable strategy identity")
        if leg.post_only:
            if leg.side is Side.BUY and (
                book.best_ask is None or leg.price >= book.best_ask
            ):
                raise ValueError("post-only BUY leg would cross or lock the best ask")
            if leg.side is Side.SELL and (
                book.best_bid is None or leg.price <= book.best_bid
            ):
                raise ValueError("post-only SELL leg would cross or lock the best bid")
        if self.config.research_only and intent.mode not in {
            TradingMode.PAPER,
            TradingMode.SHADOW,
        }:
            raise ValueError("research-only coordinator cannot build canary/live intents")

    async def _freeze_before_exchange(
        self,
        state: OrderGroupState,
        reason: str,
    ) -> CoordinatorResult:
        return await self._freeze_state(state, reason)

    async def _freeze_after_ambiguous_batch(
        self,
        state: OrderGroupState,
        executions: list[ExecutionResult],
        reason: str,
    ) -> CoordinatorResult:
        accepted = [
            execution.order_id
            for execution in executions
            if execution.order_id and execution.status in self._accepted_statuses
        ]
        await self._cancel_accepted_best_effort(accepted)
        frozen = await self._freeze_state(state, reason)
        return frozen.model_copy(update={"executions": tuple(executions)})

    async def _freeze_state(
        self,
        state: OrderGroupState,
        reason: str,
    ) -> CoordinatorResult:
        frozen, _ = self.order_manager.freeze_after_hedge_failure(
            state,
            reason=reason,
        )
        committed = await self.store.commit_transition_atomic(
            expected_group_version=state.group.version,
            state=frozen,
            reason="freeze",
        )
        return CoordinatorResult(
            status=(
                CoordinatorStatus.FROZEN if committed else CoordinatorStatus.CONFLICT
            ),
            state=frozen if committed else None,
            message=reason,
        )

    async def _cancel_accepted_best_effort(self, order_ids) -> None:
        ids = tuple(order_id for order_id in order_ids if order_id)
        if not ids:
            return
        try:
            await self.broker.cancel_orders(ids, "pair coordinator state conflict")
        except Exception:
            pass

    @staticmethod
    def _status_for_state(state: OrderGroupState) -> CoordinatorStatus:
        mapping = {
            OrderGroupStatus.PAIRED: CoordinatorStatus.PAIRED,
            OrderGroupStatus.FROZEN: CoordinatorStatus.FROZEN,
            OrderGroupStatus.FAILED: CoordinatorStatus.FAILED,
            OrderGroupStatus.WORKING: CoordinatorStatus.WORKING,
            OrderGroupStatus.IMBALANCED: CoordinatorStatus.WORKING,
            OrderGroupStatus.CANCELLING: CoordinatorStatus.WORKING,
            OrderGroupStatus.HEDGING: CoordinatorStatus.WORKING,
        }
        return mapping.get(state.group.status, CoordinatorStatus.SUBMITTED)
