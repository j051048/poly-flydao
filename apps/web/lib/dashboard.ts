// Pure parsing and formatting helpers for the dashboard.
//
// The home page only renders state; every shape it consumes is normalised
// here so the contract with the backend stays testable without a browser.

export type JsonRecord = Record<string, unknown>;
export type BusyAction = "cycle" | "arm" | "disarm" | null;
export type HealthPhase = "loading" | "online" | "degraded" | "offline";
export type Notice = { tone: "success" | "error" | "info"; text: string };
export type LiveMode = "canary" | "live";

export interface HealthView {
  phase: HealthPhase;
  store?: string;
  mode?: string;
  checkedAt?: number;
}

export interface ControlView {
  accountId?: string;
  mode?: string;
  armed?: boolean;
  armedUntil?: string;
  killSwitch?: boolean;
  acceptNewIntents?: boolean;
  version?: number;
  updatedAt?: string;
}

export interface JobView {
  id?: string;
  status?: string;
  mode?: string;
  marketsScanned?: number;
  forecastsCreated?: number;
  candidatesCreated?: number;
  executions?: number;
  skipped?: Array<{ code: string; count: number }>;
  message?: string;
}

export interface RiskLimitView {
  key: string;
  label: string;
  value: string;
}

export interface StatusView {
  configuredMode?: string;
  aiProvider?: string;
  forecastModel?: string;
  personalEnabled?: boolean;
  liveSupported?: boolean;
  personalPaused?: boolean;
  autoRunEnabled?: boolean;
  personalCycleCount?: number;
  control: ControlView;
  riskLimits: RiskLimitView[];
  latestJob?: JobView;
  fetchedAt: number;
}

export interface NotificationView {
  id: number;
  severity: string;
  title: string;
  message: string;
  createdAt?: string;
  read: boolean;
}

export const RISK_LIMITS = [
  { key: "min_edge", label: "最小净优势", format: "percent" },
  { key: "max_order_usd", label: "单笔订单上限", format: "usd" },
  { key: "max_trade_risk_pct", label: "单次交易风险", format: "percent" },
  { key: "max_event_exposure_pct", label: "单事件敞口", format: "percent" },
  { key: "max_gross_exposure_pct", label: "总敞口", format: "percent" },
  { key: "daily_loss_limit_pct", label: "单日亏损熔断", format: "percent" },
  { key: "max_drawdown_pct", label: "最大回撤熔断", format: "percent" },
] as const;

export function asRecord(value: unknown): JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

export function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

export function asNumber(value: unknown): number | undefined {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

export function asBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

export function formatRisk(value: unknown, format: "percent" | "usd"): string {
  const numeric = asNumber(value);
  if (numeric === undefined) return "未返回";
  return new Intl.NumberFormat(format === "usd" ? "en-US" : "zh-CN", {
    style: format === "usd" ? "currency" : "percent",
    ...(format === "usd" ? { currency: "USD" } : {}),
    maximumFractionDigits: 2,
  }).format(numeric);
}

export function parseJob(payload: unknown): JobView {
  const root = asRecord(payload);
  const job = asRecord(root.job);
  const source = Object.keys(job).length ? job : root;
  const summary = asRecord(source.result_summary);
  const skipped = asRecord(summary.skipped);
  const executions = Array.isArray(source.executions)
    ? source.executions.length
    : asNumber(summary.executions ?? source.executions ?? source.execution_count);
  return {
    id: asString(source.job_id ?? source.request_id ?? source.id ?? source.run_id),
    status:
      asString(source.status ?? source.state) ??
      (source.completed_at ? "completed" : undefined),
    mode: asString(source.mode),
    marketsScanned: asNumber(summary.markets_scanned ?? source.markets_scanned),
    forecastsCreated: asNumber(
      summary.forecasts_created ?? source.forecasts_created,
    ),
    candidatesCreated: asNumber(
      summary.candidates_created ?? source.candidates_created,
    ),
    executions,
    skipped: Object.entries(skipped)
      .map(([code, count]) => ({ code, count: asNumber(count) ?? 0 }))
      .filter((item) => item.count > 0)
      .sort((left, right) => right.count - left.count),
    message: asString(source.message ?? source.error_code),
  };
}

export function jobCompletionText(job: JobView): string {
  if (
    job.marketsScanned === undefined &&
    job.forecastsCreated === undefined &&
    job.executions === undefined
  ) {
    return "任务已完成；旧任务没有可展示的运行摘要。";
  }
  return `任务已完成：扫描 ${job.marketsScanned ?? 0} 个市场，生成 ${job.forecastsCreated ?? 0} 个预测，执行 ${job.executions ?? 0} 笔。`;
}

const EXACT_SKIP_LABELS: Record<string, string> = {
  market_liquidity_screen: "市场流动性不足",
  forecast_cooldown: "预测仍在冷却期",
  insufficient_distinct_evidence: "独立证据不足",
  forecast_insufficient_citations: "AI 引用来源不足",
  no_positive_value_candidate: "费用后没有正优势",
  market_resolution_too_close: "距离结算太近",
  stale_order_book: "盘口数据过期",
  market_filter_excluded: "不在所选市场范围内",
  market_filter_reduce_only: "仅允许减仓，不开新仓",
  ai_budget_exhausted: "当日 AI 预算已用尽",
  ai_budget_unavailable: "AI 预算无法校验（安全跳过）",
  quarantined_token: "该市场存在隔离资产",
  market_not_tradeable: "市场当前不可交易",
  empty_order_book: "盘口没有可成交价格",
};

export function skipReasonLabel(code: string): string {
  if (EXACT_SKIP_LABELS[code]) return EXACT_SKIP_LABELS[code];
  if (code.startsWith("forecast_generation_error")) return "AI 预测调用失败";
  if (code.startsWith("evidence_pipeline_error")) return "外部证据检索失败";
  if (code.startsWith("risk_")) return "被风险规则拒绝";
  if (code.startsWith("ai_budget_unavailable")) return "AI 预算无法校验（安全跳过）";
  return code.replaceAll("_", " ");
}

export function parseStatus(payload: unknown): StatusView {
  const root = asRecord(payload);
  const personal = asRecord(root.personal);
  const personalAi = asRecord(personal.ai);
  const personalWallet = asRecord(personal.wallet);
  const control = asRecord(root.control);
  const risk = asRecord(root.risk_limits ?? root.risk_policy);
  return {
    configuredMode: asString(personal.mode ?? root.mode),
    aiProvider: asString(personalAi.provider ?? root.ai_provider),
    forecastModel: asString(personalAi.forecast_model ?? root.forecast_model),
    personalEnabled: asBoolean(personal.enabled ?? root.personal_mode),
    liveSupported: asBoolean(personal.live_supported),
    personalPaused: asBoolean(personalWallet.paused),
    autoRunEnabled: asBoolean(personal.auto_run_enabled),
    personalCycleCount: asNumber(personal.cycle_count),
    control: {
      accountId: asString(control.account_id),
      mode: asString(control.mode),
      armed: asBoolean(control.armed),
      armedUntil: asString(control.armed_until),
      killSwitch: asBoolean(control.kill_switch),
      acceptNewIntents: asBoolean(control.accept_new_intents),
      version: asNumber(control.version),
      updatedAt: asString(control.updated_at),
    },
    riskLimits: RISK_LIMITS.map((definition) => ({
      key: definition.key,
      label: definition.label,
      value: formatRisk(risk[definition.key], definition.format),
    })),
    latestJob: root.latest_job
      ? parseJob(root.latest_job)
      : personal.last_cycle
        ? parseJob(personal.last_cycle)
        : undefined,
    fetchedAt: Date.now(),
  };
}

export function parseNotifications(payload: unknown): NotificationView[] {
  const rows = asRecord(payload).items;
  return (Array.isArray(rows) ? rows : []).map((value) => {
    const row = asRecord(value);
    return {
      id: Number(row.id),
      severity: asString(row.severity) ?? "info",
      title: asString(row.title) ?? "系统通知",
      message: asString(row.message) ?? "",
      createdAt: asString(row.created_at),
      read: Boolean(row.read_at),
    };
  });
}

export function modeLabel(mode?: string): string {
  return (
    {
      paper: "模拟盘",
      shadow: "影子模式",
      canary: "金丝雀实盘",
      live: "受控实盘",
    }[mode ?? ""] ??
    mode ??
    "未知"
  );
}

export function formatDate(value?: string | number): string {
  if (value === undefined) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

export function countdownLabel(
  armedUntil: string | undefined,
  now: number,
): string {
  if (!armedUntil || !now) return "无有效窗口";
  const remaining = new Date(armedUntil).getTime() - now;
  if (!Number.isFinite(remaining) || remaining <= 0) return "窗口已过期";
  const seconds = Math.ceil(remaining / 1000);
  return `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, "0")} 秒`;
}
