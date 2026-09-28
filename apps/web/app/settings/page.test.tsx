import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import SettingsPage from "./page";

const { request } = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("../../lib/api", () => ({ apiRequest: request, ApiError: class extends Error {}, readableApiError: () => "请求失败" }));
vi.mock("../../lib/supabase/browser", () => ({ getSupabaseBrowserClient: () => null }));
vi.mock("../../components/MfaPanel", () => ({ default: ({ onAssuranceChange }: { onAssuranceChange: (value: boolean) => void }) =>
  <button onClick={() => onAssuranceChange(true)}>完成 MFA 测试挑战</button> }));
afterEach(() => { cleanup(); vi.useRealTimers(); request.mockReset(); });

it("requires verified MFA then shows saved policy pending until the worker acknowledges it", async () => {
  vi.useFakeTimers();
  let acknowledged = false;
  request.mockImplementation(async (path: string) => ({ status: 200, data:
    path === "/health" ? { ok: true }
      : path === "/v1/me" ? { runtime_profile: { version: 4 } }
        : path === "/v1/personal/status" ? { enabled: true, mode: "paper", desired_mode: "paper", mode_applied: true }
          : path === "/v1/status" ? {
            risk_policy_pending: !acknowledged,
            risk_limits: { max_order_usd: acknowledged ? "2" : "17", applied: acknowledged, version: acknowledged ? 2 : 1 },
            desired_risk_policy: { max_order_usd: "2", version: 2 }, deployment_risk_limits: { max_order_usd: "17" },
          } : {},
  }));
  await act(async () => { render(<SettingsPage />); });
  expect(screen.getAllByRole("button", { name: "保存此档位" })[0]).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "完成 MFA 测试挑战" }));
  await act(async () => { fireEvent.click(screen.getAllByRole("button", { name: "保存此档位" })[0]); });
  expect(request).toHaveBeenCalledWith("/v1/me/risk-policy", expect.objectContaining({ method: "PUT", body: { expected_profile_version: 4, preset: "conservative" } }));
  expect(screen.getByText(/已保存「保守」风控档位，等待 Worker 确认/)).toBeInTheDocument();
  expect(screen.queryByText(/Worker 已确认当前风控/)).toBeNull();
  acknowledged = true;
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(screen.getByText(/Worker 已确认当前风控/)).toBeInTheDocument();
});
