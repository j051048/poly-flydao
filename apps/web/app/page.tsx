"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8080"
).replace(/\/+$/, "");

const ENDPOINTS = {
  health: "/health",
  status: "/v1/status",
  cycle: "/v1/cycles/run",
  arm: "/v1/control/arm",
  disarm: "/v1/control/disarm",
} as const;

type JsonRecord = Record<string, unknown>;
type BusyAction = "cycle" | "arm" | "disarm" | null;
type HealthPhase = "loading" | "online" | "degraded" | "offline";
type NoticeTone = "success" | "error" | "info";
type LiveMode = "canary" | "live";

interface HealthView {
  phase: HealthPhase;
  store?: string;
  mode?: string;
  realMoney?: boolean;
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

interface RiskLimitView {
  key: string;
  label: string;
  value: string;
}

interface StatusView {
  configuredMode?: string;
  aiProvider?: string;
  forecastModel?: string;
  control: ControlView;
  riskLimits: RiskLimitView[];
  fetchedAt: number;
}

interface CycleView {
  runId?: string;
  mode?: string;
  marketsScanned?: number;
  forecastsCreated?: number;
  candidatesCreated?: number;
  intentsApproved?: number;
  executions?: number;
  skipped?: number;
  completedAt?: string;
}

interface Notice {
  tone: NoticeTone;
  text: string;
}

class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
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

function asRecord(value: unknown): JsonRecord | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as JsonRecord)
    : undefined;
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function asNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

function asBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function formatRiskValue(value: unknown, format: "percent" | "usd"): string {
  const numeric = asNumber(value);
  if (numeric === undefined) return "未返回";
  if (format === "percent") {
    return new Intl.NumberFormat("zh-CN", {
      style: "percent",
      minimumFractionDigits: 0,
      maximumFractionDigits: 2,
    }).format(numeric);
  }
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  }).format(numeric);
}

function parseStatus(payload: unknown): StatusView {
  const root = asRecord(payload) || {};
  const control = asRecord(root.control) || {};
  const risk = asRecord(root.risk_limits) || {};

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
      value: formatRiskValue(risk[definition.key], definition.format),
    })),
    fetchedAt: Date.now(),
  };
}

function parseCycle(payload: unknown): CycleView {
  const root = asRecord(payload) || {};
  const executions = Array.isArray(root.executions)
    ? root.executions.length
    : asNumber(root.executions);
  const skippedRecord = asRecord(root.skipped);
  const skipped = skippedRecord
    ? Object.values(skippedRecord).reduce<number>((total, value) => {
        return total + (asNumber(value) || 0);
      }, 0)
    : asNumber(root.skipped);

  return {
    runId: asString(root.run_id),
    mode: asString(root.mode),
    marketsScanned: asNumber(root.markets_scanned),
    forecastsCreated: asNumber(root.forecasts_created),
    candidatesCreated: asNumber(root.candidates_created),
    intentsApproved: asNumber(root.intents_approved),
    executions,
    skipped,
    completedAt: asString(root.completed_at),
  };
}

function httpErrorMessage(status: number): string {
  if (status === 401) return "管理员令牌无效，请重新输入。";
  if (status === 409) return "当前运行模式或持久化控制状态不允许此操作。";
  if (status === 503) return "API 未启用管理员操作，或依赖服务暂不可用。";
  if (status >= 500) return "API 内部错误，请先保持停用并检查 Zeabur 日志。";
  return "请求被 API 拒绝（HTTP " + status + "）。";
}

async function requestApi(
  path: string,
  options: { method?: "GET" | "POST"; token?: string; body?: JsonRecord } = {},
): Promise<unknown> {
  const controller = new AbortController();
  const timeout = globalThis.setTimeout(() => controller.abort(), 12_000);
  const headers: Record<string, string> = {
    Accept: "application/json",
  };

  if (options.token) headers.Authorization = "Bearer " + options.token;
  if (options.body) headers["Content-Type"] = "application/json";

  try {
    const response = await fetch(API_BASE_URL + path, {
      method: options.method || "GET",
      headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
      cache: "no-store",
      credentials: "omit",
      referrerPolicy: "no-referrer",
      signal: controller.signal,
    });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : undefined;

    if (!response.ok) throw new ApiError(response.status, httpErrorMessage(response.status));
    return payload;
  } finally {
    globalThis.clearTimeout(timeout);
  }
}

function readableError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof DOMException && error.name === "AbortError") {
    return "API 请求超时，请检查服务与网络。";
  }
  return "无法连接控制 API；请检查地址、HTTPS 与 CORS 白名单。";
}

function modeLabel(mode?: string): string {
  const labels: Record<string, string> = {
    paper: "模拟盘",
    shadow: "影子模式",
    canary: "金丝雀实盘",
    live: "受控实盘",
  };
  return mode ? labels[mode] || mode : "未知";
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
  if (!armedUntil || now === 0) return "无有效窗口";
  const remaining = new Date(armedUntil).getTime() - now;
  if (!Number.isFinite(remaining) || remaining <= 0) return "窗口已过期";
  const totalSeconds = Math.ceil(remaining / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes + " 分 " + String(seconds).padStart(2, "0") + " 秒";
}

function Metric({ label, value }: { label: string; value: string | number | undefined }) {
  return (
    <div className="metric">
      <dt>{label}</dt>
      <dd>{value ?? "—"}</dd>
    </div>
  );
}

export default function HomePage() {
  const [health, setHealth] = useState<HealthView>({ phase: "loading" });
  const [status, setStatus] = useState<StatusView | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [adminToken, setAdminToken] = useState("");
  const [armMinutes, setArmMinutes] = useState(5);
  const [riskConfirmed, setRiskConfirmed] = useState(false);
  const [busy, setBusy] = useState<BusyAction>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [lastCycle, setLastCycle] = useState<CycleView | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [now, setNow] = useState(0);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    const token = adminToken.trim();
    const healthPromise = requestApi(ENDPOINTS.health)
      .then((payload) => {
        const root = asRecord(payload) || {};
        const ok = root.ok === true;
        setHealth({
          phase: ok ? "online" : "degraded",
          store: asString(root.store),
          mode: asString(root.mode),
          realMoney: asBoolean(root.real_money),
          checkedAt: Date.now(),
        });
      })
      .catch(() => {
        setHealth({ phase: "offline", checkedAt: Date.now() });
      });

    const statusPromise = token
      ? requestApi(ENDPOINTS.status, { token })
          .then((payload) => {
            setStatus(parseStatus(payload));
            setStatusError(null);
          })
          .catch((error: unknown) => {
            setStatusError(readableError(error));
          })
      : Promise.resolve().then(() => {
          setStatus(null);
          setStatusError("输入管理员令牌后读取受保护的控制状态。");
        });

    await Promise.allSettled([healthPromise, statusPromise]);
    setRefreshing(false);
  }, [adminToken]);

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

  const configuredMode = status?.configuredMode || health.mode;
  const armableMode: LiveMode | null =
    configuredMode === "canary" || configuredMode === "live" ? configuredMode : null;
  const tokenReady = adminToken.trim().length > 0;
  const control = status?.control;
  const armedUntilMs = control?.armedUntil ? new Date(control.armedUntil).getTime() : 0;
  const effectivelyArmed = Boolean(
    control?.armed &&
      !control.killSwitch &&
      armedUntilMs > now &&
      (control.mode === "canary" || control.mode === "live"),
  );
  const apiUsesSafeTransport = useMemo(() => {
    return (
      API_BASE_URL.startsWith("https://") ||
      API_BASE_URL.startsWith("http://localhost") ||
      API_BASE_URL.startsWith("http://127.0.0.1")
    );
  }, []);

  async function runCycle() {
    if (!tokenReady) {
      setNotice({ tone: "error", text: "请先输入管理员令牌。" });
      return;
    }
    if (
      effectivelyArmed &&
      !window.confirm("当前实盘窗口处于解锁状态；运行周期可能提交真实资金订单。确认继续？")
    ) {
      return;
    }
    setBusy("cycle");
    setNotice({ tone: "info", text: "正在执行单个扫描周期…" });
    try {
      const payload = await requestApi(ENDPOINTS.cycle, {
        method: "POST",
        token: adminToken.trim(),
      });
      const cycle = parseCycle(payload);
      setLastCycle(cycle);
      setNotice({
        tone: "success",
        text: "周期已完成：扫描 " + (cycle.marketsScanned ?? 0) + " 个市场，执行 " + (cycle.executions ?? 0) + " 笔。",
      });
      await refresh();
    } catch (error) {
      setNotice({ tone: "error", text: readableError(error) });
    } finally {
      setBusy(null);
    }
  }

  async function armTrading() {
    if (!tokenReady || !armableMode || !riskConfirmed) return;
    const confirmed = window.confirm(
      "确认将 " + modeLabel(armableMode) + " 解锁 " + armMinutes + " 分钟？到期后应自动拒绝新交易。",
    );
    if (!confirmed) return;

    setBusy("arm");
    setNotice({ tone: "info", text: "正在申请短时实盘窗口…" });
    try {
      await requestApi(ENDPOINTS.arm, {
        method: "POST",
        token: adminToken.trim(),
        body: { mode: armableMode, minutes: armMinutes },
      });
      setRiskConfirmed(false);
      setNotice({
        tone: "success",
        text: modeLabel(armableMode) + " 已短时解锁 " + armMinutes + " 分钟。",
      });
      await refresh();
    } catch (error) {
      setNotice({ tone: "error", text: readableError(error) });
    } finally {
      setBusy(null);
    }
  }

  async function disarmTrading() {
    if (!tokenReady) {
      setNotice({ tone: "error", text: "请先输入管理员令牌。" });
      return;
    }
    setBusy("disarm");
    setNotice({ tone: "info", text: "正在停用交易并撤销挂单…" });
    try {
      await requestApi(ENDPOINTS.disarm, {
        method: "POST",
        token: adminToken.trim(),
      });
      setRiskConfirmed(false);
      setNotice({ tone: "success", text: "交易已停用，kill switch 已打开，并已请求撤销挂单。" });
      await refresh();
    } catch (error) {
      setNotice({ tone: "error", text: readableError(error) });
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
          <h1 style={{ fontSize: "24px", margin: 0 }}>控制台总览</h1>
        </div>
        <div className="api-address" title={API_BASE_URL}>
          <span className={"status-dot " + health.phase} />
          <span>{API_BASE_URL}</span>
        </div>
      </header>

      <section className="safety-banner" aria-label="安全边界">
        <div className="shield" aria-hidden="true">◆</div>
        <div>
          <strong>私钥隔离边界</strong>
          <p>
            本页永不请求、显示或保存钱包私钥、助记词及 CLOB 凭据。管理员令牌只保存在当前页面内存，刷新或关闭页面即清除。
          </p>
        </div>
      </section>

      {!apiUsesSafeTransport && (
        <div className="transport-warning" role="alert">
          当前 API 地址不是 HTTPS。除本机开发外，请先改用 Zeabur HTTPS 域名，再输入管理员令牌。
        </div>
      )}

      <section className="summary-grid" aria-label="运行摘要">
        <article className="summary-card">
          <div className="card-heading">
            <span>API / 数据库</span>
            <span className={"pill " + health.phase}>{healthText}</span>
          </div>
          <div className="primary-value">{health.store === "healthy" ? "连接正常" : health.phase === "online" ? "API 正常" : healthText}</div>
          <p className="muted">最近检查 {formatDate(health.checkedAt)}</p>
        </article>

        <article className="summary-card">
          <div className="card-heading">
            <span>运行模式</span>
            <span className={"mode-chip mode-" + (configuredMode || "unknown")}>
              {String(configuredMode || "UNKNOWN").toUpperCase()}
            </span>
          </div>
          <div className="primary-value">{modeLabel(configuredMode)}</div>
          <p className="muted">
            {status?.aiProvider || "AI 未知"} / {status?.forecastModel || "模型未返回"}
          </p>
        </article>

        <article className={"summary-card gate-card " + (effectivelyArmed ? "danger" : "safe")}>
          <div className="card-heading">
            <span>交易闸门</span>
            <span className={"pill " + (effectivelyArmed ? "armed" : "disarmed")}>
              {effectivelyArmed ? "已解锁" : "已锁定"}
            </span>
          </div>
          <div className="primary-value">
            {effectivelyArmed ? countdownLabel(control?.armedUntil, now) : "不会提交新实盘订单"}
          </div>
          <p className="muted">
            Kill switch：{control?.killSwitch === false ? "关闭" : "开启或未知"} · 控制版本 {control?.version ?? "—"}
          </p>
        </article>

        <article className="summary-card">
          <div className="card-heading">
            <span>状态同步</span>
            <button className="text-button" type="button" onClick={() => void refresh()} disabled={refreshing}>
              {refreshing ? "刷新中…" : "立即刷新"}
            </button>
          </div>
          <div className="primary-value">{statusError ? "读取失败" : "每 20 秒"}</div>
          <p className={statusError ? "error-text" : "muted"}>
            {statusError || "最近同步 " + formatDate(status?.fetchedAt)}
          </p>
        </article>
      </section>

      <div className="main-grid">
        <section className="panel controls-panel">
          <div className="section-heading">
            <div>
              <p className="eyebrow">MANUAL CONTROL</p>
              <h2>人工控制</h2>
            </div>
            <span className="read-only-chip">默认只读</span>
          </div>

          <label className="field-label" htmlFor="admin-token">管理员令牌</label>
          <div className="token-row">
            <input
              id="admin-token"
              type="password"
              value={adminToken}
              onChange={(event) => setAdminToken(event.target.value)}
              placeholder="仅在执行操作前输入"
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
            />
            <button
              className="secondary-button"
              type="button"
              onClick={() => {
                setAdminToken("");
                setRiskConfirmed(false);
                setNotice({ tone: "info", text: "页面内存中的管理员令牌已清除。" });
              }}
              disabled={!adminToken || busy !== null}
            >
              清除
            </button>
          </div>
          <p className="field-help">不写入 LocalStorage、Cookie、URL、日志或 Vercel 环境变量。</p>

          <div className="action-stack">
            <article className="action-card">
              <div>
                <h3>执行一个周期</h3>
                <p>立即扫描市场、生成预测、过风险闸门，并按当前模式处理执行。</p>
              </div>
              <button
                className="primary-button"
                type="button"
                onClick={() => void runCycle()}
                disabled={!tokenReady || busy !== null || !apiUsesSafeTransport}
              >
                {busy === "cycle" ? "执行中…" : "运行周期"}
              </button>
            </article>

            <article className="action-card arm-action">
              <div className="action-copy">
                <h3>短时解锁</h3>
                <p>
                  仅 canary/live 可用；最长 15 分钟。当前目标：
                  <strong>{armableMode ? modeLabel(armableMode) : "当前模式不可解锁"}</strong>
                </p>
              </div>
              <div className="arm-settings">
                <label htmlFor="arm-minutes">窗口</label>
                <select
                  id="arm-minutes"
                  value={armMinutes}
                  onChange={(event) => setArmMinutes(Number(event.target.value))}
                  disabled={busy !== null}
                >
                  <option value={1}>1 分钟</option>
                  <option value={3}>3 分钟</option>
                  <option value={5}>5 分钟</option>
                  <option value={10}>10 分钟</option>
                  <option value={15}>15 分钟</option>
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
                disabled={!tokenReady || !armableMode || !riskConfirmed || busy !== null || !apiUsesSafeTransport}
              >
                {busy === "arm" ? "解锁中…" : "短时解锁交易"}
              </button>
            </article>

            <article className="action-card stop-action">
              <div>
                <h3>立即停用</h3>
                <p>打开 kill switch、清除解锁窗口，并请求撤销当前挂单。</p>
              </div>
              <button
                className="danger-button"
                type="button"
                onClick={() => void disarmTrading()}
                disabled={!tokenReady || busy !== null || !apiUsesSafeTransport}
              >
                {busy === "disarm" ? "停用中…" : "停用并撤单"}
              </button>
            </article>
          </div>

          <div className="notice-slot" aria-live="polite" aria-atomic="true">
            {notice && <div className={"notice " + notice.tone}>{notice.text}</div>}
          </div>
        </section>

        <aside className="side-column">
          <section className="panel">
            <div className="section-heading compact">
              <div>
                <p className="eyebrow">RISK LIMITS</p>
                <h2>风险上限</h2>
              </div>
              <span className="version-label">API 实时值</span>
            </div>
            <dl className="risk-list">
              {(status?.riskLimits || RISK_LIMITS.map((item) => ({ key: item.key, label: item.label, value: "等待 API" }))).map((limit) => (
                <div className="risk-row" key={limit.key}>
                  <dt>{limit.label}</dt>
                  <dd>{limit.value}</dd>
                </div>
              ))}
            </dl>
            <p className="panel-note">这些值来自 Zeabur API；前端不允许修改风险配置。</p>
          </section>

          <section className="panel cycle-panel">
            <div className="section-heading compact">
              <div>
                <p className="eyebrow">LAST MANUAL CYCLE</p>
                <h2>最近手动周期</h2>
              </div>
              {lastCycle?.mode && <span className="mode-chip">{lastCycle.mode.toUpperCase()}</span>}
            </div>
            {lastCycle ? (
              <>
                <p className="run-id" title={lastCycle.runId}>{lastCycle.runId || "无运行 ID"}</p>
                <dl className="metrics-grid">
                  <Metric label="扫描" value={lastCycle.marketsScanned} />
                  <Metric label="预测" value={lastCycle.forecastsCreated} />
                  <Metric label="候选" value={lastCycle.candidatesCreated} />
                  <Metric label="批准" value={lastCycle.intentsApproved} />
                  <Metric label="执行" value={lastCycle.executions} />
                  <Metric label="跳过" value={lastCycle.skipped} />
                </dl>
                <p className="panel-note">完成于 {formatDate(lastCycle.completedAt)}</p>
              </>
            ) : (
              <div className="empty-state">
                <span aria-hidden="true">◎</span>
                <p>本页面尚未触发周期。</p>
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
              <div><dt>账户</dt><dd title={control?.accountId}>{control?.accountId || "—"}</dd></div>
              <div><dt>控制模式</dt><dd>{modeLabel(control?.mode)}</dd></div>
              <div><dt>解锁到期</dt><dd>{formatDate(control?.armedUntil)}</dd></div>
              <div><dt>接受新意图</dt><dd>{control?.acceptNewIntents === undefined ? "未返回" : control.acceptNewIntents ? "是" : "否"}</dd></div>
              <div><dt>更新时间</dt><dd>{formatDate(control?.updatedAt)}</dd></div>
            </dl>
          </section>
        </aside>
      </div>

      <footer>
        <span>控制面与签名器隔离</span>
        <span>·</span>
        <span>所有实盘操作仍受服务端模式、时限、风险闸门与地理限制约束</span>
      </footer>
    </main>
  );
}
