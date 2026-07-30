"use client";

import { useCallback, useEffect, useState } from "react";

import { apiRequest, readableApiError } from "../../lib/api";

type JsonRecord = Record<string, unknown>;

interface EvidenceView {
  id: string;
  title: string;
  summary: string;
  url?: string;
  publishedAt?: string;
  reliability?: number;
}

interface AnalysisView {
  id: string;
  question: string;
  slug?: string;
  asOf?: string;
  model?: string;
  probabilityYes?: number;
  probabilityLow?: number;
  probabilityHigh?: number;
  confidence?: number;
  rationale?: string;
  evidenceFor: string[];
  evidenceAgainst: string[];
  assumptions: string[];
  invalidationConditions: string[];
  evidence: EvidenceView[];
}

function record(value: unknown): JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

function text(...values: unknown[]): string | undefined {
  return values.find(
    (value): value is string => typeof value === "string" && value.length > 0,
  );
}

function numberValue(value: unknown): number | undefined {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is string => typeof item === "string" && item.length > 0,
      )
    : [];
}

function safeSourceUrl(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}

function parseAnalysis(payload: unknown): AnalysisView[] {
  const root = record(payload);
  const items = Array.isArray(root.items) ? root.items : [];
  return items.map((value, index) => {
    const item = record(value);
    const rationale = record(item.rationale);
    const evidence = Array.isArray(item.evidence) ? item.evidence : [];
    return {
      id: text(item.id) ?? `analysis-${index}`,
      question: text(item.question) ?? "市场问题暂未同步",
      slug: text(item.slug),
      asOf: text(item.as_of),
      model: text(item.model),
      probabilityYes: numberValue(item.probability_yes),
      probabilityLow: numberValue(item.probability_low),
      probabilityHigh: numberValue(item.probability_high),
      confidence: numberValue(item.confidence),
      rationale: text(rationale.text, item.rationale),
      evidenceFor: strings(rationale.evidence_for),
      evidenceAgainst: strings(rationale.evidence_against),
      assumptions: strings(rationale.assumptions),
      invalidationConditions: strings(item.invalidation_conditions),
      evidence: evidence.map((source, sourceIndex) => {
        const row = record(source);
        return {
          id: text(row.id) ?? `${index}-${sourceIndex}`,
          title: text(row.source_title) ?? "未命名来源",
          summary: text(row.summary) ?? "无摘要",
          url: safeSourceUrl(row.source_url),
          publishedAt: text(row.published_at, row.fetched_at),
          reliability: numberValue(row.reliability_score),
        };
      }),
    };
  });
}

function percent(value?: number, digits = 0): string {
  if (value === undefined) return "—";
  return new Intl.NumberFormat("zh-CN", {
    style: "percent",
    maximumFractionDigits: digits,
  }).format(value);
}

function formatDate(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function BulletList({
  title,
  items,
  tone,
}: {
  title: string;
  items: string[];
  tone: "positive" | "negative" | "neutral";
}) {
  if (!items.length) return null;
  return (
    <div className={`analysis-list ${tone}`}>
      <strong>{title}</strong>
      <ul>
        {items.map((item, index) => (
          <li key={`${title}-${index}`}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

export default function AnalysisPage() {
  const [items, setItems] = useState<AnalysisView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await apiRequest<unknown>("/v1/me/analysis?limit=20");
      setItems(parseAnalysis(result.data));
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
          <p className="eyebrow">AI DECISION LOG</p>
          <h1>AI 分析记录</h1>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
        >
          {loading ? "读取中…" : "刷新分析"}
        </button>
      </header>

      <section className="safety-banner">
        <span className="shield" aria-hidden="true">◆</span>
        <div>
          <strong>这里展示概率判断，不是盈利承诺</strong>
          <p>
            重点看概率区间、反方证据和失效条件。系统只有在扣除费用后仍有足够优势并通过风险门槛时，才会产生交易候选。
          </p>
        </div>
      </section>

      {error && (
        <div className="notice error page-notice" role="alert">
          {error}
        </div>
      )}

      {loading ? (
        <section className="panel empty-state" aria-live="polite">
          <span aria-hidden="true">◆</span>
          <p>正在读取该账户最近的 AI 判断与证据。</p>
        </section>
      ) : items.length === 0 ? (
        <section className="panel empty-state">
          <span aria-hidden="true">◆</span>
          <p>还没有 AI 分析记录。</p>
          <p>先在控制台运行一次 Paper 周期；Worker 完成后，这里会显示概率与来源。</p>
        </section>
      ) : (
        <section className="analysis-feed" aria-label="最近 AI 分析">
          {items.map((item) => {
            const yes = item.probabilityYes;
            const no = yes === undefined ? undefined : 1 - yes;
            return (
              <article className="panel analysis-card" key={item.id}>
                <div className="analysis-card-heading">
                  <div>
                    <p className="eyebrow">
                      {item.model ?? "MODEL"} · {formatDate(item.asOf)}
                    </p>
                    <h2>{item.question}</h2>
                  </div>
                  {item.slug && (
                    <a
                      className="text-button"
                      href={`https://polymarket.com/event/${encodeURIComponent(item.slug)}`}
                      rel="noreferrer"
                      target="_blank"
                    >
                      查看市场
                    </a>
                  )}
                </div>

                <div className="probability-grid">
                  <div className="probability-value yes">
                    <span>YES 概率</span>
                    <strong>{percent(yes, 1)}</strong>
                  </div>
                  <div className="probability-value no">
                    <span>NO 概率</span>
                    <strong>{percent(no, 1)}</strong>
                  </div>
                  <div className="probability-value">
                    <span>不确定区间</span>
                    <strong>
                      {percent(item.probabilityLow)}–{percent(item.probabilityHigh)}
                    </strong>
                  </div>
                  <div className="probability-value">
                    <span>模型信心</span>
                    <strong>{percent(item.confidence)}</strong>
                  </div>
                </div>

                {item.rationale && <p className="analysis-rationale">{item.rationale}</p>}

                <div className="analysis-columns">
                  <BulletList
                    title="支持 YES 的证据"
                    items={item.evidenceFor}
                    tone="positive"
                  />
                  <BulletList
                    title="反方证据"
                    items={item.evidenceAgainst}
                    tone="negative"
                  />
                  <BulletList
                    title="关键假设"
                    items={item.assumptions}
                    tone="neutral"
                  />
                  <BulletList
                    title="判断失效条件"
                    items={item.invalidationConditions}
                    tone="negative"
                  />
                </div>

                <details className="source-disclosure">
                  <summary>查看独立来源（{item.evidence.length}）</summary>
                  {item.evidence.length ? (
                    <div className="source-list">
                      {item.evidence.map((source) => (
                        <article key={source.id}>
                          <div>
                            {source.url ? (
                              <a href={source.url} rel="noreferrer" target="_blank">
                                {source.title}
                              </a>
                            ) : (
                              <strong>{source.title}</strong>
                            )}
                            <span>
                              {formatDate(source.publishedAt)}
                              {source.reliability === undefined
                                ? ""
                                : ` · 可靠度 ${percent(source.reliability)}`}
                            </span>
                          </div>
                          <p>{source.summary}</p>
                        </article>
                      ))}
                    </div>
                  ) : (
                    <p className="panel-note">
                      该条旧记录没有可展示的来源；真实资金模式会因独立来源不足而拒绝交易。
                    </p>
                  )}
                </details>
              </article>
            );
          })}
        </section>
      )}
    </main>
  );
}
