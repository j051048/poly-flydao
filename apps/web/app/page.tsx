"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import EquityChart from "../components/EquityChart";
import {
  API_BASE_URL,
  apiRequest,
  readableApiError,
} from "../lib/api";

type JsonRecord = Record<string, unknown>;
type BusyAction = "cycle" | "arm" | "disarm" | null;
type HealthPhase = "loading" | "online" | "degraded" | "offline";
type Notice = { tone: "success" | "error" | "info"; text: string };
type LiveMode = "canary" | "live";

interface HealthView {
  phase: HealthPhase;
  store?: string;
  mode?: string;
  checkedAt?: number;
}

interface ControlView {
  accountId?: string;
  mode?: string;
  armed?: boolean;
  armedUntil?: string;
  killSwitch?: boolean;
  acceptNewIntents?: boolean;
  version?: number;
  updatedAt?: string;
}

interface StatusView {
  configuredMode?: string;
  aiProvider?: string;
  forecastModel?: string;
  personalEnabled?: boolean;
  liveSupported?: boolean;
  personalPaused?: boolean;
  autoRunEnabled?: boolean;
  personalCycleCount?: number;
  control: ControlView;
  riskLimits: Array<{ key: string; label: string; value: string }>;
  latestJob?: JobView;
  fetchedAt: number;
}

interface JobView {
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

interface NotificationView {
  id: number;
  severity: string;
  title: string;
  message: string;
  createdAt?: string;
  read: boolean;
}

const RISK_LIMITS = [
  { key: "min_edge", label: "最小净优势", format: "percent" },
  { key: "max_order_usd", label: "单笔订单上限", format: "usd" },
  { key: "max_trade_risk_pct", label: "单次交易风险", format: "percent" },
  { key: "max_event_exposure_pct", label: "单事件敞口", format: "percent" },
  { key: "max_gross_exposure_pct", label: "总敞口", format: "percent" },
  { key: "daily_loss_limit_pct", label: "单日亏损熔断", format: "percent" },
  { key: "max_drawdown_pct", label: "最大回撤熔断", format: "percent" },
] as const;

function asRecord(value: unknown): JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function asNumber(value: unknown): number | undefined {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function asBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function formatRisk(value: unknown, format: "percent" | "usd"): string {
  const numeric = asNumber(value);
  if (numeric === undefined) return "未返回";
  return new Intl.NumberFormat(format === "usd" ? "en-US" : "zh-CN", {
    style: format === "usd" ? "currency" : "percent",
    ...(format === "usd" ? { currency: "USD" } : {}),
    maximumFractionDigits: 2,
  }).format(numeric);
}

function parseJob(payload: unknown): JobView {
  const root = asRecord(payload);
  const job = asRecord(root.job);
  const source = Object.keys(job).length ? job : root;
  const summary = asRecord(source.result_summary);
  const skipped = asRecord(summary.skipped);
  const executions = Array.isArray(source.executions)
    ? source.executions.length
    : asNumber(
        summary.executions ?? source.executions ?? source.execution_count,
      );
  return {
    id: asString(
      source.job_id ?? source.request_id ?? source.id ?? source.run_id,
    ),
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

function jobCompletionText(job: JobView): string {
  if (
    job.marketsScanned === undefined &&
    job.forecastsCreated === undefined &&
    job.executions === undefined
  ) {
    return "任务已完成；旧任务没有可展示的运行摘要。";
  }
  return `任务已完成：扫描 ${job.marketsScanned ?? 0} 个市场，生成 ${job.forecastsCreated ?? 0} 个预测，执行 ${job.executions ?? 0} 笔。`;
}

function skipReasonLabel(code: string): string {
  const exact: Record<string, string> = {
    market_liquidity_screen: "市场流动性不足",
    forecast_cooldown: "预测仍在冷却期",
    insufficient_distinct_evidence: "独立证据不足",
    forecast_insufficient_citations: "AI 引用来源不足",
    no_positive_value_candidate: "费用后没有正优势",
    market_resolution_too_close: "距离结算太近",
    stale_order_book: "盘口数据过期",
  };
  if (exact[code]) return exact[code];
  if (code.startsWith("forecast_generation_error")) return "AI 预测调用失败";
  if (code.startsWith("evidence_pipeline_error")) return "外部证据检索失败";
  if (code.startsWith("risk_")) return "被风险规则拒绝";
  return code.replaceAll("_", " ");
}

function parseStatus(payload: unknown): StatusView {
  const root = asRecord(payload);
  const personal = asRecord(root.personal);
  const personalAi = asRecord(personal.ai);
  const personalWallet = asRecord(personal.wallet);
  const control = asRecord(root.control);
  const risk = asRecord(root.risk_limits ?? root.risk_policy);
  return {
    configuredMode: asString(personal.mode ?? root.mode),
    aiProvider: asString(personalAi.provider ?? root.ai_provider),
    forecastModel: asString(
      personalAi.forecast_model ?? root.forecast_model,
    ),
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

function modeLabel(mode?: string): string {
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

function formatDate(value?: string | number): string {
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

function countdownLabel(armedUntil: string | undefined, now: number): string {
  if (!armedUntil || !now) return "无有效窗口";
  const remaining = new Date(armedUntil).getTime() - now;
  if (!Number.isFinite(remaining) || remaining <= 0) return "窗口已过期";
  const seconds = Math.ceil(remaining / 1000);
  return `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, "0")} 秒`;
}

export default function HomePage() {
  const [health, setHealth] = useState<HealthView>({ phase: "loading" });
  const [status, setStatus] = useState<StatusView | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [busy, setBusy] = useState<BusyAction>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [lastJob, setLastJob] = useState<JobView | null>(null);
  const [riskConfirmed, setRiskConfirmed] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [now, setNow] = useState(0);
  const [notifications, setNotifications] = useState<NotificationView[]>([]);
  const cycleIdempotencyKey = useRef<string | null>(null);
  const personalCycleBaseline = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    const healthPromise = apiRequest<unknown>("/health", { authenticated: false })
      .then(({ data }) => {
        const root = asRecord(data);
        setHealth({
          phase: root.ok === true ? "online" : "degraded",
          store: asString(root.store ?? root.control_plane),
          mode: asString(root.mode),
          checkedAt: Date.now(),
        });
      })
      .catch(() => setHealth({ phase: "offline", checkedAt: Date.now() }));

    const statusPromise = apiRequest<unknown>("/v1/status")
      .then(({ data }) => {
        const parsed = parseStatus(data);
        setStatus(parsed);
        if (
          parsed.latestJob &&
          (personalCycleBaseline.current === null ||
            (parsed.personalCycleCount ?? 0) > personalCycleBaseline.current ||
            parsed.latestJob.status?.toLowerCase() === "running")
        ) {
          setLastJob(parsed.latestJob);
        }
        setStatusError(null);
      })
      .catch((error) => setStatusError(readableApiError(error)));

    const notificationsPromise = apiRequest<unknown>("/v1/me/notifications?limit=8")
      .then(({ data }) => {
        const rows = asRecord(data).items;
        setNotifications(
          (Array.isArray(rows) ? rows : []).map((value) => {
            const row = asRecord(value);
            return {
              id: Number(row.id),
              severity: asString(row.severity) ?? "info",
              title: asString(row.title) ?? "系统通知",
              message: asString(row.message) ?? "",
              createdAt: asString(row.created_at),
              read: Boolean(row.read_at),
            };
          }),
        );
      })
      .catch(() => undefined);

    await Promise.allSettled([healthPromise, statusPromise, notificationsPromise]);
    setRefreshing(false);
  }, []);

  useEffect(() => {
    void refresh();
    const refreshTimer = globalThis.setInterval(() => void refresh(), 20_000);
    const clockTimer = globalThis.setInterval(() => setNow(Date.now()), 1_000);
    setNow(Date.now());
    return () => {
      globalThis.clearInterval(refreshTimer);
      globalThis.clearInterval(clockTimer);
    };
  }, [refresh]);

  useEffect(() => {
    const jobId = lastJob?.id;
    const jobStatus = lastJob?.status?.toLowerCase();
    if (
      !jobId ||
      (jobStatus &&
        !["queued", "pending", "claimed", "running", "retry"].includes(jobStatus))
    ) {
      return;
    }

    let cancelled = false;
    const poll = async () => {
      try {
        if (status?.personalEnabled) {
          const result = await apiRequest<unknown>("/v1/status");
          if (cancelled) return;
          const updatedStatus = parseStatus(result.data);
          setStatus(updatedStatus);
          const completedRequestedCycle =
            personalCycleBaseline.current === null ||
            (updatedStatus.personalCycleCount ?? 0) >
              personalCycleBaseline.current;
          if (
            updatedStatus.latestJob &&
            (completedRequestedCycle ||
              updatedStatus.latestJob.status?.toLowerCase() === "running")
          ) {
            setLastJob(updatedStatus.latestJob);
            const updatedState = updatedStatus.latestJob.status?.toLowerCase();
            if (
              completedRequestedCycle &&
              updatedState &&
              ["completed", "succeeded"].includes(updatedState)
            ) {
              personalCycleBaseline.current = null;
              setNotice({
                tone: "success",
                text: jobCompletionText(updatedStatus.latestJob),
              });
            } else if (
              completedRequestedCycle &&
              updatedState &&
              ["failed", "cancelled", "dead"].includes(updatedState)
            ) {
              personalCycleBaseline.current = null;
              setNotice({
                tone: "error",
                text:
                  updatedStatus.latestJob.message ??
                  `周期以 ${updatedState} 状态结束。`,
              });
            }
          }
          return;
        }
        const result = await apiRequest<unknown>(
          `/v1/jobs/${encodeURIComponent(jobId)}`,
        );
        if (cancelled) return;
        const updated = parseJob(result.data);
        setLastJob(updated);
        if (
          updated.status &&
          ["completed", "succeeded"].includes(updated.status)
        ) {
          setNotice({
            tone: "success",
            text: jobCompletionText(updated),
          });
        } else if (
          updated.status &&
          ["failed", "cancelled", "dead"].includes(updated.status)
        ) {
          setNotice({
            tone: "error",
            text: updated.message ?? `任务以 ${updated.status} 状态结束。`,
          });
        }
      } catch {
        // The regular 20-second status refresh remains available. A transient
        // polling failure must not convert a successfully queued job to failed.
      }
    };

    const timer = globalThis.setInterval(() => void poll(), 3_000);
    void poll();
    return () => {
      cancelled = true;
      globalThis.clearInterval(timer);
    };
  }, [lastJob?.id, lastJob?.status, status?.personalEnabled]);

  async function markNotificationRead(notificationId: number) {
    try {
      await apiRequest(`/v1/me/notifications/${notificationId}/read`, {
        method: "PUT",
      });
      setNotifications((current) =>
        current.map((row) =>
          row.id === notificationId ? { ...row, read: true } : row,
        ),
      );
    } catch (error) {
      setNotice({ tone: "error", text: readableApiError(error) });
    }
  }

  const configuredMode = status?.configuredMode ?? health.mode;
  const armableMode: LiveMode | null =
    configuredMode === "canary" || configuredMode === "live"
      ? configuredMode
      : null;
  const control = status?.control;
  const personalRealMode = Boolean(status?.personalEnabled && armableMode);
  const personalPaused = status?.personalPaused === true;
  const armedUntilMs = control?.armedUntil
    ? new Date(control.armedUntil).getTime()
    : 0;
  const effectivelyArmed = Boolean(
    control?.armed &&
      !control.killSwitch &&
      armedUntilMs > now &&
      (control.mode === "canary" || control.mode === "live"),
  );
  const apiUsesSafeTransport = useMemo(
    () =>
      API_BASE_URL.startsWith("https://") ||
      API_BASE_URL.startsWith("http://localhost") ||
      API_BASE_URL.startsWith("http://127.0.0.1"),
    [],
  );

  async function runCycle() {
    if (
      effectivelyArmed &&
      !window.confirm("当前实盘窗口已解锁，本任务可能提交真实资金订单。确认继续？")
    ) {
      return;
    }
    setBusy("cycle");
    setNotice({ tone: "info", text: "正在启动个人运行周期…" });
    try {
      cycleIdempotencyKey.current ??= crypto.randomUUID();
      if (status?.personalEnabled) {
        personalCycleBaseline.current = status.personalCycleCount ?? 0;
      }
      const result = await apiRequest<unknown>("/v1/personal/cycles/run", {
        method: "POST",
        body: { mode: configuredMode ?? "paper" },
        idempotencyKey: cycleIdempotencyKey.current,
        timeoutMs: 30_000,
      });
      cycleIdempotencyKey.current = null;
      const parsedResponse = asRecord(result.data);
      const job = {
        ...parseJob(result.data),
        status: asString(parsedResponse.state) ?? "queued",
        mode: asString(parsedResponse.mode) ?? configuredMode,
      };
      setLastJob(job);
      setNotice({
        tone: "success",
          text:
            result.status === 202
              ? `周期已交给个人 Worker${job.id ? `（${job.id}）` : ""}，可以继续使用控制台。`
              : jobCompletionText(job),
      });
      await refresh();
    } catch (error) {
      setNotice({ tone: "error", text: readableApiError(error) });
    } finally {
      setBusy(null);
    }
  }

  async function armTrading() {
    if (!armableMode || !riskConfirmed || !control?.version) return;
    if (
      !window.confirm(
        `确认恢复${modeLabel(armableMode)}自动运行？它会持续运行，直到你点击“立即停用”或关闭 Zeabur 实盘开关。`,
      )
    ) {
      return;
    }
    setBusy("arm");
    try {
      await apiRequest("/v1/control/arm", {
        method: "POST",
        body: {
          mode: armableMode,
          minutes: 10,
          expected_version: control.version,
        },
        idempotencyKey: crypto.randomUUID(),
      });
      setRiskConfirmed(false);
      setNotice({
        tone: "success",
        text: `${modeLabel(armableMode)}恢复请求已交给个人 Worker；内部短期授权会由它自动续期。`,
      });
      await refresh();
    } catch (error) {
      setNotice({ tone: "error", text: readableApiError(error) });
    } finally {
      setBusy(null);
    }
  }

  async function disarmTrading() {
    setBusy("disarm");
    setNotice({ tone: "info", text: "正在打开 kill switch 并请求撤销挂单…" });
    try {
      const result = await apiRequest<unknown>("/v1/control/disarm", {
        method: "POST",
        body: {},
        idempotencyKey: crypto.randomUUID(),
      });
      setRiskConfirmed(false);
      const response = asRecord(result.data);
      const pending =
        result.status === 202 || response.cancellation_pending === true;
      setNotice({
        tone: pending ? "info" : "success",
        text: pending
          ? "交易已停用，撤单任务正在 worker 中处理。请等待开放订单归零。"
          : "交易已停用，kill switch 已打开且挂单已撤销。",
      });
      await refresh();
    } catch (error) {
      setNotice({ tone: "error", text: readableApiError(error) });
    } finally {
      setBusy(null);
    }
  }

  const healthText = {
    loading: "检查中",
    online: "在线",
    degraded: "降级",
    offline: "离线",
  }[health.phase];

  return (
    <main className="page-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">PERSONAL CONTROL</p>
          <h1>控制台总览</h1>
        </div>
        <div className="api-address" title={API_BASE_URL}>
          <span className={`status-dot ${health.phase}`} />
          <span>{API_BASE_URL}</span>
        </div>
      </header>

      {(!lastJob || status?.aiProvider === "mock") && (
        <section className="getting-started-banner">
          <div>
            <p className="eyebrow">FIRST RUN</p>
            <strong>第一次使用？让向导告诉你下一步</strong>
            <p>在 Zeabur 填好 AI 环境变量，再运行一次不会下真实订单的 Paper 任务。</p>
          </div>
          <Link className="primary-button" href="/setup">
            打开新手向导
          </Link>
        </section>
      )}

      <section className="safety-banner">
        <span className="shield" aria-hidden="true">◆</span>
        <div>
          <strong>个人后端已与当前登录账户绑定</strong>
          <p>
            本页只发送登录令牌和运行指令；AI Key 与 EVM 私钥仅由 Zeabur
            个人服务读取，不会进入浏览器。
          </p>
        </div>
      </section>

      {!apiUsesSafeTransport && (
        <div className="transport-warning" role="alert">
          非本机 API 必须使用 HTTPS；当前地址已阻止敏感控制操作。
        </div>
      )}

      <section className="summary-grid" aria-label="运行摘要">
        <article className="summary-card">
          <div className="card-heading">
            <span>API / 数据库</span>
            <span className={`pill ${health.phase}`}>{healthText}</span>
          </div>
          <div className="primary-value">
            {health.store === "healthy" ? "连接正常" : healthText}
          </div>
          <p className="muted">最近检查 {formatDate(health.checkedAt)}</p>
        </article>

        <article className="summary-card">
          <div className="card-heading">
            <span>运行模式</span>
            <span className={`mode-chip mode-${configuredMode ?? "unknown"}`}>
              {(configuredMode ?? "UNKNOWN").toUpperCase()}
            </span>
          </div>
          <div className="primary-value">{modeLabel(configuredMode)}</div>
          <p className="muted">
            {status?.aiProvider ?? "AI 未配置"} / {status?.forecastModel ?? "模型未返回"}
          </p>
        </article>

        <article className={`summary-card ${effectivelyArmed ? "danger" : "safe"}`}>
          <div className="card-heading">
            <span>交易闸门</span>
            <span className={`pill ${effectivelyArmed ? "armed" : "disarmed"}`}>
              {effectivelyArmed ? "已解锁" : "已锁定"}
            </span>
          </div>
          <div className="primary-value">
            {effectivelyArmed
              ? countdownLabel(control?.armedUntil, now)
              : "不会提交新实盘订单"}
          </div>
          <p className="muted">
            Kill switch：{control?.killSwitch === false ? "关闭" : "开启或未知"}
          </p>
        </article>

        <article className="summary-card">
          <div className="card-heading">
            <span>状态同步</span>
            <button
              className="text-button"
              type="button"
              onClick={() => void refresh()}
              disabled={refreshing}
            >
              {refreshing ? "刷新中…" : "立即刷新"}
            </button>
          </div>
          <div className="primary-value">{statusError ? "读取失败" : "每 20 秒"}</div>
          <p className={statusError ? "error-text" : "muted"}>
            {statusError ?? `最近同步 ${formatDate(status?.fetchedAt)}`}
          </p>
        </article>
      </section>

      <EquityChart />

      {notifications.some((item) => !item.read) && (
        <section className="panel notification-panel" aria-label="系统提醒">
          <div className="section-heading">
            <div>
              <p className="eyebrow">ATTENTION</p>
              <h2>需要关注</h2>
            </div>
            <span className="pill degraded">
              {notifications.filter((item) => !item.read).length} 条未读
            </span>
          </div>
          <div className="notification-list">
            {notifications.filter((item) => !item.read).map((item) => (
              <article className={`notification-item ${item.severity}`} key={item.id}>
                <div>
                  <strong>{item.title}</strong>
                  <p>{item.message}</p>
                  <small>{formatDate(item.createdAt)}</small>
                </div>
                <button
                  type="button"
                  className="text-button"
                  onClick={() => void markNotificationRead(item.id)}
                >
                  已处理
                </button>
              </article>
            ))}
          </div>
        </section>
      )}

      <div className="main-grid">
        <section className="panel controls-panel">
          <div className="section-heading">
            <div>
              <p className="eyebrow">CONTROL ACTIONS</p>
              <h2>个人机器人操作</h2>
            </div>
            <span className="read-only-chip">单账户模式</span>
          </div>

          <div className="action-stack">
            <article className="action-card">
              <div>
                <h3>创建运行任务</h3>
                <p>同一 Zeabur 服务中的常驻 Worker 会立即领取并执行本次周期。</p>
              </div>
              <button
                className="primary-button"
                type="button"
                onClick={() => void runCycle()}
                disabled={busy !== null || !apiUsesSafeTransport}
              >
                {busy === "cycle" ? "创建中…" : "运行周期"}
              </button>
            </article>

            {status?.personalEnabled && !status.liveSupported ? (
              <article className="action-card arm-action">
                <div className="action-copy">
                  <h3>真钱模式默认关闭</h3>
                  <p>先观察 Paper / Shadow；需要实盘时必须在 Zeabur 显式开启个人实盘。</p>
                </div>
                <Link className="secondary-button" href="/settings#runtime">
                  查看模式说明
                </Link>
              </article>
            ) : !armableMode ? (
              <article className="action-card arm-action">
                <div className="action-copy">
                  <h3>当前保持安全模式</h3>
                  <p>后端现在是 Paper / Shadow，不需要交易授权；切换模式只能在 Zeabur 完成。</p>
                </div>
                <Link className="secondary-button" href="/settings#runtime">
                  查看模式说明
                </Link>
              </article>
            ) : personalPaused ? (
              <article className="action-card arm-action">
                <div className="action-copy">
                  <h3>恢复自动实盘</h3>
                  <p>恢复后 Worker 会自动维护内部短期授权；“立即停用”会跨重启保持。</p>
                </div>
                <label className="confirm-row">
                  <input
                    type="checkbox"
                    checked={riskConfirmed}
                    onChange={(event) => setRiskConfirmed(event.target.checked)}
                    disabled={!armableMode || busy !== null}
                  />
                  <span>我确认这可能提交真实资金订单，并已核对风险上限。</span>
                </label>
                <button
                  className="warning-button"
                  type="button"
                  onClick={() => void armTrading()}
                  disabled={
                    !armableMode ||
                    !riskConfirmed ||
                    !control?.version ||
                    busy !== null ||
                    !apiUsesSafeTransport
                  }
                >
                  {busy === "arm" ? "恢复中…" : "恢复自动实盘"}
                </button>
              </article>
            ) : (
              <article className="action-card arm-action">
                <div className="action-copy">
                  <h3>自动实盘已启用</h3>
                  <p>
                    Worker 会自动刷新余额、allowance 与内部授权；未入金时只会等待，不会提交订单。
                  </p>
                </div>
                <span className="read-only-chip">
                  {effectivelyArmed ? "授权有效" : "准备中"}
                </span>
              </article>
            )}

            <article className="action-card stop-action">
              <div>
                <h3>立即停用</h3>
                <p>
                  {personalRealMode
                    ? "立即打开 kill switch，并让个人 Worker 停止新交易、撤销挂单。"
                    : "Paper / Shadow 不会发送真实订单；停止自动周期请修改 Zeabur 环境变量。"}
                </p>
              </div>
              {personalRealMode ? (
                <button
                  className="danger-button"
                  type="button"
                  onClick={() => void disarmTrading()}
                  disabled={
                    personalPaused || busy !== null || !apiUsesSafeTransport
                  }
                >
                  {personalPaused
                    ? "已停用"
                    : busy === "disarm"
                      ? "停用中…"
                      : "停用并撤单"}
                </button>
              ) : (
                <Link className="secondary-button" href="/settings#runtime">
                  查看自动运行变量
                </Link>
              )}
            </article>
          </div>

          <div className="notice-slot" aria-live="polite">
            {notice && <div className={`notice ${notice.tone}`}>{notice.text}</div>}
          </div>
        </section>

        <aside className="side-column">
          <section className="panel">
            <div className="section-heading compact">
              <div>
                <p className="eyebrow">RISK LIMITS</p>
                <h2>风险上限</h2>
              </div>
              <span className="version-label">服务端值</span>
            </div>
            <dl className="risk-list">
              {(status?.riskLimits ??
                RISK_LIMITS.map((item) => ({
                  key: item.key,
                  label: item.label,
                  value: "等待 API",
                }))).map((limit) => (
                <div className="risk-row" key={limit.key}>
                  <dt>{limit.label}</dt>
                  <dd>{limit.value}</dd>
                </div>
              ))}
            </dl>
          </section>

          <section className="panel">
            <div className="section-heading compact">
              <div>
                <p className="eyebrow">LATEST JOB</p>
                <h2>最近任务</h2>
              </div>
              {lastJob?.status && <span className="pill">{lastJob.status}</span>}
            </div>
            {lastJob ? (
              <dl className="detail-list">
                <div><dt>任务 ID</dt><dd title={lastJob.id}>{lastJob.id ?? "—"}</dd></div>
                <div><dt>模式</dt><dd>{modeLabel(lastJob.mode)}</dd></div>
                <div><dt>扫描市场</dt><dd>{lastJob.marketsScanned ?? "等待 worker"}</dd></div>
                <div><dt>AI 预测</dt><dd>{lastJob.forecastsCreated ?? "等待 worker"}</dd></div>
                <div><dt>候选机会</dt><dd>{lastJob.candidatesCreated ?? "等待 worker"}</dd></div>
                <div><dt>执行订单</dt><dd>{lastJob.executions ?? "等待 worker"}</dd></div>
                {lastJob.skipped && lastJob.skipped.length > 0 && (
                  <div className="skip-reasons">
                    <dt>主要未交易原因</dt>
                    <dd>
                      {lastJob.skipped.slice(0, 3).map((item) => (
                        <span key={item.code}>
                          {skipReasonLabel(item.code)} × {item.count}
                        </span>
                      ))}
                    </dd>
                  </div>
                )}
              </dl>
            ) : (
              <div className="empty-state">
                <span aria-hidden="true">◎</span>
                <p>暂无运行任务。</p>
              </div>
            )}
          </section>

          <section className="panel control-details">
            <div className="section-heading compact">
              <div>
                <p className="eyebrow">RUNTIME CONTROL</p>
                <h2>控制详情</h2>
              </div>
            </div>
            <dl className="detail-list">
              <div><dt>账户</dt><dd title={control?.accountId}>{control?.accountId ?? "来自 JWT"}</dd></div>
              <div><dt>控制模式</dt><dd>{modeLabel(control?.mode)}</dd></div>
              <div><dt>解锁到期</dt><dd>{formatDate(control?.armedUntil)}</dd></div>
              <div><dt>接受新意图</dt><dd>{control?.acceptNewIntents === true ? "是" : "否或未知"}</dd></div>
              <div><dt>控制版本</dt><dd>{control?.version ?? "—"}</dd></div>
              <div><dt>更新时间</dt><dd>{formatDate(control?.updatedAt)}</dd></div>
            </dl>
          </section>
        </aside>
      </div>
    </main>
  );
}
