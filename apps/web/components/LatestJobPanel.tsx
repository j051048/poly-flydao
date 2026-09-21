import {
  type JobView,
  modeLabel,
  skipReasonLabel,
} from "../lib/dashboard";

/** Latest cycle result, including why markets were not traded. */
export default function LatestJobPanel({ job }: { job: JobView | null }) {
  return (
    <section className="panel">
      <div className="section-heading compact">
        <div>
          <p className="eyebrow">LATEST JOB</p>
          <h2>最近任务</h2>
        </div>
        {job?.status && <span className="pill">{job.status}</span>}
      </div>
      {job ? (
        <dl className="detail-list">
          <div>
            <dt>任务 ID</dt>
            <dd title={job.id}>{job.id ?? "—"}</dd>
          </div>
          <div>
            <dt>模式</dt>
            <dd>{modeLabel(job.mode)}</dd>
          </div>
          <div>
            <dt>扫描市场</dt>
            <dd>{job.marketsScanned ?? "等待 worker"}</dd>
          </div>
          <div>
            <dt>AI 预测</dt>
            <dd>{job.forecastsCreated ?? "等待 worker"}</dd>
          </div>
          <div>
            <dt>候选机会</dt>
            <dd>{job.candidatesCreated ?? "等待 worker"}</dd>
          </div>
          <div>
            <dt>执行订单</dt>
            <dd>{job.executions ?? "等待 worker"}</dd>
          </div>
          {job.skipped && job.skipped.length > 0 && (
            <div className="skip-reasons">
              <dt>主要未交易原因</dt>
              <dd>
                {job.skipped.slice(0, 3).map((item) => (
                  <span key={item.code}>
                    {skipReasonLabel(item.code)} × {item.count}
                  </span>
                ))}
              </dd>
            </div>
          )}
        </dl>
      ) : (
        <div className="empty-state">
          <span aria-hidden="true">◎</span>
          <p>暂无运行任务。</p>
        </div>
      )}
    </section>
  );
}
