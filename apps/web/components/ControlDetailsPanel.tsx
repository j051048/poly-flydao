import { type ControlView, formatDate, modeLabel } from "../lib/dashboard";

/** Raw runtime-control record, for operators who need the exact values. */
export default function ControlDetailsPanel({ control }: { control?: ControlView }) {
  return (
    <section className="panel control-details">
      <div className="section-heading compact">
        <div>
          <p className="eyebrow">RUNTIME CONTROL</p>
          <h2>控制详情</h2>
        </div>
      </div>
      <dl className="detail-list">
        <div>
          <dt>账户</dt>
          <dd title={control?.accountId}>{control?.accountId ?? "来自 JWT"}</dd>
        </div>
        <div>
          <dt>控制模式</dt>
          <dd>{modeLabel(control?.mode)}</dd>
        </div>
        <div>
          <dt>解锁到期</dt>
          <dd>{formatDate(control?.armedUntil)}</dd>
        </div>
        <div>
          <dt>接受新意图</dt>
          <dd>{control?.acceptNewIntents === true ? "是" : "否或未知"}</dd>
        </div>
        <div>
          <dt>控制版本</dt>
          <dd>{control?.version ?? "—"}</dd>
        </div>
        <div>
          <dt>更新时间</dt>
          <dd>{formatDate(control?.updatedAt)}</dd>
        </div>
      </dl>
    </section>
  );
}
