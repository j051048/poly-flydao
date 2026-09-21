"""Row decoders shared by the Supabase store and its mixins."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from polybot.models import QuarantineRecord
from polybot.stores.payloads import _decimal_value


def _rpc_boolean(value: Any, function_name: str) -> bool:
    """Decode PostgREST scalar booleans without treating ``[False]`` as true."""

    if isinstance(value, list):
        value = value[0] if value else False
    if isinstance(value, dict):
        value = value.get(function_name, value.get("ok", value.get("valid", False)))
    return value is True


def _text_or_none(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quarantine_from_row(row: dict[str, Any]) -> QuarantineRecord:
    return QuarantineRecord(
        kind=row.get("kind") or "trade",
        external_key=str(row.get("external_key") or ""),
        reason=str(row.get("reason") or "unknown"),
        condition_id=row.get("condition_id"),
        token_id=row.get("token_id"),
        side=row.get("side"),
        size=(None if row.get("size") is None else _decimal_value(row.get("size"))),
        price=(None if row.get("price") is None else _decimal_value(row.get("price"))),
        notional_usd=(
            None if row.get("notional_usd") is None else _decimal_value(row.get("notional_usd"))
        ),
        occurred_at=(
            None
            if row.get("occurred_at") is None
            else datetime.fromisoformat(str(row["occurred_at"]))
        ),
        detail=row.get("detail") or {},
    )
