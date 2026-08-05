from __future__ import annotations

from polybot.metrics import Metrics, decimal_cost_usd


def test_metrics_counter_gauge_render_and_reset() -> None:
    metrics = Metrics()
    metrics.increment("polybot_cycles_total", {"mode": "paper"})
    metrics.increment("polybot_cycles_total", {"mode": "paper"})
    metrics.increment("polybot_cycle_skips_total", {"code": "forecast_cooldown"})
    metrics.observe("polybot_ai_latency_ms", 123.5)

    text = metrics.render()

    assert "polybot_cycles_total{mode=\"paper\"} 2" in text
    assert "polybot_cycle_skips_total{code=\"forecast_cooldown\"} 1" in text
    assert "polybot_ai_latency_ms 123.5" in text

    metrics.reset()
    assert "polybot_cycles_total" not in metrics.render()


def test_metrics_labels_escape_quotes_and_backslashes() -> None:
    metrics = Metrics()
    metrics.increment("polybot_http_requests_total", {"method": 'GET"', "status": '2\\0'})
    text = metrics.render()
    assert 'method="GET\\""' in text
    assert 'status="2\\\\0"' in text


def test_decimal_cost_convert() -> None:
    assert decimal_cost_usd(None) == 0.0
    assert abs(decimal_cost_usd(__import__("decimal").Decimal("0.00045")) - 0.00045) < 1e-12
