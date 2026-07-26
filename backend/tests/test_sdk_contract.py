from __future__ import annotations

import asyncio
import inspect
from decimal import Decimal
from importlib.metadata import version

from polymarket import AsyncSecureClient, PublicClient, SecureClient
from polymarket.models.clob.account import ClobTrade, OpenOrder
from polymarket.models.clob.market_events import parse_market_event
from polymarket.models.clob.order_response import (
    AcceptedOrder,
    RawOrderResponse,
    normalize_order_response,
)
from polymarket.models.clob.user_events import parse_user_event
from polymarket.models.data.activity import RedeemActivity
from polymarket.models.data.portfolio import Position


def _keyword_names(owner: type, method: str) -> set[str]:
    return set(inspect.signature(getattr(owner, method)).parameters)


def test_official_sdk_version_and_public_contract() -> None:
    assert version("polymarket-client") == "0.2.0"
    assert {"closed", "order", "ascending", "page_size"} <= _keyword_names(
        PublicClient, "list_markets"
    )
    assert {"token_id"} <= _keyword_names(PublicClient, "get_order_book")


def test_official_sdk_secure_contract_used_by_executor() -> None:
    assert {"token_id", "price", "size", "side", "post_only", "expiration"} <= _keyword_names(
        SecureClient, "create_limit_order"
    )
    assert {"signed_order"} <= _keyword_names(SecureClient, "post_order")
    assert {"asset_type", "token_id"} <= _keyword_names(SecureClient, "get_balance_allowance")
    assert hasattr(SecureClient, "setup_trading_approvals")
    assert not inspect.iscoroutinefunction(SecureClient.setup_trading_approvals)
    assert hasattr(SecureClient, "cancel_all")
    assert hasattr(SecureClient, "wait_for_order_fill_settlement")
    assert {"order_id", "trade_ids", "transactions_hashes"} <= set(AcceptedOrder.model_fields)


def test_official_sdk_secure_contract_used_by_reconciler() -> None:
    assert inspect.iscoroutinefunction(AsyncSecureClient.create)
    assert inspect.iscoroutinefunction(AsyncSecureClient.setup_trading_approvals)
    assert {"after"} <= _keyword_names(AsyncSecureClient, "list_account_trades")
    assert {
        "activity_types",
        "start",
        "sort_by",
        "sort_direction",
        "page_size",
    } <= _keyword_names(AsyncSecureClient, "list_activity")
    assert {"size_threshold"} <= _keyword_names(AsyncSecureClient, "list_positions")
    assert hasattr(AsyncSecureClient, "list_open_orders")
    assert {"order_id"} <= _keyword_names(AsyncSecureClient, "get_order")
    assert hasattr(AsyncSecureClient, "subscribe")
    assert {
        "condition_id",
        "token_id",
        "current_value",
        "initial_value",
        "size",
    } <= set(Position.model_fields)
    assert {"condition_id", "amount", "timestamp", "transaction_hash"} <= set(
        RedeemActivity.model_fields
    )
    assert {
        "id",
        "condition_id",
        "token_id",
        "taker_order_id",
        "status",
        "transaction_hash",
    } <= set(ClobTrade.model_fields)
    assert {"id", "condition_id", "token_id", "size_matched", "status"} <= set(
        OpenOrder.model_fields
    )


async def test_reconciler_awaits_async_secure_client_factory(monkeypatch) -> None:
    from polybot.reconcile import OrderReconciler
    from polybot.stores.memory import MemoryStore

    class FakeAsyncClient:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    created = FakeAsyncClient()
    calls: list[dict[str, str | None]] = []

    async def create(**kwargs):
        calls.append(kwargs)
        return created

    monkeypatch.setattr(AsyncSecureClient, "create", staticmethod(create))
    reconciler = OrderReconciler(
        private_key="private",
        wallet=None,
        account_id="account",
        store=MemoryStore(),
    )

    assert reconciler.client is None
    first, second = await asyncio.gather(
        reconciler._ensure_client(),
        reconciler._ensure_client(),
    )
    assert first is created
    assert second is created
    assert calls == [{"private_key": "private", "wallet": None}]
    await reconciler.close()
    assert created.closed


async def test_reconciler_closes_client_created_during_concurrent_close(
    monkeypatch,
) -> None:
    from polybot.reconcile import OrderReconciler
    from polybot.stores.memory import MemoryStore

    started = asyncio.Event()
    release = asyncio.Event()

    class FakeAsyncClient:
        def __init__(self) -> None:
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1

    created = FakeAsyncClient()

    async def create(**kwargs):
        started.set()
        await release.wait()
        return created

    monkeypatch.setattr(AsyncSecureClient, "create", staticmethod(create))
    reconciler = OrderReconciler(
        private_key="private",
        wallet=None,
        account_id="account",
        store=MemoryStore(),
    )
    initialize = asyncio.create_task(reconciler._ensure_client())
    await started.wait()
    close = asyncio.create_task(reconciler.close())
    release.set()

    try:
        await initialize
    except RuntimeError as exc:
        assert "closed during" in str(exc)
    else:
        raise AssertionError("client initialized after reconciler close")
    await close
    await reconciler.close()

    assert created.close_calls == 1
    assert reconciler.client is None


def test_official_sdk_accepted_order_aliases_preserve_pending_trade_ids() -> None:
    raw = RawOrderResponse.model_validate(
        {
            "errorMsg": "",
            "makingAmount": "1.20",
            "takingAmount": "3",
            "orderID": "order-1",
            "status": "matched",
            "success": True,
            "tradeIDs": ["trade-1"],
            "transactionsHashes": ["0x" + "ab" * 32],
        }
    )
    accepted = normalize_order_response(raw)
    assert isinstance(accepted, AcceptedOrder)
    assert accepted.trade_ids == ("trade-1",)
    assert accepted.transactions_hashes == ("0x" + "ab" * 32,)


def test_official_sdk_websocket_shapes_leave_constraints_and_trader_side_optional() -> None:
    condition_id = "0x" + "11" * 32
    book = parse_market_event(
        {
            "event_type": "book",
            "market": condition_id,
            "asset_id": "123",
            "bids": [{"price": "0.40", "size": "3"}],
            "asks": [{"price": "0.41", "size": "4"}],
            "timestamp": "1750000000000",
        }
    )
    assert book.payload.tick_size is None
    assert book.payload.min_order_size is None

    trade = parse_user_event(
        {
            "event_type": "trade",
            "id": "trade-1",
            "taker_order_id": "external-taker",
            "market": condition_id,
            "asset_id": "123",
            "side": "BUY",
            "size": "3",
            "price": "0.40",
            "status": "MATCHED",
            "owner": "0x" + "22" * 20,
            "maker_orders": [
                {
                    "order_id": "maker-1",
                    "owner": "owner-1",
                    "matched_amount": "2.5",
                    "price": "0.60",
                    "asset_id": "456",
                    "side": "SELL",
                    "outcome": "Down",
                }
            ],
        }
    )
    assert trade.payload.trader_side is None
    assert trade.payload.maker_orders
    assert trade.payload.maker_orders[0].matched_amount == Decimal("2.5")


def test_official_sdk_position_outcome_is_only_a_display_label() -> None:
    position = Position.model_validate(
        {
            "conditionId": "0x" + "33" * 32,
            "asset": "123",
            "size": "2",
            "initialValue": "0.8",
            "currentValue": "1.1",
            "outcome": "Up",
        }
    )
    assert position.outcome == "Up"
    assert position.token_id == "123"
