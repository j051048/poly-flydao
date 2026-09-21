"""Reconciliation, quarantine, and ledger-integrity methods for the store.

Split out of :mod:`polybot.stores.supabase_store`. These are the methods that
prove what the bot actually did with real money — they map broker order/trade
updates onto durable rows, quarantine anything that cannot be mapped, and
answer the worker's "is this order/token still ours" questions. Kept in one
place so the audit-trail invariants stay reviewable together.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

from polybot.fees import matched_taker_fee_usd
from polybot.models import (
    AccountActivityUpdate,
    AccountPositionUpdate,
    OrderReconcileTarget,
    QuarantineRecord,
    UserOrderUpdate,
    UserTradeUpdate,
    utc_now,
)
from polybot.stores.ledger import IncompleteFillLedgerError
from polybot.stores.payloads import (
    _ORDER_SETTLEMENT_RANK,
    _aggregate_order_fill_payload,
    _decimal_value,
    _merge_fill_payload,
)
from polybot.stores.supabase_rows import _quarantine_from_row, _text_or_none


class SupabaseReconcileMixin:
    async def record_quarantine(self, account_id: str, record: QuarantineRecord) -> None:
        self._require_account(account_id)
        payload = {
            "account_id": account_id,
            "kind": record.kind.value,
            "external_key": record.external_key,
            "reason": record.reason,
            "condition_id": record.condition_id,
            "token_id": record.token_id,
            "side": record.side,
            "size": _text_or_none(record.size),
            "price": _text_or_none(record.price),
            "notional_usd": _text_or_none(record.notional_usd),
            "occurred_at": (
                record.occurred_at.isoformat() if record.occurred_at is not None else None
            ),
            "detail": record.detail,
            "updated_at": utc_now().isoformat(),
        }
        await self._execute(
            self.client.table("reconciliation_quarantine").upsert(
                payload,
                on_conflict="account_id,kind,external_key",
            )
        )

    async def list_quarantine(
        self, account_id: str, limit: int = 200
    ) -> list[QuarantineRecord]:
        self._require_account(account_id)
        response = await self._execute(
            self.client.table("reconciliation_quarantine")
            .select("*")
            .eq("account_id", account_id)
            .order("created_at", desc=True)
            .limit(max(1, min(limit, 1000)))
        )
        return [_quarantine_from_row(row) for row in response.data or []]

    async def reconcile_order(self, update: UserOrderUpdate, account_id: str) -> None:
        self._require_account(account_id)
        status_map = {
            "LIVE": "live",
            "UNMATCHED": "live",
            "DELAYED": "submitted",
            "MATCHED": "matched",
            "CANCELED": "cancelled",
            "CANCELLED": "cancelled",
        }
        normalized_status = update.status.removeprefix("ORDER_STATUS_")
        incoming_status = status_map.get(normalized_status, "unknown")
        response = await self._execute(
            self.client.table("orders")
            .select("id,status,original_size,filled_size,response_payload,matched_at,cancelled_at")
            .eq("account_id", account_id)
            .eq("clob_order_id", update.clob_order_id)
            .limit(1)
        )
        rows = response.data or []
        if not rows:
            await self._audit(
                account_id,
                action="order_reconcile_orphan",
                entity_type="clob_order",
                entity_id=update.clob_order_id,
                metadata={"status": update.status, "condition_id": update.condition_id},
            )
            return

        order = rows[0]
        current_status = str(order.get("status") or "submitted")
        original_size = _decimal_value(order.get("original_size"), str(update.original_size))
        if abs(original_size - update.original_size) > Decimal("0.000001"):
            raise IncompleteFillLedgerError(
                "CLOB order size does not match its durable order record"
            )
        filled_size = min(
            original_size,
            max(_decimal_value(order.get("filled_size")), update.size_matched),
        )
        if incoming_status == "unknown":
            status = current_status
        elif Decimal("0") < filled_size < original_size and incoming_status != "cancelled":
            status = "partially_filled"
        elif filled_size >= original_size and incoming_status in {"live", "submitted"}:
            status = "matched"
        else:
            status = incoming_status

        if current_status == "confirmed":
            status = current_status
        elif current_status in {"cancelled", "expired", "rejected", "failed"}:
            if not (filled_size >= original_size and status == "matched"):
                status = current_status
        elif _ORDER_SETTLEMENT_RANK.get(current_status, 0) > _ORDER_SETTLEMENT_RANK.get(status, 0):
            status = current_status

        payload: dict[str, Any] = {
            "filled_size": str(filled_size),
            "remaining_size": str(max(Decimal("0"), original_size - filled_size)),
            "status": status,
            "response_payload": {
                **(order.get("response_payload") or {}),
                "last_order_event": update.raw,
            },
            "open_snapshot_miss_count": 0,
            "open_snapshot_missing_since": None,
        }
        if status == "matched" and not order.get("matched_at"):
            payload["matched_at"] = update.occurred_at.isoformat()
        if status == "cancelled" and not order.get("cancelled_at"):
            payload["cancelled_at"] = update.occurred_at.isoformat()
        await self._execute(self.client.table("orders").update(payload).eq("id", order["id"]))

    async def reconcile_open_order_snapshot(
        self, open_order_ids: set[str], account_id: str
    ) -> list[OrderReconcileTarget]:
        self._require_account(account_id)
        response = await self._execute(
            self.client.table("orders")
            .select("clob_order_id,status,open_snapshot_miss_count")
            .eq("account_id", account_id)
            .in_(
                "status",
                ["submitted", "live", "partially_filled", "unknown", "cancel_pending"],
            )
        )
        missing: list[OrderReconcileTarget] = []
        for row in response.data or []:
            clob_order_id = row.get("clob_order_id")
            if clob_order_id and str(clob_order_id) not in open_order_ids:
                missing.append(
                    OrderReconcileTarget(
                        clob_order_id=str(clob_order_id),
                        status=str(row["status"]),
                        missing_confirmations=int(row.get("open_snapshot_miss_count") or 0),
                    )
                )
        return missing

    async def confirm_orders_absent(self, clob_order_ids: set[str], account_id: str) -> None:
        self._require_account(account_id)
        if not clob_order_ids:
            return
        response = await self._execute(
            self.client.table("orders")
            .select("id,clob_order_id,status,open_snapshot_miss_count,open_snapshot_missing_since")
            .eq("account_id", account_id)
            .in_("clob_order_id", sorted(clob_order_ids))
            .in_(
                "status",
                ["submitted", "live", "partially_filled", "unknown", "cancel_pending"],
            )
        )
        for row in response.data or []:
            order_id = str(row["clob_order_id"])
            previous_status = str(row["status"])
            misses = int(row.get("open_snapshot_miss_count") or 0) + 1
            terminal = previous_status == "cancel_pending" or misses >= 2
            now = utc_now().isoformat()
            payload: dict[str, Any] = {
                "status": "cancelled" if terminal else "unknown",
                "open_snapshot_miss_count": misses,
                "open_snapshot_missing_since": row.get("open_snapshot_missing_since") or now,
            }
            if terminal:
                payload["cancelled_at"] = now
            await self._execute(self.client.table("orders").update(payload).eq("id", row["id"]))
            if misses == 1:
                await self._audit(
                    account_id,
                    action="order_disappeared_from_open_set",
                    entity_type="clob_order",
                    entity_id=order_id,
                    metadata={"previous_status": previous_status},
                )
            if terminal:
                await self._audit(
                    account_id,
                    action="order_absence_confirmed_terminal",
                    entity_type="clob_order",
                    entity_id=order_id,
                    metadata={
                        "previous_status": previous_status,
                        "confirmed_absent_count": misses,
                    },
                )

    async def reconcile_trade(self, update: UserTradeUpdate, account_id: str) -> None:
        self._require_account(account_id)
        response = await self._execute(
            self.client.table("orders")
            .select(
                "id,order_intent_id,clob_order_id,status,original_size,filled_size,"
                "outcome_token_id,side,matched_at,mined_at,confirmed_at"
            )
            .eq("account_id", account_id)
            .in_("clob_order_id", update.candidate_order_ids)
            .limit(1)
        )
        rows = response.data or []
        if not rows:
            await self._audit(
                account_id,
                action="trade_reconcile_orphan",
                entity_type="clob_trade",
                entity_id=update.clob_trade_id,
                metadata={
                    "candidate_order_ids": update.candidate_order_ids,
                    "condition_id": update.condition_id,
                },
            )
            raise IncompleteFillLedgerError(
                "account trade does not map to a durable bot order; dedicated-wallet "
                "ledger integrity cannot be proven"
            )
        order = rows[0]
        order_id = order["id"]
        if (
            str(order.get("outcome_token_id") or "") != update.token_id
            or str(order.get("side") or "").upper() != update.side.value
        ):
            raise IncompleteFillLedgerError(
                "account trade economics do not match the durable bot order"
            )
        fill_key = hashlib.sha256(
            f"{account_id}:{update.clob_trade_id}:{order_id}".encode()
        ).hexdigest()
        status = update.status.removeprefix("TRADE_STATUS_")
        if status == "MATCHED_NOT_BROADCASTED":
            status = "MATCHED"
        if status not in {"MATCHED", "MINED", "CONFIRMED", "RETRYING", "FAILED"}:
            status = "MATCHED"
        fee = matched_taker_fee_usd(
            size=update.size,
            price=update.price,
            fee_rate_bps=update.fee_rate_bps,
            trader_side=update.trader_side,
        )
        payload = {
            "account_id": account_id,
            "order_id": order_id,
            "market_id": await self._db_market_id_by_condition(update.condition_id),
            "fill_key": fill_key,
            "clob_trade_id": update.clob_trade_id,
            "outcome_token_id": update.token_id,
            "side": update.side.value,
            "liquidity_role": update.trader_side.lower() if update.trader_side else None,
            "price": str(update.price),
            "size": str(update.size),
            "fee_pusd": str(fee),
            "settlement_status": status,
            "transaction_hash": update.transaction_hash,
            "matched_at": update.matched_at.isoformat(),
            "mined_at": update.updated_at.isoformat() if status in {"MINED", "CONFIRMED"} else None,
            "confirmed_at": update.updated_at.isoformat() if status == "CONFIRMED" else None,
            "raw_payload": update.raw,
        }
        existing_fill = await self._execute(
            self.client.table("fills")
            .select("settlement_status,transaction_hash,mined_at,confirmed_at")
            .eq("fill_key", fill_key)
            .limit(1)
        )
        payload = _merge_fill_payload(
            (existing_fill.data or [None])[0],
            payload,
        )
        await self._execute(self.client.table("fills").upsert(payload, on_conflict="fill_key"))
        # The reconciler and live broker share this store instance. Invalidate
        # the short replay cache immediately when a WS/REST fill is observed.
        self._fill_ledger_cache = None
        fill_response = await self._execute(
            self.client.table("fills")
            .select("size,price,fee_pusd,settlement_status,matched_at,mined_at,confirmed_at")
            .eq("account_id", account_id)
            .eq("order_id", order_id)
        )
        order_payload = _aggregate_order_fill_payload(
            order,
            list(fill_response.data or []),
        )
        resulting_status = str(order_payload["status"])
        order_payload["matched_at"] = order.get("matched_at") or update.matched_at.isoformat()
        if resulting_status in {"mined", "confirmed"}:
            order_payload["mined_at"] = order.get("mined_at") or update.updated_at.isoformat()
        if resulting_status == "confirmed":
            order_payload["confirmed_at"] = (
                order.get("confirmed_at") or update.updated_at.isoformat()
            )
        order_payload["open_snapshot_miss_count"] = 0
        order_payload["open_snapshot_missing_since"] = None
        await self._execute(self.client.table("orders").update(order_payload).eq("id", order_id))

    async def reconcile_account_activity(
        self, update: AccountActivityUpdate, account_id: str
    ) -> None:
        self._require_account(account_id)
        await self._execute(
            self.client.table("account_activities").upsert(
                {
                    "account_id": account_id,
                    "activity_key": update.activity_key,
                    "activity_type": update.activity_type,
                    "condition_id": update.condition_id,
                    "amount_pusd": str(update.amount_usd),
                    "transaction_hash": update.transaction_hash,
                    "occurred_at": update.occurred_at.isoformat(),
                    "raw_payload": update.raw,
                },
                on_conflict="account_id,activity_key",
            )
        )
        self._fill_ledger_cache = None

    async def reconcile_positions(
        self, positions: list[AccountPositionUpdate], account_id: str
    ) -> None:
        self._require_account(account_id)
        existing = await self._execute(
            self.client.table("positions")
            .select("outcome_token_id")
            .eq("account_id", account_id)
            .gt("shares", 0)
        )
        active_tokens = {position.token_id for position in positions}
        for row in existing.data or []:
            token_id = str(row["outcome_token_id"])
            if token_id not in active_tokens:
                await self._execute(
                    self.client.table("positions")
                    .update(
                        {
                            "shares": "0",
                            "cost_basis_pusd": "0",
                            "unrealized_pnl_pusd": "0",
                            "as_of": utc_now().isoformat(),
                        }
                    )
                    .eq("account_id", account_id)
                    .eq("outcome_token_id", token_id)
                )
        if not positions:
            return
        payloads: list[dict[str, Any]] = []
        for position in positions:
            payloads.append(
                {
                    "account_id": account_id,
                    "market_id": await self._db_market_id_by_condition(position.condition_id),
                    "outcome": position.outcome.value,
                    "outcome_token_id": position.token_id,
                    "shares": str(position.size),
                    "average_entry_price": (
                        str(position.average_entry_price)
                        if position.average_entry_price is not None
                        else None
                    ),
                    "cost_basis_pusd": str(position.cost_basis_usd),
                    "realized_pnl_pusd": str(position.realized_pnl_usd),
                    "mark_price": (
                        str(position.mark_price) if position.mark_price is not None else None
                    ),
                    "unrealized_pnl_pusd": str(position.unrealized_pnl_usd),
                    "as_of": position.as_of.isoformat(),
                }
            )
        await self._execute(
            self.client.table("positions").upsert(
                payloads,
                on_conflict="account_id,market_id,outcome_token_id",
            )
        )

    async def _audit(
        self,
        account_id: str,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        metadata: dict[str, Any],
    ) -> None:
        self._require_account(account_id)
        await self._execute(
            self.client.table("audit_events").insert(
                {
                    "account_id": account_id,
                    "actor_type": "worker",
                    "action": action,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "metadata": metadata,
                }
            )
        )

    async def _db_market_id(self, external_market_id: str) -> str:
        cached = self._market_ids.get(external_market_id)
        if cached:
            return cached
        response = await self._execute(
            self.client.table("markets")
            .select("id")
            .eq("gamma_market_id", external_market_id)
            .limit(1)
        )
        rows = response.data or []
        if not rows:
            raise RuntimeError(f"market {external_market_id} has not been persisted")
        self._market_ids[external_market_id] = rows[0]["id"]
        return rows[0]["id"]

    async def _db_market_id_by_condition(self, condition_id: str) -> str:
        cached = self._condition_market_ids.get(condition_id)
        if cached:
            return cached
        response = await self._execute(
            self.client.table("markets").select("id").eq("condition_id", condition_id).limit(1)
        )
        rows = response.data or []
        if not rows:
            raise RuntimeError(f"condition {condition_id} has not been persisted")
        self._condition_market_ids[condition_id] = rows[0]["id"]
        return rows[0]["id"]
