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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await apiRequest<PerformanceSnapshot>("/v1/me/performance");
      setSnapshot(result.data);
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
    </main>
  );
}
