from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from cryptography.exceptions import InvalidTag

from polybot.brokers.polymarket import PolymarketBroker
from polybot.config import (
    BETA_SDK_ACK_TEXT,
    DEDICATED_WALLET_ACK_TEXT,
    LIVE_ACK_TEXT,
    Settings,
    TradingMode,
)
from polybot.geoblock import GeoblockChecker, GeoblockResult
from polybot.models import (
    ExecutionStatus,
    Outcome,
    RuntimeControl,
    Side,
    TradeIntent,
    utc_now,
)
from polybot.stores.memory import MemoryStore
from polybot.tenant_crypto import EncryptionContext


async def test_geoblock_fails_closed_on_malformed_response() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"country": "XX"}))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await GeoblockChecker("https://example.test", client=client).check()
    assert not result.allowed
    assert result.reason == "malformed geoblock response"


class AllowedChecker:
    async def check(self) -> GeoblockResult:
        return GeoblockResult(True, reason="eligible")


class FakeSecureClient:
    def __init__(self) -> None:
        self.calls = 0
        self.batch_calls = 0
        self.cancelled_order_ids: list[str] = []
        self.order_kwargs: list[dict[str, object]] = []
        self.approval_calls = 0

    def create_limit_order(self, **kwargs):
        self.order_kwargs.append(kwargs)
        return SimpleNamespace(
            builder="0x00",
            expiration=kwargs.get("expiration", 0),
            maker="0x01",
            maker_amount=1,
            metadata="0x",
            order_type="GTD" if kwargs.get("expiration") else "GTC",
            post_only=False,
            salt=1,
            side="BUY",
            signature="0x02",
            signature_type=2,
            signer="0x03",
            taker_amount=1,
            timestamp=1,
            token_id=kwargs["token_id"],
        )

    def post_order(self, signed_order):
        self.calls += 1
        return SimpleNamespace(
            ok=True,
            order_id="order-1",
            status="live",
            trade_ids=("trade-1",),
            transactions_hashes=("0xtx",),
        )

    def post_orders(self, signed_orders):
        self.batch_calls += 1
        return tuple(
            SimpleNamespace(
                ok=True,
                order_id=f"batch-order-{index}",
                status="live",
                trade_ids=(),
                transactions_hashes=(),
            )
            for index, _ in enumerate(signed_orders)
        )

    def cancel_order(self, *, order_id):
        self.cancelled_order_ids.append(order_id)
        return None

    def cancel_orders(self, *, order_ids):
        self.cancelled_order_ids.extend(order_ids)
        return None

    def cancel_all(self):
        return None

    def setup_trading_approvals(self):
        self.approval_calls += 1

    def get_balance_allowance(self, **kwargs):
        return SimpleNamespace(balance=10_000_000, allowances={"exchange": 10_000_000})

    def list_open_orders(self):
        return SyncItems([])


class SyncItems:
    def __init__(self, items):
        self.items = items

    def iter_items(self):
        yield from self.items


def live_settings() -> Settings:
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


def post_only_intent(
    settings: Settings,
    *,
    intent_hash: str,
    token_id: str,
    outcome: Outcome,
    price: Decimal,
) -> TradeIntent:
    size = Decimal("2")
    return TradeIntent(
        intent_hash=intent_hash,
        account_id=settings.account_id,
        run_id="pair-run",
        mode=TradingMode.CANARY,
        market_id="m1",
        event_id="e1",
        bucket="crypto",
        token_id=token_id,
        outcome=outcome,
        side=Side.BUY,
        price=price,
        size=size,
        notional_usd=size * price,
        edge_after_costs=Decimal("0.02"),
        forecast_id=None,
        strategy="pair_accumulator_v1",
        post_only=True,
    )


async def test_live_approval_setup_waits_for_funding_and_verifies_allowance() -> None:
    settings = live_settings()
    store = MemoryStore()

    class ApprovalClient(FakeSecureClient):
        def __init__(self, balance: int) -> None:
            super().__init__()
            self.balance = balance
            self.approved = False

        def get_balance_allowance(self, **kwargs):
            allowance = 10_000_000 if self.approved else 0
            return SimpleNamespace(
                balance=self.balance,
                allowances={"exchange": allowance},
            )

        def setup_trading_approvals(self):
            super().setup_trading_approvals()
            self.approved = True

    await store.set_runtime_control(
        RuntimeControl(
            account_id=settings.account_id,
            mode=TradingMode.CANARY,
            armed=True,
            accept_new_intents=True,
            armed_until=utc_now() + timedelta(minutes=5),
            kill_switch=False,
        )
    )
    lease = await store.claim_worker_lease(
        settings.account_id,
        "approval-test-worker",
        timedelta(seconds=30),
    )
    assert lease is not None

    async def lease_guard() -> int | None:
        valid = await store.validate_worker_lease(
            settings.account_id,
            "approval-test-worker",
            lease.fencing_token,
        )
        return lease.fencing_token if valid else None

    unfunded = PolymarketBroker(
        settings,
        store,
        client=ApprovalClient(0),
        geoblock=AllowedChecker(),
    )
    unfunded.set_execution_guard(lease_guard)
    with pytest.raises(RuntimeError, match="fund"):
        await unfunded.ensure_trading_approvals()

    funded_client = ApprovalClient(10_000_000)
    funded = PolymarketBroker(
        settings,
        store,
        client=funded_client,
        geoblock=AllowedChecker(),
    )
    funded.set_execution_guard(lease_guard)
    await funded.ensure_trading_approvals()
    assert funded_client.approval_calls == 1


async def test_live_approval_setup_obeys_official_geoblock() -> None:
    settings = live_settings()
    store = MemoryStore()
    class ApprovalClient(FakeSecureClient):
        def get_balance_allowance(self, **kwargs):
            from types import SimpleNamespace
            return SimpleNamespace(
                balance=10_000_000,
                allowances={"exchange": 0},
            )

    client = ApprovalClient()
    await store.set_runtime_control(
        RuntimeControl(
            account_id=settings.account_id,
            mode=TradingMode.CANARY,
            armed=True,
            accept_new_intents=True,
            armed_until=utc_now() + timedelta(minutes=5),
            kill_switch=False,
        )
    )

    class BlockedChecker:
        async def check(self) -> GeoblockResult:
            return GeoblockResult(False, reason="region unavailable")

    broker = PolymarketBroker(
        settings,
        store,
        client=client,
        geoblock=BlockedChecker(),
    )

    async def lease_guard() -> int | None:
        return 1

    broker.set_execution_guard(lease_guard)
    with pytest.raises(RuntimeError, match="geoblock"):
        await broker.ensure_trading_approvals()
    assert client.approval_calls == 0


async def test_live_approval_setup_rechecks_authority_before_broadcast() -> None:
    settings = live_settings()
    store = MemoryStore()

    class ApprovalClient(FakeSecureClient):
        def get_balance_allowance(self, **kwargs):
            return SimpleNamespace(
                balance=10_000_000,
                allowances={"exchange": 0},
            )

    client = ApprovalClient()
    await store.set_runtime_control(
        RuntimeControl(
            account_id=settings.account_id,
            mode=TradingMode.CANARY,
            armed=True,
            accept_new_intents=True,
            armed_until=utc_now() + timedelta(minutes=5),
            kill_switch=False,
        )
    )
    broker = PolymarketBroker(
        settings,
        store,
        client=client,
        geoblock=AllowedChecker(),
    )
    calls = 0

    async def expiring_guard() -> int | None:
        nonlocal calls
        calls += 1
        return 7 if calls == 1 else None

    broker.set_execution_guard(expiring_guard)
    with pytest.raises(RuntimeError, match="authority changed"):
        await broker.ensure_trading_approvals()
    assert client.approval_calls == 0


async def test_live_broker_requires_short_lived_arm(yes_book) -> None:
    settings = live_settings()
    store = MemoryStore()
    client = FakeSecureClient()
    broker = PolymarketBroker(settings, store, client=client, geoblock=AllowedChecker())
    intent = TradeIntent(
        intent_hash="b" * 64,
        account_id=settings.account_id,
        run_id="r1",
        mode=TradingMode.CANARY,
        market_id="m1",
        event_id="e1",
        bucket="test",
        token_id="yes-1",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=Decimal("0.41"),
        size=Decimal("2"),
        notional_usd=Decimal("0.82"),
        edge_after_costs=Decimal("0.10"),
        forecast_id="11111111-1111-1111-1111-111111111111",
        strategy="test",
    )
    denied = await broker.submit(intent, yes_book)
    assert denied.status.value == "rejected"
    assert client.calls == 0

    await store.set_runtime_control(
        RuntimeControl(
            account_id=settings.account_id,
            mode=TradingMode.CANARY,
            armed=True,
            accept_new_intents=True,
            armed_until=utc_now() + timedelta(minutes=5),
            kill_switch=False,
        )
    )
    lease = await store.claim_worker_lease(
        settings.account_id, "test-worker-owner", timedelta(seconds=30)
    )
    assert lease is not None

    async def lease_guard() -> int | None:
        valid = await store.validate_worker_lease(
            settings.account_id, "test-worker-owner", lease.fencing_token
        )
        return lease.fencing_token if valid else None

    broker.set_execution_guard(lease_guard)
    accepted = await broker.submit(intent, yes_book)
    assert accepted.status.value == "accepted"
    assert client.calls == 1
    assert intent.intent_hash in store.signed_orders
    ciphertext = store.signed_orders[intent.intent_hash][1]
    context = EncryptionContext(
        account_id=intent.account_id,
        purpose="signed-order",
        object_id=intent.intent_hash,
        key_version=settings.payload_key_version,
    )
    assert b'"signature":"0x02"' in broker.cipher.decrypt(ciphertext, context)
    with pytest.raises(InvalidTag):
        broker.cipher.decrypt(
            ciphertext,
            context.__class__(
                account_id="22222222-2222-2222-2222-222222222222",
                purpose=context.purpose,
                object_id=context.object_id,
                key_version=context.key_version,
            ),
        )
    assert accepted.raw["trade_ids"] == ["trade-1"]
    assert accepted.raw["transaction_hashes"] == ["0xtx"]
    assert accepted.raw["order_type"] == "GTD"
    assert client.order_kwargs[0]["expiration"] > int(utc_now().timestamp()) + 180


async def test_live_post_only_pair_uses_non_atomic_sdk_batch(yes_book, no_book) -> None:
    settings = live_settings()
    store = MemoryStore()
    client = FakeSecureClient()
    broker = PolymarketBroker(settings, store, client=client, geoblock=AllowedChecker())
    await store.set_runtime_control(
        RuntimeControl(
            account_id=settings.account_id,
            mode=TradingMode.CANARY,
            armed=True,
            accept_new_intents=True,
            armed_until=utc_now() + timedelta(minutes=5),
            kill_switch=False,
        )
    )
    lease = await store.claim_worker_lease(
        settings.account_id,
        "pair-worker",
        timedelta(seconds=30),
    )
    assert lease is not None

    async def lease_guard() -> int | None:
        valid = await store.validate_worker_lease(
            settings.account_id,
            "pair-worker",
            lease.fencing_token,
        )
        return lease.fencing_token if valid else None

    broker.set_execution_guard(lease_guard)
    results = await broker.submit_batch(
        [
            (
                post_only_intent(
                    settings,
                    intent_hash="1" * 64,
                    token_id="yes-1",
                    outcome=Outcome.YES,
                    price=Decimal("0.39"),
                ),
                yes_book,
            ),
            (
                post_only_intent(
                    settings,
                    intent_hash="2" * 64,
                    token_id="no-1",
                    outcome=Outcome.NO,
                    price=Decimal("0.59"),
                ),
                no_book,
            ),
        ]
    )
    assert [result.status for result in results] == [
        ExecutionStatus.ACCEPTED,
        ExecutionStatus.ACCEPTED,
    ]
    assert client.batch_calls == 1
    assert client.calls == 0
    assert all(call["post_only"] is True for call in client.order_kwargs)
    assert all(result.raw["batch_non_atomic"] is True for result in results)


async def test_post_only_batch_rejects_crossing_leg_before_signing(
    yes_book,
    no_book,
) -> None:
    settings = live_settings()
    client = FakeSecureClient()
    broker = PolymarketBroker(
        settings,
        MemoryStore(),
        client=client,
        geoblock=AllowedChecker(),
    )
    results = await broker.submit_batch(
        [
            (
                post_only_intent(
                    settings,
                    intent_hash="3" * 64,
                    token_id="yes-1",
                    outcome=Outcome.YES,
                    price=Decimal("0.40"),
                ),
                yes_book,
            ),
            (
                post_only_intent(
                    settings,
                    intent_hash="4" * 64,
                    token_id="no-1",
                    outcome=Outcome.NO,
                    price=Decimal("0.59"),
                ),
                no_book,
            ),
        ]
    )
    assert all(result.status is ExecutionStatus.REJECTED for result in results)
    assert "would cross" in results[0].message
    assert client.batch_calls == 0
    assert not client.order_kwargs


async def test_targeted_cancel_calls_sdk_and_verifies_absence() -> None:
    settings = live_settings()
    client = FakeSecureClient()
    broker = PolymarketBroker(
        settings,
        MemoryStore(),
        client=client,
        geoblock=AllowedChecker(),
    )
    assert await broker.cancel_order("order-1", "unit test")
    assert await broker.cancel_orders(["order-2", "order-3"], "unit test")
    assert client.cancelled_order_ids == ["order-1", "order-2", "order-3"]


async def test_mixed_batch_response_cancels_accepted_leg_and_reports_non_atomicity(
    yes_book,
    no_book,
) -> None:
    class MixedResponseClient(FakeSecureClient):
        def post_orders(self, signed_orders):
            self.batch_calls += 1
            return (
                SimpleNamespace(
                    ok=True,
                    order_id="accepted-leg",
                    status="live",
                    trade_ids=(),
                    transactions_hashes=(),
                ),
                SimpleNamespace(
                    ok=False,
                    order_id="",
                    status="rejected",
                    message="maker constraint",
                    code="POST_ONLY",
                    trade_ids=(),
                    transactions_hashes=(),
                ),
            )

    settings = live_settings()
    store = MemoryStore()
    client = MixedResponseClient()
    broker = PolymarketBroker(settings, store, client=client, geoblock=AllowedChecker())
    await store.set_runtime_control(
        RuntimeControl(
            account_id=settings.account_id,
            mode=TradingMode.CANARY,
            armed=True,
            accept_new_intents=True,
            armed_until=utc_now() + timedelta(minutes=5),
            kill_switch=False,
        )
    )
    lease = await store.claim_worker_lease(
        settings.account_id,
        "mixed-worker",
        timedelta(seconds=30),
    )
    assert lease is not None

    async def lease_guard() -> int | None:
        return lease.fencing_token

    broker.set_execution_guard(lease_guard)
    results = await broker.submit_batch(
        [
            (
                post_only_intent(
                    settings,
                    intent_hash="5" * 64,
                    token_id="yes-1",
                    outcome=Outcome.YES,
                    price=Decimal("0.39"),
                ),
                yes_book,
            ),
            (
                post_only_intent(
                    settings,
                    intent_hash="6" * 64,
                    token_id="no-1",
                    outcome=Outcome.NO,
                    price=Decimal("0.59"),
                ),
                no_book,
            ),
        ]
    )
    assert results[0].status is ExecutionStatus.ERROR
    assert "fill race remains ambiguous" in results[0].message
    assert results[1].status is ExecutionStatus.REJECTED
    assert client.cancelled_order_ids == ["accepted-leg"]


async def test_live_broker_refuses_order_when_gtd_expiry_is_too_close(yes_book) -> None:
    settings = live_settings()
    store = MemoryStore()
    client = FakeSecureClient()
    broker = PolymarketBroker(settings, store, client=client, geoblock=AllowedChecker())
    await store.set_runtime_control(
        RuntimeControl(
            account_id=settings.account_id,
            mode=TradingMode.CANARY,
            armed=True,
            accept_new_intents=True,
            armed_until=utc_now() + timedelta(minutes=3),
            kill_switch=False,
        )
    )
    intent = TradeIntent(
        intent_hash="a" * 64,
        account_id=settings.account_id,
        run_id="r1",
        mode=TradingMode.CANARY,
        market_id="m1",
        event_id="e1",
        bucket="test",
        token_id="yes-1",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=Decimal("0.41"),
        size=Decimal("2"),
        notional_usd=Decimal("0.82"),
        edge_after_costs=Decimal("0.10"),
        forecast_id=None,
        strategy="test",
    )

    result = await broker.submit(intent, yes_book)

    assert result.status is ExecutionStatus.REJECTED
    assert "3.5 minutes" in result.message
    assert client.order_kwargs == []


async def test_live_broker_rejects_stale_book_before_signing(yes_book) -> None:
    settings = live_settings()
    store = MemoryStore()
    client = FakeSecureClient()
    broker = PolymarketBroker(settings, store, client=client, geoblock=AllowedChecker())
    intent = TradeIntent(
        intent_hash="e" * 64,
        account_id=settings.account_id,
        run_id="r1",
        mode=TradingMode.CANARY,
        market_id="m1",
        event_id="e1",
        bucket="test",
        token_id="yes-1",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=Decimal("0.41"),
        size=Decimal("2"),
        notional_usd=Decimal("0.82"),
        edge_after_costs=Decimal("0.10"),
        forecast_id=None,
        strategy="test",
    )
    stale = yes_book.model_copy(
        update={"captured_at": utc_now() - timedelta(seconds=settings.max_book_age_seconds + 1)}
    )

    result = await broker.submit(intent, stale)

    assert result.status is ExecutionStatus.REJECTED
    assert client.calls == 0


async def test_stale_fencing_token_is_rejected() -> None:
    store = MemoryStore()
    first = await store.claim_worker_lease("account", "worker-one", timedelta(seconds=0))
    assert first is not None
    second = await store.claim_worker_lease("account", "worker-two", timedelta(seconds=30))
    assert second is not None
    assert second.fencing_token > first.fencing_token
    assert not await store.validate_worker_lease("account", "worker-one", first.fencing_token)
    assert await store.validate_worker_lease("account", "worker-two", second.fencing_token)


async def test_stale_fencing_token_cannot_transition_signed_order() -> None:
    settings = live_settings()
    store = MemoryStore()
    first = await store.claim_worker_lease(
        settings.account_id, "first-worker", timedelta(seconds=0)
    )
    assert first is not None
    intent = TradeIntent(
        intent_hash="d" * 64,
        account_id=settings.account_id,
        run_id="r1",
        mode=TradingMode.CANARY,
        market_id="m1",
        event_id="e1",
        bucket="test",
        token_id="yes-1",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=Decimal("0.41"),
        size=Decimal("2"),
        notional_usd=Decimal("0.82"),
        edge_after_costs=Decimal("0.10"),
        forecast_id=None,
        strategy="test",
    )
    await store.prepare_signed_order(
        intent,
        signed_order_hash="signed-hash",
        payload_ciphertext=b"ciphertext",
        key_version=1,
        fencing_token=first.fencing_token,
    )
    second = await store.claim_worker_lease(
        settings.account_id, "second-worker", timedelta(seconds=30)
    )
    assert second is not None
    with pytest.raises(RuntimeError, match="worker lease expired"):
        await store.mark_order_submitting(
            intent.intent_hash, settings.account_id, first.fencing_token
        )


async def test_cancel_all_failure_is_not_swallowed() -> None:
    class BrokenCancelClient(FakeSecureClient):
        def cancel_all(self):
            raise OSError("exchange unavailable")

    broker = PolymarketBroker(
        live_settings(), MemoryStore(), client=BrokenCancelClient(), geoblock=AllowedChecker()
    )
    with pytest.raises(OSError, match="exchange unavailable"):
        await broker.cancel_all("test")


async def test_ambiguous_post_remains_durably_blocking(yes_book) -> None:
    class AmbiguousClient(FakeSecureClient):
        def post_order(self, signed_order):
            self.calls += 1
            raise TimeoutError("unknown transport result")

    settings = live_settings()
    store = MemoryStore()
    client = AmbiguousClient()
    broker = PolymarketBroker(settings, store, client=client, geoblock=AllowedChecker())
    await store.set_runtime_control(
        RuntimeControl(
            account_id=settings.account_id,
            mode=TradingMode.CANARY,
            armed=True,
            accept_new_intents=True,
            armed_until=utc_now() + timedelta(minutes=5),
            kill_switch=False,
        )
    )
    lease = await store.claim_worker_lease(
        settings.account_id, "ambiguous-worker", timedelta(seconds=30)
    )
    assert lease is not None

    async def lease_guard() -> int | None:
        return lease.fencing_token

    broker.set_execution_guard(lease_guard)
    intent = TradeIntent(
        intent_hash="c" * 64,
        account_id=settings.account_id,
        run_id="r1",
        mode=TradingMode.CANARY,
        market_id="m1",
        event_id="e1",
        bucket="test",
        token_id="yes-1",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=Decimal("0.41"),
        size=Decimal("2"),
        notional_usd=Decimal("0.82"),
        edge_after_costs=Decimal("0.10"),
        forecast_id="11111111-1111-1111-1111-111111111111",
        strategy="test",
    )
    result = await broker.submit(intent, yes_book)
    assert result.status.value == "error"
    assert await store.has_unresolved_live_orders(settings.account_id)


async def test_control_change_during_post_forces_verified_cancellation(yes_book) -> None:
    class ChangingControlStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.control_reads = 0

        async def get_runtime_control(self, account_id: str) -> RuntimeControl:
            self.control_reads += 1
            control = await super().get_runtime_control(account_id)
            if self.control_reads <= 3:
                return control
            return RuntimeControl(
                account_id=account_id,
                mode=TradingMode.CANARY,
                armed=False,
                kill_switch=True,
                cancellation_pending=True,
                version=control.version + 1,
            )

    settings = live_settings()
    store = ChangingControlStore()
    client = FakeSecureClient()
    broker = PolymarketBroker(settings, store, client=client, geoblock=AllowedChecker())
    await store.set_runtime_control(
        RuntimeControl(
            account_id=settings.account_id,
            mode=TradingMode.CANARY,
            armed=True,
            accept_new_intents=True,
            armed_until=utc_now() + timedelta(minutes=5),
            kill_switch=False,
        )
    )
    lease = await store.claim_worker_lease(
        settings.account_id, "post-race-worker", timedelta(seconds=30)
    )
    assert lease is not None

    async def lease_guard() -> int | None:
        return lease.fencing_token

    broker.set_execution_guard(lease_guard)
    intent = TradeIntent(
        intent_hash="f" * 64,
        account_id=settings.account_id,
        run_id="r1",
        mode=TradingMode.CANARY,
        market_id="m1",
        event_id="e1",
        bucket="test",
        token_id="yes-1",
        outcome=Outcome.YES,
        side=Side.BUY,
        price=Decimal("0.41"),
        size=Decimal("2"),
        notional_usd=Decimal("0.82"),
        edge_after_costs=Decimal("0.10"),
        forecast_id=None,
        strategy="test",
    )

    result = await broker.submit(intent, yes_book)

    assert result.status is ExecutionStatus.CANCELLED
    assert client.calls == 1
    assert not await store.has_unresolved_live_orders(settings.account_id)
