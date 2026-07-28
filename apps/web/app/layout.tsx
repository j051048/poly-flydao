import type { Metadata } from "next";
import type { ReactNode } from "react";

import LegacySecretCleanup from "../components/LegacySecretCleanup";
import Sidebar from "../components/Sidebar";
import "./globals.css";

export const metadata: Metadata = {
  title: "Polybot 控制台",
  description: "Polymarket AI 多租户交易机器人控制台",
  robots: { index: false, follow: false },
};

export default function RootLayout({
  children,
}: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="zh-CN">
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
