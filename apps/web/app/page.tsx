"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { API_BASE_URL, ApiError, apiRequest, readableApiError } from "../lib/api";
import ControlDetailsPanel from "../components/ControlDetailsPanel";
import LatestJobPanel from "../components/LatestJobPanel";
import NotificationsPanel from "../components/NotificationsPanel";
import RiskLimitsPanel from "../components/RiskLimitsPanel";
import ToastViewport, { useToasts } from "../components/Toast";
import {
  type BusyAction,
  type HealthView,
  type JobView,
  type LiveMode,
  type Notice,
  type NotificationView,
  type RiskLimitView,
  type StatusView,
  RISK_LIMITS,
  asRecord,
  asString,
  countdownLabel,
  formatDate,
  jobCompletionText,
  modeLabel,
  parseJob,
  parseNotifications,
  parseStatus,
  tradingGateState,
} from "../lib/dashboard";

const EquityChart = dynamic(() => import("../components/EquityChart"), {
  ssr: false,
});

export default function HomePage() {
  const [health, setHealth] = useState<HealthView>({ phase: "loading" });
  const [status, setStatus] = useState<StatusView | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [busy, setBusy] = useState<BusyAction>(null);
  const [stopping, setStopping] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [lastJob, setLastJob] = useState<JobView | null>(null);
  const [riskConfirmed, setRiskConfirmed] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [now, setNow] = useState(0);
  const [notifications, setNotifications] = useState<NotificationView[]>([]);
  const { toasts, push: pushToast, dismiss: dismissToast } = useToasts();
  const cycleIdempotencyKey = useRef<string | null>(null);
  const personalRequestId = useRef<string | null>(null);
  const cycleEpoch = useRef(0);
  const actionEpoch = useRef(0);
  const refreshSequence = useRef(0);
  const stopInFlight = useRef<Promise<void> | null>(null);

  const refresh = useCallback(async () => {
    const sequence = ++refreshSequence.current;
    const epoch = actionEpoch.current;
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
        if (sequence !== refreshSequence.current || epoch !== actionEpoch.current) return;
        const parsed = parseStatus(data);
        setStatus(parsed);
        if (parsed.latestJob && personalRequestId.current === null) setLastJob(parsed.latestJob);
        setStatusError(null);
      })
      .catch((error) => {
        if (sequence === refreshSequence.current && epoch === actionEpoch.current) setStatusError(readableApiError(error));
      });

    const notificationsPromise = apiRequest<unknown>("/v1/me/notifications?limit=8")
      .then(({ data }) => setNotifications(parseNotifications(data)))
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
    const requestId = personalRequestId.current;
    if (!requestId || !["queued", "pending", "running", "claimed"].includes(lastJob?.status ?? "")) return;
    let cancelled = false;
    let polling = false;
    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const result = await apiRequest<unknown>(`/v1/personal/cycles/${encodeURIComponent(requestId)}`);
        if (cancelled || personalRequestId.current !== requestId) return;
        const job = parseJob(result.data);
        setLastJob(job);
        if (["succeeded", "failed", "cancelled"].includes(job.status ?? "")) {
          personalRequestId.current = null;
          if (cycleEpoch.current === actionEpoch.current) {
            const completed: Notice = job.status === "succeeded"
              ? { tone: "success", text: jobCompletionText(job) }
              : { tone: "error", text: job.message ?? "周期执行失败。" };
            setNotice(completed);
            pushToast(completed.text, completed.tone);
          }
          await refresh();
        }
      } catch (error) {
        // A queued request keeps its identity through transient polling failures.
        if (!cancelled && error instanceof ApiError && error.status === 404) {
          personalRequestId.current = null;
          setLastJob((previous) => previous ? { ...previous, status: "unknown", message: "服务重启或请求记录过期，无法确认此请求结果；请核对最近周期。" } : previous);
        }
      } finally { polling = false; }
    };
    const timer = globalThis.setInterval(() => void poll(), 3_000);
    void poll();
    return () => { cancelled = true; globalThis.clearInterval(timer); };
  }, [lastJob?.id, lastJob?.status, pushToast, refresh]);

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
      const failure: Notice = { tone: "error", text: readableApiError(error) };
      setNotice(failure);
      pushToast(failure.text, failure.tone);
    }
  }

  const configuredMode = status?.configuredMode;
  const armableMode: LiveMode | null =
    configuredMode === "canary" || configuredMode === "live"
      ? configuredMode
      : null;
  const control = status?.control;
  const personalRealMode = Boolean(status?.personalEnabled && armableMode);
  const personalPaused = status?.personalPaused === true;
  const gate = tradingGateState(status, now, Boolean(statusError));
  const stateFresh = gate !== "unknown" && gate !== "stale";
  const effectivelyArmed = gate === "armed";
  const gateLabel = { armed: "授权有效", locked: "已确认锁定", stale: "状态已过期", unknown: "状态未确认" }[gate];
  const gateText = gate === "armed" ? countdownLabel(control?.armedUntil, now)
    : gate === "locked" ? "已禁止新的实盘意图"
    : "无法确认后台是否仍在交易";
  const apiUsesSafeTransport = useMemo(
    () =>
      API_BASE_URL.startsWith("https://") ||
      API_BASE_URL.startsWith("http://localhost") ||
      API_BASE_URL.startsWith("http://127.0.0.1"),
    [],
  );
  const riskLimits: RiskLimitView[] = useMemo(
    () =>
      status?.riskLimits ??
      RISK_LIMITS.map((item) => ({
        key: item.key,
        label: item.label,
        value: "等待 API",
      })),
    [status?.riskLimits],
  );

  async function runCycle() {
    if (
      effectivelyArmed &&
      !window.confirm("当前实盘窗口已解锁，本任务可能提交真实资金订单。确认继续？")
    ) {
      return;
    }
    if (!status || !stateFresh || status.modeApplied === false || stopping) return;
    const epoch = actionEpoch.current;
    cycleEpoch.current = epoch;
    setBusy("cycle");
    const startingNotice: Notice = { tone: "info", text: "正在启动个人运行周期…" };
    setNotice(startingNotice);
    pushToast(startingNotice.text, startingNotice.tone);
    try {
      cycleIdempotencyKey.current ??= crypto.randomUUID();
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
      personalRequestId.current = job.id ?? null;
      setLastJob(job);
      if (epoch !== actionEpoch.current) return;
      const settledNotice: Notice = {
        tone: "success",
        text:
          result.status === 202
            ? `周期已交给个人 Worker${job.id ? `（${job.id}）` : ""}，可以继续使用控制台。`
            : jobCompletionText(job),
      };
      setNotice(settledNotice);
      pushToast(settledNotice.text, settledNotice.tone);
      await refresh();
    } catch (error) {
      if (epoch !== actionEpoch.current) return;
      const failure: Notice = { tone: "error", text: readableApiError(error) };
      setNotice(failure);
      pushToast(failure.text, failure.tone);
    } finally {
      setBusy(null);
    }
  }

  async function armTrading() {
    if (!armableMode || !riskConfirmed || !control?.version || !stateFresh || stopping || status?.modeApplied === false) return;
    if (
      !window.confirm(
        `确认恢复${modeLabel(armableMode)}自动运行？它会持续运行，直到你点击“立即停用”或关闭 Zeabur 实盘开关。`,
      )
    ) {
      return;
    }
    const epoch = actionEpoch.current;
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
      if (epoch !== actionEpoch.current) {
        // A stop was requested while resume was outstanding. Reassert it after
        // the late response, even if the first stop has already completed.
        await stopInFlight.current;
        await disarmTrading();
        return;
      }
      setRiskConfirmed(false);
      const armed: Notice = {
        tone: "success",
        text: `${modeLabel(armableMode)}恢复请求已交给个人 Worker；内部短期授权会由它自动续期。`,
      };
      setNotice(armed);
      pushToast(armed.text, armed.tone);
      await refresh();
    } catch (error) {
      if (epoch !== actionEpoch.current) { await stopInFlight.current; await disarmTrading(); return; }
      const failure: Notice = { tone: "error", text: readableApiError(error) };
      setNotice(failure);
      pushToast(failure.text, failure.tone);
    } finally {
      setBusy(null);
    }
  }

  async function disarmTrading(): Promise<void> {
    if (stopInFlight.current) return stopInFlight.current;
    actionEpoch.current += 1;
    setStopping(true);
    setRiskConfirmed(false);
    const operation = (async () => {
      const pending: Notice = { tone: "info", text: "停用请求正在发送；等待后端确认禁止新单及撤单结果…" };
      setNotice(pending);
      pushToast(pending.text, pending.tone);
      try {
        const result = await apiRequest<unknown>("/v1/control/disarm", {
          method: "POST", body: {}, idempotencyKey: crypto.randomUUID(),
        });
        const response = asRecord(result.data);
        const cancellationPending = result.status === 202 || response.cancellation_pending !== false || response.cancellation_verified !== true;
        const stopped: Notice = {
          tone: cancellationPending ? "info" : "success",
          text: cancellationPending
            ? "停用已受理，正在核实撤单与未决成交；请等待后台确认。停用不会平掉已有仓位。"
            : "后端已确认停用及撤单。已有持仓仍需单独管理。",
        };
        setNotice(stopped);
        pushToast(stopped.text, stopped.tone);
        await refresh();
      } catch (error) {
        const failure: Notice = { tone: "error", text: `无法确认停用成功：${readableApiError(error)}` };
        setNotice(failure);
        pushToast(failure.text, failure.tone);
      }
    })();
    stopInFlight.current = operation;
    try { await operation; } finally { stopInFlight.current = null; setStopping(false); }
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
          <div className="primary-value">{modeLabel(configuredMode)}{!stateFresh ? "（待确认）" : ""}</div>
          {status?.desiredMode && (status.modeApplied === false || status.desiredMode !== configuredMode) && <p className="muted">目标：{modeLabel(status.desiredMode)} · 等待 Worker 确认</p>}
          <p className="muted">
            {status?.aiProvider ?? "AI 未配置"} / {status?.forecastModel ?? "模型未返回"}
          </p>
        </article>

        <article className={`summary-card ${gate === "armed" ? "danger" : gate === "locked" ? "safe" : ""}`}>
          <div className="card-heading">
            <span>交易闸门</span>
            <span className={`pill ${gate === "armed" ? "armed" : gate === "locked" ? "disarmed" : "degraded"}`}>
              {gateLabel}
            </span>
          </div>
          <div className="primary-value">
            {gateText}
          </div>
          <p className="muted">
            最后确认 {formatDate(status?.fetchedAt)}；Kill switch：{stateFresh ? control?.killSwitch === true ? "开启" : "关闭" : "未知"}
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

      <EquityChart mode={configuredMode} />

      <NotificationsPanel
        notifications={notifications}
        onMarkRead={(id) => void markNotificationRead(id)}
      />

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
                disabled={busy !== null || stopping || !stateFresh || status?.modeApplied === false || !apiUsesSafeTransport}
              >
                {busy === "cycle" ? "创建中…" : "运行周期"}
              </button>
            </article>

            {!stateFresh || status?.modeApplied === false ? (
              <article className="action-card"><div><h3>等待确认运行状态</h3><p>实际模式与交易授权确认后才可恢复交易。紧急停用始终可用。</p></div></article>
            ) : status?.personalEnabled && !status.liveSupported ? (
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
                  <p>后端已确认 Paper / Shadow；模式变更以 Worker 实际确认结果为准。</p>
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
                    disabled={!armableMode || busy !== null || stopping || !stateFresh}
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
                    busy !== null || stopping || !stateFresh ||
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
                    : "向后端请求禁止新实盘订单并核实撤单；即使当前状态未知也可以操作。"}
                </p>
              </div>
              <button
                className="danger-button" type="button" onClick={() => void disarmTrading()}
                disabled={stopping || !apiUsesSafeTransport}
              >{stopping ? "停用中…" : "停用并撤单"}</button>
            </article>
          </div>

          <div className="notice-slot" aria-live="polite">
            {notice && <div className={`notice ${notice.tone}`}>{notice.text}</div>}
          </div>
        </section>

        <aside className="side-column">
          <RiskLimitsPanel limits={riskLimits} confirmed={stateFresh && status?.riskApplied === true} />

          <LatestJobPanel job={lastJob} />

          <ControlDetailsPanel control={control} />
        </aside>
      </div>

      <ToastViewport toasts={toasts} onDismiss={dismissToast} />
    </main>
  );
}
