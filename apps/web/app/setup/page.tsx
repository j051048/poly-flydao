"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, apiRequest, readableApiError } from "../../lib/api";
import { parsePersonalRuntimeStatus } from "../../lib/personal-runtime";
import {
  buildSetupSteps,
  setupProgress,
  type SetupStep,
} from "../../lib/readiness";

interface Snapshot {
  health?: unknown;
  personal?: unknown;
  status?: unknown;
}

const STATE_LABELS: Record<SetupStep["state"], string> = {
  done: "已完成",
  todo: "下一步",
  working: "处理中",
  blocked: "需修复",
};

export default function SetupPage() {
  const [snapshot, setSnapshot] = useState<Snapshot>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [firstRunBusy, setFirstRunBusy] = useState(false);
  const [firstRunMessage, setFirstRunMessage] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    const [healthResult, personalResult, statusResult] =
      await Promise.allSettled([
        apiRequest<unknown>("/health", { authenticated: false }),
        apiRequest<unknown>("/v1/personal/status"),
        apiRequest<unknown>("/v1/status"),
      ]);

    const next: Snapshot = {};
    if (healthResult.status === "fulfilled") {
      next.health = healthResult.value.data;
    }
    if (statusResult.status === "fulfilled") {
      next.status = statusResult.value.data;
    }
    if (personalResult.status === "fulfilled") {
      next.personal = personalResult.value.data;
    } else if (
      personalResult.reason instanceof ApiError &&
      personalResult.reason.status === 404 &&
      statusResult.status === "fulfilled"
    ) {
      next.personal = statusResult.value.data;
    }

    const failure = [
      healthResult,
      statusResult,
      ...(next.personal ? [] : [personalResult]),
    ].find(
      (result): result is PromiseRejectedResult => result.status === "rejected",
    );
    if (failure) setError(readableApiError(failure.reason));
    setSnapshot(next);
    setLoading(false);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const steps = useMemo(() => buildSetupSteps(snapshot), [snapshot]);
  const progress = setupProgress(steps);
  const nextStep = steps.find((step) => step.state !== "done");
  const completedSteps = steps.filter((step) => step.state === "done").length;
  const personalStatus = parsePersonalRuntimeStatus(snapshot.personal);
  const paperMode = personalStatus.mode === "paper";
  const aiReady = steps.some(
    (step) => step.key === "environment" && step.state === "done",
  );

  async function runFirstPaperCycle() {
    const cycleCountBefore = parsePersonalRuntimeStatus(
      snapshot.personal,
    ).cycleCount;
    setFirstRunBusy(true);
    setFirstRunMessage("正在创建首次 Paper 模拟任务…");
    setError(null);
    try {
      const queued = await apiRequest<unknown>("/v1/personal/cycles/run", {
        method: "POST",
        body: { mode: "paper" },
        idempotencyKey: `setup-paper:${crypto.randomUUID()}`,
      });
      const payload = queued.data as Record<string, unknown>;
      const jobId = String(payload.id ?? payload.job_id ?? "");
      if (!jobId) {
        for (let attempt = 0; attempt < 45; attempt += 1) {
          await new Promise((resolve) => window.setTimeout(resolve, 2000));
          const response = await apiRequest<unknown>("/v1/personal/status");
          const current = parsePersonalRuntimeStatus(response.data);
          setSnapshot((previous) => ({ ...previous, personal: response.data }));
          if (
            current.cycleCount > cycleCountBefore &&
            current.lastCycle?.state === "succeeded"
          ) {
            setFirstRunMessage("首次 Paper 周期完成，可以进入控制台查看结果。");
            await refresh();
            return;
          }
          if (
            current.cycleCount > cycleCountBefore &&
            current.lastCycle?.state === "failed"
          ) {
            throw new Error(current.lastCycle.message ?? "首次 Paper 周期失败。");
          }
        }
        setFirstRunMessage("周期仍在运行，稍后点“重新检查”即可，不必停留在本页。");
        return;
      }

      for (let attempt = 0; attempt < 60; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 2000));
        const response = await apiRequest<unknown>(`/v1/jobs/${jobId}`);
        const job = response.data as Record<string, unknown>;
        if (job.status === "succeeded") {
          setFirstRunMessage("首次 Paper 周期完成，可以进入控制台查看结果。");
          await refresh();
          return;
        }
        if (job.status === "failed") {
          throw new Error(`首次任务失败：${String(job.error_code || "unknown")}`);
        }
      }
      setFirstRunMessage("任务仍在运行，稍后可在控制台查看，不必停留在本页。");
    } catch (caught) {
      setError(readableApiError(caught));
      setFirstRunMessage(null);
    } finally {
      setFirstRunBusy(false);
    }
  }

  return (
    <main className="page-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">PERSONAL START</p>
          <h1>个人启动向导</h1>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
        >
          {loading ? "检查中…" : "重新检查"}
        </button>
      </header>

      <section className="onboarding-hero">
        <div>
          <span className="onboarding-kicker">一次只做一件事</span>
          <h2>{nextStep ? `现在只做：${nextStep.title}` : "个人机器人已经就绪"}</h2>
          <p>
            {nextStep
              ? nextStep.description
              : "AI、常驻 Worker 和 Paper 自动周期都已准备好，可以进入控制台。"}
          </p>
          {nextStep?.key === "paper" && paperMode ? (
            <button
              className="primary-button onboarding-primary-action"
              type="button"
              onClick={() => void runFirstPaperCycle()}
              disabled={
                firstRunBusy ||
                loading ||
                !aiReady ||
                !personalStatus.workerReady ||
                nextStep.state === "working"
              }
            >
              {firstRunBusy || nextStep.state === "working"
                ? "Paper 模拟运行中…"
                : "运行一次安全模拟"}
            </button>
          ) : nextStep ? (
            <Link
              className="primary-button onboarding-primary-action"
              href={nextStep.href}
            >
              {nextStep.actionLabel}
            </Link>
          ) : (
            <Link className="primary-button onboarding-primary-action" href="/">
              进入控制台
            </Link>
          )}
          {nextStep && (
            <p className="field-help">完成后回来点“重新检查”，系统会自动推进。</p>
          )}
          {firstRunMessage && <p className="field-help">{firstRunMessage}</p>}
        </div>
        <div
          className="progress-ring"
          style={{ "--progress": `${progress}%` } as React.CSSProperties}
        >
          <strong>{progress}%</strong>
          <span>个人启动</span>
        </div>
      </section>

      {error && (
        <div className="notice error page-notice" role="alert">
          状态读取失败：{error}
        </div>
      )}

      <details className="setup-checklist" open>
        <summary>
          <span>四步启动清单</span>
          <strong>{completedSteps} / {steps.length} 已完成</strong>
        </summary>
        <section className="setup-steps" aria-label="个人启动步骤">
          {steps.map((step, index) => (
            <article className={`setup-step ${step.state}`} key={step.key}>
              <div className="step-index" aria-hidden="true">
                {step.state === "done" ? "✓" : index + 1}
              </div>
              <div className="step-copy">
                <div className="step-title-row">
                  <h2>{step.title}</h2>
                  <span className={`pill setup-${step.state}`}>
                    {STATE_LABELS[step.state]}
                  </span>
                </div>
                <p>{step.description}</p>
              </div>
              {step.state !== "done" && step.key !== "paper" && (
                <Link className="secondary-button step-action" href={step.href}>
                  {step.actionLabel}
                </Link>
              )}
            </article>
          ))}
        </section>
      </details>

      <details className="panel mode-explainer">
        <summary className="mode-explainer-summary">
          <div>
            <p className="eyebrow">MODE LADDER</p>
            <h2>个人版支持哪些模式？</h2>
          </div>
          <span className="read-only-chip">点击展开</span>
        </summary>
        <div className="mode-ladder">
          <div><strong>Paper</strong><span>模拟成交，首次运行用它</span></div>
          <div><strong>Shadow</strong><span>实时分析，但不发送订单</span></div>
          <div><strong>Canary</strong><span>显式开启后的小额真钱模式</span></div>
          <div><strong>Live</strong><span>显式开启后的完整实盘模式</span></div>
        </div>
        <p className="panel-note">
          默认只允许 Paper / Shadow；配置 POLYBOT_PERSONAL_LIVE_ENABLED=true
          且通过启动检查后才可使用 Canary / Live。自动执行不等于保证盈利。
        </p>
      </details>
    </main>
  );
}
