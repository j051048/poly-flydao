"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { safeInternalPath } from "../../lib/navigation";
import {
  getSupabaseBrowserClient,
  isSupabaseBrowserConfigured,
} from "../../lib/supabase/browser";

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const configured = isSupabaseBrowserConfigured();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(
    configured
      ? loginErrorMessage(searchParams.get("error"))
      : "Supabase Auth 未配置。请在 Vercel 配置公开的项目 URL 和 publishable/anon key。",
  );

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const supabase = getSupabaseBrowserClient();
    if (!supabase) return;

    setLoading(true);
    setErrorMessage(null);
    try {
      const { error } = await supabase.auth.signInWithPassword({
        email: email.trim(),
        password,
      });
      if (error) throw error;
      setPassword("");
      router.replace(safeInternalPath(searchParams.get("next")));
      router.refresh();
    } catch (error) {
      setErrorMessage(
        error instanceof Error ? error.message : "登录失败，请稍后重试。",
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="panel auth-panel">
      <div className="auth-heading">
        <div className="brand-mark" aria-hidden="true">PM</div>
        <p className="eyebrow">SECURE SESSION</p>
        <h1>登录控制台</h1>
        <p>登录会话只允许你本人操作个人机器人，不使用共享管理员令牌。</p>
      </div>

      <form className="form-stack" onSubmit={handleSubmit}>
        <label className="form-field" htmlFor="email">
          <span className="field-label">电子邮箱</span>
          <input
            id="email"
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            autoComplete="email"
            required
          />
        </label>
        <label className="form-field" htmlFor="password">
          <span className="field-label">密码</span>
          <input
            id="password"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
          />
        </label>

        {errorMessage && (
          <div className="notice error" role="alert">
            {errorMessage}
            {!configured && (
              <>
                {" "}
                <Link href="/diagnostics">打开三端部署检查</Link>
              </>
            )}
          </div>
        )}

        <button
          className="primary-button full-width"
          type="submit"
          disabled={!configured || loading}
        >
          {loading ? "登录中…" : "登录"}
        </button>
      </form>

      <p className="auth-switch">
        个人版不开放网页注册。Owner 账户请在 Supabase Dashboard 中创建。
      </p>
    </section>
  );
}

function loginErrorMessage(value: string | null): string | null {
  if (!value) return null;
  return (
    {
      auth_not_configured: "Supabase Auth 尚未配置，请先完成部署检查。",
      invalid_callback: "登录回调无效或已过期，请重新登录。",
      auth_callback_failed: "登录回调失败，请检查 Supabase Redirect URL。",
    }[value] ?? "登录会话无效，请重新登录。"
  );
}

export default function LoginPage() {
  return (
    <main className="auth-shell">
      <Suspense fallback={<div className="panel auth-panel">正在加载登录页…</div>}>
        <LoginForm />
      </Suspense>
    </main>
  );
}
