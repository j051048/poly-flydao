import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import HomePage from "./page";

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("../lib/api", () => ({ API_BASE_URL: "https://api.example.test", ApiError: class extends Error {}, apiRequest: request,
  readableApiError: () => "网络不可用" }));
vi.mock("next/dynamic", () => ({ default: () => () => null }));

function snapshot(paused = false) {
  return { personal: { enabled: true, mode: "live", desired_mode: "live", mode_applied: true,
    live_supported: true, wallet: { paused }, cycle_count: 1 },
    control: { armed: !paused, kill_switch: paused, accept_new_intents: !paused, mode: "live", version: 10,
      armed_until: new Date(Date.now() + 60_000).toISOString() } };
}
function deferred() {
  let resolve!: (value: unknown) => void;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
beforeEach(() => { vi.useFakeTimers(); vi.spyOn(window, "confirm").mockReturnValue(true); });
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); request.mockReset(); });

describe("safety controls", () => {
  it("keeps the stop available when state cannot be read and makes no safety promise", async () => {
    request.mockRejectedValue(new Error("offline"));
    await act(async () => { render(<HomePage />); });
    expect(screen.getByText("状态未确认")).toBeInTheDocument();
    expect(screen.getByText("无法确认后台是否仍在交易")).toBeInTheDocument();
    expect(screen.queryByText("不会提交新实盘订单")).toBeNull();
    expect(screen.getByRole("button", { name: "停用并撤单" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "运行周期" })).toBeDisabled();
  });

  it("marks last known live state stale after a failed refresh", async () => {
    let offline = false;
    request.mockImplementation(async (path: string) => {
      if (offline) throw new Error("offline");
      return { status: 200, data: path === "/v1/status" ? snapshot() : { ok: true, items: [] } };
    });
    await act(async () => { render(<HomePage />); });
    expect(screen.getAllByText("授权有效").length).toBeGreaterThan(0);
    offline = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(screen.getByText("状态已过期")).toBeInTheDocument();
    expect(screen.queryByText("已确认锁定")).toBeNull();
  });

  it("dispatches stop while a cycle POST is still outstanding and ignores its late notice", async () => {
    const cycle = deferred();
    let paused = false;
    request.mockImplementation(async (path: string) => {
      if (path === "/v1/personal/cycles/run") return cycle.promise;
      if (path === "/v1/control/disarm") { paused = true; return { status: 202, data: { cancellation_pending: true } }; }
      if (path.startsWith("/v1/personal/cycles/")) return { status: 200, data: { id: "request-1", state: "queued" } };
      return { status: 200, data: path === "/v1/status" ? snapshot(paused) : { ok: true, items: [] } };
    });
    await act(async () => { render(<HomePage />); });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "运行周期" })); });
    expect(screen.getByRole("button", { name: "停用并撤单" })).toBeEnabled();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "停用并撤单" })); });
    expect(request.mock.calls.some(([path]) => path === "/v1/control/disarm")).toBe(true);
    await act(async () => { cycle.resolve({ status: 202, data: { id: "request-1", mode: "live", state: "queued" } }); });
    expect(screen.queryByText(/周期已交给个人 Worker/)).toBeNull();
    expect(screen.getAllByText(/停用已受理/).length).toBeGreaterThan(0);
  });

  it("reasserts stop after a late resume reply without publishing resume success", async () => {
    const arm = deferred();
    request.mockImplementation(async (path: string) => {
      if (path === "/v1/control/arm") return arm.promise;
      if (path === "/v1/control/disarm") return { status: 202, data: { cancellation_pending: true } };
      return { status: 200, data: path === "/v1/status" ? snapshot(true) : { ok: true, items: [] } };
    });
    await act(async () => { render(<HomePage />); });
    fireEvent.click(screen.getByRole("checkbox"));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "恢复自动实盘" })); });
    expect(screen.getByRole("button", { name: "停用并撤单" })).toBeEnabled();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "停用并撤单" })); });
    await act(async () => { arm.resolve({ status: 200, data: {} }); });
    expect(request.mock.calls.filter(([path]) => path === "/v1/control/disarm")).toHaveLength(2);
    expect(screen.queryByText(/恢复请求已交给个人 Worker/)).toBeNull();
  });
});
