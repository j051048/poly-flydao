"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  apiRequest,
  readableApiError,
} from "../../lib/api";
import { validateCustomAIBaseUrl } from "../../lib/ai-provider";
import { getSupabaseBrowserClient } from "../../lib/supabase/browser";

type Notice = { tone: "success" | "error" | "info"; text: string };

interface CredentialStatus {
  aiConfigured: boolean;
  aiCredentialId?: string;
  provider?: string;
  aiBaseUrl?: string;
  model?: string;
  keyMask?: string;
  aiStatus?: string;
  walletConfigured: boolean;
  walletId?: string;
  walletAddress?: string;
  signatureType?: number;
  walletStatus?: string;
  riskPolicyId?: string;
  desiredMode?: string;
  autoRunEnabled: boolean;
  cycleIntervalSeconds: number;
  expectedVersion: number;
}

interface MfaEnrollment {
  factorId: string;
  qrCode: string;
  secret: string;
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function stringValue(...values: unknown[]): string | undefined {
  return values.find(
    (value): value is string => typeof value === "string" && value.length > 0,
  );
}

function booleanValue(...values: unknown[]): boolean | undefined {
  return values.find((value): value is boolean => typeof value === "boolean");
}

function parseCredentialStatus(
  payload: unknown,
  preferredProvider?: string,
): CredentialStatus {
  const root = record(payload);
  const aiItems = Array.isArray(root.ai_credentials) ? root.ai_credentials : [];
  const walletItems = Array.isArray(root.wallets)
    ? root.wallets
    : Array.isArray(root.trading_wallets)
      ? root.trading_wallets
      : [];
  const activeAiItems = aiItems.filter((item) => {
      const status = stringValue(record(item).status);
      return !status || !["revoked", "deleted"].includes(status);
    });
  const activeAi =
    activeAiItems.find(
      (item) => stringValue(record(item).provider) === preferredProvider,
    ) ??
    activeAiItems[0] ??
    aiItems[0];
  const activeWallet =
    walletItems.find((item) => {
      const status = stringValue(record(item).status);
      return !status || !["revoked", "deleted"].includes(status);
    }) ?? walletItems[0];
  const ai = record(root.ai ?? root.ai_credential ?? activeAi);
  const wallet = record(
    root.wallet ?? root.trading_wallet ?? activeWallet,
  );
  const walletAddress = stringValue(
    wallet.address,
    wallet.wallet_address,
    wallet.deposit_wallet_address,
    root.wallet_address,
  );
  const keyMask = stringValue(
    ai.masked,
    ai.masked_key,
    root.ai_key_masked,
  );
  const signatureType = Number(
    wallet.signature_type ?? root.signature_type ?? Number.NaN,
  );

  return {
    aiConfigured:
      booleanValue(ai.configured, root.ai_configured) ??
      Boolean(keyMask || ai.status === "active"),
    aiCredentialId: stringValue(ai.id, ai.credential_id),
    provider: stringValue(ai.provider, root.ai_provider),
    model: stringValue(ai.model, root.forecast_model),
    keyMask,
    aiStatus: stringValue(ai.status),
    walletConfigured:
      booleanValue(wallet.configured, root.wallet_configured) ??
      Boolean(walletAddress),
    walletId: stringValue(wallet.id, wallet.wallet_id),
    walletAddress,
    signatureType: Number.isFinite(signatureType) ? signatureType : undefined,
    walletStatus: stringValue(wallet.status),
    autoRunEnabled: false,
    cycleIntervalSeconds: 60,
    expectedVersion: 1,
  };
}

export default function SettingsPage() {
  const [status, setStatus] = useState<CredentialStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);

  // Secret values deliberately live only in React memory and are cleared
  // immediately after every submission attempt.
  const [apiKey, setApiKey] = useState("");
  const [privateKey, setPrivateKey] = useState("");
  const [provider, setProvider] = useState("openrouter");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [showAdvancedImport, setShowAdvancedImport] = useState(false);
  const [importConfirmed, setImportConfirmed] = useState(false);
  const importIdempotencyKey = useRef<string | null>(null);
  const [desiredMode, setDesiredMode] = useState("paper");
  const [autoRunEnabled, setAutoRunEnabled] = useState(false);
  const [cycleIntervalSeconds, setCycleIntervalSeconds] = useState(60);
  const [mfaLevel, setMfaLevel] = useState("aal1");
  const [mfaFactorId, setMfaFactorId] = useState<string | null>(null);
  const [mfaEnrollment, setMfaEnrollment] = useState<MfaEnrollment | null>(null);
  const [mfaCode, setMfaCode] = useState("");
  const baseUrlValidation =
    provider === "custom"
      ? validateCustomAIBaseUrl(baseUrl)
      : { normalized: null, error: null };

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [credentialsResult, meResult] = await Promise.all([
        apiRequest<unknown>("/v1/me/credentials/status"),
        apiRequest<unknown>("/v1/me"),
      ]);
      const me = record(meResult.data);
      const runtimeProfile = record(me.runtime_profile);
      const configuredProvider =
        stringValue(runtimeProfile.ai_provider) ?? "openrouter";
      const parsed = parseCredentialStatus(
        credentialsResult.data,
        configuredProvider,
      );
      parsed.provider =
        stringValue(configuredProvider, parsed.provider) ?? "openrouter";
      parsed.aiBaseUrl = stringValue(runtimeProfile.ai_base_url);
      parsed.model = stringValue(runtimeProfile.forecast_model, parsed.model);
      parsed.aiCredentialId =
        stringValue(runtimeProfile.ai_credential_id, parsed.aiCredentialId);
      parsed.walletId = stringValue(
        runtimeProfile.trading_wallet_id,
        parsed.walletId,
      );
      parsed.riskPolicyId = stringValue(runtimeProfile.risk_policy_id);
      parsed.desiredMode =
        stringValue(runtimeProfile.desired_mode) ?? "paper";
      parsed.autoRunEnabled =
        booleanValue(runtimeProfile.auto_run_enabled) ?? false;
      parsed.cycleIntervalSeconds =
        Number(runtimeProfile.cycle_interval_seconds) || 60;
      parsed.expectedVersion =
        Number(runtimeProfile.version) || 1;
      setStatus(parsed);
      if (parsed.provider) setProvider(parsed.provider);
      if (parsed.aiBaseUrl) setBaseUrl(parsed.aiBaseUrl);
      if (parsed.model) setModel(parsed.model);
      setDesiredMode(parsed.desiredMode);
      setAutoRunEnabled(parsed.autoRunEnabled);
      setCycleIntervalSeconds(parsed.cycleIntervalSeconds);

      const supabase = getSupabaseBrowserClient();
      if (supabase) {
        const [assurance, factors] = await Promise.all([
          supabase.auth.mfa.getAuthenticatorAssuranceLevel(),
          supabase.auth.mfa.listFactors(),
        ]);
        if (!assurance.error) {
          setMfaLevel(assurance.data.currentLevel ?? "aal1");
        }
        if (!factors.error) {
          const verified = factors.data.totp.find(
            (factor) => factor.status === "verified",
          );
          setMfaFactorId(verified?.id ?? null);
        }
      }
    } catch (error) {
      setNotice({ tone: "error", text: readableApiError(error) });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (
      !status?.walletStatus ||
      !["provisioning", "pending_verification", "revocation_pending"].includes(
        status.walletStatus,
      )
    ) {
      return;
    }
    const timer = window.setInterval(() => {
      void refresh();
    }, 3000);
    return () => window.clearInterval(timer);
  }, [refresh, status?.walletStatus]);

  async function saveAiCredential(includeSecret: boolean) {
    const trimmedKey = apiKey.trim();
    if (includeSecret && !trimmedKey) return;
    if (provider === "custom" && !baseUrlValidation.normalized) {
      setNotice({
        tone: "error",
        text: baseUrlValidation.error ?? "请输入安全的 HTTPS Base URL。",
      });
      return;
    }
    if (!includeSecret && provider !== status?.provider) {
      setNotice({
        tone: "error",
        text: "切换 AI 提供商时必须同时输入并加密保存该提供商的 API Key。",
      });
      return;
    }
    if (includeSecret && mfaLevel !== "aal2") {
      setNotice({
        tone: "error",
        text: "保存或轮换 AI Key 前必须先完成 TOTP 双因素验证。",
      });
      return;
    }
    setBusy("ai");
    setNotice({
      tone: "info",
      text: includeSecret ? "正在加密保存 AI 凭证…" : "正在更新模型设置…",
    });

    try {
      let credentialId = status?.aiCredentialId;
      if (includeSecret) {
        const credentialResult = await apiRequest<unknown>(
          "/v1/me/credentials/ai",
          {
            method: "PUT",
            body: {
              provider,
              api_key: trimmedKey,
              label: "default",
            },
          },
        );
        const response = record(credentialResult.data);
        const credential = record(response.credential);
        credentialId = stringValue(
          response.id,
          response.credential_id,
          credential.id,
        );
      }
      await apiRequest("/v1/me/runtime-profile", {
        method: "PUT",
        body: {
          expected_version: status?.expectedVersion ?? 1,
          ai_provider: provider,
          ai_base_url: baseUrlValidation.normalized,
          forecast_model: model.trim(),
          ...(credentialId ? { ai_credential_id: credentialId } : {}),
          ...(status?.walletId
            ? { trading_wallet_id: status.walletId }
            : {}),
          ...(status?.riskPolicyId
            ? { risk_policy_id: status.riskPolicyId }
            : {}),
          desired_mode: status?.desiredMode ?? "paper",
          auto_run_enabled: status?.autoRunEnabled ?? false,
          cycle_interval_seconds: status?.cycleIntervalSeconds ?? 60,
        },
      });
      setNotice({
        tone: "success",
        text: includeSecret
          ? "AI Key 已写入后端密钥库，浏览器中的输入值已清空。"
          : "模型设置已保存到租户运行配置。",
      });
      await refresh();
    } catch (error) {
      setNotice({ tone: "error", text: readableApiError(error) });
    } finally {
      setApiKey("");
      setBusy(null);
    }
  }

  async function revokeAiCredential() {
    if (mfaLevel !== "aal2") {
      setNotice({
        tone: "error",
        text: "撤销 AI 凭证前必须先完成 TOTP 双因素验证。",
      });
      return;
    }
    if (!window.confirm("确认撤销 AI 凭证？自动任务将停止调用该模型。")) return;
    setBusy("revoke-ai");
    try {
      await apiRequest(
        `/v1/me/credentials/ai?provider=${encodeURIComponent(provider)}`,
        { method: "DELETE" },
      );
      setNotice({ tone: "success", text: "AI 凭证已撤销。" });
      await refresh();
    } catch (error) {
      setNotice({ tone: "error", text: readableApiError(error) });
    } finally {
      setBusy(null);
    }
  }

  async function importWallet() {
    const secret = privateKey.trim();
    if (!secret || !importConfirmed) return;
    setBusy("import-wallet");
    setNotice({ tone: "info", text: "正在将专用机器人私钥写入隔离签名服务…" });
    try {
      importIdempotencyKey.current ??= crypto.randomUUID();
      await apiRequest("/v1/me/wallets/import", {
        method: "POST",
        body: {
          private_key: secret,
          label: "default",
          signature_type: 3,
          confirm_standard_allowances: true,
        },
        idempotencyKey: importIdempotencyKey.current,
      });
      importIdempotencyKey.current = null;
      setImportConfirmed(false);
      setShowAdvancedImport(false);
      setNotice({
        tone: "success",
        text: "专用钱包已加密导入，Worker 正在派生入金地址；页面会自动刷新状态。",
      });
      await refresh();
    } catch (error) {
      setNotice({ tone: "error", text: readableApiError(error) });
    } finally {
      setPrivateKey("");
      setBusy(null);
    }
  }

  async function revokeWallet() {
    if (!status?.walletId) return;
    if (
      !window.confirm(
        "确认撤销该机器人钱包？后端将停止新任务；请先确认没有开放订单。",
      )
    ) {
      return;
    }
    setBusy("revoke-wallet");
    try {
      const result = await apiRequest<unknown>(
        `/v1/me/wallets/${encodeURIComponent(status.walletId)}`,
        { method: "DELETE" },
      );
      setNotice({
        tone: result.status === 202 ? "info" : "success",
        text:
          result.status === 202
            ? "钱包撤销已进入队列，等待 worker 完成撤单和停用。"
            : "钱包已撤销。",
      });
      await refresh();
    } catch (error) {
      setNotice({ tone: "error", text: readableApiError(error) });
    } finally {
      setBusy(null);
    }
  }

  async function saveAutomation() {
    if (!status?.aiCredentialId || !status.riskPolicyId || !model.trim()) {
      setNotice({
        tone: "error",
        text: "请先配置 AI 凭证与风险策略，再保存自动运行设置。",
      });
      return;
    }
    if (provider !== status.provider) {
      setNotice({
        tone: "error",
        text: "请先加密保存新提供商的 API Key，再启用自动运行。",
      });
      return;
    }
    if (provider === "custom" && !baseUrlValidation.normalized) {
      setNotice({
        tone: "error",
        text: baseUrlValidation.error ?? "请输入安全的 HTTPS Base URL。",
      });
      return;
    }
    if (
      autoRunEnabled &&
      ["canary", "live"].includes(desiredMode) &&
      mfaLevel !== "aal2"
    ) {
      setNotice({
        tone: "error",
        text: "启用自动实盘前必须先完成下方 TOTP 双因素验证。",
      });
      return;
    }
    setBusy("automation");
    try {
      await apiRequest("/v1/me/runtime-profile", {
        method: "PUT",
        body: {
          expected_version: status.expectedVersion ?? 1,
          ai_provider: provider,
          ai_base_url: baseUrlValidation.normalized,
          forecast_model: model.trim(),
          ai_credential_id: status.aiCredentialId,
          ...(status.walletId ? { trading_wallet_id: status.walletId } : {}),
          risk_policy_id: status.riskPolicyId,
          desired_mode: desiredMode,
          auto_run_enabled: autoRunEnabled,
          cycle_interval_seconds: cycleIntervalSeconds,
        },
      });
      setNotice({
        tone: "success",
        text: autoRunEnabled
          ? "自动周期已启用；实盘模式仍只在短时 arm 窗口内下单。"
          : "运行模式已保存，自动周期保持关闭。",
      });
      await refresh();
    } catch (error) {
      setNotice({ tone: "error", text: readableApiError(error) });
    } finally {
      setBusy(null);
    }
  }

  async function beginMfaEnrollment() {
    const supabase = getSupabaseBrowserClient();
    if (!supabase) return;
    setBusy("mfa-enroll");
    try {
      const { data, error } = await supabase.auth.mfa.enroll({
        factorType: "totp",
        friendlyName: "Polybot Control",
      });
      if (error) throw error;
      setMfaEnrollment({
        factorId: data.id,
        qrCode: data.totp.qr_code,
        secret: data.totp.secret,
      });
      setMfaCode("");
      setNotice({
        tone: "info",
        text: "请用验证器扫描二维码，再输入 6 位验证码完成绑定。",
      });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "无法开始 MFA 绑定。",
      });
    } finally {
      setBusy(null);
    }
  }

  async function verifyMfa() {
    const supabase = getSupabaseBrowserClient();
    const factorId = mfaEnrollment?.factorId ?? mfaFactorId;
    if (!supabase || !factorId || !/^\d{6}$/.test(mfaCode)) return;
    setBusy("mfa-verify");
    try {
      const { error } = await supabase.auth.mfa.challengeAndVerify({
        factorId,
        code: mfaCode,
      });
      if (error) throw error;
      await supabase.auth.refreshSession();
      setMfaLevel("aal2");
      setMfaFactorId(factorId);
      setMfaEnrollment(null);
      setMfaCode("");
      setNotice({
        tone: "success",
        text: "本次会话已提升到 AAL2，可以导入钱包和短时解锁实盘。",
      });
    } catch (error) {
      setNotice({
        tone: "error",
        text: error instanceof Error ? error.message : "MFA 验证失败。",
      });
    } finally {
      setBusy(null);
    }
  }

  return (
    <main className="page-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">TENANT CREDENTIALS</p>
          <h1>凭证与钱包</h1>
        </div>
        <button
          className="secondary-button"
          type="button"
          onClick={() => void refresh()}
          disabled={loading || busy !== null}
        >
          {loading ? "读取中…" : "刷新状态"}
        </button>
      </header>

      <section className="safety-banner">
        <span className="shield" aria-hidden="true">◆</span>
        <div>
          <strong>一次提交，日常请求不携带密钥</strong>
          <p>
            Key 仅在当前输入框内存中短暂存在，经 HTTPS 提交到租户密钥库后立即清空。
            页面只显示状态、末尾掩码和公开钱包地址，永不回显原文。
          </p>
        </div>
      </section>

      {notice && (
        <div className={`notice ${notice.tone} page-notice`} role="status">
          {notice.text}
        </div>
      )}

      <div className="settings-grid">
        <section className="panel">
          <div className="section-heading">
            <div>
              <p className="eyebrow">AI PROFILE</p>
              <h2>第三方 AI 凭证</h2>
            </div>
            <span className={`pill ${status?.aiConfigured ? "online" : "offline"}`}>
              {status?.aiConfigured ? "已配置" : "未配置"}
            </span>
          </div>

          <dl className="detail-list credential-status">
            <div><dt>提供商</dt><dd>{status?.provider ?? "—"}</dd></div>
            {status?.provider === "custom" && (
              <div><dt>Base URL</dt><dd>{status?.aiBaseUrl ?? "—"}</dd></div>
            )}
            <div><dt>模型</dt><dd>{status?.model ?? "—"}</dd></div>
            <div><dt>Key</dt><dd>{status?.keyMask ?? "永不回显"}</dd></div>
            <div><dt>状态</dt><dd>{status?.aiStatus ?? "—"}</dd></div>
          </dl>

          <div className="form-stack credential-form">
            <label className="form-field" htmlFor="provider">
              <span className="field-label">AI 提供商</span>
              <select
                id="provider"
                value={provider}
                onChange={(event) => setProvider(event.target.value)}
              >
                <option value="openrouter">OpenRouter</option>
                <option value="openai">OpenAI</option>
                <option value="anthropic">Anthropic</option>
                <option value="litellm">LiteLLM</option>
                <option value="custom">自定义 OpenAI 兼容中转站</option>
              </select>
            </label>
            {provider === "custom" && (
              <label className="form-field" htmlFor="base-url">
                <span className="field-label">Base URL</span>
                <input
                  id="base-url"
                  type="url"
                  value={baseUrl}
                  onChange={(event) => setBaseUrl(event.target.value)}
                  placeholder="例如 https://my-proxy.example.com/v1"
                  autoComplete="off"
                  required
                  maxLength={256}
                  aria-invalid={Boolean(baseUrlValidation.error)}
                  aria-describedby="base-url-help"
                />
                <span
                  className={`field-help ${baseUrlValidation.error ? "error" : ""}`}
                  id="base-url-help"
                >
                  {baseUrlValidation.error ??
                    "仅支持公开 HTTPS 域名；后端还会执行 DNS、私网地址与重定向防护。"}
                </span>
              </label>
            )}
            <label className="form-field" htmlFor="model">
              <span className="field-label">模型 ID</span>
              <input
                id="model"
                type="text"
                value={model}
                onChange={(event) => setModel(event.target.value)}
                placeholder="例如 openai/gpt-5-mini"
                autoComplete="off"
              />
            </label>
            <label className="form-field" htmlFor="api-key">
              <span className="field-label">
                {status?.aiConfigured ? "轮换 API Key" : "API Key"}
              </span>
              <input
                id="api-key"
                type="password"
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
                placeholder="只输入一次，提交后立即清空"
                autoComplete="off"
                autoCapitalize="off"
                spellCheck={false}
              />
            </label>

            <div className="button-row">
              <button
                className="primary-button"
                type="button"
                onClick={() => void saveAiCredential(true)}
                disabled={
                  !apiKey.trim() ||
                  !model.trim() ||
                  (provider === "custom" && !baseUrlValidation.normalized) ||
                  mfaLevel !== "aal2" ||
                  busy !== null
                }
              >
                {busy === "ai" ? "保存中…" : status?.aiConfigured ? "轮换并验证 Key" : "加密保存 Key"}
              </button>
              {status?.aiConfigured && (
                <>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => void saveAiCredential(false)}
                    disabled={
                      !model.trim() ||
                      provider !== status.provider ||
                      (provider === "custom" && !baseUrlValidation.normalized) ||
                      busy !== null
                    }
                  >
                    保存模型设置
                  </button>
                  <button
                    className="danger-button"
                    type="button"
                    onClick={() => void revokeAiCredential()}
                    disabled={mfaLevel !== "aal2" || busy !== null}
                  >
                    撤销凭证
                  </button>
                </>
              )}
            </div>
          </div>
        </section>

        <section className="panel">
          <div className="section-heading">
            <div>
              <p className="eyebrow">SESSION SECURITY</p>
              <h2>双因素验证</h2>
            </div>
            <span className={`pill ${mfaLevel === "aal2" ? "online" : "offline"}`}>
              {mfaLevel === "aal2" ? "AAL2 已验证" : "需要验证"}
            </span>
          </div>
          <p className="field-help">
            保存或撤销 AI Key、导入或撤销钱包、启用自动实盘和短时解锁都要求当前会话达到 AAL2。
          </p>
          {mfaEnrollment && (
            <div className="mfa-enrollment">
              {/* Supabase returns a data URL; CSP permits data images only. */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={mfaEnrollment.qrCode}
                alt="TOTP 验证器二维码"
                width={180}
                height={180}
              />
              <p className="field-help">
                无法扫码时手工输入：<code>{mfaEnrollment.secret}</code>
              </p>
            </div>
          )}
          {mfaLevel !== "aal2" && (
            <div className="form-stack">
              {!mfaEnrollment && !mfaFactorId && (
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => void beginMfaEnrollment()}
                  disabled={busy !== null}
                >
                  {busy === "mfa-enroll" ? "创建中…" : "绑定 TOTP 验证器"}
                </button>
              )}
              {(mfaEnrollment || mfaFactorId) && (
                <>
                  <label className="form-field" htmlFor="mfa-code">
                    <span className="field-label">6 位验证码</span>
                    <input
                      id="mfa-code"
                      inputMode="numeric"
                      autoComplete="one-time-code"
                      pattern="[0-9]{6}"
                      maxLength={6}
                      value={mfaCode}
                      onChange={(event) =>
                        setMfaCode(event.target.value.replace(/\D/g, "").slice(0, 6))
                      }
                    />
                  </label>
                  <button
                    className="primary-button"
                    type="button"
                    onClick={() => void verifyMfa()}
                    disabled={!/^\d{6}$/.test(mfaCode) || busy !== null}
                  >
                    {busy === "mfa-verify" ? "验证中…" : "验证并提升会话"}
                  </button>
                </>
              )}
            </div>
          )}
        </section>

        <section className="panel">
          <div className="section-heading">
            <div>
              <p className="eyebrow">AUTOMATION</p>
              <h2>自动运行</h2>
            </div>
            <span className={`pill ${autoRunEnabled ? "online" : "offline"}`}>
              {autoRunEnabled ? "已启用" : "默认关闭"}
            </span>
          </div>
          <div className="form-stack">
            <label className="form-field" htmlFor="desired-mode">
              <span className="field-label">运行模式</span>
              <select
                id="desired-mode"
                value={desiredMode}
                onChange={(event) => setDesiredMode(event.target.value)}
              >
                <option value="paper">Paper（模拟）</option>
                <option value="shadow">Shadow（只生成意图）</option>
                <option value="canary">Canary（单笔硬上限 $5）</option>
                <option value="live">Live（真实资金）</option>
              </select>
            </label>
            <label className="form-field" htmlFor="cycle-interval">
              <span className="field-label">周期间隔（秒）</span>
              <input
                id="cycle-interval"
                type="number"
                min={30}
                max={3600}
                step={10}
                value={cycleIntervalSeconds}
                onChange={(event) =>
                  setCycleIntervalSeconds(
                    Math.min(3600, Math.max(30, Number(event.target.value) || 60)),
                  )
                }
              />
            </label>
            <label className="confirm-row">
              <input
                type="checkbox"
                checked={autoRunEnabled}
                onChange={(event) => setAutoRunEnabled(event.target.checked)}
              />
              <span>允许 Zeabur Worker 按周期自动创建租户任务。</span>
            </label>
            <button
              className="primary-button"
              type="button"
              onClick={() => void saveAutomation()}
              disabled={busy !== null || loading}
            >
              {busy === "automation" ? "保存中…" : "保存自动运行设置"}
            </button>
            <p className="field-help">
              Canary/Live 即使启用自动周期，也只有在控制台短时 arm
              且租约、对账、风控全部健康时才能下单。
            </p>
          </div>
        </section>

        <section className="panel">
          <div className="section-heading">
            <div>
              <p className="eyebrow">TRADING WALLET</p>
              <h2>机器人专属钱包</h2>
            </div>
            <span className={`pill ${status?.walletConfigured ? "online" : "offline"}`}>
              {status?.walletConfigured ? "已绑定" : "未绑定"}
            </span>
          </div>

          <dl className="detail-list credential-status">
            <div>
              <dt>公开地址</dt>
              <dd title={status?.walletAddress}>{status?.walletAddress ?? "—"}</dd>
            </div>
            <div><dt>签名类型</dt><dd>{status?.signatureType ?? "—"}</dd></div>
            <div><dt>状态</dt><dd>{status?.walletStatus ?? "—"}</dd></div>
          </dl>

          {!status?.walletConfigured && (
            <div className="wallet-actions">
              <button
                className="secondary-button full-width"
                type="button"
                disabled
              >
                服务端生成钱包尚未启用
              </button>
              <p className="field-help">
                当前请展开下方高级选项，导入一个全新、小额、专用的钱包。
              </p>
            </div>
          )}

          <details
            className="advanced-panel"
            open={showAdvancedImport}
            onToggle={(event) =>
              setShowAdvancedImport((event.currentTarget as HTMLDetailsElement).open)
            }
          >
            <summary>高级：导入已有专用机器人钱包</summary>
            <div className="form-stack">
              <div className="notice error">
                绝对不要输入主钱包或存有其他资产的钱包私钥。该 signer
                一旦泄露，专用钱包内资金可能全部损失。
              </div>
              <label className="form-field" htmlFor="private-key">
                <span className="field-label">专用 EVM 私钥</span>
                <input
                  id="private-key"
                  type="password"
                  value={privateKey}
                  onChange={(event) => setPrivateKey(event.target.value)}
                  placeholder="0x…（提交后立即清空）"
                  autoComplete="off"
                  autoCapitalize="off"
                  spellCheck={false}
                />
              </label>
              <label className="confirm-row">
                <input
                  type="checkbox"
                  checked={importConfirmed}
                  onChange={(event) => setImportConfirmed(event.target.checked)}
                />
                <span>
                  我确认这是新建的小额专用钱包，并授权 Worker
                  在地址入金且实盘解锁后提交 Polymarket 标准交易 approvals；它不是主钱包。
                </span>
              </label>
              <button
                className="warning-button"
                type="button"
                onClick={() => void importWallet()}
                disabled={!privateKey.trim() || !importConfirmed || busy !== null}
              >
                {busy === "import-wallet" ? "导入中…" : "加密导入专用钱包"}
              </button>
            </div>
          </details>

          {status?.walletAddress && (
            <div className="deposit-callout">
              <strong>启动资金地址</strong>
              <code>{status.walletAddress}</code>
              <p>仅按后端明确展示的网络与资产入金，并先使用可承受损失的小额资金。</p>
            </div>
          )}
          {status?.walletId && (
            <button
              className="danger-button full-width"
              type="button"
              onClick={() => void revokeWallet()}
              disabled={busy !== null}
              style={{ marginTop: "16px" }}
            >
              {busy === "revoke-wallet" ? "撤销中…" : "撤销机器人钱包"}
            </button>
          )}
        </section>
      </div>
    </main>
  );
}
