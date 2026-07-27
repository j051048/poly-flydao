"use client";

export default function AssetsPage() {
  return (
    <main className="page-shell">
      <header className="topbar">
        <div>
          <h1 style={{ fontSize: "24px", margin: 0 }}>个人资产</h1>
        </div>
      </header>

      <section className="summary-grid" style={{ marginBottom: "24px" }}>
        <article className="summary-card">
          <div className="card-heading">
            <span>总资产 (USD)</span>
            <span className="pill online">安全</span>
          </div>
          <div className="primary-value">$ 0.00</div>
          <p className="muted">钱包连接后实时更新</p>
        </article>

        <article className="summary-card">
          <div className="card-heading">
            <span>当前敞口风险</span>
            <span className="mode-chip mode-safe">SAFE</span>
          </div>
          <div className="primary-value">0%</div>
          <p className="muted">投资组合的风险暴露度</p>
        </article>

        <article className="summary-card">
          <div className="card-heading">
            <span>活跃订单</span>
          </div>
          <div className="primary-value">0</div>
          <p className="muted">正在处理或已挂单</p>
        </article>
      </section>

      <section className="panel">
        <div className="section-heading">
          <div>
            <p className="eyebrow">PORTFOLIO</p>
            <h2>仓位详情</h2>
          </div>
        </div>
        
        <div className="empty-state" style={{ padding: "60px 0" }}>
          <span aria-hidden="true" style={{ fontSize: "32px", marginBottom: "16px", display: "inline-block", color: "var(--muted)" }}>◎</span>
          <p style={{ margin: 0, color: "var(--muted)" }}>暂未检测到任何资产或仓位。</p>
          <p style={{ margin: "8px 0 0", fontSize: "14px", color: "var(--muted)", opacity: 0.7 }}>
            请前往“系统配置”绑定您的 EVM 钱包。
          </p>
        </div>
      </section>
    </main>
  );
}
