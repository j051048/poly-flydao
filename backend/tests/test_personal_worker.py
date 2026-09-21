from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import polybot.worker as worker_module
from polybot.brokers.paper import PaperBroker
from polybot.brokers.polymarket import PolymarketBroker
from polybot.config import Settings, TradingMode
from polybot.models import EngineCycleResult, RuntimeControl, WorkerLease, utc_now
from polybot.personal_execution import (
    PersonalPaperAccountState,
    PersonalRuntimeBinding,
)
from polybot.stores.memory import MemoryStore
from polybot.stores.supabase_store import SupabaseStore
from polybot.worker import (
    _clear_personal_live_downgrade,
    _load_personal_paper_broker,
    _prepare_personal_live_cycle,
    _save_personal_paper_broker,
)

ACCOUNT = "11111111-1111-4111-8111-111111111111"
SIGNER = "0x" + ("11" * 20)
DEPOSIT = "0x" + ("22" * 20)
COLLATERAL = "0x" + ("33" * 20)


def _binding(*, paused: bool = False) -> PersonalRuntimeBinding:
    now = utc_now()
    return PersonalRuntimeBinding(
        account_id=ACCOUNT,
        signer_address=SIGNER,
        deposit_wallet_address=DEPOSIT,
        chain_id=137,
        collateral_token=COLLATERAL,
        binding_version=3,
        paused=paused,
        collateral_balance_pusd=Decimal("25"),
        allowances_ready=True,
        readiness_checked_at=now,
        readiness_owner_id="personal-worker-1",
        readiness_fencing_token=7,
        last_seen_at=now,
        updated_at=now,
    )


class _LiveRepository:
    def __init__(self, *, paused: bool = False, expired: bool = False) -> None:
        self.binding = _binding(paused=paused)
        self.events: list[str] = []
        armed_until = utc_now() - timedelta(seconds=1) if expired else None
        self.control = RuntimeControl(
            account_id=ACCOUNT,
            mode=TradingMode.CANARY if expired else TradingMode.PAPER,
            armed=expired,
            accept_new_intents=expired,
            armed_until=armed_until,
            kill_switch=not expired,
            version=2,
        )

    async def bind_wallet(self, **kwargs: object) -> PersonalRuntimeBinding:
        self.events.append("bind")
        assert kwargs["fencing_token"] == 7
        return self.binding

    async def get_runtime_control(self) -> RuntimeControl:
        self.events.append("control")
        return self.control

    async def arm(self, **kwargs: object) -> RuntimeControl:
        self.events.append("arm")
        assert kwargs["owner_id"] == "personal-worker-1"
        assert kwargs["fencing_token"] == 7
        assert kwargs["expected_version"] == 2
        self.control = RuntimeControl(
            account_id=ACCOUNT,
            mode=TradingMode.CANARY,
            armed=True,
            accept_new_intents=True,
            armed_until=kwargs["armed_until"],
            kill_switch=False,
            version=3,
        )
        return self.control

    async def record_wallet_readiness(self, **kwargs: object) -> PersonalRuntimeBinding:
        self.events.append("readiness")
        assert kwargs["binding_version"] == 3
        assert kwargs["collateral_balance_pusd"] == Decimal("25")
        return self.binding


class _LiveBroker:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.client = SimpleNamespace(
            signer=SIGNER,
            wallet=DEPOSIT,
            environment=SimpleNamespace(
                chain_id=137,
                collateral_token=COLLATERAL,
            ),
        )

    async def ensure_trading_approvals(self) -> tuple[Decimal, bool]:
        self.events.append("approvals")
        return Decimal("25"), True


async def test_personal_live_preflight_orders_bind_arm_approval_and_readiness() -> None:
    repository = _LiveRepository()
    broker = _LiveBroker(repository.events)
    settings = Settings(_env_file=None, mode="paper").model_copy(
        update={"mode": TradingMode.CANARY}
    )

    ready = await _prepare_personal_live_cycle(
        settings=settings,
        broker=broker,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        owner_id="personal-worker-1",
        fencing_token=7,
        logger=logging.getLogger("test.personal.worker"),
    )

    assert ready
    assert repository.events == ["bind", "control", "arm", "approvals", "readiness"]
    assert repository.control.armed_until is not None
    assert repository.control.armed_until <= utc_now() + timedelta(minutes=10)


async def test_personal_live_paused_or_expired_control_never_bootstraps_orders() -> None:
    paused_repository = _LiveRepository(paused=True)
    paused_broker = _LiveBroker(paused_repository.events)
    settings = Settings(_env_file=None, mode="paper").model_copy(
        update={"mode": TradingMode.CANARY}
    )
    assert not await _prepare_personal_live_cycle(
        settings=settings,
        broker=paused_broker,  # type: ignore[arg-type]
        repository=paused_repository,  # type: ignore[arg-type]
        owner_id="personal-worker-1",
        fencing_token=7,
        logger=logging.getLogger("test.personal.worker"),
    )
    assert paused_repository.events == ["bind"]

    expired_repository = _LiveRepository(expired=True)
    expired_broker = _LiveBroker(expired_repository.events)
    assert not await _prepare_personal_live_cycle(
        settings=settings,
        broker=expired_broker,  # type: ignore[arg-type]
        repository=expired_repository,  # type: ignore[arg-type]
        owner_id="personal-worker-1",
        fencing_token=7,
        logger=logging.getLogger("test.personal.worker"),
    )
    assert expired_repository.events == ["bind", "control"]


class _PaperRepository:
    def __init__(self, state: dict[str, object], *, save_succeeds: bool = True) -> None:
        self.state = state
        self.save_succeeds = save_succeeds
        self.saved_state: dict[str, object] | None = None

    async def load_paper_state(self, **kwargs: object) -> PersonalPaperAccountState:
        assert kwargs == {"owner_id": "personal-worker-1", "fencing_token": 9}
        return PersonalPaperAccountState(
            state=self.state,
            version=4,
            updated_at=utc_now(),
        )

    async def save_paper_state(self, **kwargs: object) -> PersonalPaperAccountState | None:
        self.saved_state = kwargs["state"]  # type: ignore[assignment]
        if not self.save_succeeds:
            return None
        return PersonalPaperAccountState(
            state=self.saved_state,
            version=5,
            updated_at=utc_now(),
        )


async def test_personal_paper_state_restores_and_fenced_save_can_fail_closed() -> None:
    source = PaperBroker(Decimal("1000"))
    source.cash = Decimal("875")
    repository = _PaperRepository(source.export_state())
    settings = Settings(_env_file=None, bankroll_usd=Decimal("1000"))

    restored = await _load_personal_paper_broker(
        settings=settings,
        repository=repository,  # type: ignore[arg-type]
        owner_id="personal-worker-1",
        fencing_token=9,
    )
    assert restored.cash == Decimal("875")
    assert await _save_personal_paper_broker(
        repository=repository,  # type: ignore[arg-type]
        broker=restored,
        owner_id="personal-worker-1",
        fencing_token=9,
    )
    assert repository.saved_state is not None
    assert repository.saved_state["cash"] == "875"

    rejected = _PaperRepository(source.export_state(), save_succeeds=False)
    assert not await _save_personal_paper_broker(
        repository=rejected,  # type: ignore[arg-type]
        broker=restored,
        owner_id="personal-worker-1",
        fencing_token=9,
    )


class _CleanupClient:
    def __init__(self, open_orders: list[object] | None = None) -> None:
        self.open_orders = open_orders or []
        self.cancel_calls = 0
        self.closed = False

    def cancel_all(self) -> None:
        self.cancel_calls += 1

    def list_open_orders(self):
        return SimpleNamespace(iter_items=lambda: iter(self.open_orders))

    def close(self) -> None:
        self.closed = True


def _personal_paper_settings(*, private_key: str | None = "12" * 32) -> Settings:
    return Settings(
        _env_file=None,
        personal_mode=True,
        mode="paper",
        component="all",
        account_id=ACCOUNT,
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
        ai_api_key="personal-ai-key",
        polymarket_private_key=private_key,
    )


async def test_personal_live_downgrade_disarms_cancels_acks_then_aligns_paper() -> None:
    settings = _personal_paper_settings()
    store = MemoryStore()
    owner = "personal-worker-1"
    lease = await store.claim_worker_lease(
        ACCOUNT,
        owner,
        timedelta(minutes=2),
    )
    assert lease is not None
    await store.set_runtime_control(
        RuntimeControl(
            account_id=ACCOUNT,
            mode=TradingMode.LIVE,
            armed=True,
            accept_new_intents=True,
            armed_until=utc_now() + timedelta(minutes=5),
            kill_switch=False,
            version=3,
        )
    )
    store.set_runtime_control = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("downgrade cleanup must use atomic disarm RPCs")
    )
    client = _CleanupClient()

    ready = await _clear_personal_live_downgrade(
        settings=settings,
        store=store,
        owner_id=owner,
        fencing_token=lease.fencing_token,
        logger=logging.getLogger("test.personal.downgrade"),
        client_factory=lambda key, wallet: client,
    )

    assert ready
    assert client.cancel_calls == 1
    assert client.closed
    store.set_runtime_control.assert_not_awaited()
    control = await store.get_runtime_control(ACCOUNT)
    assert control.mode is TradingMode.PAPER
    assert control.kill_switch
    assert not control.armed
    assert not control.cancellation_pending


async def test_personal_live_downgrade_blocks_without_key_or_active_lease() -> None:
    settings = _personal_paper_settings(private_key=None)
    store = MemoryStore()
    owner = "personal-worker-1"
    lease = await store.claim_worker_lease(
        ACCOUNT,
        owner,
        timedelta(minutes=2),
    )
    assert lease is not None
    await store.set_runtime_control(
        RuntimeControl(
            account_id=ACCOUNT,
            mode=TradingMode.CANARY,
            version=2,
        )
    )
    factory_calls = 0

    def factory(key: str, wallet: str | None):
        nonlocal factory_calls
        factory_calls += 1
        return _CleanupClient()

    assert not await _clear_personal_live_downgrade(
        settings=settings,
        store=store,
        owner_id=owner,
        fencing_token=lease.fencing_token,
        logger=logging.getLogger("test.personal.downgrade"),
        client_factory=factory,
    )
    assert factory_calls == 0
    latched = await store.get_runtime_control(ACCOUNT)
    assert latched.mode is TradingMode.CANARY
    assert latched.cancellation_pending

    assert not await _clear_personal_live_downgrade(
        settings=settings,
        store=store,
        owner_id="new-worker-without-lease",
        fencing_token=lease.fencing_token + 1,
        logger=logging.getLogger("test.personal.downgrade"),
        client_factory=factory,
    )
    assert factory_calls == 0


async def test_personal_live_downgrade_keeps_latch_for_open_or_ambiguous_orders() -> None:
    settings = _personal_paper_settings()
    store = MemoryStore()
    owner = "personal-worker-1"
    lease = await store.claim_worker_lease(
        ACCOUNT,
        owner,
        timedelta(minutes=2),
    )
    assert lease is not None
    await store.set_runtime_control(
        RuntimeControl(
            account_id=ACCOUNT,
            mode=TradingMode.LIVE,
            version=2,
        )
    )
    open_client = _CleanupClient([SimpleNamespace(id="open-order")])
    assert not await _clear_personal_live_downgrade(
        settings=settings,
        store=store,
        owner_id=owner,
        fencing_token=lease.fencing_token,
        logger=logging.getLogger("test.personal.downgrade"),
        client_factory=lambda key, wallet: open_client,
    )
    assert (await store.get_runtime_control(ACCOUNT)).cancellation_pending

    open_client.open_orders = []
    store.unresolved_live_orders.add("ambiguous-intent")
    assert not await _clear_personal_live_downgrade(
        settings=settings,
        store=store,
        owner_id=owner,
        fencing_token=lease.fencing_token,
        logger=logging.getLogger("test.personal.downgrade"),
        client_factory=lambda key, wallet: open_client,
    )
    assert (await store.get_runtime_control(ACCOUNT)).cancellation_pending


async def test_failed_personal_paper_cycle_reloads_last_committed_state(
    monkeypatch,
) -> None:
    stop = asyncio.Event()
    trigger = asyncio.Event()
    trigger.set()
    settings = _personal_paper_settings()
    store = SupabaseStore(
        settings.supabase_url,
        settings.supabase_service_role_key.get_secret_value(),
        account_id=ACCOUNT,
        client=SimpleNamespace(),  # type: ignore[arg-type]
    )
    lease = WorkerLease(
        account_id=ACCOUNT,
        owner_id="placeholder",
        fencing_token=11,
        expires_at=utc_now() + timedelta(minutes=2),
    )
    control = RuntimeControl(account_id=ACCOUNT, mode=TradingMode.PAPER, version=3)
    store.health = AsyncMock(return_value=True)  # type: ignore[method-assign]
    store.claim_worker_lease = AsyncMock(return_value=lease)  # type: ignore[method-assign]
    store.validate_worker_lease = AsyncMock(return_value=True)  # type: ignore[method-assign]
    store.get_runtime_control = AsyncMock(return_value=control)  # type: ignore[method-assign]
    store.has_unresolved_live_orders = AsyncMock(return_value=False)  # type: ignore[method-assign]
    store.release_worker_lease = AsyncMock(return_value=True)  # type: ignore[method-assign]

    committed = PaperBroker(settings.bankroll_usd).export_state()

    class Repository:
        def __init__(self) -> None:
            self.load_calls = 0
            self.save_calls = 0

        async def schema_ready(self) -> bool:
            return True

        async def load_paper_state(self, **kwargs: object) -> PersonalPaperAccountState:
            self.load_calls += 1
            return PersonalPaperAccountState(
                state=committed,
                version=self.load_calls,
                updated_at=utc_now(),
            )

        async def save_paper_state(self, **kwargs: object) -> PersonalPaperAccountState:
            self.save_calls += 1
            return PersonalPaperAccountState(
                state=kwargs["state"],  # type: ignore[arg-type]
                version=10,
                updated_at=utc_now(),
            )

    repository = Repository()

    class Engine:
        execution_guard = None

        def __init__(self, broker: PaperBroker) -> None:
            self.broker = broker
            self.observed_cash: list[Decimal] = []

        async def run_cycle(self) -> EngineCycleResult:
            self.observed_cash.append(self.broker.cash)
            if len(self.observed_cash) == 1:
                self.broker.cash = Decimal("1")
                trigger.set()
                raise RuntimeError("simulated failure after broker mutation")
            stop.set()
            return EngineCycleResult(
                run_id="paper-reloaded",
                mode=TradingMode.PAPER,
                completed_at=utc_now(),
            )

    class Runtime:
        def __init__(self) -> None:
            self.store = store
            self.broker = PaperBroker(settings.bankroll_usd)
            self.engine = Engine(self.broker)
            self.reconciler = None
            self.live_broker = None
            self.mode_aware = None

        @property
        def paper_broker(self) -> PaperBroker:
            return self.broker

        def replace_paper_broker(self, broker: PaperBroker) -> None:
            self.broker = broker
            self.engine.broker = broker

        async def close(self) -> None:
            return None

    runtime = Runtime()

    class RepositoryFactory:
        def __new__(cls, client, account_id):
            del client, account_id
            return repository

    monkeypatch.setattr(worker_module, "build_runtime", lambda selected: runtime)
    monkeypatch.setattr(
        worker_module,
        "PersonalExecutionRepository",
        RepositoryFactory,
    )

    await worker_module.run_worker(
        settings=settings,
        stop_event=stop,
        cycle_trigger=trigger,
        install_signal_handlers=False,
    )

    assert repository.load_calls == 2
    assert repository.save_calls == 1
    assert runtime.engine.observed_cash == [Decimal("1000"), Decimal("1000")]


async def test_successful_personal_live_cycle_keeps_limit_orders_until_shutdown(
    monkeypatch,
) -> None:
    stop = asyncio.Event()
    trigger = asyncio.Event()
    trigger.set()
    settings = Settings(
        _env_file=None,
        personal_mode=True,
        personal_live_enabled=True,
        mode="canary",
        component="all",
        account_id=ACCOUNT,
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
        ai_api_key="personal-ai-key",
        polymarket_private_key="12" * 32,
    )
    store = SupabaseStore(
        settings.supabase_url,
        settings.supabase_service_role_key.get_secret_value(),
        account_id=ACCOUNT,
        client=SimpleNamespace(),  # type: ignore[arg-type]
    )
    control = RuntimeControl(
        account_id=ACCOUNT,
        mode=TradingMode.CANARY,
        armed=True,
        accept_new_intents=True,
        armed_until=utc_now() + timedelta(minutes=10),
        kill_switch=False,
        version=3,
    )
    lease = WorkerLease(
        account_id=ACCOUNT,
        owner_id="placeholder",
        fencing_token=7,
        expires_at=utc_now() + timedelta(minutes=2),
    )
    store.health = AsyncMock(return_value=True)  # type: ignore[method-assign]
    store.claim_worker_lease = AsyncMock(return_value=lease)  # type: ignore[method-assign]
    store.validate_worker_lease = AsyncMock(return_value=True)  # type: ignore[method-assign]
    store.get_runtime_control = AsyncMock(return_value=control)  # type: ignore[method-assign]
    store.expire_runtime_control = AsyncMock(return_value=None)  # type: ignore[method-assign]
    store.has_unresolved_live_orders = AsyncMock(return_value=False)  # type: ignore[method-assign]
    disarmed = RuntimeControl(
        account_id=ACCOUNT,
        mode=TradingMode.CANARY,
        kill_switch=True,
        cancellation_pending=True,
        version=4,
    )
    store.disarm_runtime_control = AsyncMock(return_value=disarmed)  # type: ignore[method-assign]
    store.acknowledge_runtime_cancellation = AsyncMock(  # type: ignore[method-assign]
        return_value=disarmed.model_copy(update={"cancellation_pending": False, "version": 5})
    )
    store.release_worker_lease = AsyncMock(return_value=True)  # type: ignore[method-assign]

    class Broker(PolymarketBroker):
        def __init__(self) -> None:
            self.client = SimpleNamespace(
                signer=SIGNER,
                wallet=DEPOSIT,
                wallet_type=3,
                environment=SimpleNamespace(
                    chain_id=137,
                    collateral_token=COLLATERAL,
                ),
            )
            self.cancel_reasons: list[str] = []
            self.guard = None

        def set_execution_guard(self, guard):
            self.guard = guard

        async def ensure_trading_approvals(self) -> tuple[Decimal, bool]:
            return Decimal("25"), True

        async def cancel_all(self, reason: str) -> bool:
            self.cancel_reasons.append(reason)
            return True

        async def redeem_resolved(self) -> int:
            return 0

    broker = Broker()

    class Engine:
        execution_guard = None

        async def run_cycle(self) -> EngineCycleResult:
            stop.set()
            return EngineCycleResult(
                run_id="personal-live-success",
                mode=TradingMode.CANARY,
                completed_at=utc_now(),
            )

    class Runtime:
        def __init__(self) -> None:
            self.store = store
            self.broker = broker
            self.engine = Engine()
            self.reconciler = None
            self.live_broker = broker
            self.mode_aware = None

        async def close(self) -> None:
            return None

    repository = _LiveRepository()
    repository.control = control

    class RepositoryFactory:
        def __new__(cls, client, account_id):
            del client, account_id
            return repository

    async def schema_ready() -> bool:
        return True

    repository.schema_ready = schema_ready  # type: ignore[attr-defined]
    repository.set_paused = AsyncMock(return_value=repository.binding)  # type: ignore[attr-defined]
    monkeypatch.setattr(worker_module, "build_runtime", lambda selected: Runtime())
    monkeypatch.setattr(
        worker_module,
        "PersonalExecutionRepository",
        RepositoryFactory,
    )

    await worker_module.run_worker(
        settings=settings,
        stop_event=stop,
        cycle_trigger=trigger,
        install_signal_handlers=False,
    )

    assert broker.cancel_reasons == ["worker shutdown"]


async def test_desired_mode_is_clamped_and_reported_without_live_capability(
    monkeypatch,
) -> None:
    stop = asyncio.Event()
    trigger = asyncio.Event()
    trigger.set()
    settings = _personal_paper_settings()
    assert settings.personal_live_enabled is False
    store = SupabaseStore(
        settings.supabase_url,
        settings.supabase_service_role_key.get_secret_value(),
        account_id=ACCOUNT,
        client=SimpleNamespace(),  # type: ignore[arg-type]
    )
    store.health = AsyncMock(return_value=True)  # type: ignore[method-assign]
    store.claim_worker_lease = AsyncMock(  # type: ignore[method-assign]
        return_value=WorkerLease(
            account_id=ACCOUNT,
            owner_id="placeholder",
            fencing_token=5,
            expires_at=utc_now() + timedelta(minutes=2),
        )
    )
    store.validate_worker_lease = AsyncMock(return_value=True)  # type: ignore[method-assign]
    store.get_runtime_control = AsyncMock(  # type: ignore[method-assign]
        return_value=RuntimeControl(account_id=ACCOUNT, mode=TradingMode.PAPER, version=2)
    )
    store.has_unresolved_live_orders = AsyncMock(return_value=False)  # type: ignore[method-assign]
    store.release_worker_lease = AsyncMock(return_value=True)  # type: ignore[method-assign]

    class Repository:
        async def schema_ready(self) -> bool:
            return True

        async def get_desired_mode(self) -> TradingMode:
            return TradingMode.CANARY

        async def load_paper_state(self, **kwargs: object) -> None:
            del kwargs
            return None

        async def save_paper_state(self, **kwargs: object) -> PersonalPaperAccountState:
            return PersonalPaperAccountState(
                state=kwargs["state"],  # type: ignore[arg-type]
                version=1,
                updated_at=utc_now(),
            )

    class Engine:
        execution_guard = None

        async def run_cycle(self) -> EngineCycleResult:
            stop.set()
            return EngineCycleResult(
                run_id="clamped-mode",
                mode=TradingMode.PAPER,
                completed_at=utc_now(),
            )

    class Runtime:
        def __init__(self) -> None:
            self.store = store
            self.broker = PaperBroker(settings.bankroll_usd)
            self.engine = Engine()
            self.reconciler = None
            self.live_broker = None
            self.mode_aware = None

        @property
        def paper_broker(self) -> PaperBroker:
            return self.broker

        def replace_paper_broker(self, broker: PaperBroker) -> None:
            self.broker = broker

        async def close(self) -> None:
            return None

    class RepositoryFactory:
        def __new__(cls, client, account_id):
            del client, account_id
            return Repository()

    monkeypatch.setattr(worker_module, "build_runtime", lambda selected: Runtime())
    monkeypatch.setattr(
        worker_module,
        "PersonalExecutionRepository",
        RepositoryFactory,
    )

    await worker_module.run_worker(
        settings=settings,
        stop_event=stop,
        cycle_trigger=trigger,
        install_signal_handlers=False,
    )

    # The request is honoured as far as the deployment allows: still paper, but
    # the clamp is published instead of being silently swallowed.
    assert settings.mode is TradingMode.PAPER
    codes = {warning["code"] for warning in worker_module.worker_readiness()["warnings"]}
    assert "mode_downgraded" in codes


async def test_desired_mode_starts_a_live_cycle_without_a_restart(monkeypatch) -> None:
    """A dashboard switch to canary has to take effect in the running process."""

    stop = asyncio.Event()
    trigger = asyncio.Event()
    trigger.set()
    settings = Settings(
        _env_file=None,
        personal_mode=True,
        personal_live_enabled=True,
        mode="paper",
        component="all",
        account_id=ACCOUNT,
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-test",
        ai_api_key="personal-ai-key",
        polymarket_private_key="12" * 32,
    )
    store = SupabaseStore(
        settings.supabase_url,
        settings.supabase_service_role_key.get_secret_value(),
        account_id=ACCOUNT,
        client=SimpleNamespace(),  # type: ignore[arg-type]
    )
    paper_control = RuntimeControl(
        account_id=ACCOUNT,
        mode=TradingMode.PAPER,
        armed=False,
        kill_switch=True,
        version=2,
    )
    disarmed = RuntimeControl(
        account_id=ACCOUNT,
        mode=TradingMode.CANARY,
        kill_switch=True,
        cancellation_pending=True,
        version=4,
    )
    lease = WorkerLease(
        account_id=ACCOUNT,
        owner_id="placeholder",
        fencing_token=7,
        expires_at=utc_now() + timedelta(minutes=2),
    )
    store.health = AsyncMock(return_value=True)  # type: ignore[method-assign]
    store.claim_worker_lease = AsyncMock(return_value=lease)  # type: ignore[method-assign]
    store.validate_worker_lease = AsyncMock(return_value=True)  # type: ignore[method-assign]
    store.get_runtime_control = AsyncMock(return_value=paper_control)  # type: ignore[method-assign]
    store.expire_runtime_control = AsyncMock(return_value=None)  # type: ignore[method-assign]
    store.has_unresolved_live_orders = AsyncMock(return_value=False)  # type: ignore[method-assign]
    store.disarm_runtime_control = AsyncMock(return_value=disarmed)  # type: ignore[method-assign]
    store.acknowledge_runtime_cancellation = AsyncMock(  # type: ignore[method-assign]
        return_value=disarmed.model_copy(update={"cancellation_pending": False, "version": 5})
    )
    store.release_worker_lease = AsyncMock(return_value=True)  # type: ignore[method-assign]

    class Broker(PolymarketBroker):
        def __init__(self) -> None:
            self.client = SimpleNamespace(
                signer=SIGNER,
                wallet=DEPOSIT,
                wallet_type=3,
                environment=SimpleNamespace(
                    chain_id=137,
                    collateral_token=COLLATERAL,
                ),
            )
            self.cancel_reasons: list[str] = []
            self.guard = None

        def set_execution_guard(self, guard):
            self.guard = guard

        async def ensure_trading_approvals(self) -> tuple[Decimal, bool]:
            return Decimal("25"), True

        async def cancel_all(self, reason: str) -> bool:
            self.cancel_reasons.append(reason)
            return True

        async def redeem_resolved(self) -> int:
            return 0

    broker = Broker()
    cycles: list[TradingMode] = []

    class Engine:
        execution_guard = None

        async def run_cycle(self) -> EngineCycleResult:
            cycles.append(settings.mode)
            stop.set()
            return EngineCycleResult(
                run_id="hot-switch",
                mode=settings.mode,
                completed_at=utc_now(),
            )

    class Runtime:
        def __init__(self) -> None:
            self.store = store
            self.broker = broker
            self.engine = Engine()
            self.reconciler = None
            self.live_broker = broker
            self.mode_aware = None

        async def close(self) -> None:
            return None

    repository = _LiveRepository()

    async def get_desired_mode() -> TradingMode:
        return TradingMode.CANARY

    async def schema_ready() -> bool:
        return True

    # The worker generates its own owner id, so the fixture's strict ``arm``
    # assertion is replaced by one that records the call instead.
    repository.arm = AsyncMock(  # type: ignore[method-assign]
        return_value=RuntimeControl(
            account_id=ACCOUNT,
            mode=TradingMode.CANARY,
            armed=True,
            accept_new_intents=True,
            armed_until=utc_now() + timedelta(minutes=10),
            kill_switch=False,
            version=3,
        )
    )
    repository.get_desired_mode = get_desired_mode  # type: ignore[attr-defined]
    repository.schema_ready = schema_ready  # type: ignore[attr-defined]

    class RepositoryFactory:
        def __new__(cls, client, account_id):
            del client, account_id
            return repository

    monkeypatch.setattr(worker_module, "build_runtime", lambda selected: Runtime())
    monkeypatch.setattr(
        worker_module,
        "PersonalExecutionRepository",
        RepositoryFactory,
    )

    await worker_module.run_worker(
        settings=settings,
        stop_event=stop,
        cycle_trigger=trigger,
        install_signal_handlers=False,
    )

    assert cycles == [TradingMode.CANARY]
    assert repository.arm.await_count == 1
    assert repository.events == ["bind", "control", "readiness"]
    assert settings.mode is TradingMode.CANARY
    # Shutdown still goes through the real-money safety sequence.
    assert "worker shutdown" in broker.cancel_reasons
