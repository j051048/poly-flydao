from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass

from polybot.ai.base import EvidenceCollector, ForecastProvider
from polybot.ai.evidence import (
    GDELTNewsEvidenceCollector,
    NoopEvidenceCollector,
    OpenAIWebEvidenceCollector,
)
from polybot.ai.fallback import FallbackForecastProvider
from polybot.ai.graph import ForecastGraph
from polybot.ai.mock import SafeMockForecastProvider
from polybot.ai.openai_provider import OpenAIForecastProvider
from polybot.brokers.base import Broker
from polybot.brokers.mode_aware import ModeAwareBroker
from polybot.brokers.paper import ControlPlaneBroker, PaperBroker, ShadowBroker
from polybot.brokers.polymarket import PolymarketBroker
from polybot.config import Settings, TradingMode, get_settings
from polybot.engine import TradingEngine
from polybot.market import StreamingPolymarketMarketData
from polybot.market_filters.crypto_updown import (
    CryptoUpDownFilter,
    CryptoUpDownFilterConfig,
)
from polybot.models import MarketSpec
from polybot.reconcile import OrderReconciler
from polybot.risk import RiskEngine
from polybot.stores.base import StateStore
from polybot.stores.memory import MemoryStore
from polybot.stores.supabase_store import SupabaseStore
from polybot.strategy import ValueStrategy

LOGGER = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    store: StateStore
    broker: Broker
    engine: TradingEngine
    market_data: StreamingPolymarketMarketData
    forecast_provider: ForecastProvider
    reconciler: OrderReconciler | None = None
    live_broker: PolymarketBroker | None = None
    mode_aware: ModeAwareBroker | None = None

    @property
    def paper_broker(self) -> PaperBroker | None:
        """The active simulator, wherever it lives, or ``None`` for live-only."""

        if self.mode_aware is not None:
            return (
                self.mode_aware.paper
                if isinstance(self.mode_aware.paper, PaperBroker)
                else None
            )
        return self.broker if isinstance(self.broker, PaperBroker) else None

    def replace_paper_broker(self, broker: PaperBroker) -> None:
        """Install a freshly restored durable paper account without a restart."""

        if self.mode_aware is not None:
            self.mode_aware.replace_paper(broker)
            return
        self.broker = broker
        self.engine.broker = broker

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
            timeout_seconds=settings.supabase_timeout_seconds,
        )
    if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
        raise ValueError("canary/live requires durable Supabase idempotency and runtime controls")
    return MemoryStore()


def _ai(settings: Settings) -> tuple[ForecastProvider, EvidenceCollector]:
    provider_name = settings.ai_provider.lower()
    if provider_name == "mock":
        return SafeMockForecastProvider(), NoopEvidenceCollector()
    key_setting = settings.effective_ai_api_key
    if key_setting is None:
        raise ValueError(
            "an AI API key is required for a non-mock provider"
        )
    key = key_setting.get_secret_value()

    def named_provider(name: str):
        if name == "openai":
            return OpenAIForecastProvider(
                key,
                timeout_seconds=settings.ai_timeout_seconds,
            )
        if name == "litellm":
            from polybot.ai.litellm_provider import LiteLLMForecastProvider

            return LiteLLMForecastProvider(
                api_key=key,
                api_base=settings.litellm_base_url,
                timeout_seconds=settings.ai_timeout_seconds,
            )
        if name == "openai_compatible":
            from polybot.ai.openai_compatible_provider import (
                OpenAICompatibleForecastProvider,
            )

            return OpenAICompatibleForecastProvider(
                api_key=key,
                api_base=settings.litellm_base_url,
                timeout_seconds=settings.ai_timeout_seconds,
                allowed_hosts=settings.custom_ai_allowed_hosts,
            )
        raise ValueError(f"unsupported AI provider: {name}")

    providers = [named_provider(provider_name)]
    for name in settings.ai_fallback_providers.split(","):
        name = name.strip().lower()
        if not name or name == provider_name:
            continue
        providers.append(named_provider(name))
    provider: ForecastProvider
    if len(providers) > 1:
        provider = FallbackForecastProvider(providers)
    else:
        provider = providers[0]
    default_evidence = "openai_web" if provider_name == "openai" else "gdelt"
    return provider, _evidence_collector(settings, default=default_evidence)


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
        key_setting = settings.effective_ai_api_key
        if key_setting is None:
            raise ValueError(
                "OPENAI_API_KEY or POLYBOT_AI_API_KEY is required for "
                "POLYBOT_EVIDENCE_PROVIDER=openai_web"
            )
        return OpenAIWebEvidenceCollector(
            key_setting.get_secret_value(),
            model=settings.forecast_model,
            timeout_seconds=settings.ai_timeout_seconds,
        )
    raise ValueError(f"unsupported evidence provider: {name}")


def _market_filter(
    settings: Settings,
) -> Callable[[list[MarketSpec]], list[MarketSpec]] | None:
    """Build the optional candidate-universe filter described by settings.

    The filter runs after discovery and can only remove markets; the engine
    keeps honouring held positions separately. Unsupported symbols are rejected
    by ``Settings`` validation, so a misconfiguration fails at startup instead
    of silently trading nothing.
    """

    if settings.market_filter != "crypto_updown":
        return None
    classifier = CryptoUpDownFilter(
        CryptoUpDownFilterConfig(
            allowed_assets=frozenset(settings.crypto_updown_asset_symbols)
        )
    )
    LOGGER.info(
        "crypto Up/Down market filter enabled for assets: %s",
        ", ".join(settings.crypto_updown_asset_symbols),
    )

    def _apply(markets: list[MarketSpec]) -> list[MarketSpec]:
        return [classified.market for classified in classifier.filter(markets)]

    return _apply


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
    live_broker: PolymarketBroker | None = None
    mode_aware: ModeAwareBroker | None = None

    def build_live_broker() -> PolymarketBroker:
        return PolymarketBroker(settings, store)

    def build_reconciler() -> OrderReconciler:
        return OrderReconciler(
            private_key=settings.polymarket_private_key.get_secret_value(),
            wallet=settings.polymarket_deposit_wallet,
            account_id=settings.account_id,
            store=store,
            interval_seconds=settings.reconcile_interval_seconds,
            market_data=market_data,
            baseline_utc=settings.reconcile_baseline_datetime,
        )

    # A personal deployment that is allowed to touch real funds keeps every mode
    # ready at once so the operator can switch between paper, shadow, canary and
    # live from the dashboard without a redeploy. The mode dispatch itself lives
    # in ModeAwareBroker; every real-money submission still requires a fresh arm
    # that matches the currently effective mode.
    hot_switchable = bool(
        settings.personal_mode
        and settings.personal_live_enabled
        and settings.polymarket_private_key is not None
        and settings.component != "api"
    )
    if broker_override is not None:
        broker = broker_override
    elif hot_switchable:
        paper = PaperBroker(settings.bankroll_usd)
        live_broker = build_live_broker()
        mode_aware = ModeAwareBroker(
            settings,
            paper=paper,
            shadow=ShadowBroker(settings.bankroll_usd),
            live=live_broker,
            live_factory=build_live_broker,
        )
        broker: Broker = mode_aware
        reconciler = build_reconciler()
    elif settings.mode is TradingMode.PAPER:
        broker = PaperBroker(settings.bankroll_usd)
    elif settings.mode is TradingMode.SHADOW:
        broker = ShadowBroker(settings.bankroll_usd)
    elif settings.component == "api":
        broker = ControlPlaneBroker(settings.bankroll_usd)
    else:
        live_broker = build_live_broker()
        broker = live_broker
        reconciler = build_reconciler()
    engine = TradingEngine(
        settings=settings,
        market_data=market_data,
        evidence_collector=collector,
        forecaster=graph,
        strategy=ValueStrategy(settings),
        risk=RiskEngine(settings),
        broker=broker,
        store=store,
        quarantined_tokens=(
            None if reconciler is None else lambda: reconciler.quarantined_token_ids
        ),
        market_filter=_market_filter(settings),
    )
    return Runtime(
        settings=settings,
        store=store,
        broker=broker,
        engine=engine,
        market_data=market_data,
        forecast_provider=provider,
        reconciler=reconciler,
        live_broker=live_broker,
        mode_aware=mode_aware,
    )
