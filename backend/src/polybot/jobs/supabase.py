"""Service-role control-plane repository backed by Supabase/PostgREST."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from postgrest.exceptions import APIError

from polybot.config import TradingMode
from polybot.credentials import AIProvider
from polybot.jobs.schemas import (
    AccountNotReadyError,
    AIDiagnosticJob,
    CycleJob,
    CycleJobRequest,
    CycleJobStatus,
    JobConflictError,
    PerformanceSnapshot,
    PortfolioSnapshot,
    RiskPolicySnapshot,
    RuntimeProfile,
    RuntimeProfilePatch,
    WorkerStatusSnapshot,
    WorkerTradingWallet,
    _decimal_or_none,
    _decimal_or_zero,
    _decimal_text,
    _inside_window,
    _job_from_row,
    _parse_datetime,
    _performance_snapshot,
    _profile_from_row,
    _ratio_text,
)
from polybot.models import AIUsageRecord, EquityHistoryPoint, RuntimeControl, utc_now
from polybot.schema import EXPECTED_SCHEMA_VERSION


class SupabaseJobRepository:
    """Service-role control-plane repository with explicit per-call tenant scope."""

    def __init__(self, client: Any):
        self._client = client

    async def _execute(self, builder: Any) -> Any:
        try:
            return await asyncio.to_thread(builder.execute)
        except APIError as exc:
            if str(exc.code) in {"23505", "40001"}:
                raise JobConflictError("optimistic concurrency conflict") from exc
            raise

    @staticmethod
    def _first(response: Any) -> dict[str, Any] | None:
        data = getattr(response, "data", None)
        if isinstance(data, list):
            return data[0] if data and isinstance(data[0], dict) else None
        return data if isinstance(data, dict) else None

    async def health(self) -> bool:
        try:
            # Include the latest schema-contract column so a deployment that
            # forgot to apply migrations fails readiness instead of accepting
            # jobs that the worker cannot later complete.
            await self._execute(
                self._client.table("cycle_jobs").select("id,result_summary").limit(1)
            )
            version_response = await self._execute(self._client.rpc("polybot_schema_version", {}))
            version_data = getattr(version_response, "data", None)
            if isinstance(version_data, list):
                version_data = version_data[0] if version_data else None
            if isinstance(version_data, dict):
                version_data = version_data.get(
                    "polybot_schema_version",
                    version_data.get("version"),
                )
            return version_data == EXPECTED_SCHEMA_VERSION
        except Exception:
            return False

    async def get_or_create_profile(self, account_id: str) -> RuntimeProfile:
        response = await self._execute(
            self._client.rpc(
                "ensure_account_runtime_profile",
                {"p_account_id": account_id},
            )
        )
        row = self._first(response)
        if row is None:
            raise AccountNotReadyError("runtime profile is unavailable")
        return _profile_from_row(row)

    async def update_profile(self, account_id: str, patch: RuntimeProfilePatch) -> RuntimeProfile:
        response = await self._execute(
            self._client.rpc(
                "update_account_runtime_profile",
                {
                    "p_account_id": account_id,
                    "p_expected_version": patch.expected_version,
                    "p_ai_provider": patch.ai_provider.value,
                    "p_ai_base_url": patch.ai_base_url,
                    "p_forecast_model": patch.forecast_model,
                    "p_ai_credential_id": (
                        str(patch.ai_credential_id) if patch.ai_credential_id else None
                    ),
                    "p_trading_wallet_id": (
                        str(patch.trading_wallet_id) if patch.trading_wallet_id else None
                    ),
                    "p_risk_policy_id": (
                        str(patch.risk_policy_id) if patch.risk_policy_id else None
                    ),
                    "p_desired_mode": patch.desired_mode.value,
                    "p_auto_run_enabled": patch.auto_run_enabled,
                    "p_cycle_interval_seconds": patch.cycle_interval_seconds,
                },
            )
        )
        row = self._first(response)
        if row is None:
            raise JobConflictError("runtime profile changed concurrently")
        return _profile_from_row(row)

    async def enqueue(
        self,
        *,
        account_id: str,
        idempotency_key: str,
        request: CycleJobRequest,
    ) -> CycleJob:
        response = await self._execute(
            self._client.rpc(
                "enqueue_cycle_job",
                {
                    "p_account_id": account_id,
                    "p_idempotency_key": idempotency_key,
                    "p_mode": request.mode.value,
                    "p_trading_wallet_id": (
                        str(request.trading_wallet_id) if request.trading_wallet_id else None
                    ),
                    "p_ai_credential_id": (
                        str(request.ai_credential_id) if request.ai_credential_id else None
                    ),
                    "p_risk_policy_id": (
                        str(request.risk_policy_id) if request.risk_policy_id else None
                    ),
                    "p_run_after": (request.run_after.isoformat() if request.run_after else None),
                },
            )
        )
        row = self._first(response)
        if row is None:
            raise JobConflictError("cycle job could not be queued")
        return _job_from_row(row)

    async def get_job(self, *, account_id: str, job_id: UUID) -> CycleJob | None:
        response = await self._execute(
            self._client.table("cycle_jobs")
            .select(
                "id,account_id,trading_wallet_id,ai_credential_id,risk_policy_id,mode,"
                "risk_policy_version,idempotency_key,status,attempt_count,max_attempts,"
                "claimed_by,fencing_token,heartbeat_at,lease_expires_at,run_after,error_code,"
                "requested_run_after,created_at,updated_at,started_at,completed_at,result_summary"
            )
            .eq("account_id", account_id)
            .eq("id", str(job_id))
            .limit(1)
        )
        row = self._first(response)
        return _job_from_row(row) if row else None

    async def get_latest_job(self, *, account_id: str) -> CycleJob | None:
        response = await self._execute(
            self._client.table("cycle_jobs")
            .select(
                "id,account_id,trading_wallet_id,ai_credential_id,risk_policy_id,mode,"
                "risk_policy_version,idempotency_key,status,attempt_count,max_attempts,"
                "claimed_by,fencing_token,heartbeat_at,lease_expires_at,run_after,error_code,"
                "requested_run_after,created_at,updated_at,started_at,completed_at,result_summary"
            )
            .eq("account_id", account_id)
            .order("created_at", desc=True)
            .limit(1)
        )
        row = self._first(response)
        return _job_from_row(row) if row else None

    async def get_runtime_control(self, account_id: str) -> RuntimeControl:
        await self.get_or_create_profile(account_id)
        response = await self._execute(
            self._client.table("runtime_controls").select("*").eq("account_id", account_id).limit(1)
        )
        row = self._first(response)
        if row is None:
            return RuntimeControl(account_id=account_id)
        return RuntimeControl.model_validate(row)

    async def arm(
        self,
        *,
        account_id: str,
        mode: TradingMode,
        armed_until: datetime,
        expected_version: int,
    ) -> RuntimeControl | None:
        await self.assert_live_ready(account_id)
        response = await self._execute(
            self._client.rpc(
                "arm_runtime_control",
                {
                    "p_account_id": account_id,
                    "p_mode": mode.value,
                    "p_armed_until": armed_until.isoformat(),
                    "p_expected_version": expected_version,
                },
            )
        )
        row = self._first(response)
        return RuntimeControl.model_validate(row) if row else None

    async def disarm(self, *, account_id: str, mode: TradingMode) -> RuntimeControl:
        response = await self._execute(
            self._client.rpc(
                "disarm_runtime_control",
                {"p_account_id": account_id, "p_mode": mode.value},
            )
        )
        row = self._first(response)
        if row is None:
            raise JobConflictError("runtime could not be disarmed")
        return RuntimeControl.model_validate(row)

    async def assert_live_ready(self, account_id: str) -> None:
        profile = await self.get_or_create_profile(account_id)
        if (
            profile.ai_provider in {AIProvider.PLATFORM, AIProvider.MOCK}
            or profile.ai_credential_id is None
            or profile.trading_wallet_id is None
            or profile.risk_policy_id is None
        ):
            raise AccountNotReadyError(
                "active tenant AI credential, verified wallet, and risk policy are required"
            )
        credential_response, wallet_response, risk_response = await asyncio.gather(
            self._execute(
                self._client.table("credential_refs")
                .select("id")
                .eq("account_id", account_id)
                .eq("id", str(profile.ai_credential_id))
                .eq("status", "active")
                .limit(1)
            ),
            self._execute(
                self._client.table("trading_wallets")
                .select("id,chain_id,collateral_token")
                .eq("account_id", account_id)
                .eq("id", str(profile.trading_wallet_id))
                .eq("status", "active")
                .limit(1)
            ),
            self._execute(
                self._client.table("risk_policies")
                .select("id")
                .eq("account_id", account_id)
                .eq("id", str(profile.risk_policy_id))
                .eq("status", "active")
                .limit(1)
            ),
        )
        credential = self._first(credential_response)
        wallet = self._first(wallet_response)
        risk = self._first(risk_response)
        if (
            credential is None
            or risk is None
            or wallet is None
            or not wallet.get("chain_id")
            or not wallet.get("collateral_token")
        ):
            raise AccountNotReadyError(
                "active AI credential, verified wallet, and risk policy are required"
            )

    async def portfolio(
        self,
        account_id: str,
        mode: TradingMode | None = None,
    ) -> PortfolioSnapshot:
        (
            positions_response,
            orders_response,
            fills_response,
            risk_state_response,
            wallet_response,
            profile_response,
            paper_state_response,
        ) = await asyncio.gather(
            self._execute(
                self._client.table("positions")
                .select(
                    "id,market_id,outcome_token_id,outcome,shares,average_entry_price,"
                    "cost_basis_pusd,realized_pnl_pusd,mark_price,unrealized_pnl_pusd,"
                    "as_of,version,updated_at,markets(question,slug,end_at)"
                )
                .eq("account_id", account_id)
                .order("updated_at", desc=True)
                .limit(250)
            ),
            self._execute(
                self._client.table("orders")
                .select(
                    "id,order_intent_id,clob_order_id,environment,outcome_token_id,side,order_type,"
                    "limit_price,original_size,filled_size,remaining_size,status,"
                    "submitted_at,updated_at"
                )
                .eq("account_id", account_id)
                .order("updated_at", desc=True)
                .limit(250)
            ),
            self._execute(
                self._client.table("fills")
                .select(
                    "id,order_id,market_id,clob_trade_id,outcome_token_id,side,"
                    "liquidity_role,price,size,fee_pusd,settlement_status,"
                    "transaction_hash,matched_at,confirmed_at"
                )
                .eq("account_id", account_id)
                .order("matched_at", desc=True)
                .limit(100)
            ),
            self._execute(
                self._client.table("account_risk_state")
                .select(
                    "peak_equity_pusd,latest_equity_pusd,day_start_equity_pusd,risk_day,updated_at"
                )
                .eq("account_id", account_id)
                .limit(1)
            ),
            self._execute(
                self._client.table("trading_wallets")
                .select(
                    "id,label,deposit_wallet_address,signer_address,chain_id,"
                    "signature_type,status,collateral_balance_pusd,allowances_ready,"
                    "readiness_checked_at,updated_at"
                )
                .eq("account_id", account_id)
                .in_("status", ["active", "pending_verification"])
                .order("updated_at", desc=True)
                .limit(1)
            ),
            self._execute(
                self._client.table("account_runtime_profiles")
                .select("desired_mode,trading_wallet_id")
                .eq("account_id", account_id)
                .limit(1)
            ),
            self._execute(
                self._client.table("paper_account_states")
                .select("state,version,updated_at")
                .eq("account_id", account_id)
                .limit(1)
            ),
        )
        positions = [
            row
            for row in (getattr(positions_response, "data", None) or [])
            if isinstance(row, dict)
        ]
        orders = [
            row for row in (getattr(orders_response, "data", None) or []) if isinstance(row, dict)
        ]
        open_statuses = {
            "created",
            "signed",
            "submitting",
            "submitted",
            "live",
            "unknown",
            "partially_filled",
            "cancel_pending",
        }
        open_orders = [row for row in orders if str(row.get("status")) in open_statuses]
        recent_fills = [
            row for row in (getattr(fills_response, "data", None) or []) if isinstance(row, dict)
        ]
        risk_state = self._first(risk_state_response) or {}
        wallet = self._first(wallet_response)
        profile = self._first(profile_response) or {}
        paper_row = self._first(paper_state_response) or {}
        paper_state = paper_row.get("state")
        if not isinstance(paper_state, dict):
            paper_state = {}

        personal_binding: dict[str, Any] | None = None
        if mode in {TradingMode.CANARY, TradingMode.LIVE}:
            personal_response = await self._execute(
                self._client.table("personal_runtime_bindings")
                .select(
                    "account_id,deposit_wallet_address,signer_address,chain_id,"
                    "collateral_token,binding_version,paused,collateral_balance_pusd,"
                    "allowances_ready,readiness_checked_at,updated_at"
                )
                .eq("account_id", account_id)
                .limit(1)
            )
            personal_binding = self._first(personal_response)
            if wallet is None and personal_binding is not None:
                wallet = {
                    "id": f"personal:{account_id}",
                    "label": "Environment wallet",
                    "deposit_wallet_address": personal_binding.get("deposit_wallet_address"),
                    "signer_address": personal_binding.get("signer_address"),
                    "chain_id": personal_binding.get("chain_id"),
                    "collateral_token": personal_binding.get("collateral_token"),
                    "status": ("paused" if personal_binding.get("paused") else "active"),
                    "version": personal_binding.get("binding_version"),
                    "collateral_balance_pusd": personal_binding.get("collateral_balance_pusd"),
                    "allowances_ready": personal_binding.get("allowances_ready", False),
                    "readiness_checked_at": personal_binding.get("readiness_checked_at"),
                    "updated_at": personal_binding.get("updated_at"),
                }

        for position in positions:
            market = position.pop("markets", None)
            if isinstance(market, dict):
                position["market"] = market.get("question")
                position["market_slug"] = market.get("slug")
                position["market_end_at"] = market.get("end_at")
            shares = _decimal_or_zero(position.get("shares"))
            mark = _decimal_or_none(position.get("mark_price"))
            if mark is None:
                mark = _decimal_or_none(position.get("average_entry_price"))
            position["value_pusd"] = str(shares * mark) if mark is not None else None

        paper_positions_raw = paper_state.get("positions", [])
        paper_market_ids = {
            str(item.get("market_id"))
            for item in paper_positions_raw
            if isinstance(item, dict) and item.get("market_id")
        }
        paper_market_map: dict[str, dict[str, Any]] = {}
        if paper_market_ids:
            market_response = await self._execute(
                self._client.table("markets")
                .select("id,gamma_market_id,question,slug,end_at")
                .in_("gamma_market_id", sorted(paper_market_ids))
            )
            for row in getattr(market_response, "data", None) or []:
                if isinstance(row, dict):
                    for key in (row.get("gamma_market_id"), row.get("id")):
                        if key:
                            paper_market_map[str(key)] = row
        paper_positions: list[dict[str, Any]] = []
        for index, raw in enumerate(paper_positions_raw):
            if not isinstance(raw, dict):
                continue
            shares = _decimal_or_zero(raw.get("shares"))
            cost = _decimal_or_zero(raw.get("cost"))
            average = cost / shares if shares > 0 else None
            market_id = str(raw.get("market_id") or "")
            market = paper_market_map.get(market_id, {})
            paper_positions.append(
                {
                    "id": f"paper-{raw.get('token_id') or index}",
                    "market_id": market_id,
                    "market": market.get("question") or market_id,
                    "market_slug": market.get("slug"),
                    "market_end_at": market.get("end_at"),
                    "outcome_token_id": raw.get("token_id"),
                    "outcome": raw.get("outcome"),
                    "shares": str(shares),
                    "average_entry_price": str(average) if average is not None else None,
                    "mark_price": str(average) if average is not None else None,
                    "cost_basis_pusd": str(cost),
                    "value_pusd": str(cost),
                    "realized_pnl_pusd": "0",
                    "unrealized_pnl_pusd": "0",
                    "updated_at": paper_row.get("updated_at"),
                }
            )

        live_position_value = sum(
            (_decimal_or_zero(position.get("value_pusd")) for position in positions),
            Decimal("0"),
        )
        live_realized = sum(
            (_decimal_or_zero(position.get("realized_pnl_pusd")) for position in positions),
            Decimal("0"),
        )
        live_unrealized = sum(
            (_decimal_or_zero(position.get("unrealized_pnl_pusd")) for position in positions),
            Decimal("0"),
        )
        wallet_cash = _decimal_or_none(wallet.get("collateral_balance_pusd")) if wallet else None
        if wallet_cash is None and personal_binding is not None:
            wallet_cash = _decimal_or_none(personal_binding.get("collateral_balance_pusd"))
        live_equity = _decimal_or_none(risk_state.get("latest_equity_pusd"))
        if live_equity is None and wallet_cash is not None:
            live_equity = wallet_cash + live_position_value
        live_cash = wallet_cash
        if live_cash is None and live_equity is not None:
            live_cash = max(Decimal("0"), live_equity - live_position_value)
        live_summary: dict[str, Any] = {
            "position_count": len(positions),
            "open_order_count": len(open_orders),
            "recent_fill_count": len(recent_fills),
            "cash_usd": _decimal_text(live_cash),
            "available_balance_usd": _decimal_text(live_cash),
            "portfolio_value_usd": _decimal_text(live_equity),
            "total_equity_usd": _decimal_text(live_equity),
            "gross_exposure_usd": str(live_position_value),
            "gross_exposure_pct": _ratio_text(live_position_value, live_equity),
            "realized_pnl_usd": str(live_realized),
            "unrealized_pnl_usd": str(live_unrealized),
            "pnl_usd": str(live_realized + live_unrealized),
            "updated_at": risk_state.get("updated_at"),
        }
        paper_cash = _decimal_or_none(paper_state.get("cash"))
        paper_exposure = sum(
            (_decimal_or_zero(position.get("cost_basis_pusd")) for position in paper_positions),
            Decimal("0"),
        )
        paper_equity = paper_cash + paper_exposure if paper_cash is not None else None
        paper_realized = _decimal_or_none(paper_state.get("realized_pnl"))
        paper_summary: dict[str, Any] = {
            "position_count": len(paper_positions),
            "open_order_count": len(
                [row for row in open_orders if row.get("environment") == "paper"]
            ),
            "recent_fill_count": len(
                [
                    row
                    for row in orders
                    if row.get("environment") == "paper" and row.get("status") == "simulated"
                ]
            ),
            "cash_usd": _decimal_text(paper_cash),
            "available_balance_usd": _decimal_text(paper_cash),
            "portfolio_value_usd": _decimal_text(paper_equity),
            "total_equity_usd": _decimal_text(paper_equity),
            "gross_exposure_usd": str(paper_exposure),
            "gross_exposure_pct": _ratio_text(paper_exposure, paper_equity),
            "realized_pnl_usd": _decimal_text(paper_realized),
            "unrealized_pnl_usd": "0" if paper_equity is not None else None,
            "pnl_usd": _decimal_text(paper_realized),
            "updated_at": paper_row.get("updated_at"),
        }
        if mode is None:
            try:
                mode = TradingMode(str(profile.get("desired_mode") or "paper"))
            except ValueError:
                mode = TradingMode.PAPER
        shadow_summary: dict[str, Any] = {
            "position_count": 0,
            "open_order_count": 0,
            "recent_fill_count": 0,
            "cash_usd": None,
            "available_balance_usd": None,
            "portfolio_value_usd": None,
            "total_equity_usd": None,
            "gross_exposure_usd": "0",
            "gross_exposure_pct": None,
            "realized_pnl_usd": None,
            "unrealized_pnl_usd": None,
            "pnl_usd": None,
            "updated_at": None,
        }
        if mode is TradingMode.PAPER:
            selected_positions = paper_positions
            selected_summary = paper_summary
        elif mode is TradingMode.SHADOW:
            selected_positions = []
            selected_summary = shadow_summary
        else:
            selected_positions = positions
            selected_summary = live_summary
        selected_open_orders = [
            row
            for row in open_orders
            if row.get("environment") == mode.value
            or (
                mode in {TradingMode.CANARY, TradingMode.LIVE}
                and row.get("environment") in {"canary", "live"}
            )
        ]
        combined_summary = {
            **selected_summary,
            "paper": paper_summary,
            "live": live_summary,
            "shadow": shadow_summary,
        }
        return PortfolioSnapshot(
            account_id=account_id,
            positions=selected_positions,
            open_orders=selected_open_orders,
            orders=orders,
            recent_fills=recent_fills,
            summary=combined_summary,
            wallet=wallet,
            mode=mode,
        )

    async def recent_analysis(
        self,
        account_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 50:
            raise ValueError("analysis limit must be between 1 and 50")
        forecasts_response = await self._execute(
            self._client.table("forecasts")
            .select(
                "id,market_id,as_of,provider,model,p_yes,p_low,p_high,confidence,"
                "evidence_ids,rationale,invalidation_conditions,status,created_at"
            )
            .eq("account_id", account_id)
            .order("created_at", desc=True)
            .limit(limit)
        )
        forecast_rows = [
            row
            for row in (getattr(forecasts_response, "data", None) or [])
            if isinstance(row, dict)
        ]
        if not forecast_rows:
            return []

        market_ids = sorted(
            {str(row["market_id"]) for row in forecast_rows if row.get("market_id") is not None}
        )
        evidence_ids = sorted(
            {
                str(evidence_id)
                for row in forecast_rows
                for evidence_id in (
                    row.get("evidence_ids") if isinstance(row.get("evidence_ids"), list) else []
                )
            }
        )
        market_rows: list[dict[str, Any]] = []
        evidence_rows: list[dict[str, Any]] = []
        intent_rows: list[dict[str, Any]] = []
        risk_rows: list[dict[str, Any]] = []
        order_rows: list[dict[str, Any]] = []
        if market_ids:
            market_response, intent_response, risk_response = await asyncio.gather(
                self._execute(
                    self._client.table("markets")
                    .select("id,question,slug,end_at,active,closed")
                    .in_("id", market_ids)
                ),
                self._execute(
                    self._client.table("order_intents")
                    .select(
                        "id,market_id,intent_hash,outcome,side,price,size,notional_usd,"
                        "edge_after_costs,status,created_at"
                    )
                    .eq("account_id", account_id)
                    .in_("market_id", market_ids)
                    .order("created_at", desc=True)
                    .limit(200)
                ),
                self._execute(
                    self._client.table("risk_events")
                    .select("id,market_id,order_intent_id,severity,code,action,details,occurred_at")
                    .eq("account_id", account_id)
                    .in_("market_id", market_ids)
                    .order("occurred_at", desc=True)
                    .limit(300)
                ),
            )
            market_rows = [
                row
                for row in (getattr(market_response, "data", None) or [])
                if isinstance(row, dict)
            ]
            intent_rows = [
                row
                for row in (getattr(intent_response, "data", None) or [])
                if isinstance(row, dict)
            ]
            risk_rows = [
                row for row in (getattr(risk_response, "data", None) or []) if isinstance(row, dict)
            ]
            intent_ids = [str(row["id"]) for row in intent_rows if row.get("id")]
            if intent_ids:
                response = await self._execute(
                    self._client.table("orders")
                    .select(
                        "id,order_intent_id,environment,side,order_type,limit_price,"
                        "original_size,filled_size,remaining_size,average_fill_price,"
                        "status,submitted_at,updated_at"
                    )
                    .eq("account_id", account_id)
                    .in_("order_intent_id", intent_ids)
                    .order("updated_at", desc=True)
                    .limit(200)
                )
                order_rows = [
                    item
                    for item in (getattr(response, "data", None) or [])
                    if isinstance(item, dict)
                ]
        if evidence_ids:
            response = await self._execute(
                self._client.table("evidence")
                .select(
                    "id,source_url,source_title,published_at,summary,reliability_score,fetched_at"
                )
                .eq("account_id", account_id)
                .in_("id", evidence_ids)
                .limit(200)
            )
            evidence_rows = [
                row for row in (getattr(response, "data", None) or []) if isinstance(row, dict)
            ]

        markets = {str(row.get("id")): row for row in market_rows}
        evidence = {str(row.get("id")): row for row in evidence_rows}
        orders_by_intent: dict[str, list[dict[str, Any]]] = {}
        for order in order_rows:
            orders_by_intent.setdefault(str(order.get("order_intent_id")), []).append(order)
        result: list[dict[str, Any]] = []
        for row in forecast_rows:
            market = markets.get(str(row.get("market_id")), {})
            selected_evidence = [
                evidence[str(evidence_id)]
                for evidence_id in (
                    row.get("evidence_ids") if isinstance(row.get("evidence_ids"), list) else []
                )
                if str(evidence_id) in evidence
            ]
            forecast_time = _parse_datetime(row.get("created_at") or row.get("as_of"))
            window_end = forecast_time + timedelta(minutes=15) if forecast_time else None
            selected_intents = [
                intent
                for intent in intent_rows
                if str(intent.get("market_id")) == str(row.get("market_id"))
                and _inside_window(intent.get("created_at"), forecast_time, window_end)
            ]
            intent_hashes = {str(item.get("intent_hash")) for item in selected_intents}
            selected_risks = [
                risk
                for risk in risk_rows
                if str(risk.get("market_id")) == str(row.get("market_id"))
                and (
                    str((risk.get("details") or {}).get("intent_hash")) in intent_hashes
                    or _inside_window(risk.get("occurred_at"), forecast_time, window_end)
                )
            ]
            timeline: list[dict[str, Any]] = [
                {
                    "kind": "forecast",
                    "at": row.get("created_at") or row.get("as_of"),
                    "status": row.get("status"),
                    "probability_yes": row.get("p_yes"),
                    "confidence": row.get("confidence"),
                }
            ]
            timeline.extend(
                {
                    "kind": "risk",
                    "at": item.get("occurred_at"),
                    "status": item.get("action"),
                    "code": item.get("code"),
                    "severity": item.get("severity"),
                }
                for item in selected_risks
            )
            for intent in selected_intents:
                timeline.append(
                    {
                        "kind": "intent",
                        "at": intent.get("created_at"),
                        "status": intent.get("status"),
                        "outcome": intent.get("outcome"),
                        "side": intent.get("side"),
                        "price": intent.get("price"),
                        "size": intent.get("size"),
                        "notional_usd": intent.get("notional_usd"),
                        "edge_after_costs": intent.get("edge_after_costs"),
                    }
                )
                timeline.extend(
                    {
                        "kind": "order",
                        "at": order.get("updated_at"),
                        "status": order.get("status"),
                        "environment": order.get("environment"),
                        "filled_size": order.get("filled_size"),
                        "average_fill_price": order.get("average_fill_price"),
                    }
                    for order in orders_by_intent.get(str(intent.get("id")), [])
                )
            timeline.sort(key=lambda item: str(item.get("at") or ""))
            result.append(
                {
                    "id": row.get("id"),
                    "market_id": row.get("market_id"),
                    "question": market.get("question"),
                    "slug": market.get("slug"),
                    "end_at": market.get("end_at"),
                    "as_of": row.get("as_of"),
                    "provider": row.get("provider"),
                    "model": row.get("model"),
                    "probability_yes": row.get("p_yes"),
                    "probability_low": row.get("p_low"),
                    "probability_high": row.get("p_high"),
                    "confidence": row.get("confidence"),
                    "rationale": row.get("rationale"),
                    "invalidation_conditions": row.get("invalidation_conditions"),
                    "status": row.get("status"),
                    "evidence": selected_evidence,
                    "timeline": timeline,
                }
            )
        return result

    async def get_active_risk_policy(self, account_id: str) -> RiskPolicySnapshot | None:
        profile = await self.get_or_create_profile(account_id)
        if profile.risk_policy_id is None:
            return None
        response = await self._execute(
            self._client.table("risk_policies")
            .select(
                "id,account_id,version,status,max_order_usd,max_trade_risk_pct,"
                "max_event_exposure_pct,max_bucket_exposure_pct,max_gross_exposure_pct,"
                "daily_loss_limit_pct,max_drawdown_pct,min_edge,created_at"
            )
            .eq("account_id", account_id)
            .eq("id", str(profile.risk_policy_id))
            .eq("status", "active")
            .limit(1)
        )
        row = self._first(response)
        return RiskPolicySnapshot.model_validate(row) if row else None

    async def create_risk_policy_preset(
        self,
        *,
        account_id: str,
        expected_profile_version: int,
        preset: str,
    ) -> RiskPolicySnapshot:
        response = await self._execute(
            self._client.rpc(
                "create_risk_policy_preset",
                {
                    "p_account_id": account_id,
                    "p_expected_profile_version": expected_profile_version,
                    "p_preset": preset,
                },
            )
        )
        row = self._first(response)
        if row is None:
            raise JobConflictError("risk policy preset was not saved")
        return RiskPolicySnapshot.model_validate(row)

    async def worker_status(self) -> WorkerStatusSnapshot:
        response = await self._execute(
            self._client.table("worker_heartbeats")
            .select("owner_id,release,status,active_jobs,queue_lag_seconds,started_at,last_seen_at")
            .order("last_seen_at", desc=True)
            .limit(1)
        )
        row = self._first(response)
        if row is None:
            return WorkerStatusSnapshot(online=False, ready=False)
        last_seen = datetime.fromisoformat(str(row["last_seen_at"]).replace("Z", "+00:00"))
        online = last_seen >= utc_now() - timedelta(seconds=30)
        return WorkerStatusSnapshot(
            online=online,
            ready=online and row.get("status") == "ready",
            **row,
        )

    async def load_paper_state(self, account_id: str) -> dict[str, Any] | None:
        response = await self._execute(
            self._client.table("paper_account_states")
            .select("state")
            .eq("account_id", account_id)
            .limit(1)
        )
        row = self._first(response)
        state = row.get("state") if row else None
        return state if isinstance(state, dict) else None

    async def enqueue_ai_diagnostic(self, account_id: str) -> AIDiagnosticJob:
        response = await self._execute(
            self._client.rpc(
                "enqueue_ai_diagnostic",
                {"p_account_id": account_id},
            )
        )
        row = self._first(response)
        if row is None:
            raise JobConflictError("AI diagnostic could not be queued")
        return AIDiagnosticJob.model_validate(row)

    async def get_ai_diagnostic(self, *, account_id: str, job_id: UUID) -> AIDiagnosticJob | None:
        response = await self._execute(
            self._client.table("ai_diagnostic_jobs")
            .select("*")
            .eq("account_id", account_id)
            .eq("id", str(job_id))
            .limit(1)
        )
        row = self._first(response)
        return AIDiagnosticJob.model_validate(row) if row else None

    async def notifications(self, account_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        response = await self._execute(
            self._client.table("account_notifications")
            .select("id,severity,code,title,message,details,read_at,created_at")
            .eq("account_id", account_id)
            .order("created_at", desc=True)
            .limit(limit)
        )
        return [row for row in (getattr(response, "data", None) or []) if isinstance(row, dict)]

    async def mark_notification_read(self, *, account_id: str, notification_id: int) -> bool:
        response = await self._execute(
            self._client.table("account_notifications")
            .update({"read_at": utc_now().isoformat()})
            .eq("account_id", account_id)
            .eq("id", notification_id)
            .is_("read_at", "null")
            .select("id")
        )
        return self._first(response) is not None

    async def performance(self, account_id: str) -> PerformanceSnapshot:
        outcomes_response, usage_response = await asyncio.gather(
            self._execute(
                self._client.table("forecast_outcomes")
                .select("market_id,probability_yes,resolved_yes,brier_score,log_loss,resolved_at")
                .eq("account_id", account_id)
                .order("resolved_at", desc=True)
                .limit(5000)
            ),
            self._execute(
                self._client.table("ai_usage_daily")
                .select("request_units,request_limit")
                .eq("account_id", account_id)
                .eq("usage_day", utc_now().date().isoformat())
                .limit(1)
            ),
        )
        rows = [
            row for row in (getattr(outcomes_response, "data", None) or []) if isinstance(row, dict)
        ]
        usage = self._first(usage_response) or {}
        return _performance_snapshot(
            rows,
            ai_usage_used=int(usage.get("request_units") or 0),
            ai_usage_limit=int(usage.get("request_limit") or 100),
        )

    async def set_ai_budget_limit(self, *, account_id: str, request_limit: int) -> int:
        response = await self._execute(
            self._client.rpc(
                "set_ai_budget_limit",
                {
                    "p_account_id": account_id,
                    "p_request_limit": request_limit,
                },
            )
        )
        data = getattr(response, "data", None)
        if isinstance(data, list):
            data = data[0] if data else None
        if isinstance(data, dict):
            data = data.get("set_ai_budget_limit", data.get("request_limit"))
        if data is None:
            raise JobConflictError("AI budget limit was not saved")
        return int(data)

    async def list_equity_history(
        self, account_id: str, *, limit: int = 200
    ) -> list[EquityHistoryPoint]:
        if not 1 <= limit <= 1000:
            raise ValueError("equity history limit out of range")
        response = await self._execute(
            self._client.table("equity_history")
            .select("recorded_at,equity_usd,source")
            .eq("account_id", account_id)
            .order("recorded_at", desc=True)
            .limit(limit)
        )
        rows = [row for row in (getattr(response, "data", None) or []) if isinstance(row, dict)]
        return [
            EquityHistoryPoint(
                recorded_at=datetime.fromisoformat(str(row["recorded_at"])),
                equity_usd=Decimal(str(row["equity_usd"])),
                source=str(row["source"]),
            )
            for row in reversed(rows)
        ]

    async def list_ai_usage(
        self, account_id: str, *, limit: int = 50
    ) -> list[AIUsageRecord]:
        if not 1 <= limit <= 500:
            raise ValueError("AI usage limit out of range")
        response = await self._execute(
            self._client.table("ai_usage_ledger")
            .select(
                "market_id,provider,model,request_id,input_tokens,output_tokens,"
                "total_tokens,latency_ms,cost_usd,created_at"
            )
            .eq("account_id", account_id)
            .order("created_at", desc=True)
            .limit(limit)
        )
        rows = [row for row in (getattr(response, "data", None) or []) if isinstance(row, dict)]
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

    async def save_paper_state(
        self,
        *,
        job: CycleJob,
        state: dict[str, Any],
    ) -> bool:
        if job.claimed_by is None:
            return False
        response = await self._execute(
            self._client.rpc(
                "save_paper_account_state",
                {
                    "p_account_id": str(job.account_id),
                    "p_job_id": str(job.id),
                    "p_claimed_by": job.claimed_by,
                    "p_fencing_token": job.fencing_token,
                    "p_state": state,
                },
            )
        )
        data = getattr(response, "data", False)
        if isinstance(data, list):
            data = data[0] if data else False
        if isinstance(data, dict):
            data = data.get("save_paper_account_state", data.get("ok", False))
        return data is True

    async def record_worker_heartbeat(
        self,
        *,
        owner_id: str,
        release: str,
        status: str,
        active_jobs: int,
        queue_lag_seconds: Decimal | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        await self._execute(
            self._client.rpc(
                "record_worker_heartbeat",
                {
                    "p_owner_id": owner_id,
                    "p_release": release,
                    "p_status": status,
                    "p_active_jobs": active_jobs,
                    "p_queue_lag_seconds": (
                        str(queue_lag_seconds) if queue_lag_seconds is not None else None
                    ),
                    "p_details": details or {},
                },
            )
        )

    async def claim_ai_diagnostic(
        self, *, claimed_by: str, lease_seconds: int
    ) -> AIDiagnosticJob | None:
        response = await self._execute(
            self._client.rpc(
                "claim_ai_diagnostic",
                {
                    "p_claimed_by": claimed_by,
                    "p_lease_seconds": lease_seconds,
                },
            )
        )
        row = self._first(response)
        return AIDiagnosticJob.model_validate(row) if row else None

    async def finish_ai_diagnostic(
        self,
        *,
        job: AIDiagnosticJob,
        ok: bool,
        result_summary: dict[str, Any],
        error_code: str | None = None,
    ) -> AIDiagnosticJob | None:
        if job.claimed_by is None:
            return None
        response = await self._execute(
            self._client.rpc(
                "finish_ai_diagnostic",
                {
                    "p_account_id": str(job.account_id),
                    "p_job_id": str(job.id),
                    "p_claimed_by": job.claimed_by,
                    "p_fencing_token": job.fencing_token,
                    "p_ok": ok,
                    "p_result_summary": result_summary,
                    "p_error_code": error_code,
                },
            )
        )
        row = self._first(response)
        return AIDiagnosticJob.model_validate(row) if row else None

    async def consume_ai_budget(self, *, account_id: str, units: int = 1) -> tuple[bool, int, int]:
        response = await self._execute(
            self._client.rpc(
                "consume_ai_budget",
                {"p_account_id": account_id, "p_units": units},
            )
        )
        row = self._first(response) or {}
        return (
            bool(row.get("allowed", False)),
            int(row.get("used", 0)),
            int(row.get("daily_limit", 0)),
        )

    async def unresolved_market_conditions(self, *, limit: int = 100) -> list[str]:
        response = await self._execute(
            self._client.table("markets")
            .select("condition_id")
            .is_("resolved_outcome", "null")
            .lt("end_at", utc_now().isoformat())
            .order("end_at")
            .limit(limit)
        )
        return [
            str(row["condition_id"])
            for row in (getattr(response, "data", None) or [])
            if isinstance(row, dict) and row.get("condition_id")
        ]

    async def record_market_resolution(self, *, condition_id: str, outcome: str) -> int:
        response = await self._execute(
            self._client.rpc(
                "record_market_resolution",
                {"p_condition_id": condition_id, "p_outcome": outcome},
            )
        )
        data = getattr(response, "data", 0)
        if isinstance(data, list):
            data = data[0] if data else 0
        if isinstance(data, dict):
            data = data.get("record_market_resolution", data.get("count", 0))
        return int(data or 0)

    async def enqueue_due_jobs(self, *, limit: int = 100) -> int:
        response = await self._execute(
            self._client.rpc("enqueue_due_cycle_jobs", {"p_limit": limit})
        )
        data = getattr(response, "data", 0)
        if isinstance(data, list):
            data = data[0] if data else 0
        if isinstance(data, dict):
            data = data.get("enqueue_due_cycle_jobs", data.get("count", 0))
        return int(data or 0)

    async def claim_next_job(
        self,
        *,
        claimed_by: str,
        lease_seconds: int,
        account_id: str | None = None,
    ) -> CycleJob | None:
        response = await self._execute(
            self._client.rpc(
                "claim_next_cycle_job",
                {
                    "p_claimed_by": claimed_by,
                    "p_lease_seconds": lease_seconds,
                    "p_account_id": account_id,
                },
            )
        )
        row = self._first(response)
        return _job_from_row(row) if row else None

    async def heartbeat(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
        lease_seconds: int,
    ) -> bool:
        response = await self._execute(
            self._client.rpc(
                "heartbeat_cycle_job",
                {
                    "p_account_id": account_id,
                    "p_job_id": str(job_id),
                    "p_claimed_by": claimed_by,
                    "p_fencing_token": fencing_token,
                    "p_lease_seconds": lease_seconds,
                },
            )
        )
        data = getattr(response, "data", False)
        if isinstance(data, list):
            data = data[0] if data else False
        if isinstance(data, dict):
            data = data.get("heartbeat_cycle_job", data.get("ok", False))
        return data is True

    async def validate_lease(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> bool:
        response = await self._execute(
            self._client.rpc(
                "validate_cycle_job_lease",
                {
                    "p_account_id": account_id,
                    "p_job_id": str(job_id),
                    "p_claimed_by": claimed_by,
                    "p_fencing_token": fencing_token,
                },
            )
        )
        data = getattr(response, "data", False)
        if isinstance(data, list):
            data = data[0] if data else False
        if isinstance(data, dict):
            data = data.get("validate_cycle_job_lease", data.get("valid", False))
        return data is True

    async def mark_running(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> CycleJob | None:
        return await self._transition(
            "mark_cycle_job_running",
            account_id=account_id,
            job_id=job_id,
            claimed_by=claimed_by,
            fencing_token=fencing_token,
        )

    async def complete(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
        result_summary: dict[str, Any] | None = None,
    ) -> CycleJob | None:
        response = await self._execute(
            self._client.rpc(
                "complete_cycle_job_with_result",
                {
                    "p_account_id": account_id,
                    "p_job_id": str(job_id),
                    "p_claimed_by": claimed_by,
                    "p_fencing_token": fencing_token,
                    "p_result_summary": result_summary or {},
                },
            )
        )
        row = self._first(response)
        return _job_from_row(row) if row else None

    async def fail(
        self,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
        error_code: str,
        retryable: bool,
    ) -> CycleJob | None:
        response = await self._execute(
            self._client.rpc(
                "fail_cycle_job",
                {
                    "p_account_id": account_id,
                    "p_job_id": str(job_id),
                    "p_claimed_by": claimed_by,
                    "p_fencing_token": fencing_token,
                    "p_error_code": error_code,
                    "p_retryable": retryable,
                },
            )
        )
        row = self._first(response)
        saved = _job_from_row(row) if row else None
        if saved is not None and saved.status is CycleJobStatus.FAILED:
            await self._execute(
                self._client.table("account_notifications").insert(
                    {
                        "account_id": account_id,
                        "severity": "critical" if not retryable else "warning",
                        "code": "cycle_failed",
                        "title": "自动交易周期失败",
                        "message": "Worker 已停止本次周期，没有继续提交新订单。",
                        "details": {
                            "job_id": str(job_id),
                            "error_code": error_code,
                            "retryable": retryable,
                        },
                    }
                )
            )
        return saved

    async def _transition(
        self,
        function_name: str,
        *,
        account_id: str,
        job_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> CycleJob | None:
        response = await self._execute(
            self._client.rpc(
                function_name,
                {
                    "p_account_id": account_id,
                    "p_job_id": str(job_id),
                    "p_claimed_by": claimed_by,
                    "p_fencing_token": fencing_token,
                },
            )
        )
        row = self._first(response)
        return _job_from_row(row) if row else None

    async def get_worker_wallet(
        self, *, account_id: str, wallet_id: UUID
    ) -> WorkerTradingWallet | None:
        response = await self._execute(
            self._client.table("trading_wallets")
            .select(
                "id,account_id,deposit_wallet_address,signer_address,"
                "signer_credential_id,signature_type,status,version"
            )
            .eq("account_id", account_id)
            .eq("id", str(wallet_id))
            .eq("status", "active")
            .limit(1)
        )
        row = self._first(response)
        return WorkerTradingWallet.model_validate(row) if row else None

    async def get_risk_policy(
        self,
        *,
        account_id: str,
        risk_policy_id: UUID,
        expected_version: int,
    ) -> RiskPolicySnapshot | None:
        response = await self._execute(
            self._client.table("risk_policies")
            .select(
                "id,account_id,version,status,max_order_usd,max_trade_risk_pct,"
                "max_event_exposure_pct,max_bucket_exposure_pct,max_gross_exposure_pct,"
                "daily_loss_limit_pct,max_drawdown_pct,min_edge,created_at"
            )
            .eq("account_id", account_id)
            .eq("id", str(risk_policy_id))
            .eq("version", expected_version)
            .eq("status", "active")
            .limit(1)
        )
        row = self._first(response)
        return RiskPolicySnapshot.model_validate(row) if row else None
