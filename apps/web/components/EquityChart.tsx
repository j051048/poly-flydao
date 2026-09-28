"use client";

import { useCallback, useEffect, useRef, useState } from "react";
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

import { EQUITY_SCOPE_LABELS, equityScopeForMode, parseEquityHistory, type ChartPoint, type EquityScope } from "../lib/equity";

function formatUsd(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

export default function EquityChart({ mode }: { mode?: string }) {
  const [points, setPoints] = useState<ChartPoint[]>([]);
  const [selection, setSelection] = useState<EquityScope | "current">("current");
  const [confirmedScope, setConfirmedScope] = useState<EquityScope | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const scope = selection === "current" ? equityScopeForMode(mode) : selection;

  const refresh = useCallback(async (generationId: number) => {
    try {
      const result = await apiRequest<unknown>(`/v1/me/equity-history?limit=200${scope ? `&scope=${scope}` : ""}`);
      if (generationId !== generation.current) return;
      const parsed = parseEquityHistory(result.data, scope);
      setPoints(parsed.points);
      setConfirmedScope(parsed.scope);
      setError(null);
    } catch (caught) {
      if (generationId !== generation.current) return;
      setError(readableApiError(caught));
    } finally {
      if (generationId === generation.current) setLoading(false);
    }
  }, [scope]);

  useEffect(() => {
    const generationId = ++generation.current;
    setPoints([]);
    setConfirmedScope(null);
    setLoading(true);
    void refresh(generationId);
    const timer = globalThis.setInterval(() => void refresh(generationId), 20_000);
    return () => { generation.current += 1; globalThis.clearInterval(timer); };
  }, [refresh]);

  const latest = points.at(-1)?.equityUsd;

  return (
    <section className="panel equity-chart-panel" aria-label="净值曲线">
      <div className="section-heading">
        <div>
          <p className="eyebrow">EQUITY CURVE</p>
          <h2>{confirmedScope ? EQUITY_SCOPE_LABELS[confirmedScope] : "账户净值曲线"}</h2>
        </div>
        {!loading && !error && latest !== undefined && (
          <span className="pill online">{formatUsd(latest)}</span>
        )}
      </div>
      <label className="form-field"><span className="field-label">权益账本</span>
        <select aria-label="权益账本" value={selection} onChange={(event) => {
          setPoints([]); setConfirmedScope(null); setLoading(true);
          setSelection(event.target.value as EquityScope | "current");
        }}>
          <option value="current">当前实际模式</option>
          {Object.entries(EQUITY_SCOPE_LABELS).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
        </select>
      </label>
      <p className="muted">模拟与实盘分别记账；权益变动也包含入金、出金，并不等同交易收益。</p>
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
