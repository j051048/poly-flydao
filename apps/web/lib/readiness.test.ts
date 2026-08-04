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

  it("blocks the safe-paper action when the deployment is in a real-money mode", () => {
    const paper = buildSetupSteps({
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

    expect(paper?.state).toBe("blocked");
    expect(paper?.href).toBe("/settings#runtime");
    expect(paper?.description).toContain("POLYBOT_MODE=paper");
  });
});
