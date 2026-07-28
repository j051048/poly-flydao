from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from polybot.models import Side, UserTradeUpdate
from polybot.reconcile import OrderReconciler


class TraceStore:
    def __init__(self, trace: list[str]) -> None:
        self.trace = trace

    async def reconcile_trade(self, update: UserTradeUpdate, account_id: str) -> None:
        self.trace.append(f"durable:{account_id}:{update.clob_trade_id}")


class CloseOnlyClient:
    async def close(self) -> None:
        return None


def _trade() -> UserTradeUpdate:
    return UserTradeUpdate(
        clob_trade_id="trade-1",
        candidate_order_ids=["order-1"],
        condition_id="condition-1",
        token_id="yes",
        side=Side.BUY,
        trader_side="MAKER",
        price=Decimal("0.44"),
        size=Decimal("1"),
        status="MATCHED",
    )


async def test_fill_callback_runs_after_durable_trade_and_once_per_clob_trade_id() -> None:
    trace: list[str] = []
    callback_count = 0

    async def callback(update: UserTradeUpdate) -> None:
        nonlocal callback_count
        assert trace
        assert trace[-1].startswith("durable:")
        callback_count += 1
        trace.append(f"callback:{update.clob_trade_id}")

    reconciler = OrderReconciler(
        private_key="unused",
        wallet=None,
        account_id="account",
        store=TraceStore(trace),  # type: ignore[arg-type]
        client=CloseOnlyClient(),
        fill_callback=callback,
    )
    update = _trade()

    await asyncio.gather(
        reconciler._reconcile_trade_and_notify(update),
        reconciler._reconcile_trade_and_notify(update),
    )

    assert callback_count == 1
    assert trace.count("callback:trade-1") == 1
    assert trace[0].startswith("durable:")
    await reconciler.close()


async def test_failed_fill_callback_is_retried_after_core_ledger_is_durable() -> None:
    trace: list[str] = []
    callback_attempts = 0

    async def callback(update: UserTradeUpdate) -> None:
        nonlocal callback_attempts
        callback_attempts += 1
        trace.append(f"callback:{callback_attempts}:{update.clob_trade_id}")
        if callback_attempts == 1:
            raise RuntimeError("transient pair CAS conflict")

    reconciler = OrderReconciler(
        private_key="unused",
        wallet=None,
        account_id="account",
        store=TraceStore(trace),  # type: ignore[arg-type]
        client=CloseOnlyClient(),
        fill_callback=callback,
    )
    update = _trade()

    with pytest.raises(RuntimeError, match="CAS conflict"):
        await reconciler._reconcile_trade_and_notify(update)
    await reconciler._reconcile_trade_and_notify(update)

    assert callback_attempts == 2
    assert sum(item.startswith("durable:") for item in trace) == 2
    assert update.clob_trade_id in reconciler._notified_clob_trade_ids
    await reconciler.close()
