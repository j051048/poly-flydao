import type { Metadata } from "next";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "Polybot 控制台",
  description: "Polymarket AI 交易机器人的只读状态与人工控制台",
  robots: {
    index: false,
    follow: false,
  },
};

import Sidebar from "../components/Sidebar";

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <div className="app-layout">
          <Sidebar />
          <div className="main-content">
            {children}
          </div>
        </div>
      </body>
    </html>
  );
}
