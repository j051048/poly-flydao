from __future__ import annotations

from pathlib import Path

from polybot.backtest import load_jsonl, run_backtest


def test_backtest_reports_calibration_and_drawdown(settings) -> None:
    rows = load_jsonl(Path("examples/backtest_sample.jsonl"))
    report = run_backtest(rows, settings)
    assert report.rows == 5
    assert report.trades >= 1
    assert report.brier_score >= 0
    assert report.log_loss >= 0
    assert 0 <= report.max_drawdown <= 1
    assert "not a profit guarantee" in report.warning
    assert report.engine == "legacy_snapshot_v1"
    assert report.event_level is False
    assert report.live_gate_eligible is False
    assert "cannot be used as a live gate" in report.warning
