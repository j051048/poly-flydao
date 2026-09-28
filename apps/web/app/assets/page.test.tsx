import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import AssetsPage from "./page";
import { apiRequest } from "../../lib/api";

vi.mock("../../lib/api", () => ({ apiRequest: vi.fn(), readableApiError: () => "无法读取" }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it("displays per-position pUSD unrealized PnL from the backend contract", async () => {
  vi.mocked(apiRequest).mockResolvedValue({ status: 200, data: {
    mode: "canary", positions: [{ id: "p1", market: "Test market", outcome: "YES", shares: "10",
      average_entry_price: "0.4", mark_price: "0.5", value_pusd: "5", unrealized_pnl_pusd: "1.25" }],
  } });
  await act(async () => { render(<AssetsPage />); });
  expect(screen.getByText("$1.25")).toBeInTheDocument();
});
