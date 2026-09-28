import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import SetupPage from "./page";

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("../../lib/api", () => ({ apiRequest: request, ApiError: class extends Error {}, readableApiError: () => "请求失败" }));
afterEach(() => { cleanup(); vi.useRealTimers(); request.mockReset(); });

it("waits for the returned personal request rather than querying tenant jobs or an unrelated cycle", async () => {
  vi.useFakeTimers();
  request.mockImplementation(async (path: string) => {
    if (path === "/health") return { status: 200, data: { ok: true } };
    if (path === "/v1/personal/cycles/run") return { status: 202, data: { id: "personal-request-1", state: "queued" } };
    if (path === "/v1/personal/cycles/personal-request-1") return { status: 200, data: {
      id: "personal-request-1", state: "succeeded", mode: "paper", result_summary: { markets_scanned: 4, executions: 0 },
    } };
    return { status: 200, data: path === "/v1/personal/status" ? {
      enabled: true, mode: "paper", worker_ready: true, ai: { configured: true }, cycle_count: 0,
    } : {} };
  });
  await act(async () => { render(<SetupPage />); });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "运行一次安全模拟" })); });
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(request.mock.calls.some(([path]) => path === "/v1/personal/cycles/personal-request-1")).toBe(true);
  expect(request.mock.calls.some(([path]) => path.startsWith("/v1/jobs/"))).toBe(false);
  expect(screen.getByText(/首次 Paper 周期完成/)).toHaveTextContent("扫描 4 个市场");
});
