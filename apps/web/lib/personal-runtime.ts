export type PersonalTradingMode = "paper" | "shadow" | "canary" | "live";

export interface PersonalRuntimeStatus {
  enabled: boolean;
  liveSupported: boolean;
  mode: PersonalTradingMode;
  autoRunEnabled: boolean;
  workerReady: boolean;
  ready: boolean;
  cycleCount: number;
  lastCycle?: {
    id?: string;
    state?: string;
    startedAt?: string;
    completedAt?: string;
    message?: string;
    resultSummary?: Record<string, unknown>;
  };
  workerExecutionModel?: "single_account" | "tenant_queue";
  ai: {
    configured: boolean;
    provider?: string;
    baseUrl?: string;
    forecastModel?: string;
    criticModel?: string;
  };
  wallet: {
    configured: boolean;
    address?: string;
    bound: boolean;
    paused: boolean;
    signerAddress?: string;
    chainId?: number;
    collateralToken?: string;
    collateralBalancePusd?: string;
    allowancesReady: boolean;
    readinessCheckedAt?: string;
  };
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(...values: unknown[]): string | undefined {
  return values.find(
    (value): value is string => typeof value === "string" && value.length > 0,
  );
}

function boolean(...values: unknown[]): boolean | undefined {
  return values.find((value): value is boolean => typeof value === "boolean");
}

function tradingMode(value: unknown): PersonalTradingMode {
  return ["paper", "shadow", "canary", "live"].includes(String(value))
    ? (value as PersonalTradingMode)
    : "paper";
}

/**
 * Parse the dedicated personal-status response while accepting the nested
 * `personal` object exposed by `/v1/status` as a deployment-transition fallback.
 * No secret value is part of this public model.
 */
export function parsePersonalRuntimeStatus(
  payload: unknown,
): PersonalRuntimeStatus {
  const root = record(payload);
  const source = Object.keys(record(root.personal)).length
    ? record(root.personal)
    : root;
  const ai = record(source.ai);
  const wallet = record(source.wallet);
  const profile = record(source.runtime_profile ?? root.runtime_profile);
  const provider = text(ai.provider, source.ai_provider, profile.ai_provider);
  const address = text(
    wallet.address,
    wallet.deposit_wallet_address,
    source.wallet_address,
  );
  const workerExecutionModel = text(
    source.worker_execution_model,
  );
  const chainId = Number(wallet.chain_id);
  const lastCycle = record(source.last_cycle);
  const cycleCount = Number(source.cycle_count);

  return {
    enabled: boolean(source.enabled, source.personal_mode) ?? false,
    liveSupported: boolean(source.live_supported) ?? false,
    mode: tradingMode(source.mode ?? profile.desired_mode),
    autoRunEnabled:
      boolean(source.auto_run_enabled, profile.auto_run_enabled) ?? false,
    workerReady: boolean(source.worker_ready) ?? false,
    ready: boolean(source.ready) ?? false,
    cycleCount: Number.isFinite(cycleCount) ? cycleCount : 0,
    lastCycle: Object.keys(lastCycle).length
      ? {
          id: text(lastCycle.id),
          state: text(lastCycle.state, lastCycle.status),
          startedAt: text(lastCycle.started_at),
          completedAt: text(lastCycle.completed_at),
          message: text(lastCycle.message),
          resultSummary: Object.keys(record(lastCycle.result_summary)).length
            ? record(lastCycle.result_summary)
            : undefined,
        }
      : undefined,
    workerExecutionModel:
      workerExecutionModel === "single_account" ||
      workerExecutionModel === "tenant_queue"
        ? workerExecutionModel
        : undefined,
    ai: {
      configured:
        boolean(ai.configured, source.ai_configured) ??
        Boolean(provider && !["mock", "platform"].includes(provider)),
      provider,
      baseUrl: text(ai.base_url, source.ai_base_url, profile.ai_base_url),
      forecastModel: text(
        ai.forecast_model,
        source.forecast_model,
        profile.forecast_model,
      ),
      criticModel: text(ai.critic_model, source.critic_model),
    },
    wallet: {
      configured:
        boolean(wallet.configured, source.wallet_configured) ?? Boolean(address),
      address,
      bound: boolean(wallet.bound) ?? false,
      paused: boolean(wallet.paused) ?? false,
      signerAddress: text(wallet.signer_address),
      chainId: Number.isFinite(chainId) ? chainId : undefined,
      collateralToken: text(wallet.collateral_token),
      collateralBalancePusd: text(wallet.collateral_balance_pusd),
      allowancesReady: boolean(wallet.allowances_ready) ?? false,
      readinessCheckedAt: text(wallet.readiness_checked_at),
    },
  };
}

export function personalModeLabel(mode: PersonalTradingMode): string {
  return {
    paper: "Paper 模拟",
    shadow: "Shadow 影子",
    canary: "Canary 小额实盘",
    live: "Live 实盘",
  }[mode];
}

export function maskWalletAddress(address: string | undefined): string {
  if (!address) return "未提供公开地址";
  if (address.length <= 14) return address;
  return `${address.slice(0, 8)}…${address.slice(-6)}`;
}
