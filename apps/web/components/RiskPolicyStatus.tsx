import { asNumber, asRecord, asString, formatRisk, RISK_LIMITS } from "../lib/dashboard";

export default function RiskPolicyStatus({ payload }: { payload: unknown }) {
  const root = asRecord(payload);
  const effective = asRecord(root.risk_limits);
  const requested = asRecord(root.desired_risk_policy);
  const ceiling = asRecord(root.deployment_risk_limits);
  const applied = effective.applied === true && root.risk_policy_pending === false;
  return <div aria-label="风控生效状态">
    <p role="status" className={applied ? "success-text" : "muted"}>
      {applied ? "Worker 已确认当前风控" : "等待 Worker 确认风控，保存不代表已经生效"}
      {asNumber(effective.version) !== undefined && ` · 实际策略 v${String(effective.version)}`}
      {asNumber(requested.version) !== undefined && ` · 请求策略 v${String(requested.version)}`}
    </p>
    <p className="field-help">部署限制是硬边界：金额及风险比例取更严格的上限，最小净优势取更严格的下限。</p>
    <div className="table-scroll"><table className="positions-table">
      <thead><tr><th>参数</th><th>已请求</th><th>Worker 实际值</th><th>部署边界</th></tr></thead>
      <tbody>{RISK_LIMITS.map((limit) => <tr key={limit.key}>
        <td>{limit.label}</td><td>{formatRisk(requested[limit.key], limit.format)}</td>
        <td>{formatRisk(effective[limit.key], limit.format)}</td>
        <td>{formatRisk(ceiling[limit.key], limit.format)}</td>
      </tr>)}</tbody>
    </table></div>
    {asString(asRecord(root.personal).configuration_error) && <p className="notice error">{String(asRecord(root.personal).configuration_error)}</p>}
  </div>;
}
