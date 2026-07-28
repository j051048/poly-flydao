from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import UUID

from polybot.execution.coordinator import (
    AtomicFillCommit,
    CoordinatorFill,
    CoordinatorResult,
    CoordinatorStatus,
)
from polybot.execution.order_manager import OrderGroupState
from polybot.fees import matched_taker_fee_usd
from polybot.models import OrderGroup, OrderLeg, OrderPlan, UserTradeUpdate
from polybot.stores.supabase_store import TenantScopeError

CancelReconciliationBarrier = Callable[[str, tuple[str, ...]], Awaitable[None]]


class CoordinatorFillSink(Protocol):
    async def on_fill(self, fill: CoordinatorFill) -> CoordinatorResult: ...


class SupabasePairExecutionStore:
    """Account-bound persistence adapter for the P2 pair state machine.

    The service-role client bypasses RLS, so account scope is immutable and is
    checked before every query. All mutations are delegated to the transaction
    RPCs from ``0009_pair_execution_rpcs.sql``.
    """

    def __init__(
        self,
        client: Any,
        *,
        account_id: str,
        cancel_reconciliation_barrier: CancelReconciliationBarrier | None = None,
    ) -> None:
        self._client = client
        self._account_id = account_id
        self._cancel_reconciliation_barrier = cancel_reconciliation_barrier
        self._market_identity_cache: dict[str, tuple[str, str | None]] = {}

    @property
    def account_id(self) -> str:
        return self._account_id

    def _require_account(self, account_id: str) -> None:
        if account_id != self.account_id:
            raise TenantScopeError(
                f"pair store is bound to account {self.account_id}; "
                "cross-account access denied"
            )

    async def _execute(self, builder: Any) -> Any:
        return await asyncio.to_thread(builder.execute)

    @staticmethod
    def _first(response: Any) -> dict[str, Any] | None:
        data = getattr(response, "data", None)
        if isinstance(data, list):
            return data[0] if data and isinstance(data[0], dict) else None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _scalar(response: Any) -> Any:
        data = getattr(response, "data", None)
        if isinstance(data, list) and len(data) == 1 and not isinstance(data[0], dict):
            return data[0]
        return data

    async def persist_plan_atomic(self, plan: OrderPlan) -> bool:
        self._require_account(plan.group.account_id)
        if not plan.research_only or not plan.group.research_only:
            raise ValueError("P2 persistence accepts research-only plans")
        if plan.group.trading_wallet_id is None:
            raise ValueError("pair plans require a tenant trading_wallet_id")
        market_id = await self._resolve_db_market_id(plan.group)
        group = self._group_payload(plan.group)
        group.update(
            {
                "market_id": market_id,
                "trading_wallet_id": plan.group.trading_wallet_id,
                # P2 remains disconnected from live execution. plan.enabled is
                # a research-engine gate, not permission to place live orders.
                "execution_enabled": False,
            }
        )
        response = await self._execute(
            self._client.rpc(
                "persist_pair_order_plan",
                {
                    "p_account_id": self.account_id,
                    "p_group": group,
                    "p_legs": [self._leg_payload(leg) for leg in plan.legs],
                },
            )
        )
        return self._scalar(response) is True

    async def load_group_state(
        self,
        *,
        account_id: str,
        group_id: UUID,
    ) -> OrderGroupState:
        self._require_account(account_id)
        group_response = await self._execute(
            self._client.table("order_groups")
            .select(
                "id,account_id,trading_wallet_id,market_id,strategy,target_pair_size,"
                "paired_size,directional_yes_size,directional_no_size,"
                "expected_net_edge_pusd,leg_deadline_at,status,research_only,version,"
                "created_at,updated_at"
            )
            .eq("account_id", self.account_id)
            .eq("id", str(group_id))
            .limit(1)
        )
        group_row = self._first(group_response)
        if group_row is None:
            raise LookupError(f"order group {group_id} was not found")
        external_market_id, condition_id = await self._market_identity(
            str(group_row["market_id"])
        )
        group = OrderGroup.model_validate(
            {
                "id": group_row["id"],
                "account_id": group_row["account_id"],
                "trading_wallet_id": group_row["trading_wallet_id"],
                "market_id": external_market_id,
                "condition_id": condition_id,
                "strategy": group_row["strategy"],
                "target_pair_size": group_row["target_pair_size"],
                "paired_size": group_row["paired_size"],
                "directional_yes_size": group_row["directional_yes_size"],
                "directional_no_size": group_row["directional_no_size"],
                "expected_net_edge_usd": group_row["expected_net_edge_pusd"],
                "leg_deadline_at": group_row["leg_deadline_at"],
                "status": group_row["status"],
                "research_only": group_row["research_only"],
                "version": group_row["version"],
                "created_at": group_row["created_at"],
                "updated_at": group_row["updated_at"],
            }
        )
        leg_response = await self._execute(
            self._client.table("order_legs")
            .select(
                "id,group_id,outcome,token_id,side,purpose,liquidity_role,post_only,"
                "price,size,filled_size,average_fill_price,fee_paid_pusd,status,"
                "clob_order_id,deadline_at,version,created_at,updated_at"
            )
            .eq("account_id", self.account_id)
            .eq("group_id", str(group_id))
        )
        leg_rows = list(getattr(leg_response, "data", None) or [])
        legs = tuple(
            OrderLeg.model_validate(
                {
                    **row,
                    "fee_paid_usd": row.get("fee_paid_pusd", 0),
                }
            )
            for row in sorted(
                leg_rows,
                key=lambda item: (str(item.get("created_at") or ""), str(item["id"])),
            )
        )
        event_response = await self._execute(
            self._client.table("pair_inventory_events")
            .select("clob_trade_id")
            .eq("account_id", self.account_id)
            .eq("order_group_id", str(group_id))
        )
        seen_fill_ids = frozenset(
            str(row["clob_trade_id"])
            for row in (getattr(event_response, "data", None) or [])
        )
        return OrderGroupState(group=group, legs=legs, seen_fill_ids=seen_fill_ids)

    async def commit_transition_atomic(
        self,
        *,
        expected_group_version: int,
        state: OrderGroupState,
        reason: str,
    ) -> bool:
        self._require_account(state.group.account_id)
        next_version = expected_group_version + 1
        group_payload = self._group_payload(state.group)
        group_payload["version"] = next_version
        response = await self._execute(
            self._client.rpc(
                "commit_pair_group_transition",
                {
                    "p_account_id": self.account_id,
                    "p_expected_group_version": expected_group_version,
                    "p_group": group_payload,
                    "p_legs": [self._leg_payload(leg) for leg in state.legs],
                    "p_reason": reason,
                },
            )
        )
        committed = self._scalar(response) is True
        if committed and state.group.version != next_version:
            # Some deterministic state-machine operations update multiple
            # fields and historically advanced the in-memory counter twice.
            # PostgreSQL owns the fencing token: normalize the caller-visible
            # state to the exact committed CAS version.
            state.group = state.group.model_copy(update={"version": next_version})
        return committed

    async def commit_fill_atomic(
        self,
        *,
        expected_group_version: int,
        fill: CoordinatorFill,
        state: OrderGroupState,
    ) -> AtomicFillCommit:
        self._require_account(fill.account_id)
        self._require_account(state.group.account_id)
        if fill.group_id != state.group.id:
            raise ValueError("fill and state refer to different order groups")
        matching_legs = [leg for leg in state.legs if leg.id == fill.leg_id]
        if len(matching_legs) != 1:
            raise ValueError("fill must resolve to exactly one state leg")
        fill_leg = matching_legs[0]
        response = await self._execute(
            self._client.rpc(
                "commit_pair_fill",
                {
                    "p_account_id": self.account_id,
                    "p_expected_group_version": expected_group_version,
                    "p_fill": {
                        "clob_trade_id": fill.fill_id,
                        "group_id": str(fill.group_id),
                        "leg_id": str(fill.leg_id),
                        "size": str(fill.size),
                        "price": str(fill.price),
                        "fee_paid_pusd": str(fill.fee_usd),
                        "matched_at": fill.occurred_at.isoformat(),
                        "outcome": fill_leg.outcome.value,
                        "side": fill_leg.side.value,
                        "liquidity_role": fill_leg.liquidity_role.value,
                    },
                    "p_group": self._group_payload(state.group),
                    "p_legs": [self._leg_payload(leg) for leg in state.legs],
                },
            )
        )
        raw = str(self._scalar(response) or "").lower()
        try:
            return AtomicFillCommit(raw)
        except ValueError as exc:
            raise RuntimeError(f"unexpected commit_pair_fill result: {raw!r}") from exc

    async def load_after_cancel_reconciliation(
        self,
        *,
        account_id: str,
        group_id: UUID,
        order_ids: tuple[str, ...],
    ) -> OrderGroupState:
        self._require_account(account_id)
        if self._cancel_reconciliation_barrier is None:
            raise RuntimeError("cancel reconciliation barrier is not configured")
        await self._cancel_reconciliation_barrier(account_id, order_ids)
        state = await self.load_group_state(account_id=account_id, group_id=group_id)
        known_order_ids = {
            leg.clob_order_id for leg in state.legs if leg.clob_order_id is not None
        }
        if set(order_ids).difference(known_order_ids):
            raise RuntimeError("cancel barrier returned state for unknown CLOB orders")
        return state

    async def coordinator_fill_for_trade(
        self,
        update: UserTradeUpdate,
    ) -> CoordinatorFill | None:
        """Resolve a reconciled account trade to a P2 leg, if it belongs to one."""

        status = update.status.removeprefix("TRADE_STATUS_").upper()
        if status == "FAILED":
            return None
        response = await self._execute(
            self._client.table("order_legs")
            .select("id,group_id,token_id,side,clob_order_id")
            .eq("account_id", self.account_id)
            .in_("clob_order_id", update.candidate_order_ids)
        )
        rows = list(getattr(response, "data", None) or [])
        if not rows:
            return None
        matching_rows = {
            str(row["id"]): row
            for row in rows
            if str(row.get("token_id")) == update.token_id
            and str(row.get("side")).upper() == update.side.value
        }
        if len(matching_rows) != 1:
            raise ValueError(
                "reconciled trade does not resolve to exactly one pair order leg"
            )
        row = next(iter(matching_rows.values()))
        group_response = await self._execute(
            self._client.table("order_groups")
            .select("market_id")
            .eq("account_id", self.account_id)
            .eq("id", str(row["group_id"]))
            .limit(1)
        )
        group_row = self._first(group_response)
        if group_row is None:
            raise ValueError("pair order leg has no tenant-scoped durable group")
        _, condition_id = await self._market_identity(str(group_row["market_id"]))
        if condition_id != update.condition_id:
            raise ValueError("reconciled trade condition differs from its pair order group")
        fee = matched_taker_fee_usd(
            size=update.size,
            price=update.price,
            fee_rate_bps=update.fee_rate_bps,
            trader_side=update.trader_side,
        )
        return CoordinatorFill(
            fill_id=update.clob_trade_id,
            account_id=self.account_id,
            group_id=UUID(str(row["group_id"])),
            leg_id=UUID(str(row["id"])),
            size=update.size,
            price=update.price,
            fee_usd=fee,
            occurred_at=update.matched_at,
        )

    async def _resolve_db_market_id(self, group: OrderGroup) -> str:
        query = self._client.table("markets").select("id")
        if group.condition_id:
            query = query.eq("condition_id", group.condition_id)
        else:
            query = query.eq("gamma_market_id", group.market_id)
        response = await self._execute(query.limit(1))
        row = self._first(response)
        if row is None:
            raise RuntimeError(f"market {group.market_id} has not been persisted")
        return str(row["id"])

    async def _market_identity(self, db_market_id: str) -> tuple[str, str | None]:
        cached = self._market_identity_cache.get(db_market_id)
        if cached is not None:
            return cached
        response = await self._execute(
            self._client.table("markets")
            .select("gamma_market_id,condition_id")
            .eq("id", db_market_id)
            .limit(1)
        )
        row = self._first(response)
        if row is None:
            raise RuntimeError(f"database market {db_market_id} was not found")
        condition_id = str(row["condition_id"]) if row.get("condition_id") else None
        external = str(row.get("gamma_market_id") or condition_id or db_market_id)
        identity = (external, condition_id)
        self._market_identity_cache[db_market_id] = identity
        return identity

    @staticmethod
    def _group_payload(group: OrderGroup) -> dict[str, Any]:
        return {
            "id": str(group.id),
            "account_id": group.account_id,
            "strategy": group.strategy,
            "target_pair_size": str(group.target_pair_size),
            "paired_size": str(group.paired_size),
            "directional_yes_size": str(group.directional_yes_size),
            "directional_no_size": str(group.directional_no_size),
            "expected_net_edge_pusd": str(group.expected_net_edge_usd),
            "leg_deadline_at": group.leg_deadline_at.isoformat(),
            "status": group.status.value,
            "research_only": group.research_only,
            "version": group.version,
            "created_at": group.created_at.isoformat(),
            "updated_at": group.updated_at.isoformat(),
        }

    @staticmethod
    def _leg_payload(leg: OrderLeg) -> dict[str, Any]:
        return {
            "id": str(leg.id),
            "group_id": str(leg.group_id),
            "outcome": leg.outcome.value,
            "token_id": leg.token_id,
            "side": leg.side.value,
            "purpose": leg.purpose.value,
            "liquidity_role": leg.liquidity_role.value,
            "post_only": leg.post_only,
            "price": str(leg.price),
            "size": str(leg.size),
            "filled_size": str(leg.filled_size),
            "average_fill_price": (
                str(leg.average_fill_price)
                if leg.average_fill_price is not None
                else None
            ),
            "fee_paid_pusd": str(leg.fee_paid_usd),
            "status": leg.status.value,
            "clob_order_id": leg.clob_order_id,
            "deadline_at": leg.deadline_at.isoformat(),
            "version": leg.version,
            "created_at": leg.created_at.isoformat(),
            "updated_at": leg.updated_at.isoformat(),
        }


class PairReconciledFillCallback:
    """Bridge durable CLOB reconciliation to the idempotent pair coordinator."""

    def __init__(
        self,
        *,
        store: SupabasePairExecutionStore,
        coordinator: CoordinatorFillSink,
    ) -> None:
        self._store = store
        self._coordinator = coordinator

    async def __call__(self, update: UserTradeUpdate) -> None:
        fill = await self._store.coordinator_fill_for_trade(update)
        if fill is None:
            return
        result = await self._coordinator.on_fill(fill)
        if result.status in {
            CoordinatorStatus.CONFLICT,
            CoordinatorStatus.FAILED,
            CoordinatorStatus.DISABLED,
        }:
            raise RuntimeError(
                "pair fill callback did not durably apply "
                f"{fill.fill_id}: {result.status.value}"
            )
