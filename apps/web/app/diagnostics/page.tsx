"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import {
  API_BASE_URL,
  apiRequest,
  readableApiError,
} from "../../lib/api";
import { isSupabaseBrowserConfigured } from "../../lib/supabase/browser";

type CheckState = "checking" | "ok" | "error";

interface RemoteChecks {
  api: CheckState;
  database: CheckState;
  worker: CheckState;
  message?: string;
}

function CheckBadge({ state }: { state: CheckState }) {
  const label = { checking: "检查中", ok: "正常", error: "需配置" }[state];
  return <span className={`pill diagnostic-${state}`}>{label}</span>;
}

export default function DiagnosticsPage() {
  const supabaseReady = isSupabaseBrowserConfigured();
  const apiConfigured = Boolean(API_BASE_URL);
  const [remote, setRemote] = useState<RemoteChecks>({
    api: "checking",
    database: "checking",
    worker: "checking",
  });

  const runChecks = useCallback(async () => {
    if (!apiConfigured) {
      setRemote({
        api: "error",
        database: "error",
        worker: "error",
        message: "NEXT_PUBLIC_API_BASE_URL 缺失或不是安全的 HTTPS origin。",
      });
      return;
    }
    setRemote({ api: "checking", database: "checking", worker: "checking" });
    const [live, health, worker] = await Promise.allSettled([
      apiRequest<unknown>("/livez", { authenticated: false }),
      apiRequest<{ ok?: boolean }>("/health", { authenticated: false }),
      apiRequest<{ ok?: boolean }>("/worker-health", { authenticated: false }),
    ]);
    const apiOk = live.status === "fulfilled";
    const databaseOk =
      health.status === "fulfilled" && health.value.data?.ok === true;
    const workerOk =
      worker.status === "fulfilled" && worker.value.data?.ok === true;
    const failure =
      live.status === "rejected"
        ? live.reason
        : health.status === "rejected"
          ? health.reason
          : worker.status === "rejected"
            ? worker.reason
            : null;
    setRemote({
      api: apiOk ? "ok" : "error",
      database: databaseOk ? "ok" : "error",
      worker: workerOk ? "ok" : "error",
      message: failure ? readableApiError(failure) : undefined,
    });
  }, [apiConfigured]);

  useEffect(() => {
    void runChecks();
  }, [runChecks]);

  const allReady =
    supabaseReady &&
    apiConfigured &&
    remote.api === "ok" &&
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
              <p><code>NEXT_PUBLIC_API_BASE_URL</code> 必须是 Zeabur API 的 HTTPS 域名。</p>
            </div>
            <CheckBadge state={apiConfigured ? remote.api : "error"} />
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
