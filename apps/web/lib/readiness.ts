import { parsePersonalRuntimeStatus } from "./personal-runtime";

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

export function buildSetupSteps(input: ReadinessInput): SetupStep[] {
  const health = record(input.health);
  const status = record(input.status);
  const personal = parsePersonalRuntimeStatus(input.personal ?? status);
  const latestJob = record(status.latest_job);
  const jobState = text(personal.lastCycle?.state, latestJob.status);
  const jobMode = personal.lastCycle ? personal.mode : text(latestJob.mode);
  const realMoneyMode = ["canary", "live"].includes(personal.mode);
  const environmentReady =
    personal.enabled &&
    personal.ai.configured &&
    (!realMoneyMode || personal.wallet.configured);
  const paperMode = personal.mode === "paper";

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
      title: "完成一次 Paper 模拟",
      description: paperMode
        ? "不会发送真实订单，用它确认 AI、市场数据和常驻 Worker 可以协同工作。"
        : "当前后端不是 Paper。请先在 Zeabur 设置 POLYBOT_MODE=paper 并重新部署，向导绝不会把实盘伪装成模拟。",
      state:
        !paperMode
          ? "blocked"
          : jobMode === "paper" && jobState === "succeeded"
          ? "done"
          : jobMode === "paper" &&
              ["queued", "claimed", "running"].includes(jobState ?? "")
            ? "working"
            : "todo",
      actionLabel: paperMode ? "运行安全模拟" : "查看模式变量",
      href: paperMode ? "/" : "/settings#runtime",
    },
    {
      key: "automation",
      title: "开启个人自动运行",
      description: "由 Zeabur 常驻 Worker 定时运行；先保持 Paper 或 Shadow 观察。",
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
