import Link from "next/link";

export default function RegisterPage() {
  return (
    <main className="auth-shell">
      <section className="panel auth-panel">
        <div className="auth-heading">
          <div className="brand-mark" aria-hidden="true">PM</div>
          <p className="eyebrow">PERSONAL OWNER</p>
          <h1>个人版不开放注册</h1>
          <p>
            这个部署只服务一个 Owner。网页不会创建额外账户，也不会接收任何
            AI Key 或钱包私钥。
          </p>
        </div>

        <div className="notice info" role="status">
          请在 Supabase Dashboard 的 Authentication → Users 中创建或确认唯一
          Owner，然后把该用户 UUID 填入 Zeabur 的
          <code> POLYBOT_ACCOUNT_ID</code>。完成后关闭 Supabase 公开注册。
        </div>

        <div className="button-row">
          <Link className="primary-button" href="/login">返回登录</Link>
          <a
            className="secondary-button"
            href="https://github.com/j051048/poly-flydao/blob/main/docs/DEPLOYMENT.md"
            rel="noreferrer"
            target="_blank"
          >
            查看部署手册
          </a>
        </div>
      </section>
    </main>
  );
}
