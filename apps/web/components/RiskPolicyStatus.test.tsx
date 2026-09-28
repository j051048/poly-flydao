import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import RiskPolicyStatus from "./RiskPolicyStatus";
afterEach(cleanup);

it("separates saved limits from acknowledged effective limits and deployment ceilings", () => {
  const { rerender } = render(<RiskPolicyStatus payload={{
    risk_limits: { max_order_usd: "17", version: 1, applied: false },
    desired_risk_policy: { max_order_usd: "2" }, deployment_risk_limits: { max_order_usd: "17" },
    risk_policy_pending: true,
  }} />);
  expect(screen.getByRole("status")).toHaveTextContent("等待 Worker 确认");
  const row = screen.getByText("单笔订单上限").closest("tr")!;
  expect(within(row).getByText("$2.00")).toBeInTheDocument();
  expect(within(row).getAllByText("$17.00")).toHaveLength(2);
  rerender(<RiskPolicyStatus payload={{ risk_limits: { max_order_usd: "2", applied: true, version: 2 }, risk_policy_pending: false }} />);
  expect(screen.getByRole("status")).toHaveTextContent("Worker 已确认当前风控");
});
