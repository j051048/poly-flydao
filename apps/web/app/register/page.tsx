"use client";

import { useState } from "react";
import Link from "next/link";

export default function RegisterPage() {
  const [formData, setFormData] = useState({
    username: "",
    email: "",
    password: "",
  });

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const { name, value } = e.target;
    setFormData((prev) => ({ ...prev, [name]: value }));
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    alert("注册功能暂未开放。这是一个前端演示页面。");
  };

  return (
    <main className="page-shell" style={{ display: "flex", justifyContent: "center", alignItems: "center", minHeight: "calc(100vh - 62px)" }}>
      <section className="panel controls-panel" style={{ width: "100%", maxWidth: "480px" }}>
        <div className="section-heading" style={{ justifyContent: "center", marginBottom: "32px" }}>
          <div style={{ textAlign: "center" }}>
            <div className="brand-mark" aria-hidden="true" style={{ width: "48px", height: "48px", fontSize: "20px", margin: "0 auto 16px" }}>PM</div>
            <p className="eyebrow">JOIN US</p>
            <h2>注册 Polybot</h2>
          </div>
        </div>

        <form onSubmit={handleSubmit} style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
          <div>
            <label className="field-label" htmlFor="username">用户名</label>
            <div className="token-row">
              <input
                id="username"
                name="username"
                type="text"
                value={formData.username}
                onChange={handleChange}
                placeholder="请输入您的用户名"
                required
              />
            </div>
          </div>

          <div>
            <label className="field-label" htmlFor="email">电子邮箱</label>
            <div className="token-row">
              <input
                id="email"
                name="email"
                type="email"
                value={formData.email}
                onChange={handleChange}
                placeholder="name@example.com"
                required
              />
            </div>
          </div>

          <div>
            <label className="field-label" htmlFor="password">密码</label>
            <div className="token-row">
              <input
                id="password"
                name="password"
                type="password"
                value={formData.password}
                onChange={handleChange}
                placeholder="创建至少 8 位密码"
                required
              />
            </div>
          </div>

          <div style={{ marginTop: "12px" }}>
            <button
              className="primary-button"
              type="submit"
              style={{ width: "100%", justifyContent: "center", padding: "14px" }}
            >
              创建账号
            </button>
          </div>
        </form>

        <p className="muted" style={{ textAlign: "center", marginTop: "24px", fontSize: "14px" }}>
          已有账号？前往 <Link href="/" style={{ color: "var(--green)", textDecoration: "none" }}>控制台</Link> 登录。
        </p>
      </section>
    </main>
  );
}
