"use client";

import { useEffect, useState } from "react";

type Theme = "dark" | "light";

export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("dark");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
    const stored = localStorage.getItem("polybot_theme");
    const initial: Theme =
      stored === "light" || stored === "dark"
        ? stored
        : window.matchMedia("(prefers-color-scheme: light)").matches
          ? "light"
          : "dark";
    setTheme(initial);
    document.documentElement.setAttribute("data-theme", initial);
  }, []);

  if (!mounted) {
    return (
      <div className="theme-toggle-skeleton" aria-hidden="true" />
    );
  }

  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("polybot_theme", next);
  };

  return (
    <button
      type="button"
      className={`theme-toggle-btn ${theme}`}
      onClick={toggleTheme}
      title={theme === "dark" ? "切换至明夜清风 (Sunlit Daylight)" : "切换至夜曜黑域 (Cyber Nebula Dark)"}
      aria-label="切换昼夜显示主题"
    >
      <span className="toggle-track">
        <span className="toggle-indicator">
          {theme === "dark" ? (
            <span className="icon-night" role="img" aria-label="暗夜太空">🌙</span>
          ) : (
            <span className="icon-day" role="img" aria-label="阳光白昼">☀</span>
          )}
        </span>
        <span className="toggle-label-text">
          {theme === "dark" ? "深渊极曜" : "日晶白柔"}
        </span>
      </span>
    </button>
  );
}
