import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import PerformancePage from "./page";
import { apiRequest } from "../../lib/api";

vi.mock("../../lib/api", () => ({ apiRequest: vi.fn(), readableApiError: () => "无法读取" }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it("renders actual FastAPI Decimal strings and populated calibration", async () => {
  vi.mocked(apiRequest).mockImplementation(async (path) => ({ status: 200, data: path.includes("performance") ? {
    sample_size: 5, resolved_markets: 2, brier_score: "0.25", log_loss: "0.6931",
    research_only: true, calibration: [{ lower: "0.4", upper: "0.6", samples: 5, mean_forecast: "0.5", observed_frequency: "0.4" }],
  } : { items: [] } }));
  await act(async () => { render(<PerformancePage />); });
  expect(screen.getByText("0.2500")).toBeInTheDocument();
  expect(screen.getByText("0.6931")).toBeInTheDocument();
  expect(screen.getByText("RESEARCH ONLY")).toBeInTheDocument();
});

it("never advertises VALIDATED when initial loading fails", async () => {
  vi.mocked(apiRequest).mockRejectedValue(new Error("offline"));
  await act(async () => { render(<PerformancePage />); });
  expect(screen.queryByText("VALIDATED")).toBeNull();
  expect(screen.getByText("未获取")).toBeInTheDocument();
});
