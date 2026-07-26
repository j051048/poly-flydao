from __future__ import annotations

import json
import math
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field

from polybot.config import Settings
from polybot.fees import estimated_fee_per_share


class BacktestRow(BaseModel):
    market_id: str
    observed_at: datetime
    resolved_yes: bool
    ask_yes: Decimal = Field(gt=0, lt=1)
    ask_no: Decimal = Field(gt=0, lt=1)
    depth_yes_usd: Decimal = Field(gt=0)
    depth_no_usd: Decimal = Field(gt=0)
    probability_yes: Decimal = Field(ge=0, le=1)
    probability_low: Decimal = Field(ge=0, le=1)
    probability_high: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    fees_enabled: bool = False
    fee_rate: Decimal = Decimal("0")
    fee_exponent: Decimal = Decimal("1")
    latency_slippage: Decimal = Decimal("0.005")


class BacktestTrade(BaseModel):
    market_id: str
    outcome: str
    price: Decimal
    conservative_probability: Decimal
    stake: Decimal
    fee: Decimal
    pnl: Decimal
    won: bool


class BacktestReport(BaseModel):
    rows: int
    trades: int
    wins: int
    win_rate: Decimal | None
    initial_bankroll: Decimal
    final_bankroll: Decimal
    net_pnl: Decimal
    roi: Decimal
    max_drawdown: Decimal
    brier_score: Decimal
    log_loss: Decimal
    trades_detail: list[BacktestTrade]
    warning: str = (
        "Historical results are not a profit guarantee; use unseen walk-forward data and "
        "real depth."
    )


def load_jsonl(path: Path) -> list[BacktestRow]:
    rows: list[BacktestRow] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(BacktestRow.model_validate_json(line))
            except Exception as exc:
                raise ValueError(f"invalid backtest row {line_number}: {exc}") from exc
    return sorted(rows, key=lambda row: row.observed_at)


def run_backtest(rows: list[BacktestRow], settings: Settings) -> BacktestReport:
    bankroll = settings.bankroll_usd
    initial = bankroll
    peak = bankroll
    max_drawdown = Decimal("0")
    trades: list[BacktestTrade] = []
    brier_sum = Decimal("0")
    log_loss_sum = 0.0

    for row in rows:
        actual = Decimal("1") if row.resolved_yes else Decimal("0")
        brier_sum += (row.probability_yes - actual) ** 2
        p = min(max(float(row.probability_yes), 1e-9), 1 - 1e-9)
        log_loss_sum += -(math.log(p) if row.resolved_yes else math.log(1 - p))

        if row.confidence < settings.min_forecast_confidence:
            continue
        yes_edge = (
            row.probability_low - row.ask_yes - row.latency_slippage - settings.uncertainty_reserve
        )
        no_probability = Decimal("1") - row.probability_high
        no_edge = no_probability - row.ask_no - row.latency_slippage - settings.uncertainty_reserve
        if max(yes_edge, no_edge) < settings.min_edge:
            continue
        if yes_edge >= no_edge:
            outcome = "YES"
            price = row.ask_yes + row.latency_slippage
            conservative = row.probability_low
            depth = row.depth_yes_usd
            won = row.resolved_yes
        else:
            outcome = "NO"
            price = row.ask_no + row.latency_slippage
            conservative = no_probability
            depth = row.depth_no_usd
            won = not row.resolved_yes
        if price >= 1:
            continue

        kelly = max(Decimal("0"), (conservative - price) / (Decimal("1") - price))
        stake = min(
            settings.max_order_usd,
            bankroll * settings.max_trade_risk_pct,
            bankroll * settings.kelly_fraction * kelly,
            depth,
        )
        if stake <= 0:
            continue
        shares = stake / price
        fee_per_share = estimated_fee_per_share(
            price,
            enabled=row.fees_enabled,
            rate=row.fee_rate,
            exponent=row.fee_exponent,
        )
        fee = shares * fee_per_share
        pnl = shares * (Decimal("1") - price) - fee if won else -(stake + fee)
        bankroll += pnl
        peak = max(peak, bankroll)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - bankroll) / peak)
        trades.append(
            BacktestTrade(
                market_id=row.market_id,
                outcome=outcome,
                price=price,
                conservative_probability=conservative,
                stake=stake,
                fee=fee,
                pnl=pnl,
                won=won,
            )
        )
        if bankroll <= initial * (Decimal("1") - settings.max_drawdown_pct):
            break

    wins = sum(trade.won for trade in trades)
    count = len(rows)
    return BacktestReport(
        rows=count,
        trades=len(trades),
        wins=wins,
        win_rate=Decimal(wins) / Decimal(len(trades)) if trades else None,
        initial_bankroll=initial,
        final_bankroll=bankroll,
        net_pnl=bankroll - initial,
        roi=(bankroll - initial) / initial,
        max_drawdown=max_drawdown,
        brier_score=brier_sum / Decimal(count) if count else Decimal("0"),
        log_loss=Decimal(str(log_loss_sum / count)) if count else Decimal("0"),
        trades_detail=trades,
    )


def report_json(report: BacktestReport) -> str:
    return json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2)
