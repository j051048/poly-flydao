export type SetupStepState = "done" | "todo" | "working" | "blocked";

export interface SetupStep {
  key: "infrastructure" | "mfa" | "ai" | "paper" | "wallet" | "automation";
  title: string;
  description: string;
  state: SetupStepState;
  actionLabel: string;
  href: string;
}

interface ReadinessInput {
  health?: unknown;
  me?: unknown;
  credentials?: unknown;
  status?: unknown;
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

function activeItem(
  values: unknown,
  selectedId: string | undefined,
): Record<string, unknown> {
  const items = Array.isArray(values) ? values.map(record) : [];
  return (
    items.find((item) => text(item.id) === selectedId) ??
    items.find((item) => text(item.status) === "active") ??
    items.find((item) => !["revoked", "deleted"].includes(text(item.status) ?? "")) ??
    {}
  );
}

export function buildSetupSteps(input: ReadinessInput): SetupStep[] {
  const health = record(input.health);
  const me = record(input.me);
  const credentials = record(input.credentials);
  const status = record(input.status);
  const profile = record(me.runtime_profile ?? status.runtime_profile);
  const latestJob = record(status.latest_job);

  const aiId = text(profile.ai_credential_id);
  const walletId = text(profile.trading_wallet_id);
  const ai = activeItem(credentials.ai_credentials, aiId);
  const wallet = activeItem(credentials.wallets ?? credentials.trading_wallets, walletId);
  const walletState = text(wallet.status);
  const jobState = text(latestJob.status);
  const jobMode = text(latestJob.mode);
  const safeAutomatic =
    profile.auto_run_enabled === true &&
    ["paper", "shadow"].includes(text(profile.desired_mode) ?? "");

  return [
    {
      key: "infrastructure",
      title: "系统连接正常",
      description: "Vercel 能访问 Zeabur API，并且 Zeabur 已连接 Supabase。",
      state: health.ok === true ? "done" : "blocked",
      actionLabel: "检查三端部署",
      href: "/diagnostics",
    },
    {
      key: "mfa",
      title: "保护高风险操作",
      description: "绑定验证器，之后保存密钥、导入钱包或开启实盘都需要动态码。",
      state: text(me.aal) === "aal2" ? "done" : "todo",
      actionLabel: "绑定双因素验证",
      href: "/settings#mfa",
    },
    {
      key: "ai",
      title: "连接你的 AI",
      description: "选择提供商和模型，只提交一次 API Key；后续运行不会由浏览器重复携带。",
      state:
        aiId && text(ai.id) === aiId && text(ai.status) === "active"
          ? "done"
          : "todo",
      actionLabel: "配置 AI",
      href: "/settings#ai",
    },
    {
      key: "paper",
      title: "先完成一次模拟运行",
      description: "Paper 模式不会发送真实订单，用它确认 AI、市场数据和 Worker 都能工作。",
      state:
        jobMode === "paper" && jobState === "succeeded"
          ? "done"
          : jobMode === "paper" &&
              ["queued", "claimed", "running"].includes(jobState ?? "")
            ? "working"
            : "todo",
      actionLabel: "去控制台试运行",
      href: "/",
    },
    {
      key: "automation",
      title: "开启安全的自动周期",
      description: "建议先让 Paper/Shadow 自动运行并观察至少数天，再考虑小额 Canary。",
      state: safeAutomatic ? "done" : "todo",
      actionLabel: "设置自动运行",
      href: "/settings#automation",
    },
    {
      key: "wallet",
      title: "准备专属小额钱包",
      description: "只有准备进入 Canary 时才需要。不要导入主钱包，先等地址验证成功再小额入金。",
      state:
        walletId && text(wallet.id) === walletId && walletState === "active"
          ? "done"
          : walletId &&
              ["provisioning", "pending_verification", "revocation_pending"].includes(
                walletState ?? "",
              )
            ? "working"
            : "todo",
      actionLabel: "配置专属钱包",
      href: "/settings#wallet",
    },
  ];
}

export function setupProgress(steps: SetupStep[]): number {
  if (steps.length === 0) return 0;
  return Math.round(
    (steps.filter((step) => step.state === "done").length / steps.length) * 100,
  );
}
