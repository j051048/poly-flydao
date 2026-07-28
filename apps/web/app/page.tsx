"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
  executions?: number;
  message?: string;
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
  const executions = Array.isArray(source.executions)
    ? source.executions.length
    : asNumber(source.executions ?? source.execution_count);
  return {
    id: asString(source.job_id ?? source.id ?? source.run_id),
    status: asString(source.status) ?? (source.completed_at ? "completed" : undefined),
    mode: asString(source.mode),
    marketsScanned: asNumber(source.markets_scanned),
    executions,
    message: asString(source.message ?? source.error_code),
  };
}

function parseStatus(payload: unknown): StatusView {
  const root = asRecord(payload);
  const control = asRecord(root.control);
  const risk = asRecord(root.risk_limits ?? root.risk_policy);
  return {
    configuredMode: asString(root.mode),
    aiProvider: asString(root.ai_provider),
    forecastModel: asString(root.forecast_model),
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
    latestJob: root.latest_job ? parseJob(root.latest_job) : undefined,
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
  const [armMinutes, setArmMinutes] = useState(5);
  const [riskConfirmed, setRiskConfirmed] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [now, setNow] = useState(0);
  const cycleIdempotencyKey = useRef<string | null>(null);

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
        if (parsed.latestJob) setLastJob(parsed.latestJob);
        setStatusError(null);
      })
      .catch((error) => setStatusError(readableApiError(error)));

    await Promise.allSettled([healthPromise, statusPromise]);
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
            text: `任务已完成：扫描 ${updated.marketsScanned ?? 0} 个市场，执行 ${updated.executions ?? 0} 笔。`,
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
  }, [lastJob?.id, lastJob?.status]);

  const configuredMode = status?.configuredMode ?? health.mode;
  const armableMode: LiveMode | null =
    configuredMode === "canary" || configuredMode === "live"
      ? configuredMode
      : null;
  const control = status?.control;
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
    setNotice({ tone: "info", text: "正在创建租户隔离的运行任务…" });
    try {
      cycleIdempotencyKey.current ??= crypto.randomUUID();
      const result = await apiRequest<unknown>("/v1/jobs/cycles", {
        method: "POST",
        body: { mode: configuredMode ?? "paper" },
        idempotencyKey: cycleIdempotencyKey.current,
        timeoutMs: 30_000,
      });
      cycleIdempotencyKey.current = null;
      const job = parseJob(result.data);
      setLastJob(job);
      setNotice({
        tone: "success",
        text:
          result.status === 202
            ? `任务已进入队列${job.id ? `（${job.id}）` : ""}，worker 将异步执行。`
            : `周期已完成：扫描 ${job.marketsScanned ?? 0} 个市场，执行 ${job.executions ?? 0} 笔。`,
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
        `确认将${modeLabel(armableMode)}解锁 ${armMinutes} 分钟？`,
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
          minutes: armMinutes,
          expected_version: control.version,
        },
        idempotencyKey: crypto.randomUUID(),
      });
      setRiskConfirmed(false);
      setNotice({
        tone: "success",
        text: `${modeLabel(armableMode)}已短时解锁 ${armMinutes} 分钟。`,
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
          <p className="eyebrow">TENANT CONTROL PLANE</p>
          <h1>控制台总览</h1>
        </div>
        <div className="api-address" title={API_BASE_URL}>
          <span className={`status-dot ${health.phase}`} />
          <span>{API_BASE_URL}</span>
        </div>
      </header>

      <section className="safety-banner">
        <span className="shield" aria-hidden="true">◆</span>
        <div>
          <strong>当前 Supabase 会话就是租户身份</strong>
          <p>
            本页仅发送 JWT、任务参数和幂等键，不读取或转发 AI Key、EVM
            私钥、助记词及 CLOB 凭证。
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

      <div className="main-grid">
        <section className="panel controls-panel">
          <div className="section-heading">
            <div>
              <p className="eyebrow">CONTROL ACTIONS</p>
              <h2>租户操作</h2>
            </div>
            <span className="read-only-chip">JWT 已绑定</span>
          </div>

          <div className="action-stack">
            <article className="action-card">
              <div>
                <h3>创建运行任务</h3>
                <p>API 只入队；隔离 worker 获取租户租约后才加载密钥并执行。</p>
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

            <article className="action-card arm-action">
              <div className="action-copy">
                <h3>短时解锁</h3>
                <p>仅 canary/live 可用；到期后服务端自动拒绝新交易。</p>
              </div>
              <div className="arm-settings">
                <label htmlFor="arm-minutes">窗口</label>
                <select
                  id="arm-minutes"
                  value={armMinutes}
                  onChange={(event) => setArmMinutes(Number(event.target.value))}
                  disabled={busy !== null}
                >
                  {[1, 3, 5, 10, 15].map((minutes) => (
                    <option key={minutes} value={minutes}>{minutes} 分钟</option>
                  ))}
                </select>
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
                {busy === "arm" ? "解锁中…" : "短时解锁交易"}
              </button>
            </article>

            <article className="action-card stop-action">
              <div>
                <h3>立即停用</h3>
                <p>先持久化 kill switch，再由持有租约的 worker 异步撤单。</p>
              </div>
              <button
                className="danger-button"
                type="button"
                onClick={() => void disarmTrading()}
                disabled={busy !== null || !apiUsesSafeTransport}
              >
                {busy === "disarm" ? "停用中…" : "停用并撤单"}
              </button>
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
                <div><dt>执行订单</dt><dd>{lastJob.executions ?? "等待 worker"}</dd></div>
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
