"use client";

import type { Session } from "@supabase/supabase-js";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ApiError, apiRequest, readableApiError } from "../../lib/api";
import {
  maskWalletAddress,
  parsePersonalRuntimeStatus,
  personalModeLabel,
  type PersonalRuntimeStatus,
  type PersonalTradingMode,
} from "../../lib/personal-runtime";
import { getSupabaseBrowserClient } from "../../lib/supabase/browser";

type Notice = { tone: "success" | "error" | "info"; text: string };

const MODES: Array<{
  value: PersonalTradingMode;
  name: string;
  description: string;
}> = [
  { value: "paper", name: "Paper", description: "模拟成交，不动真钱" },
  { value: "shadow", name: "Shadow", description: "看实时盘口，不发订单" },
  { value: "canary", name: "Canary", description: "极小额真实订单" },
  { value: "live", name: "Live", description: "按环境风控执行实盘" },
];

type RiskPreset = "conservative" | "balanced" | "advanced";

const RISK_PRESETS: Array<{
  value: RiskPreset;
  name: string;
  description: string;
  summary: string;
}> = [
  {
    value: "conservative",
    name: "保守",
    description: "单笔 2 USD，日损 1%，回撤 4%，需要 ≥6% 净优势",
    summary: "适合验证期与小额资金",
  },
  {
    value: "balanced",
    name: "均衡",
    description: "单笔 5 USD，日损 2%，回撤 8%，需要 ≥4% 净优势",
    summary: "默认档位，适合常规运行",
  },
  {
    value: "advanced",
    name: "进取",
    description: "单笔 10 USD，日损 3%，回撤 10%，需要 ≥3% 净优势",
    summary: "仅在你充分理解回撤风险后使用",
  },
];

async function fetchPersonalStatus(): Promise<PersonalRuntimeStatus> {
  try {
    const response = await apiRequest<unknown>("/v1/personal/status");
    return parsePersonalRuntimeStatus(response.data);
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 404) throw error;
    const fallback = await apiRequest<unknown>("/v1/status");
    return parsePersonalRuntimeStatus(fallback.data);
  }
}

export default function SettingsPage() {
  const [status, setStatus] = useState<PersonalRuntimeStatus | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [apiOnline, setApiOnline] = useState(false);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [accountCopied, setAccountCopied] = useState(false);
  const [profileVersion, setProfileVersion] = useState<number | null>(null);
  const [presetBusy, setPresetBusy] = useState<RiskPreset | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setNotice(null);

    const supabase = getSupabaseBrowserClient();
    const [healthResult, statusResult, sessionResult] = await Promise.allSettled([
      apiRequest<unknown>("/health", { authenticated: false }),
      fetchPersonalStatus(),
      supabase ? supabase.auth.getSession() : Promise.resolve(null),
    ]);
    const meResult = await Promise.allSettled([apiRequest<unknown>("/v1/me")]);

    if (healthResult.status === "fulfilled") {
      const health = healthResult.value.data as Record<string, unknown>;
      setApiOnline(health.ok === true);
    } else {
      setApiOnline(false);
    }

    if (statusResult.status === "fulfilled") {
      setStatus(statusResult.value);
    } else {
      setStatus(null);
      setNotice({ tone: "error", text: readableApiError(statusResult.reason) });
    }

    if (sessionResult.status === "fulfilled" && sessionResult.value) {
      setSession(sessionResult.value.data.session);
    } else {
      setSession(null);
    }

    const me = meResult[0];
    if (me.status === "fulfilled") {
      const profile = (me.value.data as { runtime_profile?: Record<string, unknown> })
        ?.runtime_profile;
      const version = Number(profile?.risk_policy_version);
      setProfileVersion(Number.isInteger(version) && version > 0 ? version : null);
    } else {
      setProfileVersion(null);
    }

    setLoading(false);
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const realMoneyMode =
    status?.mode === "canary" || status?.mode === "live";
  const liveBalance = Number(status?.wallet.collateralBalancePusd);
  const hasLiveBalance = Number.isFinite(liveBalance) && liveBalance > 0;
  const liveWalletReady = Boolean(
    status?.wallet.bound &&
      !status.wallet.paused &&
      hasLiveBalance &&
      status.wallet.allowancesReady,
  );
  const walletStepReady = realMoneyMode
    ? liveWalletReady
    : true;
  const walletStatusText = !realMoneyMode
    ? status?.wallet.configured
      ? `已预填（${maskWalletAddress(status.wallet.address)}）`
      : "Paper / Shadow 暂不需要"
    : status?.wallet.paused
      ? "已暂停"
      : !status?.wallet.configured
        ? "缺少私钥变量"
        : !status.wallet.bound
          ? "等待 Worker 绑定"
          : !hasLiveBalance
            ? "等待余额检查或小额入金"
            : !status.wallet.allowancesReady
              ? "等待交易授权"
              : `${status.wallet.collateralBalancePusd} pUSD 可用`;

  const readyCount = [
    apiOnline,
    status?.enabled === true,
    status?.ai.configured === true,
    walletStepReady,
    status?.workerReady === true,
    status?.autoRunEnabled === true,
  ].filter(Boolean).length;

  async function applyRiskPreset(preset: RiskPreset) {
    if (!profileVersion) {
      setNotice({ tone: "error", text: "无法读取当前风控版本，请先确认 API 可用。" });
      return;
    }
    setPresetBusy(preset);
    setNotice(null);
    try {
      await apiRequest("/v1/me/risk-policy", {
        method: "PUT",
        body: {
          expected_profile_version: profileVersion,
          preset,
        },
      });
      setNotice({
        tone: "success",
        text: `已应用「${RISK_PRESETS.find((item) => item.value === preset)?.name ?? preset}」风控档位。`,
      });
      await refresh();
    } catch (error) {
      setNotice({ tone: "error", text: readableApiError(error) });
    } finally {
      setPresetBusy(null);
    }
  }

  async function copyAccountId() {
    const accountId = session?.user.id;
    if (!accountId) return;
    try {
      await navigator.clipboard.writeText(accountId);
      setAccountCopied(true);
      window.setTimeout(() => setAccountCopied(false), 1800);
    } catch {
      setNotice({ tone: "info", text: `POLYBOT_ACCOUNT_ID=${accountId}` });
    }
  }

  return (
    <main className="page-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">PERSONAL MODE</p>
          <h1>个人设置</h1>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={() => void refresh()}
          disabled={loading}
        >
          {loading ? "正在检查…" : "刷新状态"}
        </button>
      </header>

      <section className="personal-mode-banner">
        <div className="personal-mode-icon" aria-hidden="true">1</div>
        <div>
          <span className="onboarding-kicker">一套部署 · 一位使用者</span>
          <h2>密钥只在 Zeabur 环境变量里填写</h2>
          <p>
            网页不再接收 AI API Key 或 EVM 私钥。保存变量并重新部署后，
            这里只显示“是否就绪”和公开钱包地址，绝不会读取或回显密钥原文。
          </p>
        </div>
        <span className={`pill ${status?.enabled ? "online" : "offline"}`}>
          {status?.enabled ? "个人模式已开启" : "等待后端配置"}
        </span>
      </section>

      <section className="configuration-path" aria-label="个人模式配置顺序">
        <strong>只需四步</strong>
        <span>1. Zeabur 填变量</span>
        <span>2. 重新部署</span>
        <span>3. 回来刷新</span>
        <span>4. 先跑 Paper</span>
      </section>

      {notice && (
        <div
          className={`notice ${notice.tone} page-notice`}
          role={notice.tone === "error" ? "alert" : "status"}
        >
          {notice.text} {notice.tone === "error" && (
            <Link href="/diagnostics">打开三端部署检查 →</Link>
          )}
        </div>
      )}

      <section className="panel personal-readiness-panel" aria-labelledby="readiness-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">READY CHECK</p>
            <h2 id="readiness-title">运行准备度</h2>
          </div>
          <span className={`pill ${readyCount === 6 ? "online" : "degraded"}`}>
            {readyCount} / 6 已就绪
          </span>
        </div>
        <div className="personal-status-grid">
          <StatusItem
            ready={apiOnline}
            label="控制 API"
            value={apiOnline ? "Zeabur 在线" : "无法连接"}
          />
          <StatusItem
            ready={status?.enabled === true}
            label="个人模式"
            value={status?.enabled ? "已启用" : "未启用"}
          />
          <StatusItem
            ready={status?.ai.configured === true}
            label="AI"
            value={
              status?.ai.configured
                ? status.ai.provider ?? "已配置"
                : "缺少环境变量"
            }
          />
          <StatusItem
            ready={walletStepReady}
            label={realMoneyMode ? "交易钱包" : "交易钱包（实盘时需要）"}
            value={walletStatusText}
          />
          <StatusItem
            ready={status?.workerReady === true}
            label="常驻 Worker"
            value={status?.workerReady ? "已就绪" : "启动中或离线"}
          />
          <StatusItem
            ready={status?.autoRunEnabled === true}
            label="自动运行"
            value={status?.autoRunEnabled ? "常驻运行" : "尚未开启"}
          />
        </div>
      </section>

      <div className="settings-grid personal-settings-grid">
        <section className="panel" id="environment">
          <div className="section-heading">
            <div>
              <p className="eyebrow">ZEABUR ENV</p>
              <h2>需要填写的变量</h2>
            </div>
            <span className="read-only-chip">只在 Zeabur 修改</span>
          </div>

          <div className="env-setup-list">
            <EnvironmentGroup
              index="1"
              title="开启个人运行模式"
              variables={[
                "PORT=8080",
                "SERVICE_ROLE=personal",
                "WEB_CONCURRENCY=1",
                "SUPABASE_URL",
                "SUPABASE_SERVICE_ROLE_KEY",
                "POLYBOT_ACCOUNT_ID",
                "POLYBOT_DASHBOARD_ORIGINS",
                "POLYBOT_MODE=paper",
                "POLYBOT_PERSONAL_AUTO_RUN=true",
                "POLYBOT_PERSONAL_LIVE_ENABLED=false",
              ]}
              description="一个服务同时运行 API 和个人 Worker；账户 ID 填你的 Supabase 用户 UUID。"
            />
            <EnvironmentGroup
              index="2"
              title="连接 AI"
              variables={[
                "POLYBOT_AI_API_KEY",
                "POLYBOT_AI_BASE_URL",
                "POLYBOT_AI_MODEL",
              ]}
              description="中转站填写 HTTPS /v1 地址，并明确填写该服务实际支持的模型 ID；使用 OpenAI 官方接口时可不填 Base URL。"
            />
            <EnvironmentGroup
              index="3"
              title="连接交易钱包"
              variables={[
                "POLYMARKET_PRIVATE_KEY",
                "POLYMARKET_DEPOSIT_WALLET",
              ]}
              description="使用只存放启动资金的专用钱包，不要使用主钱包；公开入金地址可以不填。"
            />
          </div>

          <div className="account-id-callout">
            <div>
              <span>你的 POLYBOT_ACCOUNT_ID</span>
              <code>{session?.user.id ?? "登录后自动显示"}</code>
            </div>
            <button
              className="secondary-button"
              type="button"
              onClick={() => void copyAccountId()}
              disabled={!session?.user.id}
            >
              {accountCopied ? "已复制" : "复制账户 UUID"}
            </button>
          </div>

          <div className="personal-action-row">
            <Link className="primary-button" href="/diagnostics">
              检查部署连通性
            </Link>
            <button
              className="secondary-button"
              type="button"
              onClick={() => void refresh()}
              disabled={loading}
            >
              我已部署，重新读取
            </button>
          </div>
          <p className="panel-note">
            Vercel 只保留公开的站点与 Supabase 配置；AI Key 和钱包私钥均属于 Zeabur 后端变量。
          </p>
        </section>

        <section className="panel" id="risk-presets">
          <div className="section-heading">
            <div>
              <p className="eyebrow">RISK PRESETS</p>
              <h2>风控档位</h2>
            </div>
            <span className={`pill ${profileVersion ? "online" : "degraded"}`}>
              {profileVersion ? `策略版本 v${profileVersion}` : "无法读取版本"}
            </span>
          </div>
          <p className="field-help">
            一键应用完整风控参数组合。切换到实盘模式前，请先用保守档位完成 Paper / Shadow
            验证；应用档位会立即更新后端风控策略。
          </p>
          <div className="risk-preset-grid">
            {RISK_PRESETS.map((preset) => (
              <article
                className={`risk-preset-card ${presetBusy === preset.value ? "working" : ""}`}
                key={preset.value}
              >
                <div>
                  <strong>{preset.name}</strong>
                  <span>{preset.summary}</span>
                </div>
                <p>{preset.description}</p>
                <button
                  className="secondary-button"
                  type="button"
                  disabled={presetBusy !== null || !profileVersion}
                  onClick={() => void applyRiskPreset(preset.value)}
                >
                  {presetBusy === preset.value ? "应用中…" : "应用此档位"}
                </button>
              </article>
            ))}
          </div>
          <p className="panel-note">
            档位对应：单笔上限 / 单次风险 / 单事件敞口 / 桶敞口 / 总敞口 / 日损熔断 /
            最大回撤 / 最小净优势。现有档位以 Paper 本金 100 USD 为参考，调整本金时请按比例复核。
          </p>
        </section>

        <section className="panel" id="runtime">
          <div className="section-heading">
            <div>
              <p className="eyebrow">RISK MODE</p>
              <h2>当前风险模式</h2>
            </div>
            <span className={`pill ${status?.mode === "paper" ? "online" : "degraded"}`}>
              {status ? personalModeLabel(status.mode) : "读取中"}
            </span>
          </div>

          <div className="mode-ladder personal-mode-ladder">
            {MODES.map((mode) => (
              <div
                className={status?.mode === mode.value ? "current" : ""}
                key={mode.value}
              >
                <strong>{mode.name}</strong>
                <span>
                  {["canary", "live"].includes(mode.value) &&
                  !status?.liveSupported
                    ? "需显式开启个人实盘"
                    : mode.description}
                </span>
              </div>
            ))}
          </div>

          <dl className="detail-list personal-runtime-details">
            <div>
              <dt>自动周期</dt>
              <dd>{status?.autoRunEnabled ? "已开启" : "已关闭"}</dd>
            </div>
            <div>
              <dt>Worker 模式</dt>
              <dd>
                {status?.workerExecutionModel === "single_account"
                  ? "个人单账户"
                  : status?.workerExecutionModel ?? "等待后端返回"}
              </dd>
            </div>
            <div>
              <dt>AI 模型</dt>
              <dd>{status?.ai.forecastModel ?? "尚未配置"}</dd>
            </div>
            <div>
              <dt>AI Base URL</dt>
              <dd title={status?.ai.baseUrl}>{status?.ai.baseUrl ?? "提供商默认地址"}</dd>
            </div>
            <div>
              <dt>已完成周期</dt>
              <dd>{status?.cycleCount ?? 0}</dd>
            </div>
            <div>
              <dt>最近周期</dt>
              <dd>{status?.lastCycle?.state ?? "尚无记录"}</dd>
            </div>
            {realMoneyMode && (
              <>
                <div>
                  <dt>钱包绑定</dt>
                  <dd>{status?.wallet.bound ? "已绑定公开地址" : "等待 Worker"}</dd>
                </div>
                <div>
                  <dt>资金与授权</dt>
                  <dd>{liveWalletReady ? "已就绪" : walletStatusText}</dd>
                </div>
                <div>
                  <dt>自动实盘暂停</dt>
                  <dd>{status?.wallet.paused ? "是" : "否"}</dd>
                </div>
              </>
            )}
          </dl>

          {status &&
            ["canary", "live"].includes(status.mode) &&
            !status.liveSupported && (
            <div className="notice error personal-risk-notice">
              后端尚未确认个人实盘开关。请改回 Paper，或检查显式实盘变量后重新部署。
            </div>
          )}
          <p className="panel-note">
            默认只运行 Paper / Shadow；只有设置 POLYBOT_PERSONAL_LIVE_ENABLED=true
            并通过后端启动检查后，Canary / Live 才会开放。自动执行不能保证盈利。
          </p>
        </section>

        <section className="panel" id="session">
          <div className="section-heading">
            <div>
              <p className="eyebrow">LOGIN</p>
              <h2>登录保护</h2>
            </div>
            <span className={`pill ${session ? "online" : "offline"}`}>
              {session ? "已登录" : "未登录"}
            </span>
          </div>
          <p className="field-help">
            即使是个人部署，控制台仍使用 Supabase 登录保护，避免他人操作机器人。
          </p>
          <dl className="detail-list personal-runtime-details">
            <div>
              <dt>当前账户</dt>
              <dd>{session?.user.email ?? "等待会话"}</dd>
            </div>
            <div>
              <dt>密钥输入</dt>
              <dd>网页已禁用</dd>
            </div>
          </dl>
          <div className="personal-action-row">
            <Link className="secondary-button" href="/setup">打开新手向导</Link>
            <form action="/auth/logout" method="post">
              <button className="text-button" type="submit">退出登录</button>
            </form>
          </div>
        </section>
      </div>
    </main>
  );
}

function StatusItem({
  ready,
  label,
  value,
}: {
  ready: boolean;
  label: string;
  value: string;
}) {
  return (
    <article className={ready ? "ready" : "missing"}>
      <span className="personal-status-dot" aria-hidden="true" />
      <div>
        <small>{label}</small>
        <strong>{value}</strong>
      </div>
    </article>
  );
}

function EnvironmentGroup({
  index,
  title,
  variables,
  description,
}: {
  index: string;
  title: string;
  variables: string[];
  description: string;
}) {
  return (
    <article>
      <span className="ai-step-number" aria-hidden="true">{index}</span>
      <div>
        <h3>{title}</h3>
        <div className="env-variable-list">
          {variables.map((variable) => <code key={variable}>{variable}</code>)}
        </div>
        <p>{description}</p>
      </div>
    </article>
  );
}
