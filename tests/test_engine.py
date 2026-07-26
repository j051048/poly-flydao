from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from polybot.ai.evidence import NoopEvidenceCollector
from polybot.ai.graph import ForecastGraph
from polybot.ai.mock import StaticForecastProvider
from polybot.brokers.paper import PaperBroker
from polybot.config import TradingMode
from polybot.engine import LiveSafetyLatchError, TradingEngine
from polybot.market import StaticMarketData
from polybot.models import EvidenceItem, ExecutionStatus, PortfolioState, utc_now
from polybot.risk import RiskEngine
from polybot.stores.memory import MemoryStore
from polybot.strategy import ValueStrategy


async def _arm_canary(store: MemoryStore, account_id: str) -> None:
    current = await store.get_runtime_control(account_id)
    armed = await store.arm_runtime_control(
        account_id,
        TradingMode.CANARY,
        utc_now() + timedelta(minutes=5),
        current.version,
    )
    assert armed is not None
    assert armed.is_live_armed


async def test_end_to_end_paper_cycle(settings, market, forecast, yes_book, no_book) -> None:
    store = MemoryStore()
    broker = PaperBroker(Decimal("1000"))
    provider = StaticForecastProvider(forecast)
    engine = TradingEngine(
        settings=settings,
        market_data=StaticMarketData(
            [market], {market.yes_token_id: yes_book, market.no_token_id: no_book}
        ),
        evidence_collector=NoopEvidenceCollector(),
        forecaster=ForecastGraph(provider, primary_model="primary", critic_model="critic"),
        strategy=ValueStrategy(settings),
        risk=RiskEngine(settings),
        broker=broker,
        store=store,
    )
    report = await engine.run_cycle()
    assert report.markets_scanned == 1
    assert report.forecasts_created == 1
    assert report.intents_approved == 1
    assert len(report.executions) == 1
    assert report.executions[0].status is ExecutionStatus.PAPER_FILLED
    assert len(store.intents) == 1


async def test_closed_execution_guard_prevents_reservation(
    settings, market, forecast, yes_book, no_book
) -> None:
    async def closed() -> bool:
        return False

    store = MemoryStore()
    provider = StaticForecastProvider(forecast)
    engine = TradingEngine(
        settings=settings,
        market_data=StaticMarketData(
            [market], {market.yes_token_id: yes_book, market.no_token_id: no_book}
        ),
        evidence_collector=NoopEvidenceCollector(),
        forecaster=ForecastGraph(provider, primary_model="p", critic_model="c"),
        strategy=ValueStrategy(settings),
        risk=RiskEngine(settings),
        broker=PaperBroker(Decimal("1000")),
        store=store,
        execution_guard=closed,
    )
    report = await engine.run_cycle()
    assert report.skipped["execution_guard_closed"] == 1
    assert not store.intents


async def test_execution_guard_does_not_strand_an_intent_after_reservation(
    settings, market, forecast, yes_book, no_book
) -> None:
    calls = 0

    async def closes_after_first_check() -> bool:
        nonlocal calls
        calls += 1
        return calls == 1

    store = MemoryStore()
    engine = TradingEngine(
        settings=settings,
        market_data=StaticMarketData(
            [market], {market.yes_token_id: yes_book, market.no_token_id: no_book}
        ),
        evidence_collector=NoopEvidenceCollector(),
        forecaster=ForecastGraph(
            StaticForecastProvider(forecast), primary_model="p", critic_model="c"
        ),
        strategy=ValueStrategy(settings),
        risk=RiskEngine(settings),
        broker=PaperBroker(Decimal("1000")),
        store=store,
        execution_guard=closes_after_first_check,
    )

    report = await engine.run_cycle()

    assert calls == 1
    assert report.executions[0].status is ExecutionStatus.PAPER_FILLED
    assert store.executions


async def test_ai_cycle_reprices_after_forecast(
    settings, market, forecast, yes_book, no_book
) -> None:
    class RepricingMarketData:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def list_markets(self, limit: int):
            return [market]

        async def get_market_by_condition(self, condition_id: str):
            return market

        async def get_order_book(self, market_id: str, token_id: str):
            count = self.calls.get(token_id, 0)
            self.calls[token_id] = count + 1
            book = yes_book if token_id == market.yes_token_id else no_book
            if count == 0 or token_id != market.yes_token_id:
                return book
            # The attractive YES ask disappears while evidence/forecasting runs.
            return book.model_copy(
                update={"asks": [book.asks[0].model_copy(update={"price": Decimal("0.80")})]}
            )

    source = RepricingMarketData()
    store = MemoryStore()
    engine = TradingEngine(
        settings=settings,
        market_data=source,
        evidence_collector=NoopEvidenceCollector(),
        forecaster=ForecastGraph(
            StaticForecastProvider(forecast), primary_model="p", critic_model="c"
        ),
        strategy=ValueStrategy(settings),
        risk=RiskEngine(settings),
        broker=PaperBroker(Decimal("1000")),
        store=store,
    )

    report = await engine.run_cycle()

    assert source.calls == {market.yes_token_id: 2, market.no_token_id: 2}
    assert report.forecasts_created == 1
    assert not report.executions
    assert report.skipped["no_positive_value_candidate"] == 1


async def test_real_money_mode_requires_distinct_cited_evidence(
    settings, market, forecast, yes_book, no_book
) -> None:
    live_settings = settings.model_copy(update={"mode": TradingMode.CANARY})
    engine = TradingEngine(
        settings=live_settings,
        market_data=StaticMarketData(
            [market], {market.yes_token_id: yes_book, market.no_token_id: no_book}
        ),
        evidence_collector=NoopEvidenceCollector(),
        forecaster=ForecastGraph(
            StaticForecastProvider(forecast), primary_model="p", critic_model="c"
        ),
        strategy=ValueStrategy(live_settings),
        risk=RiskEngine(live_settings),
        broker=PaperBroker(Decimal("1000")),
        store=MemoryStore(),
    )

    report = await engine.run_cycle()

    assert report.forecasts_created == 0
    assert not report.executions
    assert report.skipped["insufficient_distinct_evidence"] == 1


async def test_real_money_evidence_requires_distinct_publishers(
    settings, market, forecast, yes_book, no_book
) -> None:
    evidence = [
        EvidenceItem(
            id="source-1",
            market_id=market.id,
            title="Report one",
            summary="First summary",
            source_url="https://example.com/story-one",
        ),
        EvidenceItem(
            id="source-2",
            market_id=market.id,
            title="Report two",
            summary="Second summary",
            source_url="https://www.example.com/story-two",
        ),
    ]

    class StaticEvidence:
        async def collect(self, market):
            return evidence

    live_settings = settings.model_copy(update={"mode": TradingMode.CANARY})
    cited = forecast.model_copy(update={"source_ids": ["source-1", "source-2"]})
    engine = TradingEngine(
        settings=live_settings,
        market_data=StaticMarketData(
            [market], {market.yes_token_id: yes_book, market.no_token_id: no_book}
        ),
        evidence_collector=StaticEvidence(),
        forecaster=ForecastGraph(
            StaticForecastProvider(cited), primary_model="p", critic_model="c"
        ),
        strategy=ValueStrategy(live_settings),
        risk=RiskEngine(live_settings),
        broker=PaperBroker(Decimal("1000")),
        store=MemoryStore(),
    )

    report = await engine.run_cycle()

    assert report.forecasts_created == 0
    assert report.skipped["insufficient_distinct_evidence"] == 1


async def test_real_money_forecast_must_cite_url_backed_sources(
    settings, market, forecast, yes_book, no_book
) -> None:
    evidence = [
        EvidenceItem(
            id="url-1",
            market_id=market.id,
            title="Publisher A",
            summary="A",
            source_url="https://publisher-a.com/report",
        ),
        EvidenceItem(
            id="url-2",
            market_id=market.id,
            title="Publisher B",
            summary="B",
            source_url="https://publisher-b.net/report",
        ),
        EvidenceItem(
            id="no-url",
            market_id=market.id,
            title="Unverifiable note",
            summary="No URL",
        ),
    ]

    class StaticEvidence:
        async def collect(self, market):
            return evidence

    live_settings = settings.model_copy(update={"mode": TradingMode.CANARY})
    weak_citations = forecast.model_copy(update={"source_ids": ["url-1", "no-url"]})
    engine = TradingEngine(
        settings=live_settings,
        market_data=StaticMarketData(
            [market], {market.yes_token_id: yes_book, market.no_token_id: no_book}
        ),
        evidence_collector=StaticEvidence(),
        forecaster=ForecastGraph(
            StaticForecastProvider(weak_citations), primary_model="p", critic_model="c"
        ),
        strategy=ValueStrategy(live_settings),
        risk=RiskEngine(live_settings),
        broker=PaperBroker(Decimal("1000")),
        store=MemoryStore(),
    )

    report = await engine.run_cycle()

    assert report.forecasts_created == 1
    assert report.skipped["forecast_insufficient_citations"] == 1


async def test_failed_ai_pipeline_still_consumes_cycle_budget(
    settings, market, forecast, yes_book, no_book
) -> None:
    class BrokenEvidenceCollector:
        async def collect(self, market):
            raise TimeoutError("provider timeout")

    second = market.model_copy(
        update={
            "id": "m2",
            "condition_id": "c2",
            "event_id": "e2",
            "yes_token_id": "yes-2",
            "no_token_id": "no-2",
        }
    )
    books = {
        market.yes_token_id: yes_book,
        market.no_token_id: no_book,
        second.yes_token_id: yes_book.model_copy(update={"token_id": second.yes_token_id}),
        second.no_token_id: no_book.model_copy(update={"token_id": second.no_token_id}),
    }
    bounded = settings.model_copy(update={"max_ai_markets_per_cycle": 1})
    engine = TradingEngine(
        settings=bounded,
        market_data=StaticMarketData([market, second], books),
        evidence_collector=BrokenEvidenceCollector(),
        forecaster=ForecastGraph(
            StaticForecastProvider(forecast), primary_model="p", critic_model="c"
        ),
        strategy=ValueStrategy(bounded),
        risk=RiskEngine(bounded),
        broker=PaperBroker(Decimal("1000")),
        store=MemoryStore(),
    )

    report = await engine.run_cycle()

    assert report.skipped["evidence_pipeline_error:TimeoutError"] == 1
    assert report.skipped["ai_cycle_budget"] == 1


async def test_live_portfolio_failure_persists_stop_and_verifies_cancel(
    settings, forecast
) -> None:
    class ReadFailsOnceStore(MemoryStore):
        fail_next_control_read = False

        async def get_runtime_control(self, account_id: str):
            if self.fail_next_control_read:
                self.fail_next_control_read = False
                raise ConnectionError("transient control read failure")
            return await super().get_runtime_control(account_id)

    class UnavailablePortfolioBroker:
        def __init__(self) -> None:
            self.cancel_reasons: list[str] = []

        async def portfolio_state(self):
            raise RuntimeError("equity cannot be valued")

        async def submit(self, intent, book):
            raise AssertionError("an unavailable live portfolio must never submit")

        async def cancel_all(self, reason: str) -> bool:
            self.cancel_reasons.append(reason)
            return True

        async def redeem_resolved(self) -> int:
            return 0

    live_settings = settings.model_copy(update={"mode": TradingMode.CANARY})
    store = ReadFailsOnceStore()
    await _arm_canary(store, live_settings.account_id)
    store.fail_next_control_read = True
    broker = UnavailablePortfolioBroker()
    engine = TradingEngine(
        settings=live_settings,
        market_data=StaticMarketData([], {}),
        evidence_collector=NoopEvidenceCollector(),
        forecaster=ForecastGraph(
            StaticForecastProvider(forecast), primary_model="p", critic_model="c"
        ),
        strategy=ValueStrategy(live_settings),
        risk=RiskEngine(live_settings),
        broker=broker,
        store=store,
    )

    report = await engine.run_cycle()

    control = await store.get_runtime_control(live_settings.account_id)
    assert report.skipped["portfolio_unavailable:RuntimeError"] == 1
    assert report.skipped["live_stop_control_read_failed:ConnectionError"] == 1
    assert report.skipped["live_stop_latched:portfolio unavailable at cycle_start"] == 1
    assert broker.cancel_reasons == ["portfolio unavailable at cycle_start"]
    assert control.kill_switch
    assert not control.armed
    assert not control.accept_new_intents
    assert not control.cancellation_pending


async def test_live_missing_equity_fields_are_treated_as_portfolio_failure(
    settings, forecast
) -> None:
    class MissingEquityBroker:
        cancelled = False

        async def portfolio_state(self) -> PortfolioState:
            return PortfolioState(
                bankroll_usd=Decimal("1000"),
                cash_usd=Decimal("1000"),
            )

        async def submit(self, intent, book):
            raise AssertionError("missing live equity must prevent submission")

        async def cancel_all(self, reason: str) -> bool:
            self.cancelled = True
            return True

        async def redeem_resolved(self) -> int:
            return 0

    live_settings = settings.model_copy(update={"mode": TradingMode.CANARY})
    store = MemoryStore()
    await _arm_canary(store, live_settings.account_id)
    broker = MissingEquityBroker()
    engine = TradingEngine(
        settings=live_settings,
        market_data=StaticMarketData([], {}),
        evidence_collector=NoopEvidenceCollector(),
        forecaster=ForecastGraph(
            StaticForecastProvider(forecast), primary_model="p", critic_model="c"
        ),
        strategy=ValueStrategy(live_settings),
        risk=RiskEngine(live_settings),
        broker=broker,
        store=store,
    )

    report = await engine.run_cycle()

    control = await store.get_runtime_control(live_settings.account_id)
    assert report.skipped["portfolio_unavailable:RuntimeError"] == 1
    assert broker.cancelled
    assert control.kill_switch
    assert not control.armed


async def test_mid_cycle_live_breach_stops_remaining_markets_and_stays_disarmed(
    settings, market, forecast, yes_book, no_book
) -> None:
    second = market.model_copy(
        update={
            "id": "m2",
            "condition_id": "c2",
            "event_id": "e2",
            "yes_token_id": "yes-2",
            "no_token_id": "no-2",
            "liquidity_usd": Decimal("1000"),
        }
    )

    class RecordingMarketData:
        def __init__(self) -> None:
            self.book_market_ids: list[str] = []

        async def list_markets(self, limit: int):
            return [market, second]

        async def get_market_by_condition(self, condition_id: str):
            raise AssertionError("no held-market lookup is expected")

        async def get_order_book(self, market_id: str, token_id: str):
            self.book_market_ids.append(market_id)
            source = yes_book if token_id in {market.yes_token_id, second.yes_token_id} else no_book
            return source.model_copy(update={"market_id": market_id, "token_id": token_id})

    safe = PortfolioState(
        bankroll_usd=Decimal("1000"),
        cash_usd=Decimal("1000"),
        equity_usd=Decimal("1000"),
        day_start_equity_usd=Decimal("1000"),
        peak_equity_usd=Decimal("1000"),
    )
    breached = safe.model_copy(
        update={
            "cash_usd": Decimal("970"),
            "equity_usd": Decimal("970"),
            "realized_pnl_today_usd": Decimal("-30"),
        }
    )

    class BreachingBroker:
        def __init__(self) -> None:
            self.portfolio_calls = 0
            self.cancel_reasons: list[str] = []

        async def portfolio_state(self) -> PortfolioState:
            self.portfolio_calls += 1
            return breached if self.portfolio_calls == 2 else safe

        async def submit(self, intent, book):
            raise AssertionError("a hard-breached live cycle must never submit")

        async def cancel_all(self, reason: str) -> bool:
            self.cancel_reasons.append(reason)
            return True

        async def redeem_resolved(self) -> int:
            return 0

    live_settings = settings.model_copy(update={"mode": TradingMode.CANARY})
    store = MemoryStore()
    await _arm_canary(store, live_settings.account_id)
    source = RecordingMarketData()
    broker = BreachingBroker()
    engine = TradingEngine(
        settings=live_settings,
        market_data=source,
        evidence_collector=NoopEvidenceCollector(),
        forecaster=ForecastGraph(
            StaticForecastProvider(forecast), primary_model="p", critic_model="c"
        ),
        strategy=ValueStrategy(live_settings),
        risk=RiskEngine(live_settings),
        broker=broker,
        store=store,
    )

    report = await engine.run_cycle()

    latched = await store.get_runtime_control(live_settings.account_id)
    assert report.skipped["hard_risk_breach:market_pre_ai"] == 1
    assert report.skipped["live_stop_latched:hard risk breach at market_pre_ai"] == 1
    assert source.book_market_ids == [market.id, market.id]
    assert broker.cancel_reasons == ["hard risk breach at market_pre_ai"]
    assert not report.executions
    assert latched.kill_switch
    assert not latched.armed
    assert not latched.accept_new_intents

    # A later safe portfolio (including after a UTC day rollover) cannot mutate
    # the durable kill latch. Only an explicit control-plane arm can do so.
    version = latched.version
    await engine.run_cycle()
    still_latched = await store.get_runtime_control(live_settings.account_id)
    assert still_latched.version == version
    assert not still_latched.armed
    manually_armed = await store.arm_runtime_control(
        live_settings.account_id,
        TradingMode.CANARY,
        utc_now() + timedelta(minutes=5),
        still_latched.version,
    )
    assert manually_armed is not None
    assert manually_armed.is_live_armed


async def test_live_stop_cancel_failure_is_loud_and_remains_latched(
    settings, forecast
) -> None:
    class CancelFailureBroker:
        async def portfolio_state(self):
            raise RuntimeError("equity unavailable")

        async def submit(self, intent, book):
            raise AssertionError("must not submit")

        async def cancel_all(self, reason: str) -> bool:
            return False

        async def redeem_resolved(self) -> int:
            return 0

    live_settings = settings.model_copy(update={"mode": TradingMode.CANARY})
    store = MemoryStore()
    await _arm_canary(store, live_settings.account_id)
    engine = TradingEngine(
        settings=live_settings,
        market_data=StaticMarketData([], {}),
        evidence_collector=NoopEvidenceCollector(),
        forecaster=ForecastGraph(
            StaticForecastProvider(forecast), primary_model="p", critic_model="c"
        ),
        strategy=ValueStrategy(live_settings),
        risk=RiskEngine(live_settings),
        broker=CancelFailureBroker(),
        store=store,
    )

    with pytest.raises(LiveSafetyLatchError):
        await engine.run_cycle()

    control = await store.get_runtime_control(live_settings.account_id)
    assert control.kill_switch
    assert not control.armed
    assert control.cancellation_pending
