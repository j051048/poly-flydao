from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from postgrest.exceptions import APIError

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
from polybot.schema import EXPECTED_SCHEMA_VERSION
from polybot.stores.ledger import (
    FillLedgerSnapshot,
    IncompleteFillLedgerError,
    replay_fill_ledger,
)
from supabase import Client, create_client

_ORDER_SETTLEMENT_RANK = {
    "matched": 1,
    "mined": 2,
    "confirmed": 3,
}
_ORDER_TERMINAL_STATUSES = {
    "confirmed",
    "simulated",
    "cancelled",
    "expired",
    "rejected",
    "failed",
}
_FILL_STATUS_RANK = {
    "MATCHED": 0,
    "RETRYING": 1,
    "MINED": 2,
    "CONFIRMED": 3,
    "FAILED": 3,
}


def _decimal_value(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(value if value is not None else default))


def _merge_execution_order_payload(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Make duplicate broker/engine saves idempotent and lifecycle-monotonic."""

    current_status = str(existing.get("status") or "created")
    incoming_status = str(incoming["status"])
    early = {"created", "signed", "submitting", "submitted", "unknown"}
    transition_allowed = current_status in early
    if incoming_status == "submitted":
        transition_allowed = current_status in early
    elif incoming_status == "unknown":
        transition_allowed = current_status in {"created", "signed", "submitting", "unknown"}
    elif incoming_status in {"rejected", "failed"}:
        transition_allowed = current_status in early
    elif incoming_status == "cancelled":
        transition_allowed = current_status not in (
            _ORDER_TERMINAL_STATUSES | set(_ORDER_SETTLEMENT_RANK)
        )
    elif incoming_status == "simulated":
        transition_allowed = current_status in {"created", "submitted", "simulated"}

    payload = dict(incoming)
    if not transition_allowed:
        payload["status"] = current_status

    original_size = _decimal_value(existing.get("original_size"), str(incoming["original_size"]))
    existing_filled = _decimal_value(existing.get("filled_size"))
    incoming_filled = _decimal_value(incoming.get("filled_size"))
    filled_size = min(original_size, max(existing_filled, incoming_filled))
    payload["original_size"] = str(original_size)
    payload["filled_size"] = str(filled_size)
    payload["remaining_size"] = str(max(Decimal("0"), original_size - filled_size))
    if existing_filled > incoming_filled and existing.get("average_fill_price") is not None:
        payload["average_fill_price"] = existing["average_fill_price"]
    payload["clob_order_id"] = existing.get("clob_order_id") or incoming.get("clob_order_id")
    payload["attempt_count"] = max(
        int(existing.get("attempt_count") or 0),
        int(incoming.get("attempt_count") or 0),
    )
    payload["submitted_at"] = existing.get("submitted_at") or incoming.get("submitted_at")
    existing_order_type = str(existing.get("order_type") or "").upper()
    incoming_order_type = str(incoming.get("order_type") or "").upper()
    # A signed GTD order is immutable exchange-side. Later execution retries
    # can have an empty response payload (notably an ambiguous POST), so they
    # must never erase the durable expiry or downgrade the row to GTC.
    if existing_order_type == "GTD":
        payload["order_type"] = "GTD"
        payload["expires_at"] = existing.get("expires_at") or incoming.get("expires_at")
    elif incoming_order_type == "GTD":
        payload["order_type"] = "GTD"
        payload["expires_at"] = incoming.get("expires_at") or existing.get("expires_at")
    else:
        payload["order_type"] = existing.get("order_type") or incoming.get("order_type")
        payload["expires_at"] = existing.get("expires_at") or incoming.get("expires_at")
    payload["response_payload"] = {
        **(existing.get("response_payload") or {}),
        **(incoming.get("response_payload") or {}),
    }
    if not transition_allowed:
        payload["last_error_detail"] = existing.get("last_error_detail")
    return payload


def _merge_fill_payload(
    existing: dict[str, Any] | None, incoming: dict[str, Any]
) -> dict[str, Any]:
    """Prevent stale REST/WS events from moving a fill out of a terminal state."""

    if existing is None:
        return incoming
    payload = dict(incoming)
    current_status = str(existing.get("settlement_status") or "MATCHED").upper()
    incoming_status = str(incoming["settlement_status"]).upper()
    if current_status in {"CONFIRMED", "FAILED"}:
        payload["settlement_status"] = current_status
    elif incoming_status == "FAILED" or (
        _FILL_STATUS_RANK.get(incoming_status, 0) >= _FILL_STATUS_RANK.get(current_status, 0)
    ):
        payload["settlement_status"] = incoming_status
    else:
        payload["settlement_status"] = current_status
    payload["transaction_hash"] = incoming.get("transaction_hash") or existing.get(
        "transaction_hash"
    )
    payload["mined_at"] = existing.get("mined_at") or incoming.get("mined_at")
    payload["confirmed_at"] = existing.get("confirmed_at") or incoming.get("confirmed_at")
    return payload


def _aggregate_order_fill_payload(
    order: dict[str, Any], fills: list[dict[str, Any]]
) -> dict[str, Any]:
    successful = [
        fill for fill in fills if str(fill.get("settlement_status") or "").upper() != "FAILED"
    ]
    original_size = _decimal_value(order.get("original_size"))
    reported_filled_size = sum(
        (_decimal_value(fill.get("size")) for fill in successful),
        Decimal("0"),
    )
    if reported_filled_size - original_size > Decimal("0.000001"):
        raise IncompleteFillLedgerError("durable fills exceed the order's original size")
    filled_size = min(original_size, reported_filled_size)
    remaining_size = max(Decimal("0"), original_size - filled_size)
    payload: dict[str, Any] = {
        "filled_size": str(filled_size),
        "remaining_size": str(remaining_size),
        "fee_paid_pusd": str(
            sum((_decimal_value(fill.get("fee_pusd")) for fill in successful), Decimal("0"))
        ),
    }
    if filled_size > 0:
        notional = sum(
            (
                _decimal_value(fill.get("price")) * _decimal_value(fill.get("size"))
                for fill in successful
            ),
            Decimal("0"),
        )
        payload["average_fill_price"] = str(notional / filled_size)

    current_status = str(order.get("status") or "submitted")
    if filled_size <= 0:
        if fills and all(
            str(fill.get("settlement_status") or "").upper() == "FAILED" for fill in fills
        ):
            payload["status"] = "failed"
        else:
            payload["status"] = current_status
    elif remaining_size > 0:
        payload["status"] = (
            current_status if current_status in {"cancelled", "expired"} else "partially_filled"
        )
    else:
        statuses = {str(fill.get("settlement_status") or "MATCHED").upper() for fill in successful}
        if statuses.intersection({"MATCHED", "RETRYING"}):
            payload["status"] = "matched"
        elif "MINED" in statuses:
            payload["status"] = "mined"
        else:
            payload["status"] = "confirmed"
    return payload


class TenantScopeError(RuntimeError):
    """Raised when service-role code attempts to cross its bound account scope."""


@dataclass(frozen=True)
class TenantExecutionFence:
    """Immutable cycle snapshot required by the final live-order database gate."""

    job_id: str
    claimed_by: str
    job_fencing_token: int
    profile_version: int
    risk_policy_id: str
    risk_policy_version: int
    mode: TradingMode

    def __post_init__(self) -> None:
        if not self.job_id or not self.claimed_by or not self.risk_policy_id:
            raise ValueError("tenant execution fence identifiers are required")
        if (
            min(
                self.job_fencing_token,
                self.profile_version,
                self.risk_policy_version,
            )
            <= 0
        ):
            raise ValueError("tenant execution fence versions must be positive")
        if self.mode not in {TradingMode.CANARY, TradingMode.LIVE}:
            raise ValueError("tenant execution fence is only valid for live modes")


@dataclass(frozen=True)
class PersonalExecutionScope:
    """Immutable owner/mode scope for the personal live submission gate."""

    owner_id: str
    mode: TradingMode

    def __post_init__(self) -> None:
        if len(self.owner_id.strip()) < 8:
            raise ValueError("personal execution owner id is invalid")
        if self.mode not in {TradingMode.CANARY, TradingMode.LIVE}:
            raise ValueError("personal execution scope is only valid for live modes")


def _rpc_boolean(value: Any, function_name: str) -> bool:
    """Decode PostgREST scalar booleans without treating ``[False]`` as true."""

    if isinstance(value, list):
        value = value[0] if value else False
    if isinstance(value, dict):
        value = value.get(function_name, value.get("ok", value.get("valid", False)))
    return value is True


class SupabaseStore:
    """Service-role-only, account-bound state store.

    Supabase's service role bypasses row-level security.  Binding the store to a
    single account and checking every account-bearing call prevents a caller bug
    from turning that privilege into a cross-tenant confused-deputy issue.
    """

    def __init__(
        self,
        url: str,
        service_role_key: str,
        *,
        account_id: str,
        client: Client | None = None,
    ):
        self.client = client or create_client(url, service_role_key)
        self._account_id = account_id
        self._market_ids: dict[str, str] = {}
        self._condition_market_ids: dict[str, str] = {}
        self._condition_event_ids: dict[str, str | None] = {}
        self._token_outcomes: dict[str, str] = {}
        self._intents: dict[str, TradeIntent] = {}
        self._fill_ledger_cache: tuple[str, float, FillLedgerSnapshot] | None = None
        self._preflight_complete = False
        self._tenant_execution_fence: TenantExecutionFence | None = None
        self._personal_execution_scope: PersonalExecutionScope | None = None

    @property
    def account_id(self) -> str:
        return self._account_id

    def _require_account(self, account_id: str) -> None:
        if account_id != self.account_id:
            raise TenantScopeError(
                f"store is bound to account {self.account_id}; cross-account access denied"
            )

    def bind_tenant_execution_fence(self, fence: TenantExecutionFence) -> None:
        """Bind this one-shot account store to exactly one live cycle job."""

        if self._tenant_execution_fence is not None or self._personal_execution_scope is not None:
            raise RuntimeError("tenant execution fence is already bound")
        self._tenant_execution_fence = fence

    def bind_personal_execution_scope(self, scope: PersonalExecutionScope) -> None:
        """Route every live submission through migration 0016's personal gate."""

        if self._tenant_execution_fence is not None or self._personal_execution_scope is not None:
            raise RuntimeError("execution scope is already bound")
        self._personal_execution_scope = scope

    async def _execute(self, builder: Any) -> Any:
        return await asyncio.to_thread(builder.execute)

    async def health(self) -> bool:
        try:
            version_response = await self._execute(self.client.rpc("polybot_schema_version", {}))
            version_data = getattr(version_response, "data", None)
            if isinstance(version_data, list):
                version_data = version_data[0] if version_data else None
            if isinstance(version_data, dict):
                version_data = version_data.get(
                    "polybot_schema_version",
                    version_data.get("version"),
                )
            if version_data != EXPECTED_SCHEMA_VERSION:
                return False
            await self._execute(
                self.client.table("runtime_controls")
                .select("account_id")
                .eq("account_id", self.account_id)
                .limit(1)
            )
            if not self._preflight_complete:
                user_response = await asyncio.to_thread(
                    self.client.auth.admin.get_user_by_id,
                    self.account_id,
                )
                user = getattr(user_response, "user", None)
                if user is None or str(getattr(user, "id", "")) != self.account_id:
                    return False
                # Validate every incremental migration by selecting one column
                # introduced by it. Empty tables are valid; missing schema is not.
                for table, column in (
                    ("account_risk_state", "risk_day"),
                    ("account_activities", "activity_key"),
                    ("orders", "open_snapshot_miss_count,expires_at"),
                    ("cycle_jobs", "risk_policy_version"),
                    ("order_groups", "execution_enabled"),
                    ("pair_inventory_events", "clob_trade_id"),
                ):
                    await self._execute(
                        self.client.table(table)
                        .select(column)
                        .eq("account_id", self.account_id)
                        .limit(1)
                    )
                # A no-match CAS is read-like but proves migration 0005 and its
                # service-role grant are available. The orders select above also
                # proves the 0006 GTD expiry column.
                await self._execute(
                    self.client.rpc(
                        "expire_runtime_control",
                        {
                            "p_account_id": self.account_id,
                            "p_mode": "canary",
                            "p_expected_version": 0,
                        },
                    )
                )
                # A deliberately ineligible mode returns false before touching
                # data, while proving migration 0010 and its service-role grant.
                await self._execute(
                    self.client.rpc(
                        "mark_tenant_order_submitting",
                        {
                            "p_account_id": self.account_id,
                            "p_intent_hash": "schema-preflight",
                            "p_worker_fencing_token": 0,
                            "p_control_version": 0,
                            "p_job_id": "00000000-0000-0000-0000-000000000000",
                            "p_claimed_by": "schema-preflight",
                            "p_job_fencing_token": 0,
                            "p_profile_version": 0,
                            "p_risk_policy_id": ("00000000-0000-0000-0000-000000000000"),
                            "p_risk_policy_version": 0,
                            "p_mode": "paper",
                        },
                    )
                )
                self._preflight_complete = True
        except Exception:
            return False
        return True

    async def save_market(self, market: MarketSpec) -> None:
        payload = {
            "gamma_market_id": market.id,
            "condition_id": market.condition_id or market.id,
            "event_id": market.event_id,
            "slug": market.slug,
            "question": market.question,
            "description": market.description,
            "resolution_source": market.resolution_source,
            "outcomes": ["YES", "NO"],
            "clob_token_ids": [market.yes_token_id, market.no_token_id],
            "active": market.active,
            "closed": market.closed,
            "accepting_orders": market.accepting_orders,
            "neg_risk": market.neg_risk,
            "tick_size": str(market.tick_size),
            "minimum_order_size": str(market.minimum_order_size),
            "end_at": market.end_at.isoformat() if market.end_at else None,
            "last_synced_at": market.updated_at.isoformat(),
            "raw_payload": {
                "category": market.category,
                "event_slug": market.event_slug,
                "tags": list(market.tags),
                "start_at": market.start_at.isoformat() if market.start_at else None,
                "outcome_labels": [market.yes_label, market.no_label],
                "resolution_rules": market.resolution_rules,
                "liquidity_usd": str(market.liquidity_usd),
                "volume_24h_usd": str(market.volume_24h_usd),
                "fees_enabled": market.fees_enabled,
                "fee_rate": str(market.fee_rate),
                "fee_exponent": str(market.fee_exponent),
                "fee_taker_only": market.fee_taker_only,
                "maker_rebate_rate": str(market.maker_rebate_rate),
            },
        }
        if market.resolved_outcome is not None:
            payload["resolved_outcome"] = market.resolved_outcome.value
            payload["resolution_confirmed_at"] = market.updated_at.isoformat()
        response = await self._execute(
            self.client.table("markets").upsert(payload, on_conflict="condition_id").select("id")
        )
        rows = response.data or []
        if not rows:
            response = await self._execute(
                self.client.table("markets")
                .select("id")
                .eq("condition_id", market.condition_id or market.id)
                .limit(1)
            )
            rows = response.data or []
        if not rows:
            raise RuntimeError("Supabase did not return the saved market id")
        self._market_ids[market.id] = rows[0]["id"]
        condition_id = market.condition_id or market.id
        self._condition_market_ids[condition_id] = rows[0]["id"]
        self._condition_event_ids[condition_id] = market.event_id
        self._token_outcomes[market.yes_token_id] = "YES"
        self._token_outcomes[market.no_token_id] = "NO"

    async def save_snapshot(self, snapshot: OrderBookSnapshot) -> None:
        market_id = await self._db_market_id(snapshot.market_id)
        outcome = self._token_outcomes.get(snapshot.token_id)
        payload: dict[str, Any] = {
            "market_id": market_id,
            "source": f"clob_rest:{snapshot.token_id}",
            "captured_at": snapshot.captured_at.isoformat(),
            "book": {
                "token_id": snapshot.token_id,
                "outcome": outcome,
                "bids": [level.model_dump(mode="json") for level in snapshot.bids],
                "asks": [level.model_dump(mode="json") for level in snapshot.asks],
                "tick_size": str(snapshot.tick_size),
                "minimum_order_size": str(snapshot.minimum_order_size),
            },
        }
        if outcome == "YES":
            payload.update(
                {
                    "yes_bid": str(snapshot.best_bid) if snapshot.best_bid is not None else None,
                    "yes_ask": str(snapshot.best_ask) if snapshot.best_ask is not None else None,
                }
            )
        elif outcome == "NO":
            payload.update(
                {
                    "no_bid": str(snapshot.best_bid) if snapshot.best_bid is not None else None,
                    "no_ask": str(snapshot.best_ask) if snapshot.best_ask is not None else None,
                }
            )
        await self._execute(self.client.table("snapshots").insert(payload))

    async def save_evidence(self, evidence: list[EvidenceItem]) -> None:
        if evidence:
            payloads = []
            for item in evidence:
                payloads.append(
                    {
                        "id": item.id,
                        "account_id": self.account_id,
                        "market_id": await self._db_market_id(item.market_id),
                        "source_type": "web" if item.source_url else "internal",
                        "source_id": item.id,
                        "source_url": item.source_url,
                        "source_title": item.title,
                        "published_at": (
                            item.published_at.isoformat() if item.published_at else None
                        ),
                        "fetched_at": item.retrieved_at.isoformat(),
                        "content_hash": item.content_hash,
                        "summary": item.summary,
                        "payload": {},
                        "reliability_score": str(item.reliability),
                    }
                )
            await self._execute(
                self.client.table("evidence").upsert(
                    payloads,
                    on_conflict="account_id,content_hash",
                )
            )

    async def save_forecast(self, forecast: Forecast, account_id: str) -> None:
        self._require_account(account_id)
        input_hash = hashlib.sha256(
            json.dumps(
                {
                    "market_id": forecast.market_id,
                    "source_ids": forecast.source_ids,
                    "model": forecast.model,
                    "prompt_version": forecast.prompt_version,
                    "created_at": forecast.created_at.isoformat(),
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        payload = {
            "id": forecast.id,
            "account_id": account_id,
            "market_id": await self._db_market_id(forecast.market_id),
            "as_of": forecast.created_at.isoformat(),
            "provider": forecast.model.split(":", 1)[0],
            "model": forecast.model,
            "prompt_version": forecast.prompt_version,
            "input_hash": input_hash,
            "p_yes": str(forecast.probability_yes),
            "p_low": str(forecast.probability_low),
            "p_high": str(forecast.probability_high),
            "confidence": str(forecast.confidence),
            "evidence_ids": forecast.source_ids,
            "rationale": {
                "text": forecast.rationale,
                "evidence_for": forecast.evidence_for,
                "evidence_against": forecast.evidence_against,
                "assumptions": forecast.assumptions,
            },
            "invalidation_conditions": forecast.invalidation_conditions,
            "created_at": forecast.created_at.isoformat(),
        }
        await self._execute(self.client.table("forecasts").insert(payload))

    async def forecast_is_fresh(self, market_id: str, account_id: str, max_age: timedelta) -> bool:
        self._require_account(account_id)
        from polybot.models import utc_now

        response = await self._execute(
            self.client.table("forecasts")
            .select("id")
            .eq("account_id", account_id)
            .eq("market_id", await self._db_market_id(market_id))
            .gte("created_at", (utc_now() - max_age).isoformat())
            .limit(1)
        )
        return bool(response.data)

    async def save_risk_decision(
        self, intent: TradeIntent, decision: RiskDecision, account_id: str
    ) -> None:
        self._require_account(account_id)
        self._require_account(intent.account_id)
        codes = decision.codes or ["risk_approved"]
        await self._execute(
            self.client.table("risk_events").insert(
                [
                    {
                        "account_id": account_id,
                        "market_id": await self._db_market_id(intent.market_id),
                        "severity": "info" if decision.approved else "warning",
                        "code": code,
                        "message": (
                            "deterministic risk gates approved intent"
                            if decision.approved
                            else f"deterministic risk gate rejected intent: {code}"
                        ),
                        "action": "notify" if decision.approved else "reject",
                        "details": {
                            **decision.details,
                            "intent_hash": intent.intent_hash,
                            "approved": decision.approved,
                        },
                    }
                    for code in codes
                ]
            )
        )

    async def reserve_intent(self, intent: TradeIntent) -> bool:
        self._require_account(intent.account_id)
        payload = {
            **intent.model_dump(mode="json"),
            "market_id": await self._db_market_id(intent.market_id),
            "status": "risk_approved",
        }
        try:
            await self._execute(self.client.table("order_intents").insert(payload))
        except APIError as exc:
            if str(getattr(exc, "code", "")) == "23505" or "duplicate" in str(exc).lower():
                return False
            raise
        self._intents[intent.intent_hash] = intent
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
        self._require_account(intent.account_id)
        normalized_order_type = order_type.upper()
        if normalized_order_type == "GTD" and expires_at is None:
            raise ValueError("GTD signed orders require an exchange expiry")
        await self._execute(
            self.client.table("order_intents")
            .update(
                {
                    "order_type": normalized_order_type,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                }
            )
            .eq("id", str(intent.id))
            .eq("account_id", intent.account_id)
        )
        payload = {
            "account_id": intent.account_id,
            "order_intent_id": str(intent.id),
            "client_order_id": intent.intent_hash,
            "signed_order_hash": signed_order_hash,
            "signed_payload_ciphertext": f"\\x{payload_ciphertext.hex()}",
            "payload_key_version": key_version,
            "worker_fencing_token": fencing_token,
            "environment": intent.mode.value,
            "outcome_token_id": intent.token_id,
            "side": intent.side.value,
            "order_type": normalized_order_type,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "limit_price": str(intent.price),
            "original_size": str(intent.size),
            "filled_size": "0",
            "remaining_size": str(intent.size),
            "status": "signed",
            "attempt_count": 0,
        }
        try:
            await self._execute(self.client.table("orders").insert(payload))
        except APIError as exc:
            duplicate = str(getattr(exc, "code", "")) == "23505" or "duplicate" in str(exc).lower()
            if not duplicate:
                raise
            response = await self._execute(
                self.client.table("orders")
                .select("signed_order_hash")
                .eq("account_id", intent.account_id)
                .eq("client_order_id", intent.intent_hash)
                .limit(1)
            )
            rows = response.data or []
            if not rows or rows[0]["signed_order_hash"] != signed_order_hash:
                raise RuntimeError("signed order idempotency conflict") from exc

    async def mark_order_submitting(
        self,
        intent_hash: str,
        account_id: str,
        fencing_token: int,
        *,
        control_version: int | None = None,
    ) -> None:
        self._require_account(account_id)
        personal_scope = self._personal_execution_scope
        if personal_scope is not None:
            if control_version is None:
                raise RuntimeError("personal submission requires a runtime-control version")
            response = await self._execute(
                self.client.rpc(
                    "mark_personal_order_submitting",
                    {
                        "p_account_id": account_id,
                        "p_intent_hash": intent_hash,
                        "p_owner_id": personal_scope.owner_id,
                        "p_worker_fencing_token": fencing_token,
                        "p_control_version": control_version,
                        "p_mode": personal_scope.mode.value,
                    },
                )
            )
            if not _rpc_boolean(response.data, "mark_personal_order_submitting"):
                raise RuntimeError(
                    "personal wallet, runtime control, readiness, or worker lease "
                    "changed before submission"
                )
            return
        fence = self._tenant_execution_fence
        if fence is not None:
            if control_version is None:
                raise RuntimeError("tenant submission requires a runtime-control version")
            response = await self._execute(
                self.client.rpc(
                    "mark_tenant_order_submitting",
                    {
                        "p_account_id": account_id,
                        "p_intent_hash": intent_hash,
                        "p_worker_fencing_token": fencing_token,
                        "p_control_version": control_version,
                        "p_job_id": fence.job_id,
                        "p_claimed_by": fence.claimed_by,
                        "p_job_fencing_token": fence.job_fencing_token,
                        "p_profile_version": fence.profile_version,
                        "p_risk_policy_id": fence.risk_policy_id,
                        "p_risk_policy_version": fence.risk_policy_version,
                        "p_mode": fence.mode.value,
                    },
                )
            )
            if not _rpc_boolean(response.data, "mark_tenant_order_submitting"):
                raise RuntimeError(
                    "tenant job, profile, risk, control, wallet, or lease fence "
                    "changed before submission"
                )
            return
        response = await self._execute(
            self.client.rpc(
                "mark_order_submitting",
                {
                    "p_account_id": account_id,
                    "p_intent_hash": intent_hash,
                    "p_fencing_token": fencing_token,
                },
            )
        )
        if not _rpc_boolean(response.data, "mark_order_submitting"):
            raise RuntimeError(
                "signed order or its active worker lease was unavailable for submission"
            )

    async def save_execution(self, result: ExecutionResult, account_id: str) -> None:
        self._require_account(account_id)
        intent = self._intents.get(result.intent_hash)
        if intent is None:
            response = await self._execute(
                self.client.table("order_intents")
                .select("*")
                .eq("intent_hash", result.intent_hash)
                .limit(1)
            )
            rows = response.data or []
            if not rows:
                raise RuntimeError("execution has no durable order intent")
            row = rows[0]
            intent_id = row["id"]
            mode = row["mode"]
            token_id = row["token_id"]
            side = row["side"]
            price = Decimal(str(row["price"]))
            size = Decimal(str(row["size"]))
        else:
            intent_id = str(intent.id)
            mode = intent.mode.value
            token_id = intent.token_id
            side = intent.side.value
            price = intent.price
            size = intent.size
        status_map = {
            "paper_filled": "simulated",
            "shadowed": "simulated",
            "accepted": "submitted",
            "rejected": "rejected",
            "duplicate": "rejected",
            "cancelled": "cancelled",
            "error": "unknown",
        }
        intent_status_map = {
            "paper_filled": "simulated",
            "shadowed": "simulated",
            "accepted": "submitted",
            "rejected": "rejected",
            "duplicate": "rejected",
            "cancelled": "cancelled",
            "error": "failed",
        }
        filled = min(result.filled_size, size)
        execution_order_type = str(result.raw.get("order_type") or "GTC").upper()
        execution_expires_at = result.raw.get("expires_at")
        payload = {
            "account_id": account_id,
            "order_intent_id": intent_id,
            "client_order_id": result.intent_hash,
            "clob_order_id": result.order_id if mode in {"canary", "live"} else None,
            "environment": mode,
            "outcome_token_id": token_id,
            "side": side,
            "order_type": execution_order_type,
            "expires_at": execution_expires_at,
            "limit_price": str(price),
            "original_size": str(size),
            "filled_size": str(filled),
            "remaining_size": str(max(Decimal("0"), size - filled)),
            "average_fill_price": (
                str(result.average_price) if result.average_price is not None else None
            ),
            "status": status_map[result.status.value],
            "attempt_count": 1,
            "last_error_detail": (
                result.message if result.status.value in {"error", "rejected"} else None
            ),
            "response_payload": {"message": result.message, **result.raw},
            "submitted_at": (
                result.created_at.isoformat()
                if result.status.value in {"accepted", "paper_filled"}
                else None
            ),
        }
        update_intent_status = True
        existing = await self._execute(
            self.client.table("orders")
            .select(
                "id,status,clob_order_id,original_size,filled_size,remaining_size,"
                "average_fill_price,attempt_count,last_error_detail,response_payload,"
                "submitted_at,order_type,expires_at"
            )
            .eq("account_id", account_id)
            .eq("client_order_id", result.intent_hash)
            .limit(1)
        )
        if existing.data:
            payload = _merge_execution_order_payload(existing.data[0], payload)
            update_intent_status = payload["status"] == status_map[result.status.value]
            await self._execute(
                self.client.table("orders")
                .update(payload)
                .eq("account_id", account_id)
                .eq("client_order_id", result.intent_hash)
            )
        else:
            await self._execute(self.client.table("orders").insert(payload))
        if update_intent_status:
            await self._execute(
                self.client.table("order_intents")
                .update({"status": intent_status_map[result.status.value]})
                .eq("id", intent_id)
                .in_(
                    "status",
                    ["proposed", "risk_approved", "signed", "submitted"],
                )
            )

    async def get_runtime_control(self, account_id: str) -> RuntimeControl:
        self._require_account(account_id)
        response = await self._execute(
            self.client.table("runtime_controls").select("*").eq("account_id", account_id).limit(1)
        )
        rows = response.data or []
        if not rows:
            return RuntimeControl(
                account_id=account_id,
                mode=TradingMode.PAPER,
                armed=False,
                kill_switch=True,
            )
        return RuntimeControl.model_validate(rows[0])

    async def set_runtime_control(self, control: RuntimeControl) -> RuntimeControl:
        self._require_account(control.account_id)
        payload = control.model_dump(mode="json")
        response = await self._execute(
            self.client.table("runtime_controls").upsert(payload, on_conflict="account_id")
        )
        row = (response.data or [payload])[0]
        return RuntimeControl.model_validate(row)

    async def arm_runtime_control(
        self,
        account_id: str,
        mode: str,
        armed_until: datetime,
        expected_version: int,
    ) -> RuntimeControl | None:
        self._require_account(account_id)
        response = await self._execute(
            self.client.rpc(
                "arm_runtime_control",
                {
                    "p_account_id": account_id,
                    "p_mode": mode,
                    "p_armed_until": armed_until.isoformat(),
                    "p_expected_version": expected_version,
                },
            )
        )
        rows = response.data or []
        if not rows:
            return None
        row = rows[0] if isinstance(rows, list) else rows
        return RuntimeControl.model_validate(row)

    async def expire_runtime_control(
        self,
        account_id: str,
        mode: str,
        expected_version: int,
    ) -> RuntimeControl | None:
        self._require_account(account_id)
        response = await self._execute(
            self.client.rpc(
                "expire_runtime_control",
                {
                    "p_account_id": account_id,
                    "p_mode": mode,
                    "p_expected_version": expected_version,
                },
            )
        )
        rows = response.data or []
        if not rows:
            return None
        row = rows[0] if isinstance(rows, list) else rows
        return RuntimeControl.model_validate(row)

    async def disarm_runtime_control(self, account_id: str, mode: str) -> RuntimeControl:
        self._require_account(account_id)
        response = await self._execute(
            self.client.rpc(
                "disarm_runtime_control",
                {
                    "p_account_id": account_id,
                    "p_mode": mode,
                },
            )
        )
        rows = response.data or []
        if not rows:
            raise RuntimeError("runtime control disarm did not return the durable latch")
        row = rows[0] if isinstance(rows, list) else rows
        return RuntimeControl.model_validate(row)

    async def acknowledge_runtime_cancellation(
        self, account_id: str, expected_version: int
    ) -> RuntimeControl | None:
        self._require_account(account_id)
        response = await self._execute(
            self.client.rpc(
                "acknowledge_runtime_cancellation",
                {
                    "p_account_id": account_id,
                    "p_expected_version": expected_version,
                },
            )
        )
        rows = response.data or []
        if not rows:
            return None
        row = rows[0] if isinstance(rows, list) else rows
        return RuntimeControl.model_validate(row)

    async def claim_worker_lease(
        self, account_id: str, owner_id: str, ttl: timedelta
    ) -> WorkerLease | None:
        self._require_account(account_id)
        response = await self._execute(
            self.client.rpc(
                "claim_worker_lease",
                {
                    "p_account_id": account_id,
                    "p_owner_id": owner_id,
                    "p_ttl_seconds": int(ttl.total_seconds()),
                },
            )
        )
        rows = response.data or []
        if not rows:
            return None
        first = rows[0] if isinstance(rows, list) else rows
        if not isinstance(first, dict) or not bool(first.get("claimed")):
            return None
        return WorkerLease(
            account_id=account_id,
            owner_id=owner_id,
            fencing_token=int(first["fencing_token"]),
            expires_at=first["expires_at"],
        )

    async def validate_worker_lease(
        self, account_id: str, owner_id: str, fencing_token: int
    ) -> bool:
        self._require_account(account_id)
        response = await self._execute(
            self.client.rpc(
                "validate_worker_lease",
                {
                    "p_account_id": account_id,
                    "p_owner_id": owner_id,
                    "p_fencing_token": fencing_token,
                },
            )
        )
        return _rpc_boolean(response.data, "validate_worker_lease")

    async def release_worker_lease(
        self, account_id: str, owner_id: str, fencing_token: int
    ) -> bool:
        self._require_account(account_id)
        response = await self._execute(
            self.client.rpc(
                "release_worker_lease",
                {
                    "p_account_id": account_id,
                    "p_owner_id": owner_id,
                    "p_fencing_token": fencing_token,
                },
            )
        )
        return _rpc_boolean(response.data, "release_worker_lease")

    async def has_unresolved_live_orders(self, account_id: str) -> bool:
        self._require_account(account_id)
        unknown = await self._execute(
            self.client.table("orders")
            .select("id")
            .eq("account_id", account_id)
            .eq("status", "unknown")
            .limit(1)
        )
        if unknown.data:
            return True
        inflight_response = await self._execute(
            self.client.table("orders")
            .select("id")
            .eq("account_id", account_id)
            .in_("status", ["signed", "submitting"])
            .limit(1)
        )
        return bool(inflight_response.data or [])

    async def record_equity_state(
        self,
        account_id: str,
        equity_usd: Decimal,
    ) -> EquityRiskState:
        self._require_account(account_id)
        response = await self._execute(
            self.client.rpc(
                "record_equity_state",
                {
                    "p_account_id": account_id,
                    "p_equity_pusd": str(equity_usd),
                },
            )
        )
        value = response.data
        if isinstance(value, list):
            value = value[0] if value else None
        if not isinstance(value, dict):
            raise RuntimeError("Supabase did not return durable equity risk state")
        return EquityRiskState(
            account_id=account_id,
            peak_equity_usd=Decimal(str(value["peak_equity_pusd"])),
            latest_equity_usd=Decimal(str(value["latest_equity_pusd"])),
            day_start_equity_usd=Decimal(str(value["day_start_equity_pusd"])),
            risk_day=value["risk_day"],
        )

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
        await self._execute(
            self.client.table("equity_history").insert(
                {
                    "account_id": account_id,
                    "recorded_at": utc_now().isoformat(),
                    "equity_usd": str(equity_usd),
                    "source": source,
                }
            )
        )

    async def list_equity_history(
        self,
        account_id: str,
        limit: int,
    ) -> list[EquityHistoryPoint]:
        if limit < 1:
            raise ValueError("equity history limit must be positive")
        response = await self._execute(
            self.client.table("equity_history")
            .select("recorded_at,equity_usd,source")
            .eq("account_id", account_id)
            .order("recorded_at", desc=True)
            .limit(min(limit, 1000))
        )
        rows = list(reversed(response.data or []))
        return [
            EquityHistoryPoint(
                recorded_at=datetime.fromisoformat(str(row["recorded_at"])),
                equity_usd=Decimal(str(row["equity_usd"])),
                source=str(row["source"]),
            )
            for row in rows
        ]

    async def record_ai_usage(self, account_id: str, usage: AIUsageRecord) -> None:
        if usage.total_tokens < 0:
            raise ValueError("AI usage tokens must be non-negative")
        await self._execute(
            self.client.table("ai_usage_ledger").insert(
                {
                    "account_id": account_id,
                    "market_id": usage.market_id,
                    "provider": usage.provider,
                    "model": usage.model,
                    "request_id": usage.request_id,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": usage.total_tokens,
                    "latency_ms": usage.latency_ms,
                    "cost_usd": str(usage.cost_usd) if usage.cost_usd is not None else None,
                    "created_at": usage.created_at.isoformat(),
                }
            )
        )

    async def list_ai_usage(
        self, account_id: str, limit: int
    ) -> list[AIUsageRecord]:
        if limit < 1:
            raise ValueError("AI usage limit must be positive")
        response = await self._execute(
            self.client.table("ai_usage_ledger")
            .select(
                "market_id,provider,model,request_id,input_tokens,output_tokens,"
                "total_tokens,latency_ms,cost_usd,created_at"
            )
            .eq("account_id", account_id)
            .order("created_at", desc=True)
            .limit(min(limit, 1000))
        )
        rows = [row for row in (response.data or []) if isinstance(row, dict)]
        return [
            AIUsageRecord(
                provider=str(row["provider"]),
                model=str(row["model"]),
                market_id=row.get("market_id"),
                request_id=row.get("request_id"),
                input_tokens=int(row.get("input_tokens") or 0),
                output_tokens=int(row.get("output_tokens") or 0),
                total_tokens=int(row.get("total_tokens") or 0),
                latency_ms=row.get("latency_ms"),
                cost_usd=(
                    Decimal(str(row["cost_usd"])) if row.get("cost_usd") is not None else None
                ),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            for row in reversed(rows)
        ]

    async def realized_pnl_since(self, account_id: str, since: datetime) -> Decimal:
        self._require_account(account_id)
        return (await self.fill_ledger_snapshot(account_id, since)).realized_pnl_usd

    async def fill_ledger_snapshot(self, account_id: str, since: datetime) -> FillLedgerSnapshot:
        """Replay complete bot fills into UTC PnL, quantities, and cost basis."""

        self._require_account(account_id)
        since_key = since.isoformat()
        now = asyncio.get_running_loop().time()
        cached = self._fill_ledger_cache
        if cached is not None and cached[0] == since_key and now - cached[1] <= 2:
            return cached[2]

        rows: list[dict[str, Any]] = []
        page_size = 1000
        max_rows = 100_000
        offset = 0
        while True:
            response = await self._execute(
                self.client.table("fills")
                .select(
                    "fill_key,market_id,outcome_token_id,side,price,size,fee_pusd,"
                    "settlement_status,matched_at,created_at"
                )
                .eq("account_id", account_id)
                .neq("settlement_status", "FAILED")
                .order("matched_at")
                .order("created_at")
                .range(offset, offset + page_size - 1)
            )
            page = list(response.data or [])
            rows.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
            if offset >= max_rows:
                raise IncompleteFillLedgerError(
                    "fill ledger exceeded the bounded replay limit; compact it before trading"
                )

        market_conditions: dict[str, str] = {}
        market_ids = sorted({str(row["market_id"]) for row in rows if row.get("market_id")})
        for start in range(0, len(market_ids), 100):
            response = await self._execute(
                self.client.table("markets")
                .select("id,condition_id")
                .in_("id", market_ids[start : start + 100])
            )
            market_conditions.update(
                {str(row["id"]): str(row["condition_id"]) for row in response.data or []}
            )
        for row in rows:
            row["condition_id"] = market_conditions.get(str(row.get("market_id") or ""), "")

        activities: list[dict[str, Any]] = []
        offset = 0
        while True:
            response = await self._execute(
                self.client.table("account_activities")
                .select("activity_key,activity_type,condition_id,amount_pusd,occurred_at")
                .eq("account_id", account_id)
                .order("occurred_at")
                .range(offset, offset + page_size - 1)
            )
            page = list(response.data or [])
            activities.extend(
                {
                    **row,
                    "amount_usd": row.get("amount_pusd", 0),
                }
                for row in page
            )
            if len(page) < page_size:
                break
            offset += page_size
            if offset >= max_rows:
                raise IncompleteFillLedgerError(
                    "account activity ledger exceeded the bounded replay limit"
                )

        value = replay_fill_ledger(rows, since=since, activities=activities)
        self._fill_ledger_cache = (since_key, now, value)
        return value

    async def event_ids_for_conditions(self, condition_ids: set[str]) -> dict[str, str]:
        wanted = {str(condition_id) for condition_id in condition_ids if condition_id}
        missing = sorted(wanted.difference(self._condition_event_ids))
        chunk_size = 100
        for start in range(0, len(missing), chunk_size):
            chunk = missing[start : start + chunk_size]
            response = await self._execute(
                self.client.table("markets")
                .select("condition_id,event_id")
                .in_("condition_id", chunk)
            )
            returned: set[str] = set()
            for row in response.data or []:
                condition_id = str(row["condition_id"])
                event_id = row.get("event_id")
                self._condition_event_ids[condition_id] = (
                    str(event_id) if event_id is not None else None
                )
                returned.add(condition_id)
            for condition_id in set(chunk).difference(returned):
                self._condition_event_ids[condition_id] = None
        return {
            condition_id: event_id
            for condition_id in wanted
            if (event_id := self._condition_event_ids.get(condition_id)) is not None
        }

    async def position_outcomes(
        self, positions: set[tuple[str, str]]
    ) -> dict[tuple[str, str], Outcome]:
        wanted = {
            (str(condition_id), str(token_id))
            for condition_id, token_id in positions
            if condition_id and token_id
        }
        if not wanted:
            return {}
        by_condition: dict[str, set[str]] = {}
        for condition_id, token_id in wanted:
            by_condition.setdefault(condition_id, set()).add(token_id)
        outcomes: dict[tuple[str, str], Outcome] = {}
        condition_ids = sorted(by_condition)
        for start in range(0, len(condition_ids), 100):
            response = await self._execute(
                self.client.table("markets")
                .select("condition_id,clob_token_ids,outcomes")
                .in_("condition_id", condition_ids[start : start + 100])
            )
            for row in response.data or []:
                condition_id = str(row["condition_id"])
                tokens = [str(value) for value in (row.get("clob_token_ids") or [])]
                labels = [str(value).upper() for value in (row.get("outcomes") or [])]
                for token_id, label in zip(tokens, labels, strict=False):
                    key = (condition_id, token_id)
                    if key in wanted and label in {Outcome.YES.value, Outcome.NO.value}:
                        outcomes[key] = Outcome(label)
        return outcomes

    async def durable_order_ids(self, candidate_order_ids: set[str], account_id: str) -> set[str]:
        self._require_account(account_id)
        candidates = sorted(str(value) for value in candidate_order_ids if value)
        durable: set[str] = set()
        for start in range(0, len(candidates), 100):
            chunk = candidates[start : start + 100]
            response = await self._execute(
                self.client.table("orders")
                .select("clob_order_id")
                .eq("account_id", account_id)
                .in_("clob_order_id", chunk)
            )
            durable.update(
                str(row["clob_order_id"]) for row in response.data or [] if row.get("clob_order_id")
            )
        return durable

    async def pending_trade_ids(self, account_id: str) -> set[str]:
        self._require_account(account_id)
        pending: set[str] = set()
        response = await self._execute(
            self.client.table("orders")
            .select("response_payload")
            .eq("account_id", account_id)
            .in_(
                "status",
                ["submitted", "live", "partially_filled", "matched", "mined", "unknown"],
            )
            .limit(10_000)
        )
        for row in response.data or []:
            payload = row.get("response_payload") or {}
            values = payload.get("trade_ids") or payload.get("tradeIDs") or ()
            if isinstance(values, (list, tuple)):
                pending.update(str(value) for value in values if value)

        response = await self._execute(
            self.client.table("fills")
            .select("clob_trade_id")
            .eq("account_id", account_id)
            .in_("settlement_status", ["MATCHED", "MINED", "RETRYING"])
            .limit(10_000)
        )
        pending.update(
            str(row["clob_trade_id"]) for row in response.data or [] if row.get("clob_trade_id")
        )

        terminal: set[str] = set()
        candidates = sorted(pending)
        for start in range(0, len(candidates), 100):
            response = await self._execute(
                self.client.table("fills")
                .select("clob_trade_id")
                .eq("account_id", account_id)
                .in_("clob_trade_id", candidates[start : start + 100])
                .in_("settlement_status", ["CONFIRMED", "FAILED"])
            )
            terminal.update(
                str(row["clob_trade_id"]) for row in response.data or [] if row.get("clob_trade_id")
            )
        return pending - terminal

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
