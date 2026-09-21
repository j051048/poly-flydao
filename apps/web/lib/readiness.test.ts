import { describe, expect, it } from "vitest";

import { buildSetupSteps, setupProgress } from "./readiness";

describe("buildSetupSteps", () => {
  it("marks a fully prepared personal paper deployment as complete", () => {
    const steps = buildSetupSteps({
      health: { ok: true },
      personal: {
        enabled: true,
        mode: "paper",
        auto_run_enabled: true,
        worker_ready: true,
        ai: { configured: true, provider: "openai" },
        wallet: { configured: true, address: "0x1234" },
      },
      status: {
        latest_job: { mode: "paper", status: "succeeded" },
      },
    });

    expect(steps.map((step) => [step.key, step.state])).toEqual([
      ["infrastructure", "done"],
      ["environment", "done"],
      ["paper", "done"],
      ["automation", "done"],
    ]);
    expect(setupProgress(steps)).toBe(100);
  });

  it("does not require a wallet before a safe paper run", () => {
    const steps = buildSetupSteps({
      health: { ok: true },
      personal: {
        enabled: true,
        mode: "paper",
        auto_run_enabled: false,
        ai: { configured: true, provider: "openai_compatible" },
        wallet: { configured: false },
      },
      status: {
        latest_job: { mode: "paper", status: "running" },
      },
    });

    expect(steps.find((step) => step.key === "environment")?.state).toBe(
      "done",
    );
    expect(steps.find((step) => step.key === "paper")?.state).toBe("working");
    expect(steps.find((step) => step.key === "automation")?.state).toBe(
      "todo",
    );
    expect(setupProgress(steps)).toBe(50);
  });

  it("falls back to the personal object nested in /v1/status", () => {
    const steps = buildSetupSteps({
      health: { ok: false },
      status: {
        personal: {
          enabled: true,
          auto_run_enabled: true,
          worker_ready: true,
          ai: { configured: true },
          wallet: { configured: false },
        },
      },
    });

    expect(steps.find((step) => step.key === "infrastructure")?.state).toBe(
      "blocked",
    );
    expect(steps.find((step) => step.key === "environment")?.state).toBe(
      "done",
    );
    expect(steps.find((step) => step.key === "automation")?.state).toBe(
      "done",
    );
  });

  it("turns the validation step into a canary check instead of blocking it", () => {
    const steps = buildSetupSteps({
      health: { ok: true },
      personal: {
        enabled: true,
        mode: "canary",
        auto_run_enabled: true,
        ai: { configured: true },
        wallet: { configured: true },
      },
      status: { latest_job: { mode: "paper", status: "succeeded" } },
    });
    const paper = steps.find((step) => step.key === "paper");

    // A canary deployment is never stuck behind a Paper run again.
    expect(paper?.state).not.toBe("blocked");
    expect(paper?.state).toBe("todo");
    expect(paper?.title).toContain("Canary");
    expect(paper?.href).toBe("/");
    expect(setupProgress(steps)).toBeGreaterThanOrEqual(50);
  });

  it("marks the canary check done once a canary cycle succeeds", () => {
    const steps = buildSetupSteps({
      health: { ok: true },
      personal: {
        enabled: true,
        mode: "canary",
        desired_mode: "canary",
        auto_run_enabled: true,
        worker_ready: true,
        ai: { configured: true },
        wallet: { configured: true, bound: true },
      },
      status: { latest_job: { mode: "canary", status: "succeeded" } },
    });

    expect(steps.map((step) => [step.key, step.state])).toEqual([
      ["infrastructure", "done"],
      ["environment", "done"],
      ["paper", "done"],
      ["automation", "done"],
    ]);
    expect(setupProgress(steps)).toBe(100);
  });

  it("ignores a stale paper cycle when the deployment moved to canary", () => {
    const canary = buildSetupSteps({
      health: { ok: true },
      personal: {
        enabled: true,
        mode: "canary",
        auto_run_enabled: true,
        ai: { configured: true },
        wallet: { configured: true },
      },
      status: { latest_job: { mode: "paper", status: "succeeded" } },
    }).find((step) => step.key === "paper");

    expect(canary?.state).toBe("todo");
  });

  it("explains a clamped mode request instead of hiding it", () => {
    const steps = buildSetupSteps({
      health: { ok: true },
      personal: {
        enabled: true,
        mode: "paper",
        desired_mode: "canary",
        mode_note: "本部署未开启实盘能力。",
        auto_run_enabled: true,
        ai: { configured: true },
        wallet: { configured: true },
      },
    });
    const paper = steps.find((step) => step.key === "paper");

    expect(paper?.description).toContain("本部署未开启实盘能力");
    expect(paper?.description).toContain("Paper 模拟");
  });

  it("surfaces quarantined reconciliation activity through the parsed status", () => {
    const steps = buildSetupSteps({
      health: { ok: true },
      personal: {
        enabled: true,
        mode: "paper",
        auto_run_enabled: true,
        ai: { configured: true },
        wallet: { configured: true },
        reconciliation: {
          baseline_at: "2026-09-21T00:00:00+00:00",
          quarantined_count: 2,
          quarantined_items: [
            {
              kind: "trade",
              external_key: "manual-1",
              reason: "unmapped_account_trade",
            },
          ],
        },
      },
    });

    expect(steps).toHaveLength(4);
  });
});
