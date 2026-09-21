import {
  parsePersonalRuntimeStatus,
  personalModeLabel,
  type PersonalTradingMode,
} from "./personal-runtime";

export type SetupStepState = "done" | "todo" | "working" | "blocked";

export interface SetupStep {
  key: "infrastructure" | "environment" | "paper" | "automation";
  title: string;
  description: string;
  state: SetupStepState;
  actionLabel: string;
  href: string;
}

interface ReadinessInput {
  health?: unknown;
  personal?: unknown;
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

const VALIDATION_COPY: Record<
  PersonalTradingMode,
  { title: string; description: string; action: string }
> = {
  paper: {
    title: "完成一次 Paper 模拟",
    description:
      "不会发送真实订单，用它确认 AI、市场数据和常驻 Worker 可以协同工作。",
    action: "运行安全模拟",
  },
  shadow: {
    title: "完成一次 Shadow 观察",
    description:
      "跟随真实行情但不提交订单，用来核对信号与风控是否按预期工作。",
    action: "运行观察周期",
  },
  canary: {
    title: "完成一次 Canary 小额实盘验证",
    description:
      "以最小仓位走通钱包绑定、授权、下单与撤单链路；这是实盘运行前的最后一步。",
    action: "运行验证周期",
  },
  live: {
    title: "确认一笔实盘成交与对账",
    description: "已进入实盘模式，先确认小额成交、持仓与对账结果符合预期。",
    action: "查看运行状态",
  },
};

export function buildSetupSteps(input: ReadinessInput): SetupStep[] {
  const health = record(input.health);
  const status = record(input.status);
  const personal = parsePersonalRuntimeStatus(input.personal ?? status);
  const latestJob = record(status.latest_job);
  const jobState = text(personal.lastCycle?.state, latestJob.status);
  const jobMode = text(latestJob.mode);
  const realMoneyMode = ["canary", "live"].includes(personal.mode);
  const environmentReady =
    personal.enabled &&
    personal.ai.configured &&
    (!realMoneyMode || personal.wallet.configured);
  const mode = personal.mode;
  const validation = VALIDATION_COPY[mode];
  // A cycle only validates the mode it actually ran in; a stale paper cycle
  // must not mark a canary deployment as verified.
  const validated =
    jobState === "succeeded" && (jobMode === undefined || jobMode === mode);
  const validating =
    ["queued", "claimed", "running"].includes(jobState ?? "") &&
    (jobMode === undefined || jobMode === mode);
  const modeNote =
    personal.desiredMode !== mode && personal.modeNote
      ? `${personal.modeNote}（当前实际运行：${personalModeLabel(mode)}）`
      : undefined;

  return [
    {
      key: "infrastructure",
      title: "三端连接正常",
      description: "Vercel 能访问 Zeabur，Zeabur 也能访问 Supabase。",
      state: health.ok === true ? "done" : "blocked",
      actionLabel: "检查部署",
      href: "/diagnostics",
    },
    {
      key: "environment",
      title: "Zeabur 环境变量已生效",
      description: personal.wallet.configured
        ? "个人模式、AI 和专用钱包都已由后端环境变量托管。"
        : "先开启个人模式并配置 AI；钱包私钥可在准备小额实盘时补上。",
      state: environmentReady ? "done" : "todo",
      actionLabel: "查看变量清单",
      href: "/settings#environment",
    },
    {
      key: "paper",
      title: validation.title,
      description: modeNote ?? validation.description,
      // Every mode gets a non-blocking step: the deployment either validates
      // itself in the mode the operator chose, or it is honestly clamped and
      // says so. The old flow forced canary users to stay blocked at 75%.
      state: validated ? "done" : validating ? "working" : "todo",
      actionLabel: validation.action,
      href: "/",
    },
    {
      key: "automation",
      title: "开启个人自动运行",
      description: realMoneyMode
        ? "由常驻 Worker 按周期自动运行实盘；随时可在控制台暂停并撤单。"
        : "由 Zeabur 常驻 Worker 定时运行；先保持 Paper 或 Shadow 观察。",
      state:
        personal.autoRunEnabled && personal.workerReady
          ? "done"
          : personal.autoRunEnabled
            ? "working"
            : "todo",
      actionLabel: "查看运行状态",
      href: "/settings#runtime",
    },
  ];
}

export function setupProgress(steps: SetupStep[]): number {
  if (steps.length === 0) return 0;
  return Math.round(
    (steps.filter((step) => step.state === "done").length / steps.length) * 100,
  );
}
