from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import attrs

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
from polybot.tenant_crypto import EncryptionContext, TenantAeadCipher


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
        payload_key = settings.resolved_signed_payload_key
        if payload_key is None:
            raise ValueError("a signed-payload encryption key is required for live execution")
        self.cipher = TenantAeadCipher(payload_key.get_secret_value())
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

    async def ensure_trading_approvals(self) -> tuple[Decimal, bool]:
        """Initialize standard allowances only after the imported wallet is funded.

        Wallet import first derives the deposit address so the user can fund it.
        The live worker performs the explicitly-authorized, idempotent approval
        setup later, while all live execution fences and the arm are current.
        """

        if self._lease_guard is None:
            raise RuntimeError("approval setup requires a healthy leased worker")
        initial_fencing_token = await self._lease_guard()
        if initial_fencing_token is None:
            raise RuntimeError("approval setup requires a healthy leased worker")
        control = await self.store.get_runtime_control(self.settings.account_id)
        if not control.is_live_armed or control.mode is not self.settings.mode:
            raise RuntimeError("approval setup requires an active runtime arm")
        balance = await asyncio.to_thread(
            self.client.get_balance_allowance,
            asset_type="COLLATERAL",
        )
        collateral = Decimal(str(balance.balance))
        if collateral <= 0:
            raise RuntimeError(
                "wallet is active but has no collateral; fund its deposit address first"
            )
        allowances = [Decimal(str(value)) for value in balance.allowances.values()]
        if allowances and min(allowances) > 0:
            return collateral / Decimal("1000000"), True

        control_version = control.version
        fresh_fencing_token = await self._lease_guard()
        fresh_control = await self.store.get_runtime_control(self.settings.account_id)
        if (
            fresh_fencing_token != initial_fencing_token
            or not fresh_control.is_live_armed
            or fresh_control.mode is not self.settings.mode
            or fresh_control.version != control_version
        ):
            raise RuntimeError("execution authority changed before approval setup")
        eligibility = await self.geoblock.check()
        if not eligibility.allowed:
            raise RuntimeError(
                f"approval setup blocked by official geoblock: {eligibility.reason}"
            )
        await asyncio.to_thread(self.client.setup_trading_approvals)
        if self._lease_guard is None or await self._lease_guard() is None:
            raise RuntimeError("execution fence changed during approval setup")
        current_control = await self.store.get_runtime_control(self.settings.account_id)
        if (
            not current_control.is_live_armed
            or current_control.mode is not self.settings.mode
            or current_control.version != control_version
        ):
            raise RuntimeError("runtime control changed during approval setup")
        verified = await asyncio.to_thread(
            self.client.get_balance_allowance,
            asset_type="COLLATERAL",
        )
        verified_allowances = [
            Decimal(str(value)) for value in verified.allowances.values()
        ]
        if not verified_allowances or min(verified_allowances) <= 0:
            raise RuntimeError("Polymarket trading approvals could not be verified")
        return Decimal(str(verified.balance)) / Decimal("1000000"), True

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
        assert control.armed_until is not None
        arm_seconds_remaining = (control.armed_until - datetime.now(UTC)).total_seconds()
        if arm_seconds_remaining < 210:
            return self._rejected(
                intent,
                "runtime arm has less than 3.5 minutes remaining for a safe GTD order",
            )
        order_expires_at = control.armed_until
        order_expiration = int(order_expires_at.timestamp())
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
        if intent.post_only:
            maker_error = self._validate_post_only_submission(intent, book)
            if maker_error:
                return self._rejected(intent, maker_error)
        elif intent.side is Side.BUY:
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
        if not allowances or min(allowances) < required:
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
                expiration=order_expiration,
            )
        except Exception as exc:
            return ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.ERROR,
                message=f"order preparation/signing failed: {type(exc).__name__}",
            )

        serialized = self._serialize_signed_order(signed_order)
        signed_order_hash = hashlib.sha256(serialized).hexdigest()
        ciphertext = self._encrypt_signed_order(intent, serialized)
        try:
            await self.store.prepare_signed_order(
                intent,
                signed_order_hash=signed_order_hash,
                payload_ciphertext=ciphertext,
                key_version=self.settings.payload_key_version,
                fencing_token=fencing_token,
                order_type="GTD",
                expires_at=order_expires_at,
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
                intent.intent_hash,
                intent.account_id,
                fencing_token,
                control_version=control_version,
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
        response_trace = {
            **self._response_trace(response),
            "order_type": "GTD",
            "expires_at": order_expires_at.isoformat(),
        }
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

    async def submit_batch(
        self,
        submissions: Sequence[tuple[TradeIntent, OrderBookSnapshot]],
    ) -> list[ExecutionResult]:
        """Post independently signed maker legs through the SDK batch endpoint.

        The CLOB batch endpoint is a transport optimization, not a transaction:
        each response is evaluated independently and mixed accept/reject batches
        trigger targeted cancellation of accepted legs. Fills racing that
        cancellation still require user-stream reconciliation.
        """

        items = list(submissions)
        if not items:
            return []
        if len(items) == 1:
            intent, book = items[0]
            return [await self.submit(intent, book)]
        if len(items) > 15:
            return self._batch_rejected(items, "batch exceeds the conservative 15-order cap")

        intents = [intent for intent, _ in items]
        account_ids = {intent.account_id for intent in intents}
        if account_ids != {self.settings.account_id}:
            return self._batch_rejected(items, "batch account identity mismatch")
        if len({intent.intent_hash for intent in intents}) != len(intents):
            return self._batch_rejected(items, "batch contains duplicate intent hashes")
        if not all(intent.post_only for intent in intents):
            return self._batch_rejected(
                items,
                "multi-order submission is restricted to post-only maker legs",
            )
        for intent, book in items:
            message = self._validate_post_only_submission(intent, book)
            if message:
                return self._batch_rejected(items, message)
        if await self.store.has_unresolved_live_orders(self.settings.account_id):
            return self._batch_rejected(items, "another durable live order is unresolved")

        control = await self.store.get_runtime_control(self.settings.account_id)
        if not control.is_live_armed or control.mode is not self.settings.mode:
            return self._batch_rejected(items, "short-lived runtime arm is absent or mismatched")
        assert control.armed_until is not None
        if (control.armed_until - datetime.now(UTC)).total_seconds() < 210:
            return self._batch_rejected(
                items,
                "runtime arm has less than 3.5 minutes remaining for safe GTD orders",
            )
        if self._lease_guard is None:
            return self._batch_rejected(
                items,
                "live batch submission is restricted to the leased worker",
            )
        fencing_token = await self._lease_guard()
        if fencing_token is None:
            return self._batch_rejected(
                items,
                "durable worker lease or reconciliation is unhealthy",
            )
        eligibility = await self.geoblock.check()
        if not eligibility.allowed:
            return self._batch_rejected(items, eligibility.reason)

        for intent in intents:
            if intent.notional_usd > self.settings.max_order_usd:
                return self._batch_rejected(items, "executor order cap exceeded")
        if (
            sum((intent.notional_usd for intent in intents), Decimal("0"))
            > self.settings.max_order_usd
        ):
            return self._batch_rejected(items, "aggregate batch order cap exceeded")
        required_by_asset: dict[tuple[str, str | None], Decimal] = {}
        for intent in intents:
            asset = (
                ("COLLATERAL", None)
                if intent.side is Side.BUY
                else ("CONDITIONAL", intent.token_id)
            )
            amount = intent.notional_usd if intent.side is Side.BUY else intent.size
            required_by_asset[asset] = required_by_asset.get(asset, Decimal("0")) + amount
        for (asset_type, token_id), amount in required_by_asset.items():
            balance = await asyncio.to_thread(
                self.client.get_balance_allowance,
                asset_type=asset_type,
                token_id=token_id,
            )
            required = amount * Decimal("1000000")
            if Decimal(balance.balance) < required:
                return self._batch_rejected(
                    items,
                    f"insufficient {asset_type.lower()} balance before batch signing",
                )
            allowances = [Decimal(value) for value in balance.allowances.values()]
            if not allowances or min(allowances) < required:
                return self._batch_rejected(
                    items,
                    f"insufficient {asset_type.lower()} allowance before batch signing",
                )

        expiration = int(control.armed_until.timestamp())
        signed_orders: list[Any] = []
        serialized_orders: list[bytes] = []
        try:
            for intent, _ in items:
                signed = await asyncio.to_thread(
                    self.client.create_limit_order,
                    token_id=intent.token_id,
                    price=intent.price,
                    size=intent.size,
                    side=intent.side.value,
                    post_only=True,
                    expiration=expiration,
                )
                signed_orders.append(signed)
                serialized_orders.append(self._serialize_signed_order(signed))
        except Exception as exc:
            return self._batch_errors(
                items,
                f"batch preparation/signing failed: {type(exc).__name__}",
            )

        try:
            for (intent, _), serialized in zip(items, serialized_orders, strict=True):
                await self.store.prepare_signed_order(
                    intent,
                    signed_order_hash=hashlib.sha256(serialized).hexdigest(),
                    payload_ciphertext=self._encrypt_signed_order(intent, serialized),
                    key_version=self.settings.payload_key_version,
                    fencing_token=fencing_token,
                    order_type="GTD",
                    expires_at=control.armed_until,
                )
        except Exception as exc:
            return self._batch_errors(
                items,
                f"signed batch was not fully persisted: {type(exc).__name__}",
            )

        if not await self._batch_gate_is_current(
            fencing_token=fencing_token,
            control_version=control.version,
            items=items,
        ):
            return self._batch_rejected(
                items,
                "worker lease, runtime control, or book changed after batch signing",
            )
        try:
            for intent in intents:
                await self.store.mark_order_submitting(
                    intent.intent_hash,
                    intent.account_id,
                    fencing_token,
                    control_version=control.version,
                )
        except Exception as exc:
            return self._batch_errors(
                items,
                f"durable batch submitting transition failed: {type(exc).__name__}",
            )
        if not await self._batch_gate_is_current(
            fencing_token=fencing_token,
            control_version=control.version,
            items=items,
        ):
            return self._batch_rejected(
                items,
                "worker lease, runtime control, or book changed before batch post",
            )

        try:
            raw_responses = await asyncio.to_thread(self.client.post_orders, signed_orders)
            responses = list(raw_responses)
        except Exception as exc:
            cancellation = "verified"
            try:
                await self.cancel_all("ambiguous live batch submission")
            except Exception:
                cancellation = "FAILED"
            return self._batch_errors(
                items,
                (
                    f"ambiguous batch post ({type(exc).__name__}); no blind retry; "
                    f"cancel-all {cancellation}"
                ),
            )
        if len(responses) != len(items):
            cancellation = False
            try:
                cancellation = await self.cancel_all("malformed batch response")
            except Exception:
                pass
            return self._batch_errors(
                items,
                "batch response count mismatch; cancellation "
                + ("verified" if cancellation else "unverified"),
            )
        if any(
            bool(getattr(response, "ok", False))
            and not str(getattr(response, "order_id", ""))
            for response in responses
        ):
            cancellation = False
            try:
                cancellation = await self.cancel_all("accepted batch response missing order id")
            except Exception:
                pass
            return self._batch_errors(
                items,
                "accepted batch response omitted order id; cancellation "
                + ("verified" if cancellation else "unverified"),
            )

        runtime_current = await self._batch_gate_is_current(
            fencing_token=fencing_token,
            control_version=control.version,
            items=items,
        )
        accepted_ids = [
            str(getattr(response, "order_id", ""))
            for response in responses
            if bool(getattr(response, "ok", False))
            and str(getattr(response, "order_id", ""))
        ]
        mixed = bool(accepted_ids) and len(accepted_ids) != len(responses)
        cancellation_verified = False
        if not runtime_current or mixed:
            try:
                cancellation_verified = await self.cancel_orders(
                    accepted_ids,
                    "runtime changed or batch legs were only partially accepted",
                )
            except Exception:
                cancellation_verified = False

        results: list[ExecutionResult] = []
        for (intent, _), response in zip(items, responses, strict=True):
            response_ok = bool(getattr(response, "ok", False))
            order_id = str(getattr(response, "order_id", "")) or None
            raw = {
                **self._response_trace(response),
                "order_type": "GTD",
                "expires_at": control.armed_until.isoformat(),
                "batch_non_atomic": True,
            }
            if response_ok and (not runtime_current or mixed):
                result = ExecutionResult(
                    intent_hash=intent.intent_hash,
                    status=ExecutionStatus.ERROR,
                    order_id=order_id,
                    message=(
                        "batch leg accepted independently; targeted cancellation "
                        + (
                            "left no open order, but fill race remains ambiguous; "
                            "freeze and reconcile"
                            if cancellation_verified
                            else "unverified; freeze group and reconcile immediately"
                        )
                    ),
                    raw=raw,
                )
            elif response_ok:
                result = ExecutionResult(
                    intent_hash=intent.intent_hash,
                    status=ExecutionStatus.ACCEPTED,
                    order_id=order_id,
                    message=(
                        "batch leg accepted independently; no atomic pair-fill guarantee"
                    ),
                    raw=raw,
                )
            else:
                result = ExecutionResult(
                    intent_hash=intent.intent_hash,
                    status=ExecutionStatus.REJECTED,
                    message=str(getattr(response, "message", "batch leg rejected")),
                    raw={
                        **raw,
                        "code": str(getattr(response, "code", "unknown")),
                    },
                )
            try:
                await self.store.save_execution(result, intent.account_id)
            except Exception as exc:
                result = ExecutionResult(
                    intent_hash=intent.intent_hash,
                    status=ExecutionStatus.ERROR,
                    order_id=result.order_id,
                    message=(
                        "CLOB batch replied but durable response save failed: "
                        f"{type(exc).__name__}"
                    ),
                    raw=result.raw,
                )
            results.append(result)
        return results

    async def cancel_order(self, order_id: str, reason: str) -> bool:
        if not order_id:
            raise ValueError("order_id is required")
        await asyncio.to_thread(self.client.cancel_order, order_id=order_id)
        return await self._verify_orders_absent({order_id}, reason)

    async def cancel_orders(self, order_ids: Sequence[str], reason: str) -> bool:
        unique = tuple(dict.fromkeys(order_id for order_id in order_ids if order_id))
        if not unique:
            return True
        await asyncio.to_thread(self.client.cancel_orders, order_ids=unique)
        return await self._verify_orders_absent(set(unique), reason)

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

    async def close(self) -> None:
        await asyncio.to_thread(self.client.close)

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

    @staticmethod
    def _serialize_signed_order(signed_order: Any) -> bytes:
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
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()

    def _validate_post_only_submission(
        self,
        intent: TradeIntent,
        book: OrderBookSnapshot,
    ) -> str | None:
        if book.market_id != intent.market_id or book.token_id != intent.token_id:
            return "intent/order-book identity mismatch"
        if intent.price % book.tick_size != 0:
            return "intent price is off the current tick grid"
        if intent.size < book.minimum_order_size:
            return "intent is below the current minimum order size"
        if self._book_is_stale(book):
            return "order book exceeded the live executor age limit"
        if (
            book.best_bid is not None
            and book.best_ask is not None
            and book.best_bid >= book.best_ask
        ):
            return "post-only submission rejected a locked or crossed order book"
        if intent.side is Side.BUY:
            if book.best_ask is None:
                return "post-only buy requires a visible best ask"
            if intent.price >= book.best_ask:
                return "post-only buy would cross or lock the best ask"
        else:
            if book.best_bid is None:
                return "post-only sell requires a visible best bid"
            if intent.price <= book.best_bid:
                return "post-only sell would cross or lock the best bid"
        return None

    def _encrypt_signed_order(self, intent: TradeIntent, serialized: bytes) -> bytes:
        return self.cipher.encrypt(
            serialized,
            EncryptionContext(
                account_id=intent.account_id,
                purpose="signed-order",
                object_id=intent.intent_hash,
                key_version=self.settings.payload_key_version,
            ),
        )

    async def _batch_gate_is_current(
        self,
        *,
        fencing_token: int,
        control_version: int,
        items: Sequence[tuple[TradeIntent, OrderBookSnapshot]],
    ) -> bool:
        if self._lease_guard is None or await self._lease_guard() != fencing_token:
            return False
        control = await self.store.get_runtime_control(self.settings.account_id)
        if (
            not control.is_live_armed
            or control.mode is not self.settings.mode
            or control.version != control_version
        ):
            return False
        return all(
            self._validate_post_only_submission(intent, book) is None
            for intent, book in items
        )

    async def _verify_orders_absent(self, order_ids: set[str], reason: str) -> bool:
        for delay in (0.0, 0.2, 0.5):
            if delay:
                await asyncio.sleep(delay)
            open_orders = await asyncio.to_thread(
                lambda: list(self.client.list_open_orders().iter_items())
            )
            remaining = {
                str(getattr(order, "id", None) or getattr(order, "order_id", ""))
                for order in open_orders
            }
            if order_ids.isdisjoint(remaining):
                return True
        raise RuntimeError(
            f"targeted cancellation verification failed ({reason}): orders remain"
        )

    @staticmethod
    def _batch_rejected(
        items: Sequence[tuple[TradeIntent, OrderBookSnapshot]],
        message: str,
    ) -> list[ExecutionResult]:
        return [
            ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.REJECTED,
                message=message,
            )
            for intent, _ in items
        ]

    @staticmethod
    def _batch_errors(
        items: Sequence[tuple[TradeIntent, OrderBookSnapshot]],
        message: str,
    ) -> list[ExecutionResult]:
        return [
            ExecutionResult(
                intent_hash=intent.intent_hash,
                status=ExecutionStatus.ERROR,
                message=message,
            )
            for intent, _ in items
        ]

    def _book_is_stale(self, book: OrderBookSnapshot) -> bool:
        age = (datetime.now(UTC) - book.captured_at).total_seconds()
        return age > self.settings.max_book_age_seconds or age < -5
