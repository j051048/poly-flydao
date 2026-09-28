import type { RiskLimitView } from "../lib/dashboard";

/** Server-side risk limits. The console never edits them; it only shows them. */
export default function RiskLimitsPanel({ limits, confirmed = false }: { limits: RiskLimitView[]; confirmed?: boolean }) {
  return (
    <section className="panel">
      <div className="section-heading compact">
        <div>
          <p className="eyebrow">RISK LIMITS</p>
          <h2>风险上限</h2>
        </div>
        <span className="version-label">{confirmed ? "Worker 实际值" : "待确认"}</span>
      </div>
      {!confirmed && <p className="muted">以下为最后读取的值；状态更新前无法确认仍然生效。</p>}
      <dl className="risk-list">
        {limits.map((limit) => (
          <div className="risk-row" key={limit.key}>
            <dt>{limit.label}</dt>
            <dd>{limit.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
