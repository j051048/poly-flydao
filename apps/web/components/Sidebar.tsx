"use client";

import type { User } from "@supabase/supabase-js";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { getSupabaseBrowserClient } from "../lib/supabase/browser";
import ThemeToggle from "./ThemeToggle";

const NAV_ITEMS = [
  { name: "新手向导", path: "/setup" },
  { name: "控制台", path: "/" },
  { name: "AI 分析", path: "/analysis" },
  { name: "效果校准", path: "/performance" },
  { name: "个人资产", path: "/assets" },
  { name: "个人设置", path: "/settings" },
];

export default function Sidebar() {
  const pathname = usePathname();
  const [user, setUser] = useState<User | null>(null);
  const isAuthPage =
    pathname === "/login" ||
    pathname === "/register" ||
    pathname === "/diagnostics" ||
    pathname.startsWith("/auth/");

  useEffect(() => {
    const supabase = getSupabaseBrowserClient();
    if (!supabase) return;

    let mounted = true;
    void supabase.auth.getSession().then(({ data }) => {
      if (mounted) setUser(data.session?.user ?? null);
    });
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, session) => {
      if (mounted) setUser(session?.user ?? null);
    });

    return () => {
      mounted = false;
      subscription.unsubscribe();
    };
  }, []);

  if (isAuthPage) return null;

  return (
    <aside className="global-sidebar">
      <div className="sidebar-brand">
        <div className="brand-mark" aria-hidden="true">
          <span className="brand-dot" />
          PM
        </div>
        <div className="brand-text">
          <p className="eyebrow">POLYMARKET AI</p>
          <h1>Polybot</h1>
        </div>
      </div>

      <div className="sidebar-theme-wrapper">
        <ThemeToggle />
      </div>

      <nav className="sidebar-nav" aria-label="主导航">
        {NAV_ITEMS.map((item) => (
          <Link
            key={item.path}
            href={item.path}
            className={`nav-link ${pathname === item.path ? "active" : ""}`}
          >
            <span className="nav-item-indicator" />
            {item.name}
          </Link>
        ))}
      </nav>

      <div className="sidebar-footer">
        {user ? (
          <div className="user-profile-widget">
            <p className="session-email" title={user.email}>
              <span className="live-pulse-dot" />
              {user.email}
            </p>
            <form action="/auth/logout" method="post" className="logout-form">
              <button className="text-button logout-btn" type="submit">退出账号</button>
            </form>
          </div>
        ) : (
          <Link className="nav-link login-link-btn" href="/login">登录主核</Link>
        )}
        <div className="sidebar-copyright">
          <p className="muted">Poly-Flydao personal beta</p>
        </div>
      </div>
    </aside>
  );
}

