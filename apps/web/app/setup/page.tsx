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
  const progress = setupProgress(steps);
  const nextStep = steps.find((step) => step.state !== "done");
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
          auto_run_enabled: true,
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
          <span className="onboarding-kicker">推荐先模拟，后入金</span>
          <h2>{nextStep ? `下一步：${nextStep.title}` : "基础设置已完成"}</h2>
          <p>
            你不需要先理解三端架构。按下面顺序完成即可；系统在缺少任何关键条件时都会拒绝真实订单。
          </p>
          <button
            className="primary-button"
            type="button"
            onClick={() => void runFirstPaperCycle()}
            disabled={firstRunBusy || loading || !snapshot.me || !aiReady}
          >
            {firstRunBusy ? "首次模拟运行中…" : "一键启动首次 Paper 模拟"}
          </button>
          {!loading && !aiReady && (
            <p className="field-help">先完成下方“连接你的 AI”，按钮才会启用。</p>
          )}
          {firstRunMessage && <p className="field-help">{firstRunMessage}</p>}
        </div>
        <div className="progress-ring" style={{ "--progress": `${progress}%` } as React.CSSProperties}>
          <strong>{progress}%</strong>
          <span>完成度</span>
        </div>
      </section>

      {error && (
        <div className="notice error page-notice" role="alert">
          部分状态读取失败：{error}
        </div>
      )}

      <section className="setup-steps" aria-label="启动步骤">
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
            {step.state !== "done" && (
              <Link className="secondary-button step-action" href={step.href}>
                {step.actionLabel}
              </Link>
            )}
          </article>
        ))}
      </section>

      <section className="panel mode-explainer">
        <div className="section-heading">
          <div>
            <p className="eyebrow">MODE LADDER</p>
            <h2>四种模式怎么选</h2>
          </div>
          <span className="read-only-chip">逐级升级</span>
        </div>
        <div className="mode-ladder">
          <div><strong>Paper</strong><span>模拟成交，适合首次运行</span></div>
          <div><strong>Shadow</strong><span>跟随实时盘口，但不发送订单</span></div>
          <div><strong>Canary</strong><span>小额真钱，单笔硬上限 $5</span></div>
          <div><strong>Live</strong><span>真实资金；必须经过长期验证</span></div>
        </div>
        <p className="panel-note">
          自动化只能稳定执行规则，不能保证盈利。是否升级应看费用后净收益、最大回撤和样本外表现，而不是只看胜率。
        </p>
      </section>
    </main>
  );
}
