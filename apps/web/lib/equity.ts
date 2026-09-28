import { asNumber, asRecord, asString } from "./dashboard";

export type EquityScope = "paper" | "shadow" | "real";
export const EQUITY_SCOPE_LABELS: Record<EquityScope, string> = {
  paper: "Paper 模拟账本", shadow: "Shadow 观察账本", real: "实盘账本（Canary / Live）",
};
export interface ChartPoint { label: string; equityUsd: number; recordedAt: number }

export function equityScopeForMode(mode?: string): EquityScope | undefined {
  if (mode === "paper" || mode === "shadow") return mode;
  return mode === "canary" || mode === "live" ? "real" : undefined;
}

export function parseEquityHistory(payload: unknown, expectedScope?: EquityScope): {
  scope: EquityScope; points: ChartPoint[];
} {
  const root = asRecord(payload);
  const scope = root.scope;
  if (!["paper", "shadow", "real"].includes(String(scope)) || (expectedScope && scope !== expectedScope)) {
    throw new Error("权益账本口径未确认，请刷新后重试。");
  }
  const points = (Array.isArray(root.items) ? root.items : []).flatMap((raw) => {
    const row = asRecord(raw);
    const source = asString(row.source)?.replace(/_cycle$/, "");
    const rowScope = source === "real" ? "real" : equityScopeForMode(source);
    // Unknown or mixed legacy source rows cannot establish the same capital ledger.
    if (rowScope !== scope) return [];
    const equityUsd = asNumber(row.equity_usd);
    const recordedAt = Date.parse(asString(row.recorded_at) ?? "");
    if (equityUsd === undefined || equityUsd < 0 || !Number.isFinite(recordedAt)) return [];
    return [{
      recordedAt, equityUsd,
      label: new Date(recordedAt).toLocaleString("zh-CN", {
        month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      }),
    }];
  }).sort((a, b) => a.recordedAt - b.recordedAt);
  return { scope: scope as EquityScope, points };
}
