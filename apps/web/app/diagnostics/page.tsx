"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import type { DeploymentCheckPayload } from "../../lib/deployment-diagnostics";
import { isSupabaseBrowserConfigured } from "../../lib/supabase/browser";

type CheckState = "checking" | "ok" | "error";

interface RemoteChecks {
  api: CheckState;
  cors: CheckState;
  database: CheckState;
  worker: CheckState;
  message?: string;
}

const CHECKING: RemoteChecks = {
  api: "checking",
  cors: "checking",
  database: "checking",
  worker: "checking",
};

function deploymentMessage(payload: DeploymentCheckPayload): string | undefined {
  if (!payload.configured) {
    return "Vercel 缺少有效的 NEXT_PUBLIC_API_BASE_URL。请填写 Zeabur API 的 HTTPS 域名并重新部署。";
  }
  if (!payload.api.reachable) {
    return `Vercel 无法访问 ${payload.apiBaseUrl ?? "Zeabur API"}。请检查 Zeabur 服务、域名和部署日志。`;
  }
  if (!payload.api.ok) {
    return `Zeabur 已响应，但 /livez 返回 HTTP ${payload.api.status ?? "未知"}，请检查 API 启动日志。`;
  }
  if (!payload.cors.ok) {
    const missing: string[] = [];
    if (!payload.cors.allowsOrigin) missing.push("当前 Vercel 域名");
    if (!payload.cors.allowsPut) missing.push("PUT 方法");
    if (!payload.cors.allowsAuthorization) missing.push("Authorization 请求头");
    if (!payload.cors.allowsContentType) missing.push("Content-Type 请求头");
    if (!payload.cors.allowsIdempotencyKey) missing.push("Idempotency-Key 请求头");
    return `Zeabur 在线，但 CORS 未放行：${missing.join("、")}。请在 Zeabur API 服务设置 POLYBOT_DASHBOARD_ORIGINS=${payload.dashboardOrigin}，确认部署最新 main 分支后重新部署。`;
  }
  if (!payload.database.ok) {
    return `Zeabur API 在线，但 /health 返回 HTTP ${payload.database.status ?? "未知"}。请检查 Supabase URL、service role 与迁移。`;
  }
  if (!payload.worker.ok) {
    return `API 与 Supabase 已连接，但 Worker 心跳未就绪（HTTP ${payload.worker.status ?? "不可达"}）。请检查 Zeabur Worker 日志和环境变量。`;
  }
  return undefined;
}

function CheckBadge({ state }: { state: CheckState }) {
  const label = { checking: "检查中", ok: "正常", error: "需配置" }[state];
  return <span className={`pill diagnostic-${state}`}>{label}</span>;
}

export default function DiagnosticsPage() {
  const supabaseReady = isSupabaseBrowserConfigured();
  const [remote, setRemote] = useState<RemoteChecks>(CHECKING);

  const runChecks = useCallback(async () => {
    setRemote(CHECKING);
    try {
      const response = await fetch("/api/deployment-check", {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as DeploymentCheckPayload;
      setRemote({
        api: payload.api.ok ? "ok" : "error",
        cors: payload.cors.ok ? "ok" : "error",
        database: payload.database.ok ? "ok" : "error",
        worker: payload.worker.ok ? "ok" : "error",
        message: deploymentMessage(payload),
      });
    } catch {
      setRemote({
        api: "error",
        cors: "error",
        database: "error",
        worker: "error",
        message:
          "Vercel 自检接口暂时不可用。请重新部署前端后重试，并查看 Vercel Function 日志。",
      });
    }
  }, []);

  useEffect(() => {
    void runChecks();
  }, [runChecks]);

  const allReady =
    supabaseReady &&
    remote.api === "ok" &&
    remote.cors === "ok" &&
    remote.database === "ok" &&
    remote.worker === "ok";

  return (
    <main className="auth-shell diagnostics-shell">
      <section className="panel diagnostics-panel">
        <div className="diagnostics-heading">
          <div>
            <p className="eyebrow">DEPLOYMENT DOCTOR</p>
            <h1>三端部署检查</h1>
            <p>只显示是否就绪，不读取或回显任何密钥。</p>
          </div>
          <button
            className="secondary-button"
            type="button"
            onClick={() => void runChecks()}
            disabled={remote.api === "checking"}
          >
            重新检查
          </button>
        </div>

        <div className="diagnostic-list">
          <article>
            <div>
              <strong>Vercel → Zeabur</strong>
              <p><code>NEXT_PUBLIC_API_BASE_URL</code> 必须是在线的 Zeabur API HTTPS 域名。</p>
            </div>
            <CheckBadge state={remote.api} />
          </article>
          <article>
            <div>
              <strong>Vercel → Supabase Auth</strong>
              <p>
                需要 <code>NEXT_PUBLIC_SUPABASE_URL</code> 和 publishable/anon 公钥，并在改动后重新部署。
              </p>
            </div>
            <CheckBadge state={supabaseReady ? "ok" : "error"} />
          </article>
          <article>
            <div>
              <strong>浏览器 → Zeabur 写请求</strong>
              <p>
                自动验证当前 Vercel 域名，以及 <code>PUT</code>、认证和幂等请求头是否被 CORS 放行。
              </p>
            </div>
            <CheckBadge state={remote.cors} />
          </article>
          <article>
            <div>
              <strong>Zeabur API → Supabase</strong>
              <p>API 的 <code>/health</code> 会验证持久化控制面是否可用。</p>
            </div>
            <CheckBadge state={remote.database} />
          </article>
          <article>
            <div>
              <strong>Zeabur 私有 Worker</strong>
              <p>
                API 通过数据库心跳确认 Worker 已完成迁移预检且持续消费队列；Worker 本身仍不绑定公网域名。
              </p>
            </div>
            <CheckBadge state={remote.worker} />
          </article>
        </div>

        {remote.message && (
          <div className="notice error" role="alert">{remote.message}</div>
        )}

        <div className={`deployment-result ${allReady ? "ready" : ""}`}>
          <strong>{allReady ? "公开连接已经就绪" : "还有配置没有完成"}</strong>
          <p>
            {allReady
              ? "现在可以登录，再用新手向导检查 MFA、AI、钱包和 Worker。"
              : "请在对应平台补齐变量，并确保 Vercel Preview 与 Production 使用了正确作用域。"}
          </p>
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
