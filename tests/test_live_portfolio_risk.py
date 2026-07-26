from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from polybot.brokers.polymarket import PolymarketBroker
from polybot.config import (
    BETA_SDK_ACK_TEXT,
    DEDICATED_WALLET_ACK_TEXT,
    LIVE_ACK_TEXT,
    Settings,
)
from polybot.models import (
    UNCLASSIFIED_EVENT_KEY,
    MarketSpec,
    Outcome,
    PortfolioState,
    Side,
    TradeCandidate,
    UserTradeUpdate,
    utc_now,
)
from polybot.risk import RiskEngine
from polybot.stores.memory import MemoryStore
from polybot.strategy import ValueStrategy


class SyncItems:
    def __init__(self, items):
        self.items = items

    def iter_items(self):
        yield from self.items


class PortfolioClient:
    def __init__(self, positions, open_orders):
        self.positions = positions
        self.open_orders = open_orders

    def list_positions(self, **kwargs):
        assert kwargs["size_threshold"] == 0
        return SyncItems(self.positions)

    def list_open_orders(self):
        return SyncItems(self.open_orders)

    def get_balance_allowance(self, **kwargs):
        return SimpleNamespace(balance=10_000_000, allowances={})


def _live_settings() -> Settings:
    return Settings(
        _env_file=None,
        mode="canary",
        max_order_usd=Decimal("5"),
        live_ack=LIVE_ACK_TEXT,
        beta_sdk_ack=BETA_SDK_ACK_TEXT,
        dedicated_wallet_ack=DEDICATED_WALLET_ACK_TEXT,
        polymarket_private_key="0xdeadbeef",
        polymarket_deposit_wallet="0x0000000000000000000000000000000000000001",
        signed_payload_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        ai_provider="openai",
        openai_api_key="test-key",
        admin_token="admin",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
    )


def _market(market_id: str, condition_id: str, event_id: str) -> MarketSpec:
    return MarketSpec(
        id=market_id,
        condition_id=condition_id,
        event_id=event_id,
        question=f"Question {market_id}?",
        yes_token_id=f"{market_id}-yes",
        no_token_id=f"{market_id}-no",
    )


async def test_live_portfolio_uses_utc_fill_ledger_and_shared_event_mapping() -> None:
    settings = _live_settings()
    store = MemoryStore()
    await store.save_market(_market("m1", "c1", "event-shared"))
    await store.save_market(_market("m2", "c2", "event-shared"))
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    await store.reconcile_trade(
        UserTradeUpdate(
            clob_trade_id="buy-before",
            candidate_order_ids=["order-buy"],
            condition_id="c1",
            token_id="m1-yes",
            side=Side.BUY,
            price=Decimal("0.40"),
            size=Decimal("10"),
            status="CONFIRMED",
            matched_at=midnight - timedelta(days=1),
            updated_at=midnight - timedelta(days=1),
        ),
        settings.account_id,
    )
    await store.reconcile_trade(
        UserTradeUpdate(
            clob_trade_id="sell-today",
            candidate_order_ids=["order-sell"],
            condition_id="c1",
            token_id="m1-yes",
            side=Side.SELL,
            price=Decimal("0.60"),
            size=Decimal("2"),
            status="CONFIRMED",
            matched_at=midnight + timedelta(seconds=1),
            updated_at=midnight + timedelta(seconds=1),
        ),
        settings.account_id,
    )
    positions = [
        SimpleNamespace(
            token_id="m1-yes",
            condition_id="c1",
            event_id=None,
            size=Decimal("8"),
            current_value=Decimal("3.2"),
            initial_value=Decimal("3.2"),
            # Cumulative exchange PnL must not leak into today's risk window.
            realized_pnl=Decimal("-99"),
        )
    ]
    open_orders = [
        SimpleNamespace(
            condition_id="c2",
            market="c2",
            side="BUY",
            price=Decimal("0.50"),
            original_size=Decimal("2"),
            size_matched=Decimal("0"),
            token_id="m2-yes",
        ),
        SimpleNamespace(
            condition_id="unknown-condition",
            market="unknown-condition",
            side="BUY",
            price=Decimal("0.50"),
            original_size=Decimal("1"),
            size_matched=Decimal("0"),
            token_id="unknown-token",
        ),
    ]
    broker = PolymarketBroker(
        settings,
        store,
        client=PortfolioClient(positions, open_orders),
    )

    portfolio = await broker.portfolio_state()

    assert portfolio.realized_pnl_today_usd == Decimal("0.40")
    assert portfolio.event_exposure_usd["event-shared"] == Decimal("4.20")
    assert portfolio.event_exposure_usd[UNCLASSIFIED_EVENT_KEY] == Decimal("0.50")
    assert portfolio.token_event_ids["m1-yes"] == "event-shared"


def test_unclassified_event_exposure_counts_against_every_candidate(
    settings, market, yes_book, no_book, forecast
) -> None:
    portfolio = PortfolioState(
        bankroll_usd=Decimal("1000"),
        cash_usd=Decimal("1000"),
        gross_exposure_usd=Decimal("20"),
        event_exposure_usd={
            "e1": Decimal("11"),
            UNCLASSIFIED_EVENT_KEY: Decimal("9"),
        },
    )
    candidate = TradeCandidate(
        market_id=market.id,
        event_id=market.event_id,
        bucket="test",
        token_id=market.yes_token_id,
        outcome=Outcome.YES,
        side=Side.BUY,
        conservative_probability=Decimal("0.70"),
        expected_price=Decimal("0.40"),
        limit_price=Decimal("0.40"),
        size=Decimal("5"),
        notional_usd=Decimal("2"),
        fee_estimate_usd=Decimal("0"),
        edge_after_costs=Decimal("0.10"),
        forecast_id=forecast.id,
        book_captured_at=utc_now(),
    )

    decision = RiskEngine(settings).evaluate_candidate(candidate, market, yes_book, portfolio)

    assert "event_exposure_cap_exceeded" in decision.codes
    assert ValueStrategy(settings).choose(
        market, forecast, yes_book, no_book, portfolio
    ) is None
