"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { apiRequest, readableApiError } from "../lib/api";

interface EquityPoint {
  recordedAt: string;
  equityUsd: number;
  source: string;
}

interface ChartPoint {
  label: string;
  equityUsd: number;
}

function formatUsd(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

function toChartPoint(row: unknown): ChartPoint | null {
  if (typeof row !== "object" || row === null) return null;
  const record = row as Record<string, unknown>;
  const equityUsd = Number(record.equity_usd ?? record.equityUsd);
  const recordedAt = String(record.recorded_at ?? record.recordedAt ?? "");
  if (!Number.isFinite(equityUsd) || !recordedAt) return null;
  const when = new Date(recordedAt);
  if (Number.isNaN(when.getTime())) return null;
  return {
    label: when.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }),
    equityUsd,
  };
}

export default function EquityChart() {
  const [points, setPoints] = useState<ChartPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const result = await apiRequest<unknown>("/v1/me/equity-history?limit=200");
      const rows = (result.data as { items?: unknown[] })?.items ?? [];
      const mapped = rows
        .map(toChartPoint)
        .filter((point): point is ChartPoint => point !== null);
      setPoints(mapped);
      setError(null);
    } catch (caught) {
      setError(readableApiError(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = globalThis.setInterval(() => void refresh(), 20_000);
    return () => globalThis.clearInterval(timer);
  }, [refresh]);

  const latest = points.at(-1)?.equityUsd;

  return (
    <section className="panel equity-chart-panel" aria-label="净值曲线">
      <div className="section-heading">
        <div>
          <p className="eyebrow">EQUITY CURVE</p>
          <h2>账户净值曲线</h2>
        </div>
        {latest !== undefined && (
          <span className="pill online">{formatUsd(latest)}</span>
        )}
      </div>
      {loading ? (
        <div className="empty-state">
          <span>…</span>
          <p>加载权益历史…</p>
        </div>
      ) : error ? (
        <div className="empty-state">
          <span>!</span>
          <p>{error}</p>
        </div>
      ) : points.length < 2 ? (
        <div className="empty-state">
          <span>◌</span>
          <p>
            至少运行两个交易周期后显示净值曲线。归档与权益记录会在每个周期自动写入。
          </p>
        </div>
      ) : (
        <div className="equity-chart-wrap">
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={points} margin={{ top: 8, right: 8, left: 8, bottom: 0 }}>
              <defs>
                <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#53f2a7" stopOpacity={0.32} />
                  <stop offset="100%" stopColor="#53f2a7" stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <CartesianGrid
                stroke="rgba(178, 255, 218, 0.08)"
                strokeDasharray="3 3"
                vertical={false}
              />
              <XAxis
                dataKey="label"
                tick={{ fill: "#8fa79c", fontSize: 10 }}
                tickLine={false}
                axisLine={{ stroke: "rgba(178, 255, 218, 0.12)" }}
                minTickGap={40}
              />
              <YAxis
                tick={{ fill: "#8fa79c", fontSize: 10 }}
                tickLine={false}
                axisLine={false}
                domain={["auto", "auto"]}
                width={54}
                tickFormatter={(value: number) => formatUsd(value)}
              />
              <Tooltip
                contentStyle={{
                  background: "rgba(15, 31, 27, 0.96)",
                  border: "1px solid rgba(178, 255, 218, 0.24)",
                  borderRadius: 12,
                  color: "#f1f8f4",
                  fontSize: 12,
                }}
                labelStyle={{ color: "#8fa79c" }}
                formatter={(value) => [formatUsd(Number(value)), "净值"]}
              />
              <Area
                type="monotone"
                dataKey="equityUsd"
                stroke="#53f2a7"
                strokeWidth={2}
                fill="url(#equityFill)"
                isAnimationActive={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </section>
  );
}
