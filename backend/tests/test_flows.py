from __future__ import annotations

import asyncio

import pytest

import polybot.flows as flows_module
from polybot.config import TradingMode
from polybot.models import EngineCycleResult


class FakeRuntime:
    def __init__(self, mode: TradingMode, report: EngineCycleResult | None = None):
        self.settings = type(
            "Settings",
            (),
            {"mode": mode},
        )()
        self.report = report
        self.closed = False
        self.engine = type(
            "Engine",
            (),
            {
                "run_cycle": self._run_cycle,
            },
        )()

    async def _run_cycle(self):
        return self.report

    async def close(self) -> None:
        self.closed = True


def test_trading_cycle_flow_returns_report_json(monkeypatch) -> None:
    report = EngineCycleResult(run_id="run-1", mode=TradingMode.PAPER, markets_scanned=2)
    runtime = FakeRuntime(TradingMode.PAPER, report)
    monkeypatch.setattr(flows_module, "build_runtime", lambda: runtime)

    result = asyncio.run(flows_module.trading_cycle_flow.fn())

    assert result["run_id"] == "run-1"
    assert result["markets_scanned"] == 2


def test_trading_cycle_flow_refuses_real_money(monkeypatch) -> None:
    runtime = FakeRuntime(TradingMode.LIVE)
    monkeypatch.setattr(flows_module, "build_runtime", lambda: runtime)

    with pytest.raises(RuntimeError, match="real-money"):
        asyncio.run(flows_module.trading_cycle_flow.fn())

    assert runtime.closed
