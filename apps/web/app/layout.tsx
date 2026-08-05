import type { Metadata } from "next";
import type { ReactNode } from "react";

import LegacySecretCleanup from "../components/LegacySecretCleanup";
import Sidebar from "../components/Sidebar";
import "./globals.css";

export const metadata: Metadata = {
  title: "Polybot 控制台 | AI 量化投资与决策中心",
  description: "个人 Polymarket AI 自动交易机能中控智能核心",
  robots: { index: false, follow: false },
};

const themeInitScript = `
(function() {
  try {
    var stored = localStorage.getItem("polybot_theme");
    var valid = stored === "light" || stored === "dark" ? stored : null;
    var target = valid || (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
    document.documentElement.setAttribute("data-theme", target);
  } catch (e) {}
})();
`;

export default function RootLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInitScript }} />
      </head>
      <body>
        <LegacySecretCleanup />
        <div className="app-layout">
          <Sidebar />
          <div className="main-content">{children}</div>
        </div>
      </body>
    </html>
  );
}

