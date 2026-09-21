from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from polybot.config import TradingMode
from polybot.personal_execution import PersonalExecutionRepository
from polybot.schema import EXPECTED_SCHEMA_VERSION

ACCOUNT = "11111111-1111-4111-8111-111111111111"
SIGNER = "0x" + ("11" * 20)
DEPOSIT = "0x" + ("22" * 20)
TOKEN = "0x" + ("33" * 20)


def _binding_row() -> dict:
    return {
        "account_id": ACCOUNT,
        "signer_address": SIGNER,
        "deposit_wallet_address": DEPOSIT,
        "chain_id": 137,
        "collateral_token": TOKEN,
        "binding_version": 3,
        "paused": False,
        "collateral_balance_pusd": "5000000",
        "allowances_ready": True,
        "readiness_checked_at": "2026-08-05T00:00:00+00:00",
        "readiness_owner_id": "worker-owner-1",
        "readiness_fencing_token": 7,
        "last_seen_at": "2026-08-05T00:00:00+00:00",
        "updated_at": "2026-08-05T00:00:00+00:00",
    }


class Query:
    def __init__(self, client: FakeClient, source: str) -> None:
        self.client = client
        self.source = source

    def select(self, column: str):
        return self

    def eq(self, field: str, value):
        self.client.last_eq = (field, value)
        return self

    def limit(self, value: int):
        self.client.last_limit = value
        return self

    def execute(self):
        self.client.executions.append(self.source)
        if self.source == "rpc:polybot_schema_version":
            return SimpleNamespace(data=EXPECTED_SCHEMA_VERSION)
        if self.source == "rpc:bind_personal_runtime_wallet":
            return SimpleNamespace(data=[_binding_row()])
        if self.source == "rpc:record_personal_wallet_readiness":
            return SimpleNamespace(data=[_binding_row()])
        if self.source == "rpc:set_personal_runtime_paused":
            return SimpleNamespace(data=[_binding_row()])
        if self.source == "rpc:resume_personal_runtime":
            return SimpleNamespace(data=[{"account_id": ACCOUNT, "version": 2}])
        if self.source == "rpc:arm_personal_runtime_control":
            return SimpleNamespace(
                data=[{"account_id": ACCOUNT, "version": 2}]
            )
        if self.source == "rpc:mark_personal_order_submitting":
            return SimpleNamespace(data=[{"mark_personal_order_submitting": True}])
        return SimpleNamespace(data=[])


class FakeClient:
    def __init__(self) -> None:
        self.executions: list[str] = []
        self.last_eq = None
        self.last_limit = None

    def table(self, name: str) -> Query:
        return Query(self, name)

    def rpc(self, name: str, params: dict) -> Query:
        self.last_params = params
        return Query(self, f"rpc:{name}")


def _repo(client: FakeClient | None = None) -> tuple[PersonalExecutionRepository, FakeClient]:
    client = client or FakeClient()
    return PersonalExecutionRepository(client, ACCOUNT), client


async def test_schema_ready_requires_migration_18() -> None:
    repo, client = _repo()
    assert await repo.schema_ready() is True
    assert client.executions == ["rpc:polybot_schema_version"]


async def test_get_binding_returns_none_when_absent() -> None:
    repo, _ = _repo()
    assert await repo.get_binding() is None


async def test_bind_wallet_round_trips_public_metadata() -> None:
    repo, client = _repo()

    binding = await repo.bind_wallet(
        owner_id="worker-owner-1",
        fencing_token=7,
        signer_address=SIGNER,
        deposit_wallet_address=DEPOSIT,
        chain_id=137,
        collateral_token=TOKEN,
    )

    assert binding is not None
    assert binding.signer_address == SIGNER
    assert binding.deposit_wallet_address == DEPOSIT
    assert binding.binding_version == 3
    assert binding.collateral_balance_pusd == Decimal("5000000")
    assert binding.allowances_ready is True
    assert client.last_params["p_fencing_token"] == 7


async def test_bind_wallet_rejects_invalid_addresses() -> None:
    repo, _ = _repo()
    with pytest.raises(ValueError):
        await repo.bind_wallet(
            owner_id="worker-owner-1",
            fencing_token=7,
            signer_address="not-an-address",
            deposit_wallet_address=DEPOSIT,
            chain_id=137,
            collateral_token=TOKEN,
        )


async def test_record_wallet_readiness_round_trips() -> None:
    repo, client = _repo()

    binding = await repo.record_wallet_readiness(
        owner_id="worker-owner-1",
        fencing_token=7,
        binding_version=3,
        collateral_balance_pusd=Decimal("42"),
        allowances_ready=True,
    )

    assert binding is not None
    assert client.last_params["p_balance_pusd"] == "42"
    assert client.last_params["p_allowances_ready"] is True


async def test_record_wallet_readiness_rejects_negative_balance() -> None:
    repo, _ = _repo()
    with pytest.raises(ValueError):
        await repo.record_wallet_readiness(
            owner_id="worker-owner-1",
            fencing_token=7,
            binding_version=3,
            collateral_balance_pusd=Decimal("-1"),
            allowances_ready=True,
        )


async def test_set_paused_only_accepts_true() -> None:
    repo, _ = _repo()
    with pytest.raises(ValueError, match="only accepts true"):
        await repo.set_paused(paused=False)


async def test_set_paused_round_trips_binding() -> None:
    repo, client = _repo()
    binding = await repo.set_paused(paused=True)
    assert binding is not None
    assert client.last_params["p_paused"] is True


async def test_resume_cas_with_version() -> None:
    repo, client = _repo()
    control = await repo.resume(expected_version=2)
    assert control is not None
    assert control.version == 2
    assert client.last_params["p_expected_version"] == 2


async def test_arm_rejects_paper_and_naive_time() -> None:
    repo, _ = _repo()
    with pytest.raises(ValueError, match="canary or live"):
        await repo.arm(
            owner_id="worker-owner-1",
            fencing_token=1,
            mode=TradingMode.PAPER,
            armed_until=datetime.now(UTC) + timedelta(minutes=5),
            expected_version=1,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        await repo.arm(
            owner_id="worker-owner-1",
            fencing_token=1,
            mode=TradingMode.CANARY,
            armed_until=datetime.now(),
            expected_version=1,
        )


async def test_arm_canary_round_trips() -> None:
    repo, client = _repo()
    control = await repo.arm(
        owner_id="worker-owner-1",
        fencing_token=1,
        mode=TradingMode.CANARY,
        armed_until=datetime.now(UTC) + timedelta(minutes=5),
        expected_version=1,
    )
    assert control is not None
    assert client.last_params["p_mode"] == "canary"


async def test_mark_order_submitting_gates_mode_and_hash() -> None:
    repo, _ = _repo()
    assert (
        await repo.mark_order_submitting(
            intent_hash="intent-1",
            owner_id="worker-owner-1",
            worker_fencing_token=1,
            control_version=1,
            mode=TradingMode.PAPER,
        )
        is False
    )
    with pytest.raises(ValueError, match="intent hash"):
        await repo.mark_order_submitting(
            intent_hash="   ",
            owner_id="worker-owner-1",
            worker_fencing_token=1,
            control_version=1,
            mode=TradingMode.CANARY,
        )


async def test_mark_order_submitting_canary_calls_rpc() -> None:
    repo, client = _repo()
    ok = await repo.mark_order_submitting(
        intent_hash="intent-1",
        owner_id="worker-owner-1",
        worker_fencing_token=1,
        control_version=1,
        mode=TradingMode.CANARY,
    )
    assert ok is True
    assert client.last_params["p_mode"] == "canary"
