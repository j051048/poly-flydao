from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import attrs
from cryptography.fernet import Fernet

from polybot.config import Settings, TradingMode
from polybot.geoblock import GeoblockChecker
from polybot.models import (
    UNCLASSIFIED_EVENT_KEY,
    ExecutionResult,
    ExecutionStatus,
    OrderBookSnapshot,
    PortfolioState,
    Side,
    TradeIntent,
)
from polybot.stores.base import StateStore


class PolymarketBroker:
    """Isolated one-shot live executor using Polymarket's official unified SDK."""

    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        *,
        client: Any | None = None,
        geoblock: GeoblockChecker | None = None,
    ):
        if settings.mode not in {TradingMode.CANARY, TradingMode.LIVE}:
            raise ValueError("PolymarketBroker is only valid for canary/live modes")
        self.settings = settings
        self.store = store
        self.geoblock = geoblock or GeoblockChecker(settings.geoblock_url)
        self.cipher = Fernet(settings.signed_payload_key.get_secret_value().encode())
        if client is None:
            from polymarket import SecureClient

            client = SecureClient.create(
                private_key=settings.polymarket_private_key.get_secret_value(),
                wallet=settings.polymarket_deposit_wallet,
            )
        self.client = client
        self._lease_guard: Callable[[], Awaitable[int | None]] | None = None

    def set_execution_guard(self, guard: Callable[[], Awaitable[int | None]]) -> None:
        """Install the worker-only durable lease guard; API/CLI runtimes stay inert."""

        self._lease_guard = guard

    async def portfolio_state(self) -> PortfolioState:
        now = datetime.now(UTC)
        utc_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        positions, open_orders, balance = await asyncio.gather(
            asyncio.to_thread(
                lambda: list(
                    self.client.list_positions(size_threshold=0).iter_items()
                )
            ),
            asyncio.to_thread(lambda: list(self.client.list_open_orders().iter_items())),
            asyncio.to_thread(self.client.get_balance_allowance, asset_type="COLLATERAL"),
        )
        condition_ids = {
            str(condition_id)
            for item in [*positions, *open_orders]
            if (
                condition_id := getattr(item, "condition_id", None)
                or getattr(item, "market", None)
            )
        }
        condition_events, ledger = await asyncio.gather(
            self.store.event_ids_for_conditions(condition_ids),
            self.store.fill_ledger_snapshot(self.settings.account_id, utc_midnight),
        )
        gross = Decimal("0")
        event_exposure: dict[str, Decimal] = {}
        token_positions: dict[str, Decimal] = {}
        token_condition_ids: dict[str, str] = {}
        token_event_ids: dict[str, str] = {}
        for position in positions:
            value = getattr(position, "current_value", None)
            if value is None:
                raise RuntimeError(
                    "position current value is missing; refusing to value it at cost"
                )
            try:
                parsed_value = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise RuntimeError("position current value is invalid") from exc
            if not parsed_value.is_finite() or parsed_value < 0:
                raise RuntimeError("position current value is invalid")
            current = parsed_value
            gross += current
            condition_id = str(
                getattr(position, "condition_id", None)
                or getattr(position, "market", None)
                or ""
            )
            event_id = getattr(position, "event_id", None) or condition_events.get(condition_id)
            event_key = str(event_id) if event_id else UNCLASSIFIED_EVENT_KEY
            event_exposure[event_key] = event_exposure.get(event_key, Decimal("0")) + current
            # polymarket-client Position exposes `asset` as token_id,
            # `conditionId` as condition_id, and optionally `eventId`.
            if position.token_id is not None:
                token_id = str(position.token_id)
                token_positions[token_id] = Decimal(str(position.size or 0))
                if condition_id:
                    token_condition_ids[token_id] = condition_id
                if event_id:
                    token_event_ids[token_id] = str(event_id)
        reserved_cash = Decimal("0")
        for order in open_orders:
            remaining = max(
                Decimal("0"),
                Decimal(str(order.original_size)) - Decimal(str(order.size_matched)),
            )
            if str(order.side).upper() == Side.BUY.value:
                pending = remaining * Decimal(str(order.price))
                reserved_cash += pending
                gross += pending
                condition_id = str(
                    getattr(order, "condition_id", None)
                    or getattr(order, "market", None)
                    or ""
                )
                event_id = condition_events.get(condition_id)
                event_key = str(event_id) if event_id else UNCLASSIFIED_EVENT_KEY
                event_exposure[event_key] = event_exposure.get(event_key, Decimal("0")) + pending
            else:
                token_id = str(order.token_id)
                token_positions[token_id] = max(
                    Decimal("0"), token_positions.get(token_id, Decimal("0")) - remaining
                )

        raw_cash = Decimal(balance.balance) / Decimal("1000000")
        available_cash = max(Decimal("0"), raw_cash - reserved_cash)
        equity = raw_cash + gross - reserved_cash
        equity_state = await self.store.record_equity_state(
            self.settings.account_id, equity
        )
        risk_bankroll = min(
            self.settings.bankroll_usd,
            max(Decimal("0.01"), equity_state.day_start_equity_usd),
        )
        return PortfolioState(
            bankroll_usd=risk_bankroll,
            cash_usd=available_cash,
            gross_exposure_usd=gross,
            event_exposure_usd=event_exposure,
            bucket_exposure_usd={"__unclassified__": gross},
            token_positions=token_positions,
            token_condition_ids=token_condition_ids,
            token_event_ids=token_event_ids,
            realized_pnl_today_usd=ledger.realized_pnl_usd,
            peak_equity_usd=equity_state.peak_equity_usd,
            equity_usd=equity,
            day_start_equity_usd=equity_state.day_start_equity_usd,
        )

    async def submit(self, intent: TradeIntent, book: OrderBookSnapshot) -> ExecutionResult:
        if book.market_id != intent.market_id or book.token_id != intent.token_id:
            return self._rejected(intent, "intent/order-book identity mismatch")
        if intent.price % book.tick_size != 0:
            return self._rejected(intent, "intent price is off the current tick grid")
        if intent.size < book.minimum_order_size:
            return self._rejected(intent, "intent is below the current minimum order size")
        if self._book_is_stale(book):
            return self._rejected(intent, "order book exceeded the live executor age limit")
        if await self.store.has_unresolved_live_orders(intent.account_id):
            return self._rejected(intent, "another durable live order is unresolved")
        control = await self.store.get_runtime_control(intent.account_id)
        if not control.is_live_armed or control.mode is not self.settings.mode:
            return self._rejected(intent, "short-lived runtime arm is absent or mismatched")
        control_version = control.version
        if self._lease_guard is None:
            return self._rejected(intent, "live submission is restricted to the leased worker")
        fencing_token = await self._lease_guard()
        if fencing_token is None:
            return self._rejected(intent, "durable worker lease or reconciliation is unhealthy")
        eligibility = await self.geoblock.check()
        if not eligibility.allowed:
            return self._rejected(intent, eligibility.reason)
        if intent.notional_usd > self.settings.max_order_usd:
            return self._rejected(intent, "executor order cap exceeded")
        if intent.side is Side.BUY:
            if book.best_ask is None or intent.price < book.best_ask:
                return self._rejected(intent, "book moved outside the signed buy price cap")
        elif book.best_bid is None or intent.price > book.best_bid:
            return self._rejected(intent, "book moved outside the signed sell price floor")

        asset_type = "COLLATERAL" if intent.side is Side.BUY else "CONDITIONAL"
        token_id = None if intent.side is Side.BUY else intent.token_id
        balance = await asyncio.to_thread(
            self.client.get_balance_allowance,
            asset_type=asset_type,
            token_id=token_id,
        )
        required = (intent.notional_usd if intent.side is Side.BUY else intent.size) * Decimal(
            "1000000"
        )
        if Decimal(balance.balance) < required:
            return self._rejected(intent, "insufficient exchange balance before signing")
        allowances = [Decimal(value) for value in balance.allowances.values()]
        if not allowances or max(allowances) < required:
            return self._rejected(intent, "insufficient exchange allowance before signing")
        if self._book_is_stale(book):
            return self._rejected(intent, "order book became stale before signing")

        try:
            signed_order = await asyncio.to_thread(
                self.client.create_limit_order,
                token_id=intent.token_id,
                price=intent.price,
                size=intent.size,
                side=intent.side.value,
                post_only=intent.post_only,
            )
        except Exception as exc:
            return ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.ERROR,
                message=f"order preparation/signing failed: {type(exc).__name__}",
            )

        if attrs.has(type(signed_order)):
            payload = attrs.asdict(signed_order)
        else:
            payload = {
                name: getattr(signed_order, name)
                for name in (
                    "builder",
                    "expiration",
                    "maker",
                    "maker_amount",
                    "metadata",
                    "order_type",
                    "post_only",
                    "salt",
                    "side",
                    "signature",
                    "signature_type",
                    "signer",
                    "taker_amount",
                    "timestamp",
                    "token_id",
                )
                if hasattr(signed_order, name)
            }
        serialized = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
        signed_order_hash = hashlib.sha256(serialized).hexdigest()
        ciphertext = self.cipher.encrypt(serialized)
        try:
            await self.store.prepare_signed_order(
                intent,
                signed_order_hash=signed_order_hash,
                payload_ciphertext=ciphertext,
                key_version=self.settings.payload_key_version,
                fencing_token=fencing_token,
            )
        except Exception as exc:
            return ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.ERROR,
                message=f"signed order was not durably persisted: {type(exc).__name__}",
            )

        fresh_token = await self._lease_guard()
        if fresh_token != fencing_token:
            return ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.REJECTED,
                message="worker fencing token changed after signing; order was not posted",
            )
        fresh_control = await self.store.get_runtime_control(intent.account_id)
        if (
            not fresh_control.is_live_armed
            or fresh_control.mode is not self.settings.mode
            or fresh_control.version != control_version
        ):
            return ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.REJECTED,
                message="runtime control changed after signing; order was not posted",
            )

        try:
            await self.store.mark_order_submitting(
                intent.intent_hash, intent.account_id, fencing_token
            )
        except Exception as exc:
            return ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.REJECTED,
                message=f"durable submitting transition failed: {type(exc).__name__}",
            )

        final_token = await self._lease_guard()
        final_control = await self.store.get_runtime_control(intent.account_id)
        if final_token != fencing_token:
            return ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.REJECTED,
                message="worker fencing token changed before post; order was not posted",
            )
        if (
            not final_control.is_live_armed
            or final_control.mode is not self.settings.mode
            or final_control.version != control_version
        ):
            return ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.REJECTED,
                message="runtime control changed before post; order was not posted",
            )
        if self._book_is_stale(book):
            return ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.REJECTED,
                message="order book became stale before post; order was not posted",
            )

        try:
            response = await asyncio.to_thread(self.client.post_order, signed_order)
        except Exception as exc:
            # Never blindly re-sign/retry after an ambiguous transport failure.
            cancellation = "verified"
            try:
                await self.cancel_all("ambiguous live submission")
            except Exception:
                cancellation = "FAILED"
            return ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.ERROR,
                message=(
                    f"ambiguous post ({type(exc).__name__}); signed payload is durable; "
                    f"no re-sign; cancel-all {cancellation}"
                ),
            )

        response_ok = bool(getattr(response, "ok", False))
        order_id = str(getattr(response, "order_id", "")) or None
        response_trace = self._response_trace(response)
        runtime_changed_during_post = False
        if response_ok:
            try:
                post_token = await self._lease_guard()
                post_control = await self.store.get_runtime_control(intent.account_id)
                runtime_changed_during_post = bool(
                    post_token != fencing_token
                    or not post_control.is_live_armed
                    or post_control.mode is not self.settings.mode
                    or post_control.version != control_version
                )
            except Exception:
                runtime_changed_during_post = True

        if runtime_changed_during_post:
            cancellation_verified = False
            try:
                cancellation_verified = await self.cancel_all(
                    "runtime control or worker lease changed during order broadcast"
                )
            except Exception:
                pass
            result = ExecutionResult(
                intent_hash=intent.intent_hash,
                status=(
                    ExecutionStatus.CANCELLED
                    if cancellation_verified
                    else ExecutionStatus.ERROR
                ),
                order_id=order_id,
                message=(
                    "runtime control changed during broadcast; accepted order was "
                    + (
                        "cancelled and zero open orders verified"
                        if cancellation_verified
                        else "not safely cancellable; manual reconciliation required"
                    )
                ),
                raw=response_trace,
            )
        elif not response_ok:
            result = ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.REJECTED,
                message=str(getattr(response, "message", "order rejected")),
                raw={
                    **response_trace,
                    "code": str(getattr(response, "code", "unknown")),
                },
            )
        else:
            result = ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.ACCEPTED,
                order_id=order_id,
                message=(
                    "accepted by CLOB; not treated as filled until user-stream/REST "
                    "reconciliation reaches a terminal state"
                ),
                raw=response_trace,
            )
        try:
            await self.store.save_execution(result, intent.account_id)
        except Exception as exc:
            return ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.ERROR,
                order_id=result.order_id,
                message=f"CLOB replied but durable response save failed: {type(exc).__name__}",
                raw=result.raw,
            )
        return result

    async def cancel_all(self, reason: str) -> bool:
        await asyncio.to_thread(self.client.cancel_all)
        for delay in (0.0, 0.2, 0.5):
            if delay:
                await asyncio.sleep(delay)
            open_orders = await asyncio.to_thread(
                lambda: list(self.client.list_open_orders().iter_items())
            )
            if not open_orders:
                return True
        raise RuntimeError(f"cancel-all verification failed ({reason}): open orders remain")

    async def redeem_resolved(self) -> int:
        if not self.settings.auto_redeem_resolved or self._lease_guard is None:
            return 0
        if await self._lease_guard() is None:
            return 0
        control = await self.store.get_runtime_control(self.settings.account_id)
        if not control.is_live_armed or control.mode is not self.settings.mode:
            return 0
        eligibility = await self.geoblock.check()
        if not eligibility.allowed:
            return 0
        positions = await asyncio.to_thread(
            lambda: list(
                self.client.list_positions(
                    size_threshold=0, redeemable=True
                ).iter_items()
            )
        )
        conditions = list(
            dict.fromkeys(
                str(position.condition_id)
                for position in positions
                if getattr(position, "redeemable", False)
            )
        )[:5]
        redeemed = 0
        for condition_id in conditions:
            try:
                handle = await asyncio.to_thread(
                    self.client.redeem_positions, condition_id=condition_id
                )
                await asyncio.to_thread(handle.wait)
            except Exception:
                continue
            redeemed += 1
        if redeemed:
            # A winning redemption moves value from conditional tokens into
            # collateral; refreshing here also records any losing outcomes that
            # resolved to zero in the durable daily-equity risk state.
            await self.portfolio_state()
        return redeemed

    @staticmethod
    def _rejected(intent: TradeIntent, message: str) -> ExecutionResult:
        return ExecutionResult(
            intent_hash=intent.intent_hash,
            status=ExecutionStatus.REJECTED,
            message=message,
        )

    @staticmethod
    def _response_trace(response: Any) -> dict[str, Any]:
        """Preserve V2 async-match identifiers without treating them as fills."""

        return {
            "status": str(getattr(response, "status", "accepted")),
            "trade_ids": [
                str(value) for value in (getattr(response, "trade_ids", None) or ())
            ],
            "transaction_hashes": [
                str(value)
                for value in (
                    getattr(response, "transactions_hashes", None)
                    or getattr(response, "transaction_hashes", None)
                    or ()
                )
            ],
        }

    def _book_is_stale(self, book: OrderBookSnapshot) -> bool:
        age = (datetime.now(UTC) - book.captured_at).total_seconds()
        return age > self.settings.max_book_age_seconds or age < -5
