import type { RiskLimitView } from "../lib/dashboard";

/** Server-side risk limits. The console never edits them; it only shows them. */
export default function RiskLimitsPanel({ limits }: { limits: RiskLimitView[] }) {
  return (
    <section className="panel">
      <div className="section-heading compact">
        <div>
          <p className="eyebrow">RISK LIMITS</p>
          <h2>风险上限</h2>
        </div>
        <span className="version-label">服务端值</span>
      </div>
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
