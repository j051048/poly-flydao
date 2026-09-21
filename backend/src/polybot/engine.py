from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from polybot.ai.base import EvidenceCollector
from polybot.ai.evidence import source_identity
from polybot.ai.graph import ForecastGraph
from polybot.brokers.base import Broker
from polybot.config import Settings, TradingMode
from polybot.market import MarketData
from polybot.metrics import Metrics
from polybot.models import (
    EngineCycleResult,
    ExecutionResult,
    ExecutionStatus,
    ForecastRequest,
    MarketSpec,
    PortfolioState,
    TradeIntent,
    utc_now,
)
from polybot.risk import RiskEngine
from polybot.stores.base import StateStore
from polybot.strategy import ValueStrategy

LOGGER = logging.getLogger(__name__)
METRICS = Metrics()

#: Provider requests spent per forecast: one primary pass and one critic pass.
AI_UNITS_PER_FORECAST = 2


class LiveSafetyLatchError(RuntimeError):
    """Raised when a real-money stop cannot be both persisted and verified."""


class _LivePortfolioUnavailable(RuntimeError):
    def __init__(self, checkpoint: str, cause: Exception):
        super().__init__(f"{checkpoint}:{type(cause).__name__}")
        self.checkpoint = checkpoint
        self.cause_name = type(cause).__name__


class _LiveHardRiskBreach(RuntimeError):
    def __init__(self, checkpoint: str):
        super().__init__(checkpoint)
        self.checkpoint = checkpoint


class TradingEngine:
    """One auditable scan-to-execution cycle."""

    def __init__(
        self,
        *,
        settings: Settings,
        market_data: MarketData,
        evidence_collector: EvidenceCollector,
        forecaster: ForecastGraph,
        strategy: ValueStrategy,
        risk: RiskEngine,
        broker: Broker,
        store: StateStore,
        execution_guard: Callable[[], Awaitable[bool]] | None = None,
        quarantined_tokens: Callable[[], frozenset[str]] | None = None,
        market_filter: Callable[[list[MarketSpec]], list[MarketSpec]] | None = None,
    ):
        self.settings = settings
        self.market_data = market_data
        self.evidence_collector = evidence_collector
        self.forecaster = forecaster
        self.strategy = strategy
        self.risk = risk
        self.broker = broker
        self.store = store
        self.execution_guard = execution_guard
        # Tokens holding inventory the bot did not create. Trading them could
        # net against unowned shares, so the engine never touches them.
        self.quarantined_tokens = quarantined_tokens
        # Optional universe narrowing (e.g. short-horizon crypto Up/Down only).
        # It is a pure candidate filter: it can only remove markets from the
        # discovery set. A market that still holds inventory is never dropped,
        # but when the filter excluded it the engine may only reduce that
        # position and will not open a new one.
        self.market_filter = market_filter
        # Per-cycle latch: once the daily AI budget refuses a reservation we stop
        # asking, so an exhausted budget costs one database call per cycle.
        self._ai_budget_blocked = False

    @property
    def _is_real_money(self) -> bool:
        return self.settings.mode in {TradingMode.CANARY, TradingMode.LIVE}

    async def _portfolio_checkpoint(self, checkpoint: str) -> PortfolioState:
        try:
            portfolio = await self.broker.portfolio_state()
        except Exception as exc:
            if self._is_real_money:
                raise _LivePortfolioUnavailable(checkpoint, exc) from exc
            raise
        if self._is_real_money:
            if (
                portfolio.equity_usd is None
                or portfolio.day_start_equity_usd is None
                or portfolio.peak_equity_usd is None
            ):
                cause = RuntimeError(
                    "live equity, UTC day-start equity and peak equity are required"
                )
                raise _LivePortfolioUnavailable(checkpoint, cause)
            if self.strategy.hard_risk_breached(portfolio):
                raise _LiveHardRiskBreach(checkpoint)
        return portfolio

    async def _persist_stop_and_verify_cancellation(
        self,
        *,
        reason: str,
        report: EngineCycleResult,
    ) -> None:
        """Persist a real-money kill latch before verifying exchange cancellation."""

        control = None
        current = None
        try:
            current = await self.store.get_runtime_control(self.settings.account_id)
        except Exception as exc:
            # A failed read must not prevent the unconditional disarm RPC. The
            # write itself is the safety boundary.
            report.skip(f"live_stop_control_read_failed:{type(exc).__name__}")

        persistence_error: Exception | None = None
        try:
            already_stopped = bool(
                current is not None
                and current.mode is self.settings.mode
                and not current.armed
                and not current.accept_new_intents
                and current.armed_until is None
                and current.kill_switch
            )
            control = (
                current
                if already_stopped
                else await self.store.disarm_runtime_control(
                    self.settings.account_id,
                    self.settings.mode,
                )
            )
            if control.armed or control.accept_new_intents or not control.kill_switch:
                raise RuntimeError("durable runtime control did not enter the stopped state")
        except Exception as exc:
            persistence_error = exc
            report.skip(f"live_stop_persistence_failed:{type(exc).__name__}")

        cancellation_error: Exception | None = None
        cancellation_verified = False
        try:
            cancellation_verified = await self.broker.cancel_all(reason)
            if not cancellation_verified:
                raise RuntimeError("cancel-all returned without zero-open-order verification")
        except Exception as exc:
            cancellation_error = exc
            report.skip(f"live_stop_cancel_failed:{type(exc).__name__}")

        if (
            persistence_error is None
            and cancellation_error is None
            and cancellation_verified
            and control is not None
            and control.cancellation_pending
        ):
            try:
                unresolved = await self.store.has_unresolved_live_orders(
                    self.settings.account_id
                )
                if not unresolved:
                    acknowledged = await self.store.acknowledge_runtime_cancellation(
                        self.settings.account_id,
                        control.version,
                    )
                    if acknowledged is None:
                        report.skip("live_stop_acknowledgement_deferred")
                else:
                    report.skip("live_stop_unresolved_order")
            except Exception as exc:
                # The durable kill remains active. The worker watcher will retry
                # acknowledgement, and re-arm remains blocked meanwhile.
                report.skip(f"live_stop_acknowledgement_failed:{type(exc).__name__}")

        if persistence_error is not None or cancellation_error is not None:
            raise LiveSafetyLatchError(
                "real-money safety stop was not fully persisted and verified"
            ) from (persistence_error or cancellation_error)

        report.skip(f"live_stop_latched:{reason}")

    async def run_cycle(self) -> EngineCycleResult:
        run_id = str(uuid4())
        report = EngineCycleResult(run_id=run_id, mode=self.settings.mode)
        self._ai_budget_blocked = False
        METRICS.increment("polybot_cycles_total", {"mode": self.settings.mode.value})
        if not await self.store.health():
            report.skip("state_store_unhealthy")
            report.completed_at = utc_now()
            return report

        try:
            cycle_portfolio = await self._portfolio_checkpoint("cycle_start")
        except _LivePortfolioUnavailable as exc:
            report.skip(f"portfolio_unavailable:{exc.cause_name}")
            await self._persist_stop_and_verify_cancellation(
                reason=f"portfolio unavailable at {exc.checkpoint}",
                report=report,
            )
            report.completed_at = utc_now()
            return report
        except _LiveHardRiskBreach as exc:
            report.skip(f"hard_risk_breach:{exc.checkpoint}")
            await self._persist_stop_and_verify_cancellation(
                reason=f"hard risk breach at {exc.checkpoint}",
                report=report,
            )
            report.completed_at = utc_now()
            return report
        except Exception as exc:
            report.skip(f"portfolio_unavailable:{type(exc).__name__}")
            report.completed_at = utc_now()
            return report

        if cycle_portfolio.equity_usd is not None:
            try:
                await self.store.record_equity_history(
                    self.settings.account_id,
                    cycle_portfolio.equity_usd,
                    source=f"{self.settings.mode.value}_cycle",
                )
            except Exception:
                LOGGER.exception(
                    "equity history recording failed; cycle continues",
                    extra={"run_id": run_id},
                )

        hard_risk_breach = self.strategy.hard_risk_breached(cycle_portfolio)
        if hard_risk_breach:
            try:
                cancellation_verified = await self.broker.cancel_all(
                    "hard daily-loss/drawdown breach"
                )
            except Exception as exc:
                report.skip(f"hard_risk_cancel_failed:{type(exc).__name__}")
                report.completed_at = utc_now()
                return report
            if not cancellation_verified:
                report.skip("hard_risk_cancel_unverified")
                report.completed_at = utc_now()
                return report
            try:
                # Cancelling BUYs releases cash; cancelling SELLs releases shares.
                # Refresh before discovering and sizing the liquidation candidates.
                cycle_portfolio = await self.broker.portfolio_state()
            except Exception as exc:
                report.skip(f"post_cancel_portfolio_unavailable:{type(exc).__name__}")
                report.completed_at = utc_now()
                return report

        try:
            markets = await self.market_data.list_markets(self.settings.market_limit)
        except Exception:
            report.skip("market_discovery_failed")
            report.completed_at = utc_now()
            return report

        markets, held_market_ids = await self._include_held_markets(
            markets, report, cycle_portfolio
        )
        # Narrow the candidate universe before any budget is spent. Held
        # positions survive the filter so they can still be exited, but they
        # are marked reduce-only to keep the filter from being bypassed.
        reduce_only_market_ids: set[str] = set()
        if self.market_filter is not None:
            allowed_market_ids = {market.id for market in self.market_filter(markets)}
            narrowed: list[MarketSpec] = []
            for market in markets:
                if market.id in allowed_market_ids:
                    narrowed.append(market)
                elif market.id in held_market_ids:
                    narrowed.append(market)
                    reduce_only_market_ids.add(market.id)
                else:
                    report.skip("market_filter_excluded")
            markets = narrowed
        if hard_risk_breach:
            # Once the portfolio is in a hard-stop state, do not spend AI budget or
            # evaluate entries. Only markets containing held shares are actionable.
            markets = [market for market in markets if market.id in held_market_ids]
        report.markets_scanned = len(markets)

        markets = sorted(
            markets,
            key=lambda item: (
                item.id in held_market_ids,
                item.liquidity_usd,
                item.volume_24h_usd,
            ),
            reverse=True,
        )
        ai_markets = 0
        for market in markets:
            try:
                used_ai = await self._process_market(
                    market,
                    report,
                    reduce_only=market.id in reduce_only_market_ids,
                    allow_ai=(
                        not hard_risk_breach
                        and market.id not in reduce_only_market_ids
                        and ai_markets < self.settings.max_ai_markets_per_cycle
                    ),
                )
                ai_markets += int(used_ai)
            except _LivePortfolioUnavailable as exc:
                report.skip(f"portfolio_unavailable:{exc.cause_name}")
                await self._persist_stop_and_verify_cancellation(
                    reason=f"portfolio unavailable at {exc.checkpoint}",
                    report=report,
                )
                break
            except _LiveHardRiskBreach as exc:
                report.skip(f"hard_risk_breach:{exc.checkpoint}")
                await self._persist_stop_and_verify_cancellation(
                    reason=f"hard risk breach at {exc.checkpoint}",
                    report=report,
                )
                break
            except Exception as exc:
                # A single malformed market/model response must not terminate the cycle.
                report.skip(f"market_error:{type(exc).__name__}")
        for code, count in report.skipped.items():
            METRICS.increment("polybot_cycle_skips_total", {"code": code}, amount=count)
        report.completed_at = utc_now()
        return report

    async def _include_held_markets(
        self,
        markets: list[MarketSpec],
        report: EngineCycleResult,
        portfolio: PortfolioState,
    ) -> tuple[list[MarketSpec], set[str]]:
        """Add exact markets for held tokens omitted by top-N discovery.

        Single-market lookup failures are recorded in the cycle report and fail
        closed for that holding without stopping other markets.
        """

        result = list(markets)
        held_tokens = {token_id for token_id, size in portfolio.token_positions.items() if size > 0}
        held_market_ids = {
            market.id
            for market in result
            if held_tokens.intersection({market.yes_token_id, market.no_token_id})
        }
        listed_tokens = {
            token_id for market in result for token_id in (market.yes_token_id, market.no_token_id)
        }
        listed_conditions = {
            market.condition_id for market in result if market.condition_id is not None
        }
        attempted_conditions: set[str] = set()

        for token_id, size in portfolio.token_positions.items():
            if size <= 0 or token_id in listed_tokens:
                continue
            condition_id = portfolio.token_condition_ids.get(token_id)
            if not condition_id:
                report.skip("held_market_identifier_missing")
                continue
            if condition_id in listed_conditions:
                # The condition is present but its advertised outcomes do not
                # contain the held asset. Trading against it would be unsafe.
                report.skip("held_market_token_mismatch")
                continue
            if condition_id in attempted_conditions:
                continue
            attempted_conditions.add(condition_id)

            try:
                market = await self.market_data.get_market_by_condition(condition_id)
            except Exception as exc:
                report.skip(f"held_market_lookup_error:{type(exc).__name__}")
                continue
            if market.condition_id not in {None, condition_id} and market.id != condition_id:
                report.skip("held_market_condition_mismatch")
                continue

            expected_event_id = portfolio.token_event_ids.get(token_id)
            if (
                expected_event_id is not None
                and market.event_id is not None
                and market.event_id != expected_event_id
            ):
                report.skip("held_market_event_mismatch")
                continue
            market_tokens = {market.yes_token_id, market.no_token_id}
            if token_id not in market_tokens:
                report.skip("held_market_token_mismatch")
                continue

            result.append(market)
            held_market_ids.add(market.id)
            listed_tokens.update(market_tokens)
            if market.condition_id is not None:
                listed_conditions.add(market.condition_id)

        return result, held_market_ids

    async def _reserve_ai_budget(self, report: EngineCycleResult) -> bool:
        """Reserve the provider requests one forecast will spend.

        This is the hard stop behind P2-3: the reservation happens before the
        evidence/forecast calls, so an exhausted daily budget cannot be
        exceeded. A store that cannot answer fails closed for the cycle instead
        of spending without a budget check.
        """

        if self._ai_budget_blocked:
            report.skip("ai_budget_exhausted")
            return False
        try:
            allowed, used, limit = await self.store.consume_ai_budget(
                self.settings.account_id, units=AI_UNITS_PER_FORECAST
            )
        except Exception as exc:
            self._ai_budget_blocked = True
            report.skip(f"ai_budget_unavailable:{type(exc).__name__}")
            LOGGER.warning(
                "AI budget reservation failed; skipping AI for this cycle",
                exc_info=True,
            )
            return False
        if not allowed:
            self._ai_budget_blocked = True
            report.skip("ai_budget_exhausted")
            LOGGER.warning(
                "daily AI budget exhausted (%s/%s); no new forecasts this cycle",
                used,
                limit,
            )
            METRICS.increment("polybot_ai_budget_exhausted_total")
            return False
        report.ai_units_reserved += AI_UNITS_PER_FORECAST
        return True

    async def _process_market(
        self,
        market,
        report: EngineCycleResult,
        *,
        allow_ai: bool = True,
        reduce_only: bool = False,
    ) -> bool:
        if not market.active or market.closed or not market.accepting_orders:
            report.skip("market_not_tradeable")
            return False
        if not market.yes_token_id or not market.no_token_id:
            report.skip("missing_outcome_tokens")
            return False
        if self.quarantined_tokens is not None:
            blocked = self.quarantined_tokens()
            if blocked and {market.yes_token_id, market.no_token_id} & blocked:
                report.skip("quarantined_token")
                return False

        await self.store.save_market(market)
        yes_book, no_book = await asyncio.gather(
            self.market_data.get_order_book(market.id, market.yes_token_id),
            self.market_data.get_order_book(market.id, market.no_token_id),
        )
        await asyncio.gather(
            self.store.save_snapshot(yes_book),
            self.store.save_snapshot(no_book),
        )
        portfolio = await self._portfolio_checkpoint("market_pre_ai")
        risk_exit = self.strategy.choose_risk_exit(market, yes_book, no_book, portfolio)
        if risk_exit is not None:
            report.candidates_created += 1
            await self._execute_candidate(market, risk_exit, portfolio, yes_book, no_book, report)
            return False

        if not allow_ai:
            # A market the candidate filter excluded but that still holds
            # inventory may only be reduced, never extended.
            report.skip("market_filter_reduce_only" if reduce_only else "ai_cycle_budget")
            return False
        if market.liquidity_usd < self.settings.min_liquidity_usd:
            report.skip("market_liquidity_screen")
            return False
        fresh = await self.store.forecast_is_fresh(
            market.id,
            self.settings.account_id,
            timedelta(seconds=self.settings.forecast_cooldown_seconds),
        )
        if fresh:
            report.skip("forecast_cooldown")
            return False
        if yes_book.best_ask is None or no_book.best_ask is None:
            report.skip("empty_order_book")
            return False

        if not await self._reserve_ai_budget(report):
            return False

        try:
            evidence = await self.evidence_collector.collect(market)
            await self.store.save_evidence(evidence)
        except Exception as exc:
            report.skip(f"evidence_pipeline_error:{type(exc).__name__}")
            return True
        live_mode = self.settings.mode in {TradingMode.CANARY, TradingMode.LIVE}
        url_evidence = {
            item.id: item
            for item in evidence
            if source_identity(item.source_url) is not None
        }
        distinct_evidence = {
            identity
            for item in url_evidence.values()
            if (identity := source_identity(item.source_url)) is not None
        }
        if live_mode and len(distinct_evidence) < self.settings.min_evidence_items:
            report.skip("insufficient_distinct_evidence")
            return True
        try:
            forecast = await self.forecaster.forecast(
                ForecastRequest(market=market, evidence=evidence)
            )
        except Exception as exc:
            report.skip(f"forecast_generation_error:{type(exc).__name__}")
            return True
        usage_records = self.forecaster.drain_usage()
        for usage in usage_records:
            if usage.cost_usd is not None:
                report.ai_cost_usd = (report.ai_cost_usd or Decimal("0")) + usage.cost_usd
            try:
                await self.store.record_ai_usage(self.settings.account_id, usage)
                METRICS.increment(
                    "polybot_ai_calls_total",
                    {"provider": usage.provider, "model": usage.model},
                )
                METRICS.observe("polybot_ai_latency_ms", float(usage.latency_ms or 0))
            except Exception:
                LOGGER.exception(
                    "AI usage ledger write failed; cycle continues",
                    extra={"market_id": market.id},
                )
        try:
            await self.store.save_forecast(forecast, self.settings.account_id)
        except Exception as exc:
            report.skip(f"forecast_persistence_error:{type(exc).__name__}")
            return True
        report.forecasts_created += 1
        cited_sources = {
            identity
            for source_id in set(forecast.source_ids)
            if (item := url_evidence.get(source_id)) is not None
            if (identity := source_identity(item.source_url)) is not None
        }
        if live_mode and len(cited_sources) < self.settings.min_evidence_items:
            report.skip("forecast_insufficient_citations")
            return True

        # Evidence search and two model calls can outlive the book-age budget.
        # Reprice and refresh the portfolio after AI so no order is sized from
        # the pre-forecast snapshot or stale cash/position state.
        yes_book, no_book = await asyncio.gather(
            self.market_data.get_order_book(market.id, market.yes_token_id),
            self.market_data.get_order_book(market.id, market.no_token_id),
        )
        await asyncio.gather(
            self.store.save_snapshot(yes_book),
            self.store.save_snapshot(no_book),
        )
        portfolio = await self._portfolio_checkpoint("market_post_ai")

        candidate = self.strategy.choose(market, forecast, yes_book, no_book, portfolio)
        if candidate is None:
            report.skip("no_positive_value_candidate")
            return True
        report.candidates_created += 1

        await self._execute_candidate(market, candidate, portfolio, yes_book, no_book, report)
        return True

    async def _execute_candidate(
        self, market, candidate, portfolio, yes_book, no_book, report: EngineCycleResult
    ) -> None:
        intent = TradeIntent.from_candidate(
            candidate,
            account_id=self.settings.account_id,
            run_id=report.run_id,
            mode=self.settings.mode,
        )
        selected_book = yes_book if candidate.token_id == market.yes_token_id else no_book
        decision = self.risk.evaluate_candidate(candidate, market, selected_book, portfolio)
        await self.store.save_risk_decision(intent, decision, self.settings.account_id)
        if not decision.approved:
            for code in decision.codes:
                report.skip(f"risk:{code}")
            return

        if self.execution_guard is not None and not await self.execution_guard():
            report.skip("execution_guard_closed")
            return

        reserved = await self.store.reserve_intent(intent)
        if not reserved:
            duplicate = ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.DUPLICATE,
                message="durable intent hash already exists",
            )
            report.executions.append(duplicate)
            report.skip("duplicate_intent")
            return
        report.intents_approved += 1
        # The engine-level guard is deliberately checked before reservation.
        # Live brokers independently revalidate unresolved orders, control,
        # reconciliation, lease/fencing and book age through the POST boundary.
        # A second generic guard here could strand a durable intent without an
        # execution record if it closed after reservation.
        execution = await self.broker.submit(intent, selected_book)
        await self.store.save_execution(execution, self.settings.account_id)
        report.executions.append(execution)
