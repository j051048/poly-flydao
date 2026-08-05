from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from polybot.config import TradingMode
from polybot.models import RuntimeControl
from supabase import Client

_EVM_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
_MAX_PAPER_STATE_BYTES = 512 * 1024


@dataclass(frozen=True, slots=True)
class PersonalRuntimeBinding:
    """Public identity/readiness for one environment-backed personal wallet."""

    account_id: str
    signer_address: str
    deposit_wallet_address: str
    chain_id: int
    collateral_token: str
    binding_version: int
    paused: bool
    collateral_balance_pusd: Decimal | None
    allowances_ready: bool
    readiness_checked_at: datetime | None
    readiness_owner_id: str | None
    readiness_fencing_token: int | None
    last_seen_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PersonalPaperAccountState:
    state: dict[str, Any]
    version: int
    updated_at: datetime


class PersonalExecutionRepository:
    """Account-bound repository for the environment-backed personal runtime.

    This repository deliberately accepts only public wallet metadata. Private
    keys and AI credentials remain process-local and must never be passed here.
    """

    def __init__(self, client: Client, account_id: str):
        self._client = client
        try:
            self._account_id = str(UUID(account_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("personal account id must be a UUID") from exc

    @property
    def account_id(self) -> str:
        return self._account_id

    async def _execute(self, builder: Any) -> Any:
        return await asyncio.to_thread(builder.execute)

    @staticmethod
    def _first(response: Any) -> dict[str, Any] | None:
        data = getattr(response, "data", None)
        if isinstance(data, list):
            return data[0] if data and isinstance(data[0], dict) else None
        return data if isinstance(data, dict) else None

    async def schema_ready(self) -> bool:
        """Return false when migration 0018 has not reached Supabase."""

        try:
            response = await self._execute(self._client.rpc("polybot_schema_version", {}))
        except Exception:
            return False
        data = getattr(response, "data", None)
        if isinstance(data, list):
            data = data[0] if data else None
        if isinstance(data, dict):
            data = data.get("polybot_schema_version", data.get("version"))
        return data == 18

    async def get_binding(self) -> PersonalRuntimeBinding | None:
        response = await self._execute(
            self._client.table("personal_runtime_bindings")
            .select("*")
            .eq("account_id", self.account_id)
            .limit(1)
        )
        row = self._first(response)
        return _binding_from_row(row) if row else None

    async def bind_wallet(
        self,
        *,
        owner_id: str,
        fencing_token: int,
        signer_address: str,
        deposit_wallet_address: str,
        chain_id: int,
        collateral_token: str,
    ) -> PersonalRuntimeBinding | None:
        owner = _owner(owner_id)
        fence = _positive_int(fencing_token, "worker fencing token")
        chain = _positive_int(chain_id, "chain id")
        response = await self._execute(
            self._client.rpc(
                "bind_personal_runtime_wallet",
                {
                    "p_account_id": self.account_id,
                    "p_owner_id": owner,
                    "p_fencing_token": fence,
                    "p_signer_address": _address(signer_address, "signer address"),
                    "p_deposit_wallet_address": _address(
                        deposit_wallet_address,
                        "deposit wallet address",
                    ),
                    "p_chain_id": chain,
                    "p_collateral_token": _address(collateral_token, "collateral token"),
                },
            )
        )
        row = self._first(response)
        return _binding_from_row(row) if row else None

    async def record_wallet_readiness(
        self,
        *,
        owner_id: str,
        fencing_token: int,
        binding_version: int,
        collateral_balance_pusd: Decimal | str,
        allowances_ready: bool,
    ) -> PersonalRuntimeBinding | None:
        balance = _nonnegative_decimal(collateral_balance_pusd, "collateral balance")
        if not isinstance(allowances_ready, bool):
            raise ValueError("allowances ready must be a boolean")
        response = await self._execute(
            self._client.rpc(
                "record_personal_wallet_readiness",
                {
                    "p_account_id": self.account_id,
                    "p_owner_id": _owner(owner_id),
                    "p_fencing_token": _positive_int(
                        fencing_token,
                        "worker fencing token",
                    ),
                    "p_binding_version": _positive_int(
                        binding_version,
                        "binding version",
                    ),
                    "p_balance_pusd": str(balance),
                    "p_allowances_ready": allowances_ready,
                },
            )
        )
        row = self._first(response)
        return _binding_from_row(row) if row else None

    async def get_runtime_control(self) -> RuntimeControl:
        response = await self._execute(
            self._client.table("runtime_controls")
            .select("*")
            .eq("account_id", self.account_id)
            .limit(1)
        )
        row = self._first(response)
        return (
            RuntimeControl.model_validate(row)
            if row
            else RuntimeControl(account_id=self.account_id)
        )

    async def set_paused(self, paused: bool) -> PersonalRuntimeBinding | None:
        """Idempotently persist the manual stop latch and cancellation request."""

        if paused is not True:
            raise ValueError("set_paused only accepts true; use resume with a control version")
        response = await self._execute(
            self._client.rpc(
                "set_personal_runtime_paused",
                {
                    "p_account_id": self.account_id,
                    "p_paused": paused,
                },
            )
        )
        row = self._first(response)
        return _binding_from_row(row) if row else None

    async def resume(self, *, expected_version: int) -> RuntimeControl | None:
        """CAS-clear the pause latch without arming real-money execution."""

        response = await self._execute(
            self._client.rpc(
                "resume_personal_runtime",
                {
                    "p_account_id": self.account_id,
                    "p_expected_version": _positive_int(
                        expected_version,
                        "runtime control version",
                    ),
                },
            )
        )
        row = self._first(response)
        return RuntimeControl.model_validate(row) if row else None

    async def arm(
        self,
        *,
        owner_id: str,
        fencing_token: int,
        mode: TradingMode,
        armed_until: datetime,
        expected_version: int,
    ) -> RuntimeControl | None:
        if mode not in {TradingMode.CANARY, TradingMode.LIVE}:
            raise ValueError("personal runtime can only arm canary or live mode")
        if armed_until.tzinfo is None or armed_until.utcoffset() is None:
            raise ValueError("armed until must be timezone-aware")
        response = await self._execute(
            self._client.rpc(
                "arm_personal_runtime_control",
                {
                    "p_account_id": self.account_id,
                    "p_owner_id": _owner(owner_id),
                    "p_fencing_token": _positive_int(
                        fencing_token,
                        "worker fencing token",
                    ),
                    "p_mode": mode.value,
                    "p_armed_until": armed_until.isoformat(),
                    "p_expected_version": _positive_int(
                        expected_version,
                        "runtime control version",
                    ),
                },
            )
        )
        row = self._first(response)
        return RuntimeControl.model_validate(row) if row else None

    async def mark_order_submitting(
        self,
        *,
        intent_hash: str,
        owner_id: str,
        worker_fencing_token: int,
        control_version: int,
        mode: TradingMode,
    ) -> bool:
        if mode not in {TradingMode.CANARY, TradingMode.LIVE}:
            return False
        intent = intent_hash.strip()
        if not intent or len(intent) > 256:
            raise ValueError("intent hash must contain at most 256 characters")
        response = await self._execute(
            self._client.rpc(
                "mark_personal_order_submitting",
                {
                    "p_account_id": self.account_id,
                    "p_intent_hash": intent,
                    "p_owner_id": _owner(owner_id),
                    "p_worker_fencing_token": _positive_int(
                        worker_fencing_token,
                        "worker fencing token",
                    ),
                    "p_control_version": _positive_int(
                        control_version,
                        "runtime control version",
                    ),
                    "p_mode": mode.value,
                },
            )
        )
        return _boolean_result(response)

    async def load_paper_state(
        self,
        *,
        owner_id: str,
        fencing_token: int,
    ) -> PersonalPaperAccountState | None:
        response = await self._execute(
            self._client.rpc(
                "load_personal_paper_account_state",
                {
                    "p_account_id": self.account_id,
                    "p_owner_id": _owner(owner_id),
                    "p_fencing_token": _positive_int(
                        fencing_token,
                        "worker fencing token",
                    ),
                },
            )
        )
        row = self._first(response)
        return _paper_state_from_row(row) if row else None

    async def save_paper_state(
        self,
        *,
        owner_id: str,
        fencing_token: int,
        state: dict[str, Any],
    ) -> PersonalPaperAccountState | None:
        if not isinstance(state, dict):
            raise ValueError("paper account state must be an object")
        try:
            encoded = json.dumps(
                state,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("paper account state must be JSON serializable") from exc
        if len(encoded) > _MAX_PAPER_STATE_BYTES:
            raise ValueError("paper account state exceeds 512 KiB")
        response = await self._execute(
            self._client.rpc(
                "save_personal_paper_account_state",
                {
                    "p_account_id": self.account_id,
                    "p_owner_id": _owner(owner_id),
                    "p_fencing_token": _positive_int(
                        fencing_token,
                        "worker fencing token",
                    ),
                    "p_state": state,
                },
            )
        )
        row = self._first(response)
        return _paper_state_from_row(row) if row else None


def _address(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not _EVM_ADDRESS.fullmatch(normalized):
        raise ValueError(f"{label} must be a 20-byte EVM address")
    return normalized


def _owner(value: str) -> str:
    normalized = value.strip()
    if len(normalized) < 8 or len(normalized) > 200:
        raise ValueError("worker owner id must contain 8 to 200 characters")
    return normalized


def _positive_int(value: int, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if normalized <= 0 or normalized != value:
        raise ValueError(f"{label} must be a positive integer")
    return normalized


def _nonnegative_decimal(value: Decimal | str, label: str) -> Decimal:
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a non-negative decimal") from exc
    if not normalized.is_finite() or normalized < 0:
        raise ValueError(f"{label} must be a non-negative decimal")
    return normalized


def _datetime(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} is not a valid timestamp") from exc
    else:
        raise ValueError(f"{label} is not a valid timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed


def _optional_datetime(value: Any, label: str) -> datetime | None:
    return None if value is None else _datetime(value, label)


def _binding_from_row(row: dict[str, Any]) -> PersonalRuntimeBinding:
    raw_balance = row.get("collateral_balance_pusd")
    return PersonalRuntimeBinding(
        account_id=str(UUID(str(row["account_id"]))),
        signer_address=_address(str(row["signer_address"]), "signer address"),
        deposit_wallet_address=_address(
            str(row["deposit_wallet_address"]),
            "deposit wallet address",
        ),
        chain_id=_positive_int(row["chain_id"], "chain id"),
        collateral_token=_address(str(row["collateral_token"]), "collateral token"),
        binding_version=_positive_int(row["binding_version"], "binding version"),
        paused=bool(row.get("paused", False)),
        collateral_balance_pusd=(
            _nonnegative_decimal(raw_balance, "collateral balance")
            if raw_balance is not None
            else None
        ),
        allowances_ready=bool(row.get("allowances_ready", False)),
        readiness_checked_at=_optional_datetime(
            row.get("readiness_checked_at"),
            "readiness checked at",
        ),
        readiness_owner_id=(
            str(row["readiness_owner_id"]) if row.get("readiness_owner_id") is not None else None
        ),
        readiness_fencing_token=(
            _positive_int(row["readiness_fencing_token"], "readiness fencing token")
            if row.get("readiness_fencing_token") is not None
            else None
        ),
        last_seen_at=_datetime(row["last_seen_at"], "last seen at"),
        updated_at=_datetime(row["updated_at"], "updated at"),
    )


def _paper_state_from_row(row: dict[str, Any]) -> PersonalPaperAccountState:
    state = row.get("state")
    if not isinstance(state, dict):
        raise ValueError("stored paper account state must be an object")
    return PersonalPaperAccountState(
        state=state,
        version=_positive_int(row["version"], "paper state version"),
        updated_at=_datetime(row["updated_at"], "paper state updated at"),
    )


def _boolean_result(response: Any) -> bool:
    data = getattr(response, "data", None)
    if isinstance(data, bool):
        return data
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, bool):
        return data
    if isinstance(data, dict):
        for key in ("mark_personal_order_submitting", "result", "success"):
            value = data.get(key)
            if isinstance(value, bool):
                return value
    return False
