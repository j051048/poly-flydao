from __future__ import annotations

from prefect import flow

from polybot.config import TradingMode
from polybot.runtime import build_runtime


@flow(name="polybot-trading-cycle", retries=0, log_prints=True)
async def trading_cycle_flow() -> dict[str, object]:
    """Prefect entry point. Submission remains single-attempt and idempotency-gated."""

    runtime = build_runtime()
    if runtime.settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
        await runtime.close()
        raise RuntimeError("real-money cycles are restricted to the leased polybot-worker")
    try:
        report = await runtime.engine.run_cycle()
        return report.model_dump(mode="json")
    finally:
        await runtime.close()
