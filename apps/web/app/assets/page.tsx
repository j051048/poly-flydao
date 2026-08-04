"use client";

import { useCallback, useEffect, useState } from "react";

import { apiRequest, readableApiError } from "../../lib/api";

interface Position {
  id: string;
  market: string;
  outcome: string;
  size: number;
  averagePrice?: number;
  currentPrice?: number;
  valueUsd?: number;
  pnlUsd?: number;
}

interface Portfolio {
  mode: string;
  totalEquityUsd?: number;
  availableBalanceUsd?: number;
  grossExposurePct?: number;
  activeOrders: number;
  realizedPnlUsd?: number;
  unrealizedPnlUsd?: number;
  walletAddress?: string;
  positions: Position[];
  updatedAt?: string;
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function numberValue(...values: unknown[]): number | undefined {
  for (const value of values) {
    if (value === null || value === undefined || value === "") continue;
    const parsed = typeof value === "number" ? value : Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return undefined;
}

function stringValue(...values: unknown[]): string | undefined {
  return values.find(
    (value): value is string => typeof value === "string" && value.length > 0,
  );
}

function parsePortfolio(payload: unknown): Portfolio {
  const root = record(payload);
  const summary = record(root.summary);
  const wallet = record(root.wallet);
  const positions = Array.isArray(root.positions) ? root.positions : [];
  const openOrders = Array.isArray(root.open_orders)
    ? root.open_orders.length
    : numberValue(
        root.active_orders,
        root.open_orders_count,
        summary.open_order_count,
      ) ?? 0;

  return {
    mode: stringValue(root.mode) ?? "paper",
    totalEquityUsd: numberValue(
      summary.total_equity_usd,
      summary.portfolio_value_usd,
      root.total_equity_usd,
      root.total_value_usd,
    ),
    availableBalanceUsd: numberValue(
      summary.available_balance_usd,
      root.available_balance_usd,
      root.cash_usd,
    ),
    grossExposurePct: numberValue(
      summary.gross_exposure_pct,
      root.gross_exposure_pct,
    ),
    activeOrders: openOrders,
    realizedPnlUsd: numberValue(summary.realized_pnl_usd, root.realized_pnl_usd),
    unrealizedPnlUsd: numberValue(
      summary.unrealized_pnl_usd,
      root.unrealized_pnl_usd,
    ),
    walletAddress: stringValue(
      wallet.address,
      wallet.deposit_wallet_address,
      root.wallet_address,
      root.deposit_wallet_address,
    ),
    updatedAt: stringValue(root.updated_at, summary.updated_at),
    positions: positions.map((value, index) => {
      const item = record(value);
      return {
        id:
          stringValue(item.id, item.position_id, item.token_id) ??
          `position-${index}`,
        market: stringValue(item.market, item.market_title, item.question) ?? "未知市场",
        outcome: stringValue(item.outcome, item.side) ?? "—",
        size: numberValue(item.size, item.quantity, item.shares) ?? 0,
        averagePrice: numberValue(
          item.average_entry_price,
          item.average_price,
          item.avg_price,
        ),
        currentPrice: numberValue(item.current_price, item.mark_price),
        valueUsd: numberValue(item.value_pusd, item.value_usd, item.current_value),
        pnlUsd: numberValue(item.pnl_usd, item.unrealized_pnl_usd),
      };
    }),
  };
}

function money(value?: number): string {
  if (value === undefined) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

function percent(value?: number): string {
  if (value === undefined) return "—";
  return new Intl.NumberFormat("zh-CN", {
    style: "percent",
    maximumFractionDigits: 2,
  }).format(value);
}

export default function AssetsPage() {
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await apiRequest<unknown>("/v1/me/portfolio");
      setPortfolio(parsePortfolio(result.data));
    } catch (caught) {
      setError(readableApiError(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <main className="page-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">PERSONAL PORTFOLIO</p>
          <h1>个人资产</h1>
          <p className="muted">
            {portfolio?.mode === "paper"
              ? "当前显示模拟盘资产；未结算仓位按成本记账，不代表真实钱包余额或市场盈利。"
              : portfolio?.mode === "shadow"
                ? "Shadow 只记录决策意图，不创建仓位或计算资产收益。"
                : "当前显示已对账的真实钱包资产。"}
          </p>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
        >
          {loading ? "同步中…" : "刷新资产"}
        </button>
      </header>

      {error && (
        <div className="notice error page-notice" role="alert">
          {error}
        </div>
      )}

      <section className="summary-grid">
        <article className="summary-card">
          <div className="card-heading">
            <span>总资产</span>
            <span className="pill online">
              {portfolio?.mode === "paper"
                ? "PAPER"
                : portfolio?.mode === "shadow"
                  ? "SHADOW"
                  : "pUSD"}
            </span>
          </div>
          <div className="primary-value">
            {loading ? "读取中…" : money(portfolio?.totalEquityUsd)}
          </div>
          <p className="muted">可用余额 {money(portfolio?.availableBalanceUsd)}</p>
        </article>

        <article className="summary-card">
          <div className="card-heading">
            <span>当前总敞口</span>
            <span className="mode-chip">RISK</span>
          </div>
          <div className="primary-value">
            {loading ? "读取中…" : percent(portfolio?.grossExposurePct)}
          </div>
          <p className="muted">服务端按个人风控口径计算</p>
        </article>

        <article className="summary-card">
          <div className="card-heading"><span>活跃订单</span></div>
          <div className="primary-value">
            {loading ? "读取中…" : portfolio?.activeOrders ?? "—"}
          </div>
          <p className="muted">待成交、部分成交和撤单中的订单</p>
        </article>

        <article className="summary-card">
          <div className="card-heading"><span>未实现盈亏</span></div>
          <div className="primary-value">
            {loading ? "读取中…" : money(portfolio?.unrealizedPnlUsd)}
          </div>
          <p className="muted">已实现 {money(portfolio?.realizedPnlUsd)}</p>
        </article>
      </section>

      <section className="panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">OPEN POSITIONS</p>
            <h2>仓位详情</h2>
          </div>
          {portfolio?.walletAddress && (
            <span className="wallet-chip" title={portfolio.walletAddress}>
              {portfolio.walletAddress}
            </span>
          )}
        </div>

        {loading ? (
          <div className="empty-state" aria-live="polite">
            <span aria-hidden="true">◌</span>
            <p>正在读取你的仓位、订单和盈亏…</p>
          </div>
        ) : portfolio && portfolio.positions.length > 0 ? (
          <div className="table-scroll">
            <table className="positions-table">
              <thead>
                <tr>
                  <th>市场</th>
                  <th>结果</th>
                  <th>份额</th>
                  <th>均价</th>
                  <th>现价</th>
                  <th>市值</th>
                  <th>未实现盈亏</th>
                </tr>
              </thead>
              <tbody>
                {portfolio.positions.map((position) => (
                  <tr key={position.id}>
                    <td>{position.market}</td>
                    <td>{position.outcome}</td>
                    <td>{position.size.toLocaleString("zh-CN")}</td>
                    <td>{position.averagePrice?.toFixed(4) ?? "—"}</td>
                    <td>{position.currentPrice?.toFixed(4) ?? "—"}</td>
                    <td>{money(position.valueUsd)}</td>
                    <td className={(position.pnlUsd ?? 0) < 0 ? "negative" : "positive"}>
                      {money(position.pnlUsd)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-state">
            <span aria-hidden="true">◎</span>
            <p>{error ? "资产读取失败。" : "当前暂无开放仓位。"}</p>
            <p>
              {portfolio?.mode === "paper"
                ? "先运行一次 Paper 周期，模拟成交后会在这里显示。"
                : portfolio?.mode === "shadow"
                  ? "Shadow 的决策轨迹请在“判断分析”页面查看。"
                  : "完成机器人钱包配置并入金后，资产会从后端同步。"}
            </p>
          </div>
        )}

        {portfolio?.updatedAt && (
          <p className="panel-note">后端更新时间：{portfolio.updatedAt}</p>
        )}
      </section>
    </main>
  );
}
