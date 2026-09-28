import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import EquityChart from "./EquityChart";

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("../lib/api", () => ({ apiRequest: request, readableApiError: () => "请求失败" }));
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  AreaChart: ({ data }: { data: unknown }) => <div data-testid="series">{JSON.stringify(data)}</div>,
  Area: () => null, CartesianGrid: () => null, Tooltip: () => null, XAxis: () => null, YAxis: () => null,
}));
afterEach(() => { cleanup(); request.mockReset(); });

it("clears the old ledger immediately and rejects a late response after switching", async () => {
  let resolvePaper!: (value: unknown) => void;
  request.mockImplementation((path: string) => path.includes("scope=paper")
    ? new Promise((resolve) => { resolvePaper = resolve; })
    : Promise.resolve({ status: 200, data: { scope: "real", items: [
      { source: "canary_cycle", equity_usd: "20", recorded_at: "2026-09-28T02:00:00Z" },
      { source: "live_cycle", equity_usd: "22", recorded_at: "2026-09-28T03:00:00Z" },
    ] } }));
  await act(async () => { render(<EquityChart mode="paper" />); });
  await act(async () => { fireEvent.change(screen.getByLabelText("权益账本"), { target: { value: "real" } }); });
  expect(screen.getByTestId("series")).toHaveTextContent('"equityUsd":20');
  await act(async () => { resolvePaper({ status: 200, data: { scope: "paper", items: [
    { source: "paper_cycle", equity_usd: "1000", recorded_at: "2026-09-28T01:00:00Z" },
    { source: "paper_cycle", equity_usd: "1001", recorded_at: "2026-09-28T02:00:00Z" },
  ] } }); });
  expect(screen.getByTestId("series")).not.toHaveTextContent('"equityUsd":1000');
  expect(screen.getByRole("heading", { name: "实盘账本（Canary / Live）" })).toBeInTheDocument();
});
