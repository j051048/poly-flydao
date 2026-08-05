"use client";

import { useCallback, useEffect, useState } from "react";

import { apiRequest, readableApiError } from "../../lib/api";

interface CalibrationBin {
  lower: number;
  upper: number;
  samples: number;
  mean_forecast?: number | null;
  observed_frequency?: number | null;
}

interface PerformanceSnapshot {
  sample_size: number;
  resolved_markets: number;
  brier_score?: number | null;
  log_loss?: number | null;
  calibration: CalibrationBin[];
  ai_usage_used: number;
  ai_usage_limit: number;
  strategy_validation: string;
  research_only: boolean;
  warning: string;
}

interface AIUsageRow {
  provider: string;
  model: string;
  marketId?: string;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  latencyMs?: number | null;
  costUsd?: number | null;
  createdAt: string;
}

function percent(value?: number | null): string {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat("zh-CN", {
    style: "percent",
    maximumFractionDigits: 1,
  }).format(value);
}

function score(value?: number | null): string {
  if (value === null || value === undefined) return "—";
  return value.toFixed(4);
}

export default function PerformancePage() {
  const [snapshot, setSnapshot] = useState<PerformanceSnapshot | null>(null);
  const [usageRows, setUsageRows] = useState<AIUsageRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await apiRequest<PerformanceSnapshot>("/v1/me/performance");
      setSnapshot(result.data);
      const usageResult = await apiRequest<unknown>("/v1/me/ai-usage?limit=10");
      const rows = (usageResult.data as { items?: unknown[] })?.items ?? [];
      setUsageRows(
        rows
          .map((value) => {
            const row = value as Record<string, unknown>;
            const cost = row.cost_usd;
            return {
              provider: String(row.provider ?? ""),
              model: String(row.model ?? ""),
              marketId: row.market_id ? String(row.market_id) : undefined,
              inputTokens: Number(row.input_tokens ?? 0),
              outputTokens: Number(row.output_tokens ?? 0),
              totalTokens: Number(row.total_tokens ?? 0),
              latencyMs: row.latency_ms === null || row.latency_ms === undefined
                ? null
                : Number(row.latency_ms),
              costUsd: cost === null || cost === undefined ? null : Number(cost),
              createdAt: String(row.created_at ?? ""),
            };
          })
          .filter((row) => row.provider),
      );
    } catch (caught) {
      setError(readableApiError(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const usageRatio = snapshot?.ai_usage_limit
    ? snapshot.ai_usage_used / snapshot.ai_usage_limit
    : 0;

  return (
    <main className="page-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">CALIBRATION &amp; VALIDATION</p>
          <h1>效果与校准</h1>
          <p className="muted">用已结算结果检验 AI 概率，而不是只看胜率。</p>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
        >
          {loading ? "读取中…" : "刷新报告"}
        </button>
      </header>

      {error && <div className="notice error page-notice" role="alert">{error}</div>}

      <section className="safety-banner">
        <span className="shield" aria-hidden="true">◆</span>
        <div>
          <strong>P2 微结构策略仍为研究模式</strong>
          <p>{snapshot?.warning ?? "样本不足时不会自动解锁实盘策略。"}</p>
        </div>
      </section>

      <section className="summary-grid" aria-label="模型效果摘要">
        <article className="summary-card">
          <div className="card-heading"><span>已评分预测</span></div>
          <div className="primary-value">{snapshot?.sample_size ?? 0}</div>
          <p className="muted">来自 {snapshot?.resolved_markets ?? 0} 个已结算市场</p>
        </article>
        <article className="summary-card">
          <div className="card-heading"><span>Brier Score</span></div>
          <div className="primary-value">{score(snapshot?.brier_score)}</div>
          <p className="muted">越低越好；必须结合样本量和分桶查看</p>
        </article>
        <article className="summary-card">
          <div className="card-heading"><span>Log Loss</span></div>
          <div className="primary-value">{score(snapshot?.log_loss)}</div>
          <p className="muted">会重罚过度自信的错误预测</p>
        </article>
        <article className="summary-card">
          <div className="card-heading"><span>今日 AI 预算</span></div>
          <div className="primary-value">{percent(usageRatio)}</div>
          <p className="muted">
            {snapshot?.ai_usage_used ?? 0} / {snapshot?.ai_usage_limit ?? 0} 请求单位
          </p>
        </article>
      </section>

      <section className="panel calibration-panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">RELIABILITY</p>
            <h2>概率校准分桶</h2>
          </div>
          <span className={`pill ${snapshot?.research_only ? "degraded" : "online"}`}>
            {snapshot?.research_only ? "RESEARCH ONLY" : "VALIDATED"}
          </span>
        </div>
        {!snapshot?.calibration?.some((item) => item.samples > 0) ? (
          <div className="empty-state">
            <p>尚无已结算预测样本。</p>
            <p>Worker 会自动核对已到期市场；积累样本后这里会出现校准曲线。</p>
          </div>
        ) : (
          <div className="calibration-grid">
            {snapshot.calibration.map((item) => (
              <article key={`${item.lower}-${item.upper}`}>
                <div>
                  <strong>{percent(item.lower)}–{percent(item.upper)}</strong>
                  <span>{item.samples} 个样本</span>
                </div>
                <div className="calibration-track" aria-hidden="true">
                  {item.mean_forecast !== null && item.mean_forecast !== undefined && (
                    <span
                      className="forecast-mark"
                      style={{ left: percent(item.mean_forecast) }}
                    />
                  )}
                  {item.observed_frequency !== null &&
                    item.observed_frequency !== undefined && (
                      <span
                        className="observed-mark"
                        style={{ left: percent(item.observed_frequency) }}
                      />
                    )}
                </div>
                <p>
                  平均预测 {percent(item.mean_forecast)} · 实际发生 {percent(item.observed_frequency)}
                </p>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="panel calibration-panel" aria-label="AI 调用成本台账">
        <div className="section-heading">
          <div>
            <p className="eyebrow">AI COST LEDGER</p>
            <h2>最近 AI 调用</h2>
          </div>
          <span className="pill">{usageRows.length} 条</span>
        </div>
        {usageRows.length === 0 ? (
          <div className="empty-state">
            <p>暂无 AI 调用记录。运行包含 AI 预测的周期后，这里会显示 token 用量、延迟与估算成本。</p>
          </div>
        ) : (
          <div className="table-scroll">
            <table className="positions-table ai-usage-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>模型</th>
                  <th>输入 token</th>
                  <th>输出 token</th>
                  <th>延迟</th>
                  <th>估算成本</th>
                </tr>
              </thead>
              <tbody>
                {usageRows.map((row, index) => (
                  <tr key={`${row.createdAt}-${index}`}>
                    <td>{row.createdAt ? new Date(row.createdAt).toLocaleString("zh-CN") : "—"}</td>
                    <td title={`${row.provider} / ${row.marketId ?? ""}`}>{row.model}</td>
                    <td>{row.inputTokens.toLocaleString()}</td>
                    <td>{row.outputTokens.toLocaleString()}</td>
                    <td>{row.latencyMs === null ? "—" : `${row.latencyMs} ms`}</td>
                    <td>
                      {typeof row.costUsd !== "number"
                        ? "—"
                        : `$${row.costUsd.toFixed(6)}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  );
}
