import { describe, expect, it } from "vitest";

import {
  maskWalletAddress,
  parsePersonalRuntimeStatus,
  personalModeLabel,
} from "./personal-runtime";

describe("parsePersonalRuntimeStatus", () => {
  it("parses the personal status endpoint without exposing secrets", () => {
    const parsed = parsePersonalRuntimeStatus({
      enabled: true,
      live_supported: false,
      mode: "shadow",
      auto_run_enabled: true,
      worker_ready: true,
      ready: true,
      cycle_count: 2,
      last_cycle: {
        id: "cycle-2",
        state: "succeeded",
        started_at: "2026-08-04T01:00:00Z",
        completed_at: "2026-08-04T01:00:12Z",
        message: "cycle completed",
        result_summary: { markets_scanned: 8 },
      },
      worker_execution_model: "single_account",
      ai: {
        configured: true,
        provider: "openai_compatible",
        base_url: "https://relay.example.com/v1",
        forecast_model: "deepseek-v4-flash",
        critic_model: "deepseek-v4-flash",
      },
      wallet: {
        configured: true,
        address: "0x1234567890abcdef1234567890abcdef12345678",
        bound: true,
        paused: true,
        signer_address: "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
        chain_id: 137,
        collateral_token: "0x1111111111111111111111111111111111111111",
        collateral_balance_pusd: "25.5",
        allowances_ready: true,
        readiness_checked_at: "2026-08-04T01:00:10Z",
      },
      api_key: "must-not-be-read",
      private_key: "must-not-be-read",
    });

    expect(parsed).toEqual({
      enabled: true,
      liveSupported: false,
      mode: "shadow",
      desiredMode: "shadow",
      modeNote: undefined,
      autoRunEnabled: true,
      workerReady: true,
      ready: true,
      cycleCount: 2,
      reconciliation: {
        baselineAt: undefined,
        quarantinedCount: 0,
        quarantinedItems: [],
        reason: undefined,
      },
      lastCycle: {
        id: "cycle-2",
        state: "succeeded",
        startedAt: "2026-08-04T01:00:00Z",
        completedAt: "2026-08-04T01:00:12Z",
        message: "cycle completed",
        resultSummary: { markets_scanned: 8 },
      },
      workerExecutionModel: "single_account",
      ai: {
        configured: true,
        provider: "openai_compatible",
        baseUrl: "https://relay.example.com/v1",
        forecastModel: "deepseek-v4-flash",
        criticModel: "deepseek-v4-flash",
      },
      wallet: {
        configured: true,
        address: "0x1234567890abcdef1234567890abcdef12345678",
        bound: true,
        paused: true,
        signerAddress: "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
        chainId: 137,
        collateralToken: "0x1111111111111111111111111111111111111111",
        collateralBalancePusd: "25.5",
        allowancesReady: true,
        readinessCheckedAt: "2026-08-04T01:00:10Z",
      },
    });
    expect(JSON.stringify(parsed)).not.toContain("must-not-be-read");
  });

  it("accepts the nested personal object returned by /v1/status", () => {
    const parsed = parsePersonalRuntimeStatus({
      mode: "live",
      personal: {
        enabled: true,
        mode: "paper",
        auto_run_enabled: false,
        ai: { configured: false, provider: "mock" },
        wallet: { configured: false },
      },
    });

    expect(parsed.enabled).toBe(true);
    expect(parsed.mode).toBe("paper");
    expect(parsed.ai.configured).toBe(false);
    expect(parsed.wallet.configured).toBe(false);
  });

  it("uses conservative defaults for an unavailable personal endpoint", () => {
    expect(parsePersonalRuntimeStatus({})).toEqual({
      enabled: false,
      liveSupported: false,
      mode: "paper",
      desiredMode: "paper",
      modeNote: undefined,
      autoRunEnabled: false,
      workerReady: false,
      ready: false,
      cycleCount: 0,
      reconciliation: {
        baselineAt: undefined,
        quarantinedCount: 0,
        quarantinedItems: [],
        reason: undefined,
      },
      lastCycle: undefined,
      workerExecutionModel: undefined,
      ai: {
        configured: false,
        provider: undefined,
        baseUrl: undefined,
        forecastModel: undefined,
        criticModel: undefined,
      },
      wallet: {
        configured: false,
        address: undefined,
        bound: false,
        paused: false,
        signerAddress: undefined,
        chainId: undefined,
        collateralToken: undefined,
        collateralBalancePusd: undefined,
        allowancesReady: false,
        readinessCheckedAt: undefined,
      },
    });
  });
});

describe("personal runtime formatting", () => {
  it("uses clear mode labels and masks public wallet addresses", () => {
    expect(personalModeLabel("canary")).toBe("Canary 小额实盘");
    expect(
      maskWalletAddress("0x1234567890abcdef1234567890abcdef12345678"),
    ).toBe("0x123456…345678");
  });
});
