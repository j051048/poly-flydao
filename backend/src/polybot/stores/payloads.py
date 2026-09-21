"""Pure payload transformations for the order and fill write path.

Extracted from :mod:`polybot.stores.supabase_store`. These functions are the
only place where duplicate broker/engine saves are made idempotent and
lifecycle-monotonic, so they are kept together and unit-tested on their own.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from polybot.stores.ledger import IncompleteFillLedgerError

_ORDER_SETTLEMENT_RANK = {
    "matched": 1,
    "mined": 2,
    "confirmed": 3,
}
_ORDER_TERMINAL_STATUSES = {
    "confirmed",
    "simulated",
    "cancelled",
    "expired",
    "rejected",
    "failed",
}
_FILL_STATUS_RANK = {
    "MATCHED": 0,
    "RETRYING": 1,
    "MINED": 2,
    "CONFIRMED": 3,
    "FAILED": 3,
}

def _decimal_value(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(value if value is not None else default))


def _merge_execution_order_payload(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Make duplicate broker/engine saves idempotent and lifecycle-monotonic."""

    current_status = str(existing.get("status") or "created")
    incoming_status = str(incoming["status"])
    early = {"created", "signed", "submitting", "submitted", "unknown"}
    transition_allowed = current_status in early
    if incoming_status == "submitted":
        transition_allowed = current_status in early
    elif incoming_status == "unknown":
        transition_allowed = current_status in {"created", "signed", "submitting", "unknown"}
    elif incoming_status in {"rejected", "failed"}:
        transition_allowed = current_status in early
    elif incoming_status == "cancelled":
        transition_allowed = current_status not in (
            _ORDER_TERMINAL_STATUSES | set(_ORDER_SETTLEMENT_RANK)
        )
    elif incoming_status == "simulated":
        transition_allowed = current_status in {"created", "submitted", "simulated"}

    payload = dict(incoming)
    if not transition_allowed:
        payload["status"] = current_status

    original_size = _decimal_value(existing.get("original_size"), str(incoming["original_size"]))
    existing_filled = _decimal_value(existing.get("filled_size"))
    incoming_filled = _decimal_value(incoming.get("filled_size"))
    filled_size = min(original_size, max(existing_filled, incoming_filled))
    payload["original_size"] = str(original_size)
    payload["filled_size"] = str(filled_size)
    payload["remaining_size"] = str(max(Decimal("0"), original_size - filled_size))
    if existing_filled > incoming_filled and existing.get("average_fill_price") is not None:
        payload["average_fill_price"] = existing["average_fill_price"]
    payload["clob_order_id"] = existing.get("clob_order_id") or incoming.get("clob_order_id")
    payload["attempt_count"] = max(
        int(existing.get("attempt_count") or 0),
        int(incoming.get("attempt_count") or 0),
    )
    payload["submitted_at"] = existing.get("submitted_at") or incoming.get("submitted_at")
    existing_order_type = str(existing.get("order_type") or "").upper()
    incoming_order_type = str(incoming.get("order_type") or "").upper()
    # A signed GTD order is immutable exchange-side. Later execution retries
    # can have an empty response payload (notably an ambiguous POST), so they
    # must never erase the durable expiry or downgrade the row to GTC.
    if existing_order_type == "GTD":
        payload["order_type"] = "GTD"
        payload["expires_at"] = existing.get("expires_at") or incoming.get("expires_at")
    elif incoming_order_type == "GTD":
        payload["order_type"] = "GTD"
        payload["expires_at"] = incoming.get("expires_at") or existing.get("expires_at")
    else:
        payload["order_type"] = existing.get("order_type") or incoming.get("order_type")
        payload["expires_at"] = existing.get("expires_at") or incoming.get("expires_at")
    payload["response_payload"] = {
        **(existing.get("response_payload") or {}),
        **(incoming.get("response_payload") or {}),
    }
    if not transition_allowed:
        payload["last_error_detail"] = existing.get("last_error_detail")
    return payload


def _merge_fill_payload(
    existing: dict[str, Any] | None, incoming: dict[str, Any]
) -> dict[str, Any]:
    """Prevent stale REST/WS events from moving a fill out of a terminal state."""

    if existing is None:
        return incoming
    payload = dict(incoming)
    current_status = str(existing.get("settlement_status") or "MATCHED").upper()
    incoming_status = str(incoming["settlement_status"]).upper()
    if current_status in {"CONFIRMED", "FAILED"}:
        payload["settlement_status"] = current_status
    elif incoming_status == "FAILED" or (
        _FILL_STATUS_RANK.get(incoming_status, 0) >= _FILL_STATUS_RANK.get(current_status, 0)
    ):
        payload["settlement_status"] = incoming_status
    else:
        payload["settlement_status"] = current_status
    payload["transaction_hash"] = incoming.get("transaction_hash") or existing.get(
        "transaction_hash"
    )
    payload["mined_at"] = existing.get("mined_at") or incoming.get("mined_at")
    payload["confirmed_at"] = existing.get("confirmed_at") or incoming.get("confirmed_at")
    return payload


def _aggregate_order_fill_payload(
    order: dict[str, Any], fills: list[dict[str, Any]]
) -> dict[str, Any]:
    successful = [
        fill for fill in fills if str(fill.get("settlement_status") or "").upper() != "FAILED"
    ]
    original_size = _decimal_value(order.get("original_size"))
    reported_filled_size = sum(
        (_decimal_value(fill.get("size")) for fill in successful),
        Decimal("0"),
    )
    if reported_filled_size - original_size > Decimal("0.000001"):
        raise IncompleteFillLedgerError("durable fills exceed the order's original size")
    filled_size = min(original_size, reported_filled_size)
    remaining_size = max(Decimal("0"), original_size - filled_size)
    payload: dict[str, Any] = {
        "filled_size": str(filled_size),
        "remaining_size": str(remaining_size),
        "fee_paid_pusd": str(
            sum((_decimal_value(fill.get("fee_pusd")) for fill in successful), Decimal("0"))
        ),
    }
    if filled_size > 0:
        notional = sum(
            (
                _decimal_value(fill.get("price")) * _decimal_value(fill.get("size"))
                for fill in successful
            ),
            Decimal("0"),
        )
        payload["average_fill_price"] = str(notional / filled_size)

    current_status = str(order.get("status") or "submitted")
    if filled_size <= 0:
        if fills and all(
            str(fill.get("settlement_status") or "").upper() == "FAILED" for fill in fills
        ):
            payload["status"] = "failed"
        else:
            payload["status"] = current_status
    elif remaining_size > 0:
        payload["status"] = (
            current_status if current_status in {"cancelled", "expired"} else "partially_filled"
        )
    else:
        statuses = {str(fill.get("settlement_status") or "MATCHED").upper() for fill in successful}
        if statuses.intersection({"MATCHED", "RETRYING"}):
            payload["status"] = "matched"
        elif "MINED" in statuses:
            payload["status"] = "mined"
        else:
            payload["status"] = "confirmed"
    return payload
