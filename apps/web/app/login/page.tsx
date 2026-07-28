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
      ? searchParams.get("error")
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
        <p>登录会话决定后端租户身份，不再使用共享管理员令牌。</p>
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
          <div className="notice error" role="alert">{errorMessage}</div>
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
        还没有账户？<Link href="/register">立即注册</Link>
      </p>
    </section>
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
