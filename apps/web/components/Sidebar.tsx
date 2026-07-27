"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export default function Sidebar() {
  const pathname = usePathname();

  const navItems = [
    { name: "控制台", path: "/" },
    { name: "个人资产", path: "/assets" },
    { name: "系统配置", path: "/settings" },
    { name: "注册账号", path: "/register" },
  ];

  return (
    <aside className="global-sidebar">
      <div className="sidebar-brand">
        <div className="brand-mark" aria-hidden="true">PM</div>
        <div className="brand-text">
          <p className="eyebrow">POLYMARKET</p>
          <h1>Polybot</h1>
        </div>
      </div>
      <nav className="sidebar-nav">
        {navItems.map((item) => {
          const isActive = pathname === item.path;
          return (
            <Link
              key={item.path}
              href={item.path}
              className={`nav-link ${isActive ? "active" : ""}`}
            >
              {item.name}
            </Link>
          );
        })}
      </nav>
      <div className="sidebar-footer">
        <p className="muted" style={{ fontSize: "12px", textAlign: "center" }}>
          Poly-Flydao v1.0
        </p>
      </div>
    </aside>
  );
}
