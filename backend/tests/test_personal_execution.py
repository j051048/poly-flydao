from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from polybot.config import TradingMode
from polybot.personal_execution import PersonalExecutionRepository

ACCOUNT_ID = "11111111-1111-1111-1111-111111111111"
ADDRESS = "0x1111111111111111111111111111111111111111"
DEPOSIT = "0x2222222222222222222222222222222222222222"
COLLATERAL = "0x3333333333333333333333333333333333333333"


class _Builder:
    def __init__(self, client: _Client, source: str, data: Any) -> None:
        self.client = client
        self.source = source
        self.data = data

    def select(self, _columns: str) -> _Builder:
        return self

    def eq(self, _column: str, _value: Any) -> _Builder:
        return self

    def limit(self, _value: int) -> _Builder:
        return self

    def execute(self) -> SimpleNamespace:
        return SimpleNamespace(data=self.data)


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}

    def rpc(self, name: str, params: dict[str, Any]) -> _Builder:
        self.calls.append((name, params))
        return _Builder(self, f"rpc:{name}", self.responses.get(name))

    def table(self, name: str) -> _Builder:
        return _Builder(self, name, self.responses.get(name))


def _binding_row(*, paused: bool = False) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "account_id": ACCOUNT_ID,
        "signer_address": ADDRESS,
        "deposit_wallet_address": DEPOSIT,
        "chain_id": 137,
        "collateral_token": COLLATERAL,
        "binding_version": 2,
        "paused": paused,
        "collateral_balance_pusd": "25.50",
        "allowances_ready": True,
        "readiness_checked_at": now,
        "readiness_owner_id": "worker-personal",
        "readiness_fencing_token": 4,
        "last_seen_at": now,
        "updated_at": now,
    }


async def test_repository_binds_only_public_wallet_metadata() -> None:
    client = _Client()
    client.responses["bind_personal_runtime_wallet"] = [_binding_row()]
    repository = PersonalExecutionRepository(client, ACCOUNT_ID)  # type: ignore[arg-type]

    binding = await repository.bind_wallet(
        owner_id="worker-personal",
        fencing_token=4,
        signer_address=ADDRESS.upper().replace("0X", "0x"),
        deposit_wallet_address=DEPOSIT,
        chain_id=137,
        collateral_token=COLLATERAL,
    )

    assert binding is not None
    assert binding.account_id == ACCOUNT_ID
    assert binding.collateral_balance_pusd is not None
    name, params = client.calls[-1]
    assert name == "bind_personal_runtime_wallet"
    assert params["p_fencing_token"] == 4
    assert params["p_signer_address"] == ADDRESS
    assert not any("private" in key or "api_key" in key for key in params)


async def test_repository_uses_personal_arm_and_atomic_submission_rpcs() -> None:
    client = _Client()
    armed_until = datetime.now(UTC) + timedelta(minutes=10)
    client.responses["arm_personal_runtime_control"] = [
        {
            "account_id": ACCOUNT_ID,
            "mode": "live",
            "armed": True,
            "accept_new_intents": True,
            "armed_until": armed_until.isoformat(),
            "kill_switch": False,
            "cancellation_pending": False,
            "version": 8,
            "updated_at": datetime.now(UTC).isoformat(),
        }
    ]
    client.responses["mark_personal_order_submitting"] = True
    repository = PersonalExecutionRepository(client, ACCOUNT_ID)  # type: ignore[arg-type]

    control = await repository.arm(
        owner_id="worker-personal",
        fencing_token=4,
        mode=TradingMode.LIVE,
        armed_until=armed_until,
        expected_version=7,
    )
    submitted = await repository.mark_order_submitting(
        intent_hash="intent-1",
        owner_id="worker-personal",
        worker_fencing_token=4,
        control_version=8,
        mode=TradingMode.LIVE,
    )

    assert control is not None and control.version == 8
    assert submitted
    assert client.calls[0][0] == "arm_personal_runtime_control"
    assert client.calls[0][1]["p_owner_id"] == "worker-personal"
    assert client.calls[0][1]["p_fencing_token"] == 4
    assert client.calls[1] == (
        "mark_personal_order_submitting",
        {
            "p_account_id": ACCOUNT_ID,
            "p_intent_hash": "intent-1",
            "p_owner_id": "worker-personal",
            "p_worker_fencing_token": 4,
            "p_control_version": 8,
            "p_mode": "live",
        },
    )


async def test_pause_is_durable_and_resume_does_not_arm() -> None:
    client = _Client()
    client.responses["set_personal_runtime_paused"] = [_binding_row(paused=True)]
    client.responses["resume_personal_runtime"] = [
        {
            "account_id": ACCOUNT_ID,
            "mode": "live",
            "armed": False,
            "accept_new_intents": False,
            "armed_until": None,
            "kill_switch": True,
            "cancellation_pending": False,
            "version": 9,
            "updated_at": datetime.now(UTC).isoformat(),
        }
    ]
    repository = PersonalExecutionRepository(client, ACCOUNT_ID)  # type: ignore[arg-type]

    binding = await repository.set_paused(True)
    control = await repository.resume(expected_version=9)

    assert binding is not None and binding.paused
    assert control is not None and control.version == 9
    assert not control.armed and control.kill_switch
    assert client.calls == [
        (
            "set_personal_runtime_paused",
            {"p_account_id": ACCOUNT_ID, "p_paused": True},
        ),
        (
            "resume_personal_runtime",
            {"p_account_id": ACCOUNT_ID, "p_expected_version": 9},
        ),
    ]

    with pytest.raises(ValueError, match="only accepts true"):
        await repository.set_paused(False)


async def test_paper_state_round_trip_is_worker_fenced() -> None:
    now = datetime.now(UTC).isoformat()
    client = _Client()
    client.responses["save_personal_paper_account_state"] = [
        {"state": {"cash": "90"}, "version": 3, "updated_at": now}
    ]
    repository = PersonalExecutionRepository(client, ACCOUNT_ID)  # type: ignore[arg-type]

    saved = await repository.save_paper_state(
        owner_id="worker-personal",
        fencing_token=4,
        state={"cash": "90"},
    )

    assert saved is not None and saved.version == 3
    name, params = client.calls[-1]
    assert name == "save_personal_paper_account_state"
    assert params["p_fencing_token"] == 4


async def test_repository_rejects_invalid_sensitive_boundaries_locally() -> None:
    repository = PersonalExecutionRepository(_Client(), ACCOUNT_ID)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="EVM address"):
        await repository.bind_wallet(
            owner_id="worker-personal",
            fencing_token=4,
            signer_address="not-a-wallet",
            deposit_wallet_address=DEPOSIT,
            chain_id=137,
            collateral_token=COLLATERAL,
        )
    with pytest.raises(ValueError, match="JSON serializable"):
        await repository.save_paper_state(
            owner_id="worker-personal",
            fencing_token=4,
            state={"bad": float("nan")},
        )
