from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from polymarket.errors import RequestRejectedError

from polybot.market import StreamingPolymarketMarketData
from polybot.models import (
    ExecutionResult,
    ExecutionStatus,
    MarketSpec,
    utc_now,
)
from polybot.reconcile import OrderReconciler
from polybot.stores.ledger import IncompleteFillLedgerError
from polybot.stores.memory import MemoryStore


class FakePublicClient:
    def __init__(self):
        self.calls = 0

    def get_order_book(self, *, token_id: str):
        self.calls += 1
        return SimpleNamespace(
            bids=(SimpleNamespace(price=Decimal("0.4"), size=Decimal("3")),),
            asks=(SimpleNamespace(price=Decimal("0.5"), size=Decimal("4")),),
            tick_size=Decimal("0.01"),
            min_order_size=Decimal("5"),
            timestamp=utc_now(),
        )


class FakeAsyncPublicClient:
    async def close(self) -> None:
        return None


async def test_market_stream_book_and_delta_feed_cache() -> None:
    client = FakePublicClient()
    source = StreamingPolymarketMarketData(client=client, async_client=FakeAsyncPublicClient())
    source._token_markets["yes"] = "market-1"
    source._apply_event(
        SimpleNamespace(
            type="book",
            payload=SimpleNamespace(
                token_id="yes",
                market="condition-1",
                bids=(SimpleNamespace(price=Decimal("0.4"), size=Decimal("3")),),
                asks=(SimpleNamespace(price=Decimal("0.5"), size=Decimal("4")),),
                # These fields are optional in the official 0.2 WebSocket
                # payload. Their absence must force a REST constraints read.
                tick_size=None,
                min_order_size=None,
                timestamp=utc_now(),
            ),
        )
    )
    cached = await source.get_order_book("market-1", "yes")
    assert client.calls == 1
    assert cached.best_bid == Decimal("0.4")
    assert cached.best_ask == Decimal("0.5")
    assert cached.minimum_order_size == Decimal("5")

    source._apply_event(
        SimpleNamespace(
            type="price_change",
            payload=SimpleNamespace(
                timestamp=utc_now(),
                price_changes=(
                    SimpleNamespace(
                        token_id="yes",
                        side="SELL",
                        price=Decimal("0.5"),
                        size=Decimal("0"),
                    ),
                    SimpleNamespace(
                        token_id="yes",
                        side="SELL",
                        price=Decimal("0.49"),
                        size=Decimal("2"),
                    ),
                ),
            ),
        )
    )
    assert source._cache["yes"].best_ask == Decimal("0.49")
    cached = await source.get_order_book("market-1", "yes")
    assert client.calls == 1
    assert cached.best_ask == Decimal("0.49")
    await source.close()


class AsyncItems:
    def __init__(self, items):
        self.items = items

    async def iter_items(self):
        for item in self.items:
            yield item


class FakeReconcileClient:
    def __init__(self):
        self.trade_after = None
        self.order = SimpleNamespace(
            id="order-1",
            condition_id="condition-1",
            market="condition-1",
            token_id="yes",
            side="BUY",
            price=Decimal("0.4"),
            original_size=Decimal("10"),
            size_matched=Decimal("3"),
            status="LIVE",
            order_type="GTC",
            outcome="YES",
            created_at=utc_now(),
        )
        self.trade = SimpleNamespace(
            id="trade-1",
            taker_order_id="order-1",
            maker_orders=(),
            condition_id="condition-1",
            market="condition-1",
            token_id="yes",
            side="BUY",
            trader_side="TAKER",
            price=Decimal("0.4"),
            size=Decimal("3"),
            outcome="YES",
            status="CONFIRMED",
            fee_rate_bps=Decimal("20"),
            transaction_hash="0xtx",
            matched_at=utc_now(),
            updated_at=utc_now(),
        )
        self.position = SimpleNamespace(
            condition_id="condition-1",
            token_id="yes",
            # Raw labels are display text and may be Up/Down/categorical.
            outcome="UP",
            size=Decimal("3"),
            avg_price=Decimal("0.4"),
            initial_value=Decimal("1.2"),
            current_value=Decimal("1.5"),
            realized_pnl=Decimal("0"),
            cur_price=Decimal("0.5"),
        )

    def list_open_orders(self):
        return AsyncItems([self.order])

    def list_account_trades(self, **kwargs):
        self.trade_after = kwargs["after"]
        return AsyncItems([self.trade])

    def list_positions(self, **kwargs):
        assert kwargs["size_threshold"] == 0
        return AsyncItems([self.position])

    def list_activity(self, **kwargs):
        assert kwargs["start"] == 1
        assert kwargs["page_size"] < 500
        return AsyncItems([])

    async def close(self):
        return None


async def seed_reconcile_store(
    store: MemoryStore,
    *,
    save_market: bool = True,
    trade_ids: list[str] | None = None,
) -> None:
    if save_market:
        await store.save_market(
            MarketSpec(
                id="market-1",
                condition_id="condition-1",
                question="Mapped binary market?",
                yes_token_id="yes",
                no_token_id="no",
            )
        )
    await store.save_execution(
        ExecutionResult(
            intent_hash="intent-1",
            status=ExecutionStatus.ACCEPTED,
            order_id="order-1",
            raw={"trade_ids": trade_ids or []},
        ),
        "account",
    )


async def test_rest_reconciliation_repairs_all_three_ledgers() -> None:
    store = MemoryStore()
    await seed_reconcile_store(store)
    reconciler = OrderReconciler(
        private_key="unused",
        wallet="unused",
        account_id="account",
        store=store,
        client=FakeReconcileClient(),
    )
    await reconciler.reconcile_rest()
    assert reconciler.client.trade_after == "0"
    assert store.order_updates[0].size_matched == Decimal("3")
    assert store.trade_updates[0].status == "CONFIRMED"
    assert store.position_updates["yes"].unrealized_pnl_usd == Decimal("0.3")
    await reconciler.close()


async def test_reconciliation_blocks_live_position_not_proven_by_fill_history() -> None:
    client = FakeReconcileClient()
    client.position.size = Decimal("4")
    store = MemoryStore()
    await seed_reconcile_store(store)
    reconciler = OrderReconciler(
        private_key="unused",
        wallet=None,
        account_id="account",
        store=store,
        client=client,
    )

    with pytest.raises(IncompleteFillLedgerError, match="does not match"):
        await reconciler.reconcile_rest()

    assert client.trade_after == "0"
    assert reconciler._trade_after == "0"
    await reconciler.close()


async def test_reconciliation_seeds_held_market_before_position_write(market) -> None:
    class FakeHeldMarketData:
        async def get_market_by_condition(self, condition_id: str):
            assert condition_id == "condition-1"
            return market.model_copy(
                update={
                    "condition_id": condition_id,
                    "yes_token_id": "yes",
                    "no_token_id": "no",
                }
            )

    class MarketAwareStore(MemoryStore):
        async def reconcile_positions(self, positions, account_id: str) -> None:
            assert any(item.condition_id == "condition-1" for item in self.markets.values())
            await super().reconcile_positions(positions, account_id)

    store = MarketAwareStore()
    await seed_reconcile_store(store, save_market=False)
    reconciler = OrderReconciler(
        private_key="unused",
        wallet="unused",
        account_id="account",
        store=store,
        client=FakeReconcileClient(),
        market_data=FakeHeldMarketData(),
    )
    await reconciler.reconcile_rest()
    assert store.position_updates["yes"].condition_id == "condition-1"
    await reconciler.close()


def test_order_update_maps_partial_state() -> None:
    raw = SimpleNamespace(
        id="order-2",
        market="condition-2",
        token_id="no",
        side="BUY",
        price=Decimal("0.3"),
        original_size=Decimal("5"),
        size_matched=Decimal("2"),
        status="LIVE",
        order_type="GTC",
        outcome="NO",
        order_event_type="UPDATE",
        timestamp=utc_now(),
    )
    update = OrderReconciler._order_update(raw)
    assert update.condition_id == "condition-2"
    assert update.event_type == "UPDATE"


def test_maker_trade_is_split_by_its_actual_matched_amount() -> None:
    maker = SimpleNamespace(
        order_id="maker-order",
        token_id="yes",
        side="SELL",
        price=Decimal("0.61"),
        matched_amount=Decimal("2.5"),
        outcome="YES",
        fee_rate_bps=Decimal("12"),
    )
    trade = SimpleNamespace(
        id="trade-maker",
        taker_order_id="other-taker",
        maker_orders=(maker,),
        condition_id="condition-1",
        market="condition-1",
        token_id="no",
        side="BUY",
        trader_side="MAKER",
        price=Decimal("0.39"),
        size=Decimal("9"),
        outcome="NO",
        status="CONFIRMED",
        fee_rate_bps=Decimal("20"),
        transaction_hash="0xtx",
        matched_at=utc_now(),
        updated_at=utc_now(),
    )
    updates = OrderReconciler._trade_updates(trade)
    assert len(updates) == 1
    assert updates[0].candidate_order_ids == ["maker-order"]
    assert updates[0].size == Decimal("2.5")
    assert updates[0].price == Decimal("0.61")


class CloseOnlyClient:
    async def close(self) -> None:
        return None


async def test_maker_trade_filters_foreign_maker_orders_against_durable_ledger() -> None:
    store = MemoryStore()
    await store.save_execution(
        ExecutionResult(
            intent_hash="maker-intent",
            status=ExecutionStatus.ACCEPTED,
            order_id="our-maker",
        ),
        "account",
    )
    maker = SimpleNamespace(
        order_id="our-maker",
        token_id="yes",
        side="SELL",
        price=Decimal("0.61"),
        matched_amount=Decimal("2.5"),
        outcome="Up",
        fee_rate_bps=Decimal("12"),
    )
    foreign = SimpleNamespace(
        order_id="foreign-maker",
        token_id="yes",
        side="SELL",
        price=Decimal("0.62"),
        matched_amount=Decimal("4"),
        outcome="Up",
        fee_rate_bps=Decimal("12"),
    )
    trade = SimpleNamespace(
        id="trade-maker",
        taker_order_id="external-taker",
        maker_orders=(maker, foreign),
        condition_id="condition-1",
        market="condition-1",
        token_id="no",
        side="BUY",
        trader_side="MAKER",
        price=Decimal("0.39"),
        size=Decimal("6.5"),
        outcome="Down",
        status="MATCHED",
        fee_rate_bps=Decimal("20"),
        transaction_hash=None,
        matched_at=utc_now(),
        updated_at=utc_now(),
    )
    reconciler = OrderReconciler(
        private_key="unused",
        wallet=None,
        account_id="account",
        store=store,
        client=CloseOnlyClient(),
    )

    updates = await reconciler._account_trade_updates(trade)

    assert [update.candidate_order_ids for update in updates] == [["our-maker"]]
    await store.reconcile_trade(updates[0], "account")
    assert {update.candidate_order_ids[0] for update in store.trade_updates} == {"our-maker"}
    await reconciler.close()


async def test_missing_trader_side_uses_whichever_candidate_is_durable() -> None:
    store = MemoryStore()
    await store.save_execution(
        ExecutionResult(
            intent_hash="maker-intent",
            status=ExecutionStatus.ACCEPTED,
            order_id="our-maker",
        ),
        "account",
    )
    maker = SimpleNamespace(
        order_id="our-maker",
        token_id="yes",
        side="SELL",
        price=Decimal("0.61"),
        matched_amount=Decimal("2"),
        outcome="Up",
        fee_rate_bps=Decimal("12"),
    )
    trade = SimpleNamespace(
        id="trade-ambiguous",
        taker_order_id="foreign-taker",
        maker_orders=(maker,),
        condition_id="condition-1",
        market="condition-1",
        token_id="no",
        side="BUY",
        trader_side=None,
        price=Decimal("0.39"),
        size=Decimal("2"),
        outcome="Down",
        status="MATCHED",
        fee_rate_bps=Decimal("20"),
        transaction_hash=None,
        matched_at=utc_now(),
        updated_at=utc_now(),
    )
    reconciler = OrderReconciler(
        private_key="unused",
        wallet=None,
        account_id="account",
        store=store,
        client=CloseOnlyClient(),
    )

    updates = await reconciler._account_trade_updates(trade)

    assert len(updates) == 1
    assert updates[0].candidate_order_ids == ["our-maker"]
    assert updates[0].trader_side is None
    await store.save_execution(
        ExecutionResult(
            intent_hash="taker-intent",
            status=ExecutionStatus.ACCEPTED,
            order_id="foreign-taker",
        ),
        "account",
    )
    with pytest.raises(IncompleteFillLedgerError, match="both taker and maker"):
        await reconciler._account_trade_updates(trade)
    await reconciler.close()


class PendingTradeClient(FakeReconcileClient):
    def __init__(self):
        super().__init__()
        self.targeted_calls = 0
        self.order.original_size = Decimal("3")
        self.order.size_matched = Decimal("3")
        self.order.status = "MATCHED"

    def list_account_trades(self, **kwargs):
        if "after" in kwargs:
            self.trade_after = kwargs["after"]
            return AsyncItems([])
        assert kwargs["id"] == "trade-1"
        self.targeted_calls += 1
        self.trade.status = "MINED" if self.targeted_calls == 1 else "CONFIRMED"
        return AsyncItems([self.trade])


async def test_accepted_trade_ids_are_polled_until_terminal() -> None:
    client = PendingTradeClient()
    store = MemoryStore()
    await seed_reconcile_store(store, trade_ids=["trade-1"])
    reconciler = OrderReconciler(
        private_key="unused",
        wallet=None,
        account_id="account",
        store=store,
        client=client,
    )

    await reconciler.reconcile_rest()
    assert client.targeted_calls == 1
    assert store.trade_updates[0].status == "MINED"
    assert await store.pending_trade_ids("account") == {"trade-1"}

    await reconciler.reconcile_rest()
    assert client.targeted_calls == 2
    assert store.trade_updates[0].status == "CONFIRMED"
    assert await store.pending_trade_ids("account") == set()
    await reconciler.close()


class MissingOrderClient:
    def __init__(self):
        self.trade_after_values: list[str] = []
        self.get_order_calls = 0

    def list_open_orders(self):
        return AsyncItems([])

    async def get_order(self, *, order_id: str):
        assert order_id == "missing-order"
        self.get_order_calls += 1
        raise RequestRejectedError("not found", status=404)

    def list_account_trades(self, **kwargs):
        assert "after" in kwargs
        self.trade_after_values.append(kwargs["after"])
        return AsyncItems([])

    def list_activity(self, **kwargs):
        return AsyncItems([])

    def list_positions(self, **kwargs):
        return AsyncItems([])

    async def close(self):
        return None


async def test_disappeared_order_requires_direct_404_and_two_full_repairs() -> None:
    store = MemoryStore()
    await store.save_execution(
        ExecutionResult(
            intent_hash="missing-intent",
            status=ExecutionStatus.ACCEPTED,
            order_id="missing-order",
        ),
        "account",
    )
    client = MissingOrderClient()
    reconciler = OrderReconciler(
        private_key="unused",
        wallet=None,
        account_id="account",
        store=store,
        client=client,
    )

    await reconciler.reconcile_rest()
    assert store.durable_orders["missing-order"] == "unknown"
    assert await store.has_unresolved_live_orders("account")

    await reconciler.reconcile_rest()
    assert store.durable_orders["missing-order"] == "cancelled"
    assert not await store.has_unresolved_live_orders("account")
    assert client.get_order_calls == 2
    assert client.trade_after_values == ["0", "0"]
    await reconciler.close()


async def test_position_missing_current_value_fails_closed() -> None:
    client = FakeReconcileClient()
    client.position.current_value = None
    store = MemoryStore()
    await seed_reconcile_store(store)
    reconciler = OrderReconciler(
        private_key="unused",
        wallet=None,
        account_id="account",
        store=store,
        client=client,
    )

    with pytest.raises(IncompleteFillLedgerError, match="required current_value"):
        await reconciler.reconcile_rest()

    assert reconciler._trade_after == "0"
    await reconciler.close()
