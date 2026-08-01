from __future__ import annotations

import inspect
from dataclasses import dataclass

from polybot.ai.base import EvidenceCollector, ForecastProvider
from polybot.ai.evidence import (
    GDELTNewsEvidenceCollector,
    NoopEvidenceCollector,
    OpenAIWebEvidenceCollector,
)
from polybot.ai.graph import ForecastGraph
from polybot.ai.mock import SafeMockForecastProvider
from polybot.ai.openai_provider import OpenAIForecastProvider
from polybot.brokers.base import Broker
from polybot.brokers.paper import ControlPlaneBroker, PaperBroker, ShadowBroker
from polybot.brokers.polymarket import PolymarketBroker
from polybot.config import Settings, TradingMode, get_settings
from polybot.engine import TradingEngine
from polybot.market import StreamingPolymarketMarketData
from polybot.reconcile import OrderReconciler
from polybot.risk import RiskEngine
from polybot.stores.base import StateStore
from polybot.stores.memory import MemoryStore
from polybot.stores.supabase_store import SupabaseStore
from polybot.strategy import ValueStrategy


@dataclass
class Runtime:
    settings: Settings
    store: StateStore
    broker: Broker
    engine: TradingEngine
    market_data: StreamingPolymarketMarketData
    forecast_provider: ForecastProvider
    reconciler: OrderReconciler | None = None

    async def close(self) -> None:
        close_provider = getattr(self.forecast_provider, "close", None)
        if close_provider is not None:
            result = close_provider()
            if inspect.isawaitable(result):
                await result
        if self.reconciler is not None:
            await self.reconciler.close()
        close_broker = getattr(self.broker, "close", None)
        if close_broker is not None:
            result = close_broker()
            if inspect.isawaitable(result):
                await result
        await self.market_data.close()


def _store(settings: Settings) -> StateStore:
    if settings.uses_supabase:
        return SupabaseStore(
            settings.supabase_url,
            settings.supabase_service_role_key.get_secret_value(),
            account_id=settings.account_id,
        )
    if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
        raise ValueError("canary/live requires durable Supabase idempotency and runtime controls")
    return MemoryStore()


def _ai(settings: Settings) -> tuple[ForecastProvider, EvidenceCollector]:
    provider_name = settings.ai_provider.lower()
    if provider_name == "mock":
        return SafeMockForecastProvider(), NoopEvidenceCollector()
    if provider_name == "openai":
        if settings.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY is required for POLYBOT_AI_PROVIDER=openai")
        key = settings.openai_api_key.get_secret_value()
        return (
            OpenAIForecastProvider(key, timeout_seconds=settings.ai_timeout_seconds),
            _evidence_collector(settings, default="openai_web"),
        )
    if provider_name == "litellm":
        from polybot.ai.litellm_provider import LiteLLMForecastProvider

        if settings.litellm_api_key is None:
            raise ValueError("LITELLM_API_KEY is required for POLYBOT_AI_PROVIDER=litellm")
        return (
            LiteLLMForecastProvider(
                api_key=settings.litellm_api_key.get_secret_value(),
                api_base=settings.litellm_base_url,
                timeout_seconds=settings.ai_timeout_seconds,
            ),
            _evidence_collector(settings, default="gdelt"),
        )
    if provider_name == "openai_compatible":
        from polybot.ai.openai_compatible_provider import (
            OpenAICompatibleForecastProvider,
        )

        if settings.litellm_api_key is None:
            raise ValueError(
                "LITELLM_API_KEY is required for POLYBOT_AI_PROVIDER=openai_compatible"
            )
        return (
            OpenAICompatibleForecastProvider(
                api_key=settings.litellm_api_key.get_secret_value(),
                api_base=settings.litellm_base_url,
                timeout_seconds=settings.ai_timeout_seconds,
                allowed_hosts=settings.custom_ai_allowed_hosts,
            ),
            _evidence_collector(settings, default="gdelt"),
        )
    raise ValueError(f"unsupported AI provider: {settings.ai_provider}")


def _evidence_collector(
    settings: Settings,
    *,
    default: str,
) -> EvidenceCollector:
    name = default if settings.evidence_provider == "auto" else settings.evidence_provider
    if name == "none":
        return NoopEvidenceCollector()
    if name == "gdelt":
        return GDELTNewsEvidenceCollector(timeout_seconds=min(settings.ai_timeout_seconds, 30))
    if name == "openai_web":
        if settings.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY is required for POLYBOT_EVIDENCE_PROVIDER=openai_web")
        return OpenAIWebEvidenceCollector(
            settings.openai_api_key.get_secret_value(),
            model=settings.forecast_model,
            timeout_seconds=settings.ai_timeout_seconds,
        )
    raise ValueError(f"unsupported evidence provider: {name}")


def build_runtime(
    settings: Settings | None = None,
    *,
    store_override: StateStore | None = None,
    broker_override: Broker | None = None,
) -> Runtime:
    settings = settings or get_settings()
    store = store_override or _store(settings)
    provider, collector = _ai(settings)
    graph = ForecastGraph(
        provider,
        primary_model=settings.forecast_model,
        critic_model=settings.critic_model,
    )
    market_data = StreamingPolymarketMarketData(max_cache_age_seconds=settings.max_book_age_seconds)
    reconciler: OrderReconciler | None = None
    if broker_override is not None:
        broker = broker_override
    elif settings.mode is TradingMode.PAPER:
        broker: Broker = PaperBroker(settings.bankroll_usd)
    elif settings.mode is TradingMode.SHADOW:
        broker = ShadowBroker(settings.bankroll_usd)
    elif settings.component == "api":
        broker = ControlPlaneBroker(settings.bankroll_usd)
    else:
        broker = PolymarketBroker(settings, store)
        reconciler = OrderReconciler(
            private_key=settings.polymarket_private_key.get_secret_value(),
            wallet=settings.polymarket_deposit_wallet,
            account_id=settings.account_id,
            store=store,
            interval_seconds=settings.reconcile_interval_seconds,
            market_data=market_data,
        )
    engine = TradingEngine(
        settings=settings,
        market_data=market_data,
        evidence_collector=collector,
        forecaster=graph,
        strategy=ValueStrategy(settings),
        risk=RiskEngine(settings),
        broker=broker,
        store=store,
    )
    return Runtime(
        settings=settings,
        store=store,
        broker=broker,
        engine=engine,
        market_data=market_data,
        forecast_provider=provider,
        reconciler=reconciler,
    )
