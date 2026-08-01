"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { apiRequest, readableApiError } from "../../lib/api";
import {
  buildSetupSteps,
  setupProgress,
  type SetupStep,
} from "../../lib/readiness";

interface Snapshot {
  health?: unknown;
  me?: unknown;
  credentials?: unknown;
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
    const requests = await Promise.allSettled([
      apiRequest<unknown>("/health", { authenticated: false }),
      apiRequest<unknown>("/v1/me"),
      apiRequest<unknown>("/v1/me/credentials/status"),
      apiRequest<unknown>("/v1/status"),
    ]);
    const next: Snapshot = {};
    const keys: Array<keyof Snapshot> = [
      "health",
      "me",
      "credentials",
      "status",
    ];
    requests.forEach((result, index) => {
      if (result.status === "fulfilled") next[keys[index]] = result.value.data;
    });
    const failure = requests.find(
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
  const coreSteps = steps.filter((step) => !step.optional);
  const progress = setupProgress(steps);
  const nextStep = coreSteps.find((step) => step.state !== "done");
  const completedCoreSteps = coreSteps.filter(
    (step) => step.state === "done",
  ).length;
  const aiReady = steps.some((step) => step.key === "ai" && step.state === "done");

  async function runFirstPaperCycle() {
    const me = snapshot.me as Record<string, unknown> | undefined;
    const profile = me?.runtime_profile as Record<string, unknown> | undefined;
    if (!profile) {
      setError("请先完成登录并刷新状态。");
      return;
    }
    setFirstRunBusy(true);
    setFirstRunMessage("正在切换安全模拟模式并创建首次任务…");
    try {
      await apiRequest("/v1/me/runtime-profile", {
        method: "PUT",
        body: {
          expected_version: Number(profile.version) || 1,
          ai_provider: String(profile.ai_provider || "mock"),
          ai_base_url: profile.ai_base_url ?? null,
          forecast_model: String(profile.forecast_model || "gpt-5.6-terra"),
          ai_credential_id: profile.ai_credential_id ?? null,
          trading_wallet_id: profile.trading_wallet_id ?? null,
          risk_policy_id: profile.risk_policy_id ?? null,
          desired_mode: "paper",
          auto_run_enabled: false,
          cycle_interval_seconds: 300,
        },
      });
      const queued = await apiRequest<unknown>("/v1/jobs/cycles", {
        method: "POST",
        body: { mode: "paper" },
        idempotencyKey: `setup-paper:${crypto.randomUUID()}`,
      });
      const jobId = String((queued.data as Record<string, unknown>).id || "");
      if (!jobId) throw new Error("任务创建成功但没有返回 ID。");
      for (let attempt = 0; attempt < 60; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 2000));
        const response = await apiRequest<unknown>(`/v1/jobs/${jobId}`);
        const job = response.data as Record<string, unknown>;
        if (job.status === "succeeded") {
          setFirstRunMessage("首次 Paper 周期完成。现在可以查看分析与模拟资产。");
          await refresh();
          return;
        }
        if (job.status === "failed") {
          throw new Error(`首次任务失败：${String(job.error_code || "unknown")}`);
        }
      }
      throw new Error("任务仍在排队，请到部署诊断检查 Worker 心跳。");
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
          <p className="eyebrow">GUIDED START</p>
          <h1>新手启动向导</h1>
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
          <h2>{nextStep ? `现在只做：${nextStep.title}` : "安全启动已完成"}</h2>
          <p>
            {nextStep
              ? nextStep.description
              : "Paper 自动周期已经准备好；专属钱包是可选项，等你准备小额实盘时再配置。"}
          </p>
          {nextStep?.key === "paper" ? (
            <button
              className="primary-button onboarding-primary-action"
              type="button"
              onClick={() => void runFirstPaperCycle()}
              disabled={
                firstRunBusy ||
                loading ||
                !snapshot.me ||
                !aiReady ||
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
            <p className="field-help">完成后回到这里，系统会自动给出下一步。</p>
          )}
          {firstRunMessage && <p className="field-help">{firstRunMessage}</p>}
        </div>
        <div className="progress-ring" style={{ "--progress": `${progress}%` } as React.CSSProperties}>
          <strong>{progress}%</strong>
          <span>安全启动</span>
        </div>
      </section>

      {error && (
        <div className="notice error page-notice" role="alert">
          部分状态读取失败：{error}
        </div>
      )}

      <details className="setup-checklist">
        <summary>
          <span>查看完整启动清单</span>
          <strong>{completedCoreSteps} / {coreSteps.length} 个必需步骤</strong>
        </summary>
        <section className="setup-steps" aria-label="启动步骤">
          {steps.map((step, index) => (
            <article
              className={`setup-step ${step.state} ${step.optional ? "optional" : ""}`}
              key={step.key}
            >
              <div className="step-index" aria-hidden="true">
                {step.state === "done" ? "✓" : index + 1}
              </div>
              <div className="step-copy">
                <div className="step-title-row">
                  <h2>{step.title}</h2>
                  <span className={`pill setup-${step.state}`}>
                    {step.optional && step.state === "todo"
                      ? "实盘时再做"
                      : STATE_LABELS[step.state]}
                  </span>
                </div>
                <p>{step.description}</p>
              </div>
              {step.state !== "done" && (
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
            <h2>以后想升级真钱？先了解四种模式</h2>
          </div>
          <span className="read-only-chip">点击展开</span>
        </summary>
        <div className="mode-ladder">
          <div><strong>Paper</strong><span>模拟成交，适合首次运行</span></div>
          <div><strong>Shadow</strong><span>跟随实时盘口，但不发送订单</span></div>
          <div><strong>Canary</strong><span>小额真钱，单笔硬上限 $5</span></div>
          <div><strong>Live</strong><span>真实资金；必须经过长期验证</span></div>
        </div>
        <p className="panel-note">
          自动化只能稳定执行规则，不能保证盈利。是否升级应看费用后净收益、最大回撤和样本外表现，而不是只看胜率。
        </p>
      </details>
    </main>
  );
}
