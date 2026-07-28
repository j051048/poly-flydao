"use client";

import Link from "next/link";
import { useState } from "react";

import {
  getSupabaseBrowserClient,
  isSupabaseBrowserConfigured,
} from "../../lib/supabase/browser";

type Message = { type: "success" | "error"; text: string };

export default function RegisterPage() {
  const configured = isSupabaseBrowserConfigured();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<Message | null>(
    configured
      ? null
      : {
          type: "error",
          text: "Supabase Auth 未配置。控制台仍可构建，但注册功能暂不可用。",
        },
  );

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const supabase = getSupabaseBrowserClient();
    if (!supabase) {
      setMessage({
        type: "error",
        text: "Supabase Auth 未配置，请联系部署管理员。",
      });
      return;
    }

    setLoading(true);
    setMessage(null);
    try {
      const { error } = await supabase.auth.signUp({
        email: email.trim(),
        password,
        options: {
          data: { username: username.trim() },
          emailRedirectTo: `${window.location.origin}/auth/callback`,
        },
      });
      if (error) throw error;
      setPassword("");
      setMessage({
        type: "success",
        text: "注册成功。请检查邮箱并完成验证，然后返回登录。",
      });
    } catch (error) {
      setMessage({
        type: "error",
        text: error instanceof Error ? error.message : "注册请求失败，请稍后重试。",
      });
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="auth-shell">
      <section className="panel auth-panel">
        <div className="auth-heading">
          <div className="brand-mark" aria-hidden="true">PM</div>
          <p className="eyebrow">CREATE ACCOUNT</p>
          <h1>注册 Polybot</h1>
          <p>每个账户拥有独立的钱包、AI 凭证和风控边界。</p>
        </div>

        <form className="form-stack" onSubmit={handleSubmit}>
          <label className="form-field" htmlFor="username">
            <span className="field-label">用户名</span>
            <input
              id="username"
              name="username"
              type="text"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
              required
            />
          </label>

          <label className="form-field" htmlFor="email">
            <span className="field-label">电子邮箱</span>
            <input
              id="email"
              name="email"
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
              name="password"
              type="password"
              minLength={8}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="new-password"
              required
            />
          </label>

          {message && (
            <div className={`notice ${message.type}`} role="status">
              {message.text}
            </div>
          )}

          <button
            className="primary-button full-width"
            type="submit"
            disabled={!configured || loading}
          >
            {loading ? "注册中…" : "创建账户"}
          </button>
        </form>

        <p className="auth-switch">
          已有账户？<Link href="/login">前往登录</Link>
        </p>
      </section>
    </main>
  );
}
