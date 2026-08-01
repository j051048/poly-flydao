import { describe, expect, it } from "vitest";

import { buildSetupSteps, setupProgress } from "./readiness";

describe("buildSetupSteps", () => {
  it("marks a fully prepared safe paper account as complete except wallet", () => {
    const steps = buildSetupSteps({
      health: { ok: true },
      me: {
        aal: "aal2",
        runtime_profile: {
          ai_credential_id: "ai-1",
          desired_mode: "paper",
          auto_run_enabled: true,
        },
      },
      credentials: {
        ai_credentials: [{ id: "ai-1", status: "active" }],
        wallets: [],
      },
      status: {
        latest_job: { mode: "paper", status: "succeeded" },
      },
    });

    expect(steps.map((step) => [step.key, step.state])).toEqual([
      ["infrastructure", "done"],
      ["mfa", "done"],
      ["ai", "done"],
      ["paper", "done"],
      ["automation", "done"],
      ["wallet", "todo"],
    ]);
    expect(steps.find((step) => step.key === "wallet")?.optional).toBe(true);
    expect(setupProgress(steps)).toBe(100);
  });

  it("shows pending wallet verification and a running paper job as working", () => {
    const steps = buildSetupSteps({
      health: { ok: false },
      me: {
        aal: "aal1",
        runtime_profile: {
          trading_wallet_id: "wallet-1",
          desired_mode: "paper",
          auto_run_enabled: false,
        },
      },
      credentials: {
        wallets: [{ id: "wallet-1", status: "pending_verification" }],
      },
      status: {
        latest_job: { mode: "paper", status: "running" },
      },
    });

    expect(steps.find((step) => step.key === "infrastructure")?.state).toBe(
      "blocked",
    );
    expect(steps.find((step) => step.key === "paper")?.state).toBe("working");
    expect(steps.find((step) => step.key === "wallet")?.state).toBe("working");
  });
});
