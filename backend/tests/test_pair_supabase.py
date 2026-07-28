from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from polybot.execution.coordinator import AtomicFillCommit, CoordinatorFill
from polybot.execution.order_manager import PairOrderManager
from polybot.models import (
    LiquidityRole,
    OrderGroup,
    OrderLeg,
    OrderLegPurpose,
    OrderPlan,
    Outcome,
    Side,
    UserTradeUpdate,
    utc_now,
)
from polybot.stores.pair_supabase import SupabasePairExecutionStore
from polybot.stores.supabase_store import TenantScopeError


class FakeQuery:
    def __init__(
        self,
        client: FakeClient,
        *,
        table: str | None = None,
        rpc: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        self.client = client
        self.table = table
        self.rpc = rpc
        self.params = params or {}
        self.filters: list[tuple[str, str, Any]] = []
        self.row_limit: int | None = None

    def select(self, columns: str) -> FakeQuery:
        return self

    def eq(self, field: str, value: Any) -> FakeQuery:
        self.filters.append(("eq", field, value))
        return self

    def in_(self, field: str, values: list[str]) -> FakeQuery:
        self.filters.append(("in", field, values))
        return self

    def limit(self, value: int) -> FakeQuery:
        self.row_limit = value
        return self

    def execute(self) -> SimpleNamespace:
        if self.rpc is not None:
            self.client.rpc_calls.append((self.rpc, self.params))
            return SimpleNamespace(data=self.client.rpc_results[self.rpc])
        assert self.table is not None
        self.client.table_calls.append((self.table, list(self.filters)))
        rows = list(self.client.tables.get(self.table, []))
        for operation, field, expected in self.filters:
            if operation == "eq":
                rows = [row for row in rows if str(row.get(field)) == str(expected)]
            else:
                wanted = {str(value) for value in expected}
                rows = [row for row in rows if str(row.get(field)) in wanted]
        if self.row_limit is not None:
            rows = rows[: self.row_limit]
        return SimpleNamespace(data=rows)


class FakeClient:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.rpc_results: dict[str, Any] = {
            "persist_pair_order_plan": True,
            "commit_pair_group_transition": True,
            "commit_pair_fill": "applied",
        }
        self.rpc_calls: list[tuple[str, dict[str, Any]]] = []
        self.table_calls: list[tuple[str, list[tuple[str, str, Any]]]] = []

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, table=name)

    def rpc(self, name: str, params: dict[str, Any]) -> FakeQuery:
        return FakeQuery(self, rpc=name, params=params)


def _plan() -> OrderPlan:
    account_id = str(uuid4())
    now = utc_now()
    group = OrderGroup(
        account_id=account_id,
        trading_wallet_id=str(uuid4()),
        market_id="gamma-market",
        condition_id="condition-1",
        target_pair_size=Decimal("2"),
        expected_net_edge_usd=Decimal("0.08"),
        leg_deadline_at=now + timedelta(seconds=3),
        research_only=True,
    )
    legs = tuple(
        OrderLeg(
            group_id=group.id,
            outcome=outcome,
            token_id=token,
            purpose=OrderLegPurpose.PAIR_ENTRY,
            liquidity_role=LiquidityRole.MAKER,
            post_only=True,
            price=price,
            size=Decimal("2"),
            deadline_at=group.leg_deadline_at,
        )
        for outcome, token, price in (
            (Outcome.YES, "yes", Decimal("0.44")),
            (Outcome.NO, "no", Decimal("0.50")),
        )
    )
    return OrderPlan(
        group=group,
        legs=legs,  # type: ignore[arg-type]
        base_cost_usd=Decimal("1.88"),
        expected_payout_usd=Decimal("2"),
        enabled=True,
        research_only=True,
    )


async def test_pair_store_binds_account_and_persists_plan_through_one_rpc() -> None:
    plan = _plan()
    client = FakeClient()
    db_market_id = str(uuid4())
    client.tables["markets"] = [
        {
            "id": db_market_id,
            "gamma_market_id": plan.group.market_id,
            "condition_id": plan.group.condition_id,
        }
    ]
    store = SupabasePairExecutionStore(client, account_id=plan.group.account_id)

    assert await store.persist_plan_atomic(plan)

    name, params = client.rpc_calls[-1]
    assert name == "persist_pair_order_plan"
    assert params["p_account_id"] == plan.group.account_id
    assert params["p_group"]["market_id"] == db_market_id
    assert params["p_group"]["execution_enabled"] is False
    assert len(params["p_legs"]) == 2

    foreign = plan.model_copy(
        update={
            "group": plan.group.model_copy(update={"account_id": str(uuid4())}),
        }
    )
    calls_before = len(client.table_calls) + len(client.rpc_calls)
    with pytest.raises(TenantScopeError, match="cross-account"):
        await store.persist_plan_atomic(foreign)
    assert len(client.table_calls) + len(client.rpc_calls) == calls_before


async def test_pair_store_loads_complete_state_and_seen_trade_ids() -> None:
    plan = _plan()
    client = FakeClient()
    db_market_id = str(uuid4())
    group_row = {
        "id": str(plan.group.id),
        "account_id": plan.group.account_id,
        "trading_wallet_id": plan.group.trading_wallet_id,
        "market_id": db_market_id,
        "strategy": plan.group.strategy,
        "target_pair_size": "2",
        "paired_size": "0",
        "directional_yes_size": "0",
        "directional_no_size": "0",
        "expected_net_edge_pusd": "0.08",
        "leg_deadline_at": plan.group.leg_deadline_at.isoformat(),
        "status": "planned",
        "research_only": True,
        "version": 1,
        "created_at": plan.group.created_at.isoformat(),
        "updated_at": plan.group.updated_at.isoformat(),
    }
    client.tables = {
        "order_groups": [group_row],
        "markets": [
            {
                "id": db_market_id,
                "gamma_market_id": "gamma-market",
                "condition_id": "condition-1",
            }
        ],
        "order_legs": [
            {
                **SupabasePairExecutionStore._leg_payload(leg),
                "account_id": plan.group.account_id,
            }
            for leg in plan.legs
        ],
        "pair_inventory_events": [
            {
                "account_id": plan.group.account_id,
                "order_group_id": str(plan.group.id),
                "clob_trade_id": "trade-1",
            }
        ],
    }
    store = SupabasePairExecutionStore(client, account_id=plan.group.account_id)

    state = await store.load_group_state(
        account_id=plan.group.account_id,
        group_id=plan.group.id,
    )

    assert state.group.market_id == "gamma-market"
    assert state.group.condition_id == "condition-1"
    assert {leg.token_id for leg in state.legs} == {"yes", "no"}
    assert state.seen_fill_ids == {"trade-1"}


async def test_pair_store_uses_group_cas_and_atomic_fill_result_contract() -> None:
    plan = _plan()
    client = FakeClient()
    store = SupabasePairExecutionStore(client, account_id=plan.group.account_id)
    state = PairOrderManager.from_plan(plan)
    transitioned = state.model_copy(
        update={
            # record_submission historically advanced the in-memory value by
            # two; the persistence adapter must normalize to the DB's +1 CAS.
            "group": state.group.model_copy(update={"version": 3}),
        }
    )
    assert await store.commit_transition_atomic(
        expected_group_version=1,
        state=transitioned,
        reason="test",
    )
    assert transitioned.group.version == 2
    transition_call = next(
        params for name, params in client.rpc_calls if name == "commit_pair_group_transition"
    )
    assert transition_call["p_group"]["version"] == 2
    fill = CoordinatorFill(
        fill_id="trade-1",
        account_id=plan.group.account_id,
        group_id=plan.group.id,
        leg_id=plan.legs[0].id,
        size=Decimal("1"),
        price=Decimal("0.44"),
    )
    after_fill = PairOrderManager().record_fill(
        transitioned,
        fill_id=fill.fill_id,
        leg_id=fill.leg_id,
        size=fill.size,
        price=fill.price,
    )

    assert (
        await store.commit_fill_atomic(
            expected_group_version=2,
            fill=fill,
            state=after_fill,
        )
        is AtomicFillCommit.APPLIED
    )
    client.rpc_results["commit_pair_fill"] = "duplicate"
    assert (
        await store.commit_fill_atomic(
            expected_group_version=2,
            fill=fill,
            state=after_fill,
        )
        is AtomicFillCommit.DUPLICATE
    )
    fill_call = next(params for name, params in client.rpc_calls if name == "commit_pair_fill")
    assert fill_call["p_fill"]["clob_trade_id"] == "trade-1"
    assert fill_call["p_account_id"] == plan.group.account_id
    assert fill_call["p_fill"]["outcome"] == plan.legs[0].outcome.value
    assert fill_call["p_fill"]["liquidity_role"] == "maker"


async def test_reconciled_trade_maps_only_to_matching_tenant_pair_leg() -> None:
    plan = _plan()
    leg = plan.legs[0]
    client = FakeClient()
    db_market_id = str(uuid4())
    client.tables["order_legs"] = [
        {
            "account_id": plan.group.account_id,
            "id": str(leg.id),
            "group_id": str(plan.group.id),
            "clob_order_id": "order-1",
            "token_id": leg.token_id,
            "side": leg.side.value,
        }
    ]
    client.tables["order_groups"] = [
        {
            "account_id": plan.group.account_id,
            "id": str(plan.group.id),
            "market_id": db_market_id,
        }
    ]
    client.tables["markets"] = [
        {
            "id": db_market_id,
            "gamma_market_id": plan.group.market_id,
            "condition_id": plan.group.condition_id,
        }
    ]
    store = SupabasePairExecutionStore(client, account_id=plan.group.account_id)
    update = UserTradeUpdate(
        clob_trade_id="trade-1",
        candidate_order_ids=["order-1"],
        condition_id="condition-1",
        token_id="yes",
        side=Side.BUY,
        trader_side="MAKER",
        price=Decimal("0.44"),
        size=Decimal("1"),
        status="MATCHED",
    )

    fill = await store.coordinator_fill_for_trade(update)

    assert fill is not None
    assert fill.fill_id == update.clob_trade_id
    assert fill.group_id == plan.group.id
    assert fill.leg_id == leg.id
    assert fill.fee_usd == 0

    assert (
        await store.coordinator_fill_for_trade(
            update.model_copy(update={"status": "FAILED"})
        )
        is None
    )


async def test_reconciled_trade_fails_closed_when_multiple_pair_legs_match() -> None:
    plan = _plan()
    client = FakeClient()
    client.tables["order_legs"] = [
        {
            "account_id": plan.group.account_id,
            "id": str(leg.id),
            "group_id": str(plan.group.id),
            "clob_order_id": f"order-{index}",
            "token_id": "yes",
            "side": "BUY",
        }
        for index, leg in enumerate(plan.legs, start=1)
    ]
    store = SupabasePairExecutionStore(client, account_id=plan.group.account_id)
    update = UserTradeUpdate(
        clob_trade_id="ambiguous-trade",
        candidate_order_ids=["order-1", "order-2"],
        condition_id="condition-1",
        token_id="yes",
        side=Side.BUY,
        trader_side="MAKER",
        price=Decimal("0.44"),
        size=Decimal("1"),
        status="MATCHED",
    )

    with pytest.raises(ValueError, match="exactly one"):
        await store.coordinator_fill_for_trade(update)

    order_leg_calls = [
        filters for table, filters in client.table_calls if table == "order_legs"
    ]
    assert order_leg_calls
    # The adapter must fetch the complete candidate set; no arbitrary LIMIT 1.
    assert {"order-1", "order-2"} == set(
        next(value for operation, field, value in order_leg_calls[-1] if operation == "in")
    )
