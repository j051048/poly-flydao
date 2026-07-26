from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


class IncompleteFillLedgerError(RuntimeError):
    """Raised when durable fills cannot establish a valid long-only cost basis."""


@dataclass(frozen=True)
class FillLedgerSnapshot:
    realized_pnl_usd: Decimal
    token_quantities: dict[str, Decimal]
    token_costs_usd: dict[str, Decimal]
    token_condition_ids: dict[str, str]
    redeemed_condition_ids: set[str]


def realized_pnl_since(
    rows: Iterable[Mapping[str, Any]],
    *,
    since: datetime,
) -> Decimal:
    """Rebuild average-cost positions and sum realized PnL after ``since``.

    The full durable fill history is required to establish each token's cost
    basis. A sell without enough preceding buys is treated as an incomplete
    ledger, never guessed from the exchange's cumulative position PnL.
    """

    return replay_fill_ledger(rows, since=since).realized_pnl_usd


def replay_fill_ledger(
    rows: Iterable[Mapping[str, Any]],
    *,
    since: datetime,
    activities: Iterable[Mapping[str, Any]] = (),
) -> FillLedgerSnapshot:
    """Replay fills plus resolution lifecycle into cost basis and balances."""

    if since.tzinfo is None or since.utcoffset() is None:
        raise ValueError("since must be timezone-aware")
    cutoff = since.astimezone(UTC)
    positions: dict[str, tuple[Decimal, Decimal]] = {}
    token_conditions: dict[str, str] = {}
    redeemed_conditions: set[str] = set()
    realized = Decimal("0")

    normalized: list[
        tuple[datetime, datetime, int, str, str, Mapping[str, Any]]
    ] = []
    for row in rows:
        if str(row.get("settlement_status", "")).upper() == "FAILED":
            continue
        matched_at = _as_utc_datetime(row.get("matched_at"), "matched_at")
        created_at = _as_utc_datetime(row.get("created_at") or matched_at, "created_at")
        normalized.append(
            (
                matched_at,
                created_at,
                0,
                str(row.get("fill_key", "")),
                "FILL",
                row,
            )
        )

    for row in activities:
        occurred_at = _as_utc_datetime(row.get("occurred_at"), "occurred_at")
        activity_type = str(row.get("activity_type") or "").upper()
        normalized.append(
            (
                occurred_at,
                occurred_at,
                1,
                str(row.get("activity_key", "")),
                activity_type,
                row,
            )
        )

    for matched_at, _, _, _, event_type, row in sorted(
        normalized, key=lambda item: item[:4]
    ):
        if event_type != "FILL":
            if event_type != "REDEEM":
                raise IncompleteFillLedgerError(
                    f"unsupported account token activity {event_type or 'UNKNOWN'}"
                )
            condition_id = str(row.get("condition_id") or "").strip()
            amount = _decimal(row.get("amount_usd", 0), "amount_usd")
            if not condition_id or amount < 0:
                raise IncompleteFillLedgerError("redeem activity has invalid economics")
            redeemed_conditions.add(condition_id)
            tokens = [
                token_id
                for token_id, (quantity, _) in positions.items()
                if quantity > 0 and token_conditions.get(token_id) == condition_id
            ]
            if not tokens:
                if amount > Decimal("0.000001"):
                    raise IncompleteFillLedgerError(
                        "redeem activity has no complete preceding token cost basis"
                    )
                continue
            released_cost = sum((positions[token_id][1] for token_id in tokens), Decimal("0"))
            if matched_at >= cutoff:
                realized += amount - released_cost
            for token_id in tokens:
                positions[token_id] = (Decimal("0"), Decimal("0"))
            continue

        token_id = str(row.get("outcome_token_id") or "").strip()
        side = str(row.get("side") or "").upper()
        if not token_id or side not in {"BUY", "SELL"}:
            raise IncompleteFillLedgerError("fill ledger contains an invalid token or side")
        condition_id = str(row.get("condition_id") or "").strip()
        if condition_id:
            known_condition = token_conditions.get(token_id)
            if known_condition and known_condition != condition_id:
                raise IncompleteFillLedgerError(
                    f"token {token_id} maps to more than one condition"
                )
            token_conditions[token_id] = condition_id
        price = _decimal(row.get("price"), "price")
        size = _decimal(row.get("size"), "size")
        fee = _decimal(row.get("fee_pusd", 0), "fee_pusd")
        if not Decimal("0") < price < Decimal("1") or size <= 0 or fee < 0:
            raise IncompleteFillLedgerError("fill ledger contains invalid economics")

        quantity, cost = positions.get(token_id, (Decimal("0"), Decimal("0")))
        if side == "BUY":
            positions[token_id] = (quantity + size, cost + price * size + fee)
            continue

        tolerance = Decimal("0.000000000001")
        if quantity <= 0 or size - quantity > tolerance:
            raise IncompleteFillLedgerError(
                f"sell fill for token {token_id} has no complete preceding buy history"
            )
        sold = min(size, quantity)
        average_cost = cost / quantity
        basis_released = average_cost * sold
        if matched_at >= cutoff:
            realized += price * sold - fee - basis_released
        remaining = max(Decimal("0"), quantity - sold)
        remaining_cost = max(Decimal("0"), cost - basis_released)
        if remaining <= tolerance:
            remaining = Decimal("0")
            remaining_cost = Decimal("0")
        positions[token_id] = (remaining, remaining_cost)

    return FillLedgerSnapshot(
        realized_pnl_usd=realized,
        token_quantities={
            token_id: quantity
            for token_id, (quantity, _) in positions.items()
            if quantity > 0
        },
        token_costs_usd={
            token_id: cost for token_id, (quantity, cost) in positions.items() if quantity > 0
        },
        token_condition_ids={
            token_id: token_conditions[token_id]
            for token_id, (quantity, _) in positions.items()
            if quantity > 0 and token_id in token_conditions
        },
        redeemed_condition_ids=redeemed_conditions,
    )


def _decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise IncompleteFillLedgerError(f"fill ledger has invalid {field}") from exc


def _as_utc_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise IncompleteFillLedgerError(f"fill ledger has invalid {field}") from exc
    else:
        raise IncompleteFillLedgerError(f"fill ledger has invalid {field}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IncompleteFillLedgerError(f"fill ledger has naive {field}")
    return parsed.astimezone(UTC)
