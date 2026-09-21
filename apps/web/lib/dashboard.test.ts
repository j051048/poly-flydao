import { describe, expect, it } from "vitest";

import {
  RISK_LIMITS,
  countdownLabel,
  jobCompletionText,
  modeLabel,
  parseJob,
  parseNotifications,
  parseStatus,
  skipReasonLabel,
} from "./dashboard";

describe("parseJob", () => {
  it("reads the nested job shape returned by the API", () => {
    const job = parseJob({
      job: {
        id: "job-1",
        status: "succeeded",
        mode: "paper",
        result_summary: {
          markets_scanned: 12,
          forecasts_created: 3,
          candidates_created: 1,
          executions: 2,
          skipped: { forecast_cooldown: 4, ai_budget_exhausted: 1 },
        },
      },
    });

    expect(job.id).toBe("job-1");
    expect(job.marketsScanned).toBe(12);
    expect(job.executions).toBe(2);
    expect(job.skipped).toEqual([
      { code: "forecast_cooldown", count: 4 },
      { code: "ai_budget_exhausted", count: 1 },
    ]);
  });

  it("tolerates a flat payload and missing summary", () => {
    const job = parseJob({ run_id: "run-9", status: "running" });

    expect(job.id).toBe("run-9");
    expect(job.marketsScanned).toBeUndefined();
    expect(job.skipped).toEqual([]);
    expect(jobCompletionText(job)).toContain("旧任务");
  });

  it("derives completion text from a real summary", () => {
    const job = parseJob({
      status: "completed",
      result_summary: { markets_scanned: 5, forecasts_created: 2, executions: 1 },
    });

    expect(jobCompletionText(job)).toContain("扫描 5 个市场");
  });
});

describe("parseStatus", () => {
  it("maps the personal payload onto the dashboard view", () => {
    const status = parseStatus({
      personal: {
        enabled: true,
        mode: "canary",
        cycle_count: 7,
        ai: { provider: "openai", forecast_model: "gpt-5" },
        wallet: { paused: false },
        last_cycle: { status: "succeeded", markets_scanned: 3 },
      },
      control: { armed: true, armed_until: "2026-09-21T10:00:00Z", version: 4 },
      risk_limits: { min_edge: 0.04, max_order_usd: 5 },
    });

    expect(status.configuredMode).toBe("canary");
    expect(status.personalEnabled).toBe(true);
    expect(status.personalCycleCount).toBe(7);
    expect(status.aiProvider).toBe("openai");
    expect(status.control.armed).toBe(true);
    expect(status.control.version).toBe(4);
    expect(status.latestJob?.marketsScanned).toBe(3);
    expect(status.riskLimits).toHaveLength(RISK_LIMITS.length);
    expect(status.riskLimits[0].value).not.toBe("未返回");
  });

  it("falls back to placeholders when risk limits are absent", () => {
    const status = parseStatus({});

    expect(status.personalEnabled).toBeUndefined();
    expect(status.riskLimits.every((limit) => limit.value === "未返回")).toBe(true);
    expect(status.latestJob).toBeUndefined();
  });
});

describe("parseNotifications", () => {
  it("reads unread state from read_at and defaults the severity", () => {
    const items = parseNotifications({
      items: [
        { id: 1, title: "t", message: "m", read_at: null },
        { id: 2, severity: "critical", title: "t2", message: "m2", read_at: "x" },
      ],
    });

    expect(items).toHaveLength(2);
    expect(items[0].read).toBe(false);
    expect(items[0].severity).toBe("info");
    expect(items[1].read).toBe(true);
    expect(items[1].severity).toBe("critical");
  });

  it("returns an empty list for a malformed payload", () => {
    expect(parseNotifications(null)).toEqual([]);
    expect(parseNotifications({ items: "nope" })).toEqual([]);
  });
});

describe("labels", () => {
  it("translates every trading mode", () => {
    expect(modeLabel("paper")).toBe("模拟盘");
    expect(modeLabel("canary")).toBe("金丝雀实盘");
    expect(modeLabel(undefined)).toBe("未知");
  });

  it("explains the newer skip reasons instead of dumping raw codes", () => {
    expect(skipReasonLabel("ai_budget_exhausted")).toContain("预算");
    expect(skipReasonLabel("market_filter_excluded")).not.toContain("_");
    expect(skipReasonLabel("forecast_generation_error:TimeoutError")).toBe(
      "AI 预测调用失败",
    );
    expect(skipReasonLabel("unknown_code")).toBe("unknown code");
  });

  it("counts down an arming window and reports expiry", () => {
    const now = Date.parse("2026-09-21T10:00:00Z");

    expect(countdownLabel("2026-09-21T10:02:05Z", now)).toBe("2 分 05 秒");
    expect(countdownLabel("2026-09-21T09:59:00Z", now)).toBe("窗口已过期");
    expect(countdownLabel(undefined, now)).toBe("无有效窗口");
  });
});
