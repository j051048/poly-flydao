from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from polybot.market import MarketData
from polybot.models import (
    AccountActivityUpdate,
    AccountPositionUpdate,
    Outcome,
    QuarantineKind,
    QuarantineRecord,
    UserOrderUpdate,
    UserTradeUpdate,
    utc_now,
)
from polybot.stores.base import StateStore
from polybot.stores.ledger import IncompleteFillLedgerError

logger = logging.getLogger(__name__)

FillCallback = Callable[[UserTradeUpdate], Awaitable[None]]

FAILURE_UNMAPPED_TRADE = "unmapped_account_trade"
FAILURE_POSITION_MISMATCH = "fill_ledger_position_mismatch"
FAILURE_LEDGER_INCOMPLETE = "fill_ledger_incomplete"
FAILURE_UNAVAILABLE = "reconciliation_error"


@dataclass(frozen=True)
class ReconciliationFailure:
    """Stable, operator-facing classification of the last reconciliation error."""

    code: str
    message: str


def classify_reconciliation_failure(exc: Exception) -> ReconciliationFailure:
    if isinstance(exc, IncompleteFillLedgerError):
        text = str(exc)
        if "does not map to a durable bot order" in text:
            return ReconciliationFailure(code=FAILURE_UNMAPPED_TRADE, message=text)
        if "live position does not match" in text:
            return ReconciliationFailure(code=FAILURE_POSITION_MISMATCH, message=text)
        return ReconciliationFailure(code=FAILURE_LEDGER_INCOMPLETE, message=text)
    return ReconciliationFailure(code=FAILURE_UNAVAILABLE, message=f"{type(exc).__name__}: {exc}")


class OrderReconciler:
    """Authenticated user WebSocket plus periodic REST repair for orders, fills and positions."""

    def __init__(
        self,
        *,
        private_key: str,
        wallet: str | None,
        account_id: str,
        store: StateStore,
        interval_seconds: int = 30,
        client: Any | None = None,
        market_data: MarketData | None = None,
        fill_callback: FillCallback | None = None,
        baseline_utc: datetime | None = None,
        quarantine_enabled: bool = True,
    ):
        self.client = client
        self._private_key = private_key
        self._wallet = wallet
        self._client_lock = asyncio.Lock()
        self.account_id = account_id
        self.store = store
        self.interval_seconds = interval_seconds
        self.market_data = market_data
        self.fill_callback = fill_callback
        if baseline_utc is not None and (
            baseline_utc.tzinfo is None or baseline_utc.utcoffset() is None
        ):
            raise ValueError("baseline_utc must be timezone-aware")
        self._baseline_utc = baseline_utc
        self._known_position_conditions: set[str] = set()
        self.healthy = asyncio.Event()
        self._reconcile_lock = asyncio.Lock()
        self._fill_callback_lock = asyncio.Lock()
        self._notified_clob_trade_ids: OrderedDict[str, None] = OrderedDict()
        self._closed = False
        self._handle: Any | None = None
        # Every cold start replays the authenticated CLOB history from epoch 0.
        # A one-day lookback can invent a profitable cost basis for an old
        # wallet, so startup cost is preferred over an unprovable ledger.
        self._trade_after = "0"
        self._activity_start = 1
        self.last_failure: ReconciliationFailure | None = None
        self.quarantine_enabled = quarantine_enabled
        self._quarantined_tokens: set[str] = set()
        self._quarantined_conditions: set[str] = set()
        self._bot_tokens_cache: set[str] | None = None
        self.quarantine_count = 0

    @property
    def baseline_utc(self) -> datetime | None:
        return self._baseline_utc

    def set_baseline(self, value: datetime | None) -> None:
        """Adopt a new ignore-before timestamp and replay the account history.

        Resetting the baseline is the operator's escape hatch for a wallet that
        was used manually before the bot existed. It only ever *ignores* older
        activity; anything after the new baseline is still strictly validated.
        """

        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("baseline must be timezone-aware")
        self._baseline_utc = value
        self._trade_after = "0"
        self._activity_start = 1
        self.healthy.clear()

    @property
    def quarantined_token_ids(self) -> frozenset[str]:
        return frozenset(self._quarantined_tokens)

    def _record_failure(self, exc: Exception) -> None:
        self.last_failure = classify_reconciliation_failure(exc)
        self.healthy.clear()

    def _clear_failure(self) -> None:
        self.last_failure = None

    async def run(self) -> None:
        periodic = asyncio.create_task(self._periodic_rest(), name="polymarket-rest-reconcile")
        backoff = 1.0
        try:
            while not self._closed:
                try:
                    client = await self._ensure_client()
                    await self.reconcile_rest()
                    from polymarket.streams import UserSpec

                    self._handle = await client.subscribe(UserSpec(markets=None))
                    self.healthy.set()
                    self._clear_failure()
                    backoff = 1.0
                    async for event in self._handle:
                        if self._closed:
                            break
                        await self._apply_user_event(event)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._record_failure(exc)
                    if self._closed:
                        break
                    logger.exception("authenticated Polymarket reconciliation failed")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                finally:
                    self.healthy.clear()
                    handle, self._handle = self._handle, None
                    if handle is not None:
                        with contextlib.suppress(Exception):
                            await handle.close()
        finally:
            self.healthy.clear()
            periodic.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await periodic

    async def _periodic_rest(self) -> None:
        while not self._closed:
            await asyncio.sleep(self.interval_seconds)
            try:
                await self.reconcile_rest()
                if self._handle is not None:
                    self.healthy.set()
                    self._clear_failure()
            except Exception as exc:
                self._record_failure(exc)
                if not self._closed:
                    logger.exception("periodic Polymarket REST reconciliation failed")

    async def reconcile_rest(self) -> None:
        await self._ensure_client()
        async with self._reconcile_lock:
            await self._reconcile_rest_once()

    async def _ensure_client(self) -> Any:
        """Create the async SDK client inside an event loop and await its factory."""

        if self._closed:
            raise RuntimeError("reconciler is closed")
        if self.client is not None:
            return self.client
        async with self._client_lock:
            if self._closed:
                raise RuntimeError("reconciler is closed")
            if self.client is None:
                from polymarket import AsyncSecureClient

                created = await AsyncSecureClient.create(
                    private_key=self._private_key,
                    wallet=self._wallet,
                )
                if self._closed:
                    await created.close()
                    raise RuntimeError("reconciler closed during client initialization")
                self.client = created
        return self.client

    async def _reconcile_rest_once(self) -> None:
        # The bot's durable token footprint can change as new orders are signed,
        # so re-read it once per pass instead of trusting a stale snapshot.
        self._bot_tokens_cache = None
        order_count = 0
        open_order_ids: set[str] = set()
        async for order in self.client.list_open_orders().iter_items():
            await self.store.reconcile_order(self._order_update(order), self.account_id)
            open_order_ids.add(str(order.id))
            order_count += 1
            if order_count >= 2000:
                raise RuntimeError("open-order reconciliation safety limit exceeded")
        missing_orders = await self.store.reconcile_open_order_snapshot(
            open_order_ids, self.account_id
        )
        confirmed_absent_order_ids: set[str] = set()
        for target in missing_orders:
            try:
                order = await self.client.get_order(order_id=target.clob_order_id)
            except Exception as exc:
                if not self._is_not_found(exc):
                    raise
                confirmed_absent_order_ids.add(target.clob_order_id)
            else:
                await self.store.reconcile_order(self._order_update(order), self.account_id)
                open_order_ids.add(target.clob_order_id)

        trade_count = 0
        latest_trade_epoch = int(self._trade_after)
        # A confirmed disappearance is repaired against the complete trade
        # history before it can become a cancellation terminal. This avoids
        # missing an eventually-consistent fill whose matched_at predates the
        # rolling cursor.
        trade_after = "0" if confirmed_absent_order_ids else self._trade_after
        seen_trade_ids: set[str] = set()
        async for trade in self.client.list_account_trades(after=trade_after).iter_items():
            for update in await self._account_trade_updates(trade):
                await self._reconcile_trade_and_notify(update)
            seen_trade_ids.add(str(trade.id))
            matched_at = getattr(trade, "matched_at", None)
            if matched_at is not None:
                latest_trade_epoch = max(latest_trade_epoch, int(matched_at.timestamp()))
            trade_count += 1
            if trade_count >= 100_000:
                raise RuntimeError("trade reconciliation safety limit exceeded")

        # AcceptedOrder.trade_ids and non-terminal durable fills are independent
        # of the rolling matched-at cursor. Poll them explicitly until the SDK
        # reports CONFIRMED or FAILED.
        pending_trade_ids = await self.store.pending_trade_ids(self.account_id)
        if len(pending_trade_ids) > 10_000:
            raise RuntimeError("pending-trade reconciliation safety limit exceeded")
        for trade_id in sorted(pending_trade_ids.difference(seen_trade_ids)):
            async for trade in self.client.list_account_trades(id=trade_id).iter_items():
                if str(trade.id) != trade_id:
                    continue
                for update in await self._account_trade_updates(trade):
                    await self._reconcile_trade_and_notify(update)
                seen_trade_ids.add(trade_id)
                trade_count += 1
                if trade_count >= 100_000:
                    raise RuntimeError("trade reconciliation safety limit exceeded")

        # Only a 404 from get_order plus the complete trade repair above counts
        # as one absence confirmation. The store requires two confirmations
        # (or an explicit cancel_pending state) before terminal cancellation.
        await self.store.confirm_orders_absent(
            confirmed_absent_order_ids,
            self.account_id,
        )
        next_trade_after = str(max(0, latest_trade_epoch - 60))

        activity_count = 0
        latest_activity_epoch = self._activity_start
        async for activity in self.client.list_activity(
            activity_types=["SPLIT", "MERGE", "REDEEM", "CONVERSION"],
            start=self._activity_start,
            sort_by="TIMESTAMP",
            sort_direction="ASC",
            page_size=100,
        ).iter_items():
            update = self._activity_update(activity)
            await self.store.reconcile_account_activity(update, self.account_id)
            latest_activity_epoch = max(latest_activity_epoch, int(update.occurred_at.timestamp()))
            activity_count += 1
            if activity_count >= 4500:
                raise RuntimeError("account activity reconciliation safety limit exceeded")
        next_activity_start = max(1, latest_activity_epoch - 60)

        raw_positions: list[tuple[Any, str, str, Decimal, Decimal, Decimal]] = []
        async for position in self.client.list_positions(size_threshold=0).iter_items():
            condition_id = self._required_position_identifier(position, "condition_id")
            token_id = self._required_position_identifier(position, "token_id")
            size = self._required_position_decimal(position, "size")
            initial = self._required_position_decimal(position, "initial_value")
            current = self._required_position_decimal(position, "current_value")
            if size < 0 or initial < 0 or current < 0:
                raise IncompleteFillLedgerError(
                    f"position {token_id} contains a negative required value"
                )
            raw_positions.append((position, condition_id, token_id, size, initial, current))

        position_keys = {
            (condition_id, token_id) for _, condition_id, token_id, _, _, _ in raw_positions
        }
        outcome_map = await self.store.position_outcomes(position_keys)
        missing_keys = position_keys.difference(outcome_map)
        if missing_keys and self.market_data is not None:
            for condition_id in dict.fromkeys(key[0] for key in sorted(missing_keys)):
                market = await self.market_data.get_market_by_condition(condition_id)
                resolved_condition = str(market.condition_id or market.id)
                if resolved_condition != condition_id:
                    raise IncompleteFillLedgerError(
                        "position market lookup returned a different condition"
                    )
                await self.store.save_market(market)
                outcome_map[(condition_id, market.yes_token_id)] = Outcome.YES
                outcome_map[(condition_id, market.no_token_id)] = Outcome.NO
                self._known_position_conditions.add(condition_id)
        missing_keys = position_keys.difference(outcome_map)
        if missing_keys:
            condition_id, token_id = sorted(missing_keys)[0]
            raise IncompleteFillLedgerError(
                "position token is not mapped by its durable market definition "
                f"({condition_id}:{token_id})"
            )

        positions: list[AccountPositionUpdate] = []
        for position, condition_id, token_id, size, initial, current in raw_positions:
            positions.append(
                AccountPositionUpdate(
                    condition_id=condition_id,
                    token_id=token_id,
                    outcome=outcome_map[(condition_id, token_id)],
                    size=size,
                    average_entry_price=Decimal(str(position.avg_price))
                    if position.avg_price is not None
                    else None,
                    cost_basis_usd=initial,
                    realized_pnl_usd=Decimal(str(position.realized_pnl or 0)),
                    mark_price=Decimal(str(position.cur_price))
                    if position.cur_price is not None
                    else None,
                    unrealized_pnl_usd=current - initial,
                )
            )

        utc_midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        ledger = await self.store.fill_ledger_snapshot(self.account_id, utc_midnight)
        live_quantities = {
            position.token_id: position.size
            for position in positions
            if position.size > 0 and position.condition_id not in ledger.redeemed_condition_ids
        }
        tolerance = Decimal("0.000001")
        foreign_token_ids: set[str] = set()
        for token_id in set(live_quantities).union(ledger.token_quantities):
            live_size = live_quantities.get(token_id, Decimal("0"))
            ledger_size = ledger.token_quantities.get(token_id, Decimal("0"))
            if abs(live_size - ledger_size) <= tolerance:
                continue
            if self.quarantine_enabled and await self._is_foreign_token(token_id):
                await self._quarantine_foreign_position(
                    token_id=token_id,
                    live_size=live_size,
                    positions=positions,
                )
                foreign_token_ids.add(token_id)
                continue
            raise IncompleteFillLedgerError(
                "live position does not match the complete durable fill ledger "
                f"for token {token_id}"
            )
        # Foreign inventory must never enter the durable position table: it is
        # not bot capital and would corrupt equity, risk, and PnL.
        owned_positions = [
            position for position in positions if position.token_id not in foreign_token_ids
        ]
        await self.store.reconcile_positions(owned_positions, self.account_id)
        # Advance only after orders, fills, positions, and the independent
        # quantity check all succeed. Failures force the next pass to replay the
        # same complete window.
        self._trade_after = next_trade_after
        self._activity_start = next_activity_start

    async def _quarantine_foreign_position(
        self,
        *,
        token_id: str,
        live_size: Decimal,
        positions: list[AccountPositionUpdate],
    ) -> None:
        """Record position inventory the bot's ledger cannot explain."""

        match = next((item for item in positions if item.token_id == token_id), None)
        record = QuarantineRecord(
            kind=QuarantineKind.POSITION,
            external_key=token_id,
            reason="foreign_position",
            condition_id=match.condition_id if match is not None else None,
            token_id=token_id,
            size=live_size,
            price=match.mark_price if match is not None else None,
            notional_usd=(
                live_size * match.mark_price
                if match is not None and match.mark_price is not None
                else None
            ),
            detail={"note": "position held outside the bot's durable fill ledger"},
        )
        await self.store.record_quarantine(self.account_id, record)
        self._quarantined_tokens.add(token_id)
        self.quarantine_count += 1
        logger.warning(
            "quarantined %s foreign position shares in token %s",
            live_size,
            token_id,
        )

    async def _apply_user_event(self, event: Any) -> None:
        if event.type == "order":
            await self.store.reconcile_order(self._order_update(event.payload), self.account_id)
        elif event.type == "trade":
            for update in await self._account_trade_updates(event.payload):
                await self._reconcile_trade_and_notify(update)

    async def _reconcile_trade_and_notify(self, update: UserTradeUpdate) -> None:
        """Persist the canonical fill before notifying an optional P2 consumer.

        REST and WebSocket delivery overlap by design. The bounded process-local
        cache avoids duplicate callback work, while the pair store's unique
        ``(account_id, clob_trade_id)`` event is the durable restart boundary.
        A failed callback is deliberately not marked as delivered, so the next
        reconciliation replay retries it after the core fill ledger is safe.
        """

        if (
            self._baseline_utc is not None
            and update.matched_at.tzinfo is not None
            and update.matched_at.utcoffset() is not None
            and update.matched_at < self._baseline_utc
        ):
            return
        await self.store.reconcile_trade(update, self.account_id)
        if self.fill_callback is None:
            return
        async with self._fill_callback_lock:
            if update.clob_trade_id in self._notified_clob_trade_ids:
                return
            await self.fill_callback(update)
            self._notified_clob_trade_ids[update.clob_trade_id] = None
            if len(self._notified_clob_trade_ids) > 100_000:
                self._notified_clob_trade_ids.popitem(last=False)

    @staticmethod
    def _order_update(value: Any) -> UserOrderUpdate:
        return UserOrderUpdate(
            clob_order_id=str(value.id),
            condition_id=str(getattr(value, "condition_id", None) or value.market),
            token_id=str(value.token_id),
            side=value.side,
            price=value.price,
            original_size=value.original_size,
            size_matched=value.size_matched,
            status=str(value.status or "LIVE").upper(),
            order_type=str(value.order_type or "GTC").upper(),
            outcome=getattr(value, "outcome", None),
            event_type=str(getattr(value, "order_event_type", "RECONCILE")),
            occurred_at=getattr(value, "timestamp", None) or utc_now(),
            raw=value.model_dump(mode="json") if hasattr(value, "model_dump") else {},
        )

    @staticmethod
    def _trade_update(value: Any) -> UserTradeUpdate:
        return OrderReconciler._trade_updates(value)[0]

    @staticmethod
    def _trade_updates(value: Any) -> list[UserTradeUpdate]:
        raw = value.model_dump(mode="json") if hasattr(value, "model_dump") else {}
        trader_side = getattr(value, "trader_side", None)
        trader_side_text = str(trader_side or "").upper()
        common = {
            "clob_trade_id": str(value.id),
            "condition_id": str(getattr(value, "condition_id", None) or value.market),
            "trader_side": trader_side,
            "status": str(value.status).upper(),
            "transaction_hash": getattr(value, "transaction_hash", None),
            "matched_at": (
                getattr(value, "matched_at", None) or getattr(value, "timestamp", None) or utc_now()
            ),
            "updated_at": getattr(value, "updated_at", None) or utc_now(),
            "raw": raw,
        }
        maker_updates = [
            UserTradeUpdate(
                **common,
                candidate_order_ids=[str(maker.order_id)],
                token_id=str(maker.token_id),
                side=maker.side,
                price=maker.price,
                size=maker.matched_amount,
                outcome=getattr(maker, "outcome", None),
                fee_rate_bps=Decimal(
                    str(
                        getattr(maker, "fee_rate_bps", None)
                        or getattr(value, "fee_rate_bps", 0)
                        or 0
                    )
                ),
            )
            for maker in (getattr(value, "maker_orders", None) or ())
            if getattr(maker, "order_id", None)
        ]
        taker_order_id = getattr(value, "taker_order_id", None)
        taker_updates = (
            [
                UserTradeUpdate(
                    **common,
                    candidate_order_ids=[str(taker_order_id)],
                    token_id=str(value.token_id),
                    side=value.side,
                    price=value.price,
                    size=value.size,
                    outcome=getattr(value, "outcome", None),
                    fee_rate_bps=Decimal(str(getattr(value, "fee_rate_bps", 0) or 0)),
                )
            ]
            if taker_order_id
            else []
        )
        if trader_side_text == "MAKER":
            return maker_updates
        if trader_side_text == "TAKER":
            return taker_updates
        # UserTradePayload.trader_side is optional on the WebSocket. Do not
        # guess: produce both economic perspectives and let the durable order
        # ledger select the account-owned one(s).
        return taker_updates + maker_updates

    async def _account_trade_updates(self, value: Any) -> list[UserTradeUpdate]:
        if self._baseline_utc is not None:
            matched_at = getattr(value, "matched_at", None)
            if matched_at is not None:
                if matched_at.tzinfo is None or matched_at.utcoffset() is None:
                    matched_at = matched_at.replace(tzinfo=UTC)
                if matched_at < self._baseline_utc:
                    return []
        updates = self._trade_updates(value)
        candidates = {
            order_id for update in updates for order_id in update.candidate_order_ids if order_id
        }
        durable = await self.store.durable_order_ids(candidates, self.account_id)
        account_updates = [
            update
            for update in updates
            if any(order_id in durable for order_id in update.candidate_order_ids)
        ]
        if not str(getattr(value, "trader_side", None) or "").strip():
            taker_order_id = str(getattr(value, "taker_order_id", "") or "")
            durable_taker = taker_order_id in durable
            durable_makers = durable.difference({taker_order_id})
            if durable_taker and durable_makers:
                raise IncompleteFillLedgerError(
                    "trade without trader_side maps to both taker and maker durable orders"
                )
        if account_updates:
            return account_updates
        if self.quarantine_enabled and await self._quarantine_unmapped_trade(value, updates):
            return []
        raise IncompleteFillLedgerError(
            "account trade does not map to a durable bot order; dedicated-wallet "
            "ledger integrity cannot be proven"
        )

    async def _bot_tokens(self) -> set[str]:
        """Cache the bot's durable token footprint for one reconciliation pass."""

        if self._bot_tokens_cache is None:
            self._bot_tokens_cache = await self.store.durable_token_ids(self.account_id)
        return self._bot_tokens_cache

    async def _is_foreign_token(self, token_id: str) -> bool:
        """True when the bot has never signed an intent or order for this token.

        Only then is unattributable activity provably *not* the bot's own
        inventory. Any overlap with the bot's durable footprint keeps the
        original fail-closed error.
        """

        if token_id in self._quarantined_tokens:
            return True
        return token_id not in await self._bot_tokens()

    async def _quarantine_unmapped_trade(
        self,
        value: Any,
        updates: list[UserTradeUpdate],
    ) -> bool:
        """Quarantine an unattributable trade instead of deadlocking the worker.

        Quarantined activity is never counted as bot inventory and never feeds
        cost basis; it merely stops one pre-existing manual trade from making a
        dedicated wallet permanently unusable.
        """

        if not updates:
            return False
        tokens = {update.token_id for update in updates if update.token_id}
        if not tokens:
            return False
        for token_id in tokens:
            if not await self._is_foreign_token(token_id):
                return False
        trade_id = str(getattr(value, "id", "") or "")
        for update in updates:
            record = QuarantineRecord(
                kind=QuarantineKind.TRADE,
                external_key=f"{trade_id or update.clob_trade_id}:{update.token_id}",
                reason="unmapped_account_trade",
                condition_id=update.condition_id,
                token_id=update.token_id,
                side=str(update.side),
                size=update.size,
                price=update.price,
                notional_usd=update.size * update.price,
                occurred_at=update.matched_at,
                detail={"candidate_order_ids": list(update.candidate_order_ids)},
            )
            await self.store.record_quarantine(self.account_id, record)
            self._quarantined_tokens.add(update.token_id)
            if update.condition_id:
                self._quarantined_conditions.add(update.condition_id)
            self.quarantine_count += 1
        logger.warning(
            "quarantined %d unattributable account trade(s) touching %d foreign token(s)",
            len(updates),
            len(tokens),
        )
        return True

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        return int(getattr(exc, "status", 0) or 0) == 404

    @staticmethod
    def _required_position_identifier(value: Any, field: str) -> str:
        raw = getattr(value, field, None)
        if raw is None or not str(raw).strip():
            raise IncompleteFillLedgerError(f"position is missing required {field}")
        return str(raw)

    @staticmethod
    def _required_position_decimal(value: Any, field: str) -> Decimal:
        raw = getattr(value, field, None)
        if raw is None:
            raise IncompleteFillLedgerError(f"position is missing required {field}")
        try:
            parsed = Decimal(str(raw))
        except Exception as exc:
            raise IncompleteFillLedgerError(f"position contains invalid required {field}") from exc
        if not parsed.is_finite():
            raise IncompleteFillLedgerError(f"position contains invalid required {field}")
        return parsed

    @staticmethod
    def _activity_update(value: Any) -> AccountActivityUpdate:
        raw = value.model_dump(mode="json") if hasattr(value, "model_dump") else {}
        activity_type = str(getattr(value, "type", "") or raw.get("type", "")).upper()
        condition_id = getattr(value, "condition_id", None) or raw.get("conditionId")
        transaction_hash = getattr(value, "transaction_hash", None) or raw.get("transactionHash")
        occurred_at = getattr(value, "timestamp", None) or utc_now()
        amount = Decimal(str(getattr(value, "amount", None) or raw.get("amount", 0) or 0))
        digest = hashlib.sha256(
            (
                f"{activity_type}:{transaction_hash or ''}:{condition_id or ''}:"
                f"{amount}:{occurred_at.isoformat()}"
            ).encode()
        ).hexdigest()
        return AccountActivityUpdate(
            activity_key=digest,
            activity_type=activity_type,
            condition_id=str(condition_id) if condition_id is not None else None,
            amount_usd=amount,
            transaction_hash=str(transaction_hash) if transaction_hash else None,
            occurred_at=occurred_at,
            raw=raw,
        )

    async def close(self) -> None:
        self._closed = True
        self.healthy.clear()
        handle, self._handle = self._handle, None
        if handle is not None:
            with contextlib.suppress(Exception):
                await handle.close()
        async with self._client_lock:
            client, self.client = self.client, None
        if client is not None:
            await client.close()
