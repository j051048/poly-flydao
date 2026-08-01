"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  apiRequest,
  readableApiError,
} from "../../lib/api";
import {
  AI_PROVIDER_OPTIONS,
  getAIProviderOption,
  initialModelForProvider,
  isTenantAIProvider,
  normalizeTenantAIProvider,
  type TenantAIProvider,
  validateCustomAIBaseUrl,
} from "../../lib/ai-provider";
import {
  POLYBOT_TOTP_FRIENDLY_NAME,
  readableMfaError,
  selectTotpFactors,
} from "../../lib/mfa";
import { getSupabaseBrowserClient } from "../../lib/supabase/browser";

type Notice = { tone: "success" | "error" | "info"; text: string };
type MfaFactorStatus = "none" | "unverified" | "verified";

const SECP256K1_ORDER = BigInt(
  "0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141",
);

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
  chainId?: number;
  collateralToken?: string;
  collateralBalance?: number;
  allowancesReady?: boolean;
  readinessCheckedAt?: string;
  aiUsageUsed?: number;
  aiUsageLimit?: number;
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
      Boolean(
        stringValue(wallet.id, wallet.wallet_id) ||
          walletAddress ||
          (wallet.status &&
            !["revoked", "deleted", "failed"].includes(String(wallet.status))),
      ),
    walletId: stringValue(wallet.id, wallet.wallet_id),
    walletAddress,
    signatureType: Number.isFinite(signatureType) ? signatureType : undefined,
    walletStatus: stringValue(wallet.status),
    chainId: Number.isFinite(Number(wallet.chain_id))
      ? Number(wallet.chain_id)
      : undefined,
    collateralToken: stringValue(wallet.collateral_token),
    collateralBalance: Number.isFinite(Number(wallet.collateral_balance_pusd))
      ? Number(wallet.collateral_balance_pusd)
      : undefined,
    allowancesReady: booleanValue(wallet.allowances_ready),
    readinessCheckedAt: stringValue(wallet.readiness_checked_at),
    autoRunEnabled: false,
    cycleIntervalSeconds: 60,
    expectedVersion: 1,
  };
}

function walletStatusLabel(
  status: string | undefined,
  configured = false,
): string {
  return (
    {
      active: "已验证",
      provisioning: "正在创建",
      pending_verification: "正在验证",
      revocation_pending: "正在撤销",
      revoked: "已撤销",
      failed: "验证失败",
    }[status ?? ""] ??
    (configured ? "已绑定" : "未绑定")
  );
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
  const [provider, setProvider] = useState<TenantAIProvider>("openrouter");
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [aiCheckState, setAiCheckState] = useState<
    "idle" | "checking" | "passed" | "failed"
  >("idle");
  const [aiBudgetLimit, setAiBudgetLimit] = useState(100);
  const [showAdvancedImport, setShowAdvancedImport] = useState(false);
  const [importConfirmed, setImportConfirmed] = useState(false);
  const [generatedWallet, setGeneratedWallet] = useState(false);
  const [backupConfirmed, setBackupConfirmed] = useState(false);
  const importIdempotencyKey = useRef<string | null>(null);
  const [desiredMode, setDesiredMode] = useState("paper");
  const [autoRunEnabled, setAutoRunEnabled] = useState(false);
  const [cycleIntervalSeconds, setCycleIntervalSeconds] = useState(60);
  const [mfaLevel, setMfaLevel] = useState("aal1");
  const [mfaFactorId, setMfaFactorId] = useState<string | null>(null);
  const [mfaFactorStatus, setMfaFactorStatus] =
    useState<MfaFactorStatus>("none");
  const [mfaEnrollment, setMfaEnrollment] = useState<MfaEnrollment | null>(null);
  const [mfaCode, setMfaCode] = useState("");
  const [addressCopied, setAddressCopied] = useState(false);
  const baseUrlValidation =
    provider === "custom"
      ? validateCustomAIBaseUrl(baseUrl)
      : { normalized: null, error: null };
  const providerOption = getAIProviderOption(provider);
  const usesAdvancedProvider = !["openrouter", "openai"].includes(provider);
  const needsNewAiKey = !status?.aiConfigured || provider !== status.provider;

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [credentialsResult, meResult, performanceResult] = await Promise.all([
        apiRequest<unknown>("/v1/me/credentials/status"),
        apiRequest<unknown>("/v1/me"),
        apiRequest<unknown>("/v1/me/performance"),
      ]);
      const me = record(meResult.data);
      const runtimeProfile = record(me.runtime_profile);
      const runtimeProvider = stringValue(runtimeProfile.ai_provider);
      const preferredProvider = isTenantAIProvider(runtimeProvider)
        ? runtimeProvider
        : undefined;
      const parsed = parseCredentialStatus(
        credentialsResult.data,
        preferredProvider,
      );
      const configuredProvider = normalizeTenantAIProvider(
        preferredProvider ?? parsed.provider,
      );
      parsed.provider = configuredProvider;
      parsed.aiBaseUrl =
        configuredProvider === "custom"
          ? stringValue(runtimeProfile.ai_base_url)
          : undefined;
      const configuredModel = preferredProvider
        ? stringValue(runtimeProfile.forecast_model, parsed.model)
        : parsed.model;
      parsed.model = initialModelForProvider(
        configuredProvider,
        configuredModel,
        parsed.aiConfigured,
      );
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
      const performance = record(performanceResult.data);
      parsed.aiUsageUsed = Number(performance.ai_usage_used) || 0;
      parsed.aiUsageLimit = Number(performance.ai_usage_limit) || 100;
      setStatus(parsed);
      setProvider(configuredProvider);
      setBaseUrl(parsed.aiBaseUrl ?? "");
      setModel(parsed.model ?? "");
      setAiBudgetLimit(parsed.aiUsageLimit ?? 100);
      setDesiredMode(parsed.desiredMode);
      setAutoRunEnabled(parsed.autoRunEnabled);
      setCycleIntervalSeconds(parsed.cycleIntervalSeconds);
    } catch (error) {
      setNotice({ tone: "error", text: readableApiError(error) });
    }

    try {
      const supabase = getSupabaseBrowserClient();
      if (supabase) {
        const [assurance, factors] = await Promise.all([
          supabase.auth.mfa.getAuthenticatorAssuranceLevel(),
          supabase.auth.mfa.listFactors(),
        ]);
        const mfaError = assurance.error ?? factors.error;
        if (mfaError) throw mfaError;
        if (!assurance.error) {
          setMfaLevel(assurance.data.currentLevel ?? "aal1");
        }
        if (!factors.error) {
          const selection = selectTotpFactors(factors.data.all);
          const selected = selection.verified ?? selection.pending;
          setMfaFactorId(selected?.id ?? null);
          setMfaFactorStatus(
            selected?.status === "verified"
              ? "verified"
              : selected?.status === "unverified"
                ? "unverified"
                : "none",
          );
          setMfaEnrollment((current) =>
            current && current.factorId === selected?.id ? current : null,
          );
        }
      }
    } catch (error) {
      setNotice({ tone: "error", text: readableMfaError(error) });
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

  function chooseProvider(nextProvider: TenantAIProvider) {
    if (nextProvider === provider) return;
    const nextOption = getAIProviderOption(nextProvider);
    const storedModel =
      nextProvider === status?.provider ? status.model?.trim() : undefined;
    setProvider(nextProvider);
    setModel(storedModel || nextOption.suggestedModel || "");
    setAiCheckState("idle");
  }

  async function saveAiCredential(includeSecret: boolean): Promise<boolean> {
    const trimmedKey = apiKey.trim();
    if (includeSecret && !trimmedKey) return false;
    if (provider === "custom" && !baseUrlValidation.normalized) {
      setNotice({
        tone: "error",
        text: baseUrlValidation.error ?? "请输入安全的 HTTPS Base URL。",
      });
      return false;
    }
    if (!includeSecret && provider !== status?.provider) {
      setNotice({
        tone: "error",
        text: "切换 AI 提供商时必须同时输入并加密保存该提供商的 API Key。",
      });
      return false;
    }
    if (includeSecret && mfaLevel !== "aal2") {
      setNotice({
        tone: "error",
        text: "保存或轮换 AI Key 前必须先完成 TOTP 双因素验证。",
      });
      return false;
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
      return true;
    } catch (error) {
      setNotice({ tone: "error", text: readableApiError(error) });
      return false;
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

  async function saveAiBudget() {
    if (mfaLevel !== "aal2") {
      setNotice({ tone: "error", text: "修改 AI 预算前必须完成 TOTP 双因素验证。" });
      return;
    }
    setBusy("ai-budget");
    try {
      await apiRequest("/v1/me/ai-budget", {
        method: "PUT",
        body: { request_limit: aiBudgetLimit },
      });
      setNotice({ tone: "success", text: "每日 AI 请求预算已更新。" });
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
    if (mfaLevel !== "aal2") {
      setNotice({
        tone: "error",
        text: "导入钱包前必须先完成 TOTP 双因素验证。",
      });
      return;
    }
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
      setGeneratedWallet(false);
      setBackupConfirmed(false);
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

  function generateDedicatedWallet() {
    let secret = "";
    do {
      const bytes = crypto.getRandomValues(new Uint8Array(32));
      secret = `0x${Array.from(bytes, (value) =>
        value.toString(16).padStart(2, "0"),
      ).join("")}`;
    } while (BigInt(secret) === BigInt(0) || BigInt(secret) >= SECP256K1_ORDER);
    setPrivateKey(secret);
    setGeneratedWallet(true);
    setBackupConfirmed(false);
    setImportConfirmed(false);
    setShowAdvancedImport(true);
    setNotice({
      tone: "info",
      text: "专用钱包已在本浏览器内生成。请先下载离线备份，再加密导入。",
    });
  }

  function downloadWalletBackup() {
    if (!generatedWallet || !privateKey) return;
    const content = [
      "Polybot dedicated trading wallet backup",
      "Never share this key. Never deposit funds you cannot afford to lose.",
      "",
      privateKey,
      "",
    ].join("\n");
    const url = URL.createObjectURL(new Blob([content], { type: "text/plain" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `polybot-wallet-backup-${new Date().toISOString().slice(0, 10)}.txt`;
    link.click();
    URL.revokeObjectURL(url);
  }

  async function revokeWallet() {
    if (!status?.walletId) return;
    if (mfaLevel !== "aal2") {
      setNotice({
        tone: "error",
        text: "撤销钱包前必须先完成 TOTP 双因素验证。",
      });
      return;
    }
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

  async function testAiCredential(requireStoredStatus = true): Promise<boolean> {
    if ((requireStoredStatus && !status?.aiConfigured) || mfaLevel !== "aal2") {
      return false;
    }
    setBusy("ai-check");
    setAiCheckState("checking");
    setNotice({ tone: "info", text: "Worker 正在验证 Key、模型和结构化输出…" });
    try {
      const queued = await apiRequest<unknown>("/v1/me/credentials/ai/check", {
        method: "POST",
      });
      const jobId = stringValue(record(queued.data).id);
      if (!jobId) throw new Error("诊断任务没有返回 ID");
      for (let attempt = 0; attempt < 45; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 2000));
        const response = await apiRequest<unknown>(
          `/v1/me/credentials/ai/check/${jobId}`,
        );
        const job = record(response.data);
        const jobStatus = stringValue(job.status);
        if (jobStatus === "succeeded") {
          const result = record(job.result_summary);
          setNotice({
            tone: "success",
            text: `AI 连接正常，结构化输出验证通过，耗时 ${Number(result.latency_ms) || 0} ms。`,
          });
          setAiCheckState("passed");
          return true;
        }
        if (jobStatus === "failed") {
          throw new Error(`AI 检查失败：${stringValue(job.error_code) ?? "unknown"}`);
        }
      }
      throw new Error("AI 检查仍在排队，请确认私有 Worker 在线后重试。 ");
    } catch (error) {
      setAiCheckState("failed");
      setNotice({ tone: "error", text: readableApiError(error) });
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function saveAndTestAi() {
    const includeSecret = Boolean(apiKey.trim());
    if (needsNewAiKey && !includeSecret) return;
    setAiCheckState("checking");
    const saved = await saveAiCredential(includeSecret);
    if (saved) {
      await testAiCredential(false);
    } else {
      setAiCheckState("failed");
    }
  }

  function applySafePaperPreset() {
    setDesiredMode("paper");
    setCycleIntervalSeconds(300);
    setAutoRunEnabled(true);
    setNotice({
      tone: "info",
      text: "已套用推荐值：Paper 模拟盘、每 5 分钟运行。请点击“保存自动运行设置”确认。",
    });
  }

  async function applyRiskPreset(
    preset: "conservative" | "balanced" | "advanced",
  ) {
    if (!status || mfaLevel !== "aal2") {
      setNotice({ tone: "error", text: "修改资金风控前请先完成 TOTP 验证。" });
      return;
    }
    setBusy(`risk-${preset}`);
    try {
      await apiRequest("/v1/me/risk-policy", {
        method: "PUT",
        body: {
          expected_profile_version: status.expectedVersion,
          preset,
        },
      });
      setNotice({
        tone: "success",
        text: `已启用${{ conservative: "保守", balanced: "均衡", advanced: "进阶" }[preset]}风控，新任务将使用新版本。`,
      });
      await refresh();
    } catch (error) {
      setNotice({ tone: "error", text: readableApiError(error) });
    } finally {
      setBusy(null);
    }
  }

  async function copyWalletAddress() {
    if (!status?.walletAddress) return;
    try {
      await navigator.clipboard.writeText(status.walletAddress);
      setAddressCopied(true);
      window.setTimeout(() => setAddressCopied(false), 2000);
    } catch {
      setNotice({
        tone: "error",
        text: "浏览器无法复制地址，请手工选择并复制。",
      });
    }
  }

  async function beginMfaEnrollment() {
    const supabase = getSupabaseBrowserClient();
    if (!supabase) return;
    setBusy("mfa-enroll");
    try {
      const factors = await supabase.auth.mfa.listFactors();
      if (factors.error) throw factors.error;
      const selection = selectTotpFactors(factors.data.all);
      if (selection.verified) {
        setMfaFactorId(selection.verified.id);
        setMfaFactorStatus("verified");
        setMfaEnrollment(null);
        setNotice({
          tone: "info",
          text: "验证器已经绑定。请输入验证器中的 6 位动态码完成本次会话验证。",
        });
        return;
      }

      for (const factorId of selection.pendingIds) {
        const removed = await supabase.auth.mfa.unenroll({ factorId });
        if (removed.error) throw removed.error;
      }

      const { data, error } = await supabase.auth.mfa.enroll({
        factorType: "totp",
        friendlyName: POLYBOT_TOTP_FRIENDLY_NAME,
      });
      if (error) throw error;
      setMfaEnrollment({
        factorId: data.id,
        qrCode: data.totp.qr_code,
        secret: data.totp.secret,
      });
      setMfaFactorId(data.id);
      setMfaFactorStatus("unverified");
      setMfaCode("");
      setNotice({
        tone: "info",
        text:
          selection.pendingIds.length > 0
            ? "上次未完成的绑定已清理。请扫描新二维码，再输入 6 位验证码。"
            : "请用验证器扫描二维码，再输入 6 位验证码完成绑定。",
      });
    } catch (error) {
      setNotice({
        tone: "error",
        text: readableMfaError(error),
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
      const refreshed = await supabase.auth.refreshSession();
      if (refreshed.error) throw refreshed.error;
      setMfaLevel("aal2");
      setMfaFactorId(factorId);
      setMfaFactorStatus("verified");
      setMfaEnrollment(null);
      setMfaCode("");
      await refresh();
      setNotice({
        tone: "success",
        text: "双因素验证完成。本次会话已解锁，可以继续配置 AI。",
      });
    } catch (error) {
      setNotice({
        tone: "error",
        text: readableMfaError(error),
      });
    } finally {
      setBusy(null);
    }
  }

  return (
    <main className="page-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">SAFE SETUP</p>
          <h1>配置中心</h1>
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
          <strong>密钥只输入一次</strong>
          <p>
            Key 会经 HTTPS 加密保存，提交后输入框立即清空；以后运行机器人时，
            浏览器不会重复发送密钥，也不会在页面回显原文。
          </p>
        </div>
      </section>

      <section className="configuration-path" aria-label="推荐配置顺序">
        <strong>推荐顺序</strong>
        <span>1. 双因素验证</span>
        <span>2. AI Key</span>
        <span>3. Paper 模拟</span>
        <span>4. 专属钱包（小额实盘时再做）</span>
      </section>

      {notice && (
        <div className={`notice ${notice.tone} page-notice`} role="status">
          {notice.text}
        </div>
      )}

      <div className="settings-grid">
        <section className="panel ai-setup-panel" id="ai">
          <div className="section-heading">
            <div>
              <p className="eyebrow">AI QUICK CONNECT</p>
              <h2>连接你的 AI</h2>
            </div>
            <span className={`pill ${status?.aiConfigured ? "online" : "offline"}`}>
              {status?.aiConfigured ? "已配置" : "未配置"}
            </span>
          </div>

          {status?.aiConfigured && (
            <div className="ai-current-connection">
              <span className="connection-dot" aria-hidden="true" />
              <div>
                <strong>
                  当前：
                  {getAIProviderOption(
                    normalizeTenantAIProvider(status.provider),
                  ).label}
                </strong>
                <span>{status.model ?? "模型待确认"} · {status.keyMask ?? "Key 已隐藏"}</span>
              </div>
            </div>
          )}

          <div className="ai-quick-flow">
            <section className="ai-flow-step" aria-labelledby="ai-step-provider">
              <span className="ai-step-number">1</span>
              <div className="ai-step-content">
                <h3 id="ai-step-provider">选择你在哪里买的 Key</h3>
                <p>不确定就选 OpenRouter，模型已经替你选好。</p>
                <div className="provider-choice" role="radiogroup" aria-label="AI 提供商">
                  <button
                    type="button"
                    role="radio"
                    aria-checked={provider === "openrouter"}
                    className={provider === "openrouter" ? "selected" : ""}
                    onClick={() => chooseProvider("openrouter")}
                  >
                    <strong>OpenRouter</strong>
                    <span className="recommended-tag">新手推荐</span>
                    <small>一个 Key，自动选模型</small>
                  </button>
                  <button
                    type="button"
                    role="radio"
                    aria-checked={provider === "openai"}
                    className={provider === "openai" ? "selected" : ""}
                    onClick={() => chooseProvider("openai")}
                  >
                    <strong>OpenAI</strong>
                    <small>使用 OpenAI 官方 Key</small>
                  </button>
                  <button
                    type="button"
                    role="radio"
                    aria-checked={usesAdvancedProvider}
                    className={usesAdvancedProvider ? "selected" : ""}
                    onClick={() => chooseProvider("custom")}
                  >
                    <strong>其他 / 中转站</strong>
                    <small>Anthropic、LiteLLM 或自定义地址</small>
                  </button>
                </div>

                {usesAdvancedProvider && (
                  <label className="form-field compact-field" htmlFor="provider">
                    <span className="field-label">具体服务类型</span>
                    <select
                      id="provider"
                      value={provider}
                      onChange={(event) =>
                        chooseProvider(event.target.value as TenantAIProvider)
                      }
                    >
                      {AI_PROVIDER_OPTIONS.filter(
                        (option) => !["openrouter", "openai"].includes(option.value),
                      ).map((option) => (
                        <option value={option.value} key={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                    <span className="field-help">{providerOption.description}</span>
                  </label>
                )}
              </div>
            </section>

            <section className="ai-flow-step" aria-labelledby="ai-step-key">
              <span className="ai-step-number">2</span>
              <div className="ai-step-content">
                <h3 id="ai-step-key">粘贴 API Key</h3>
                <p>只提交一次；保存后输入框会立即清空，页面不会再次取回原文。</p>

                {provider === "custom" && (
                  <label className="form-field" htmlFor="base-url">
                    <span className="field-label">中转站 Base URL</span>
                    <input
                      id="base-url"
                      type="url"
                      value={baseUrl}
                      onChange={(event) => {
                        setBaseUrl(event.target.value);
                        setAiCheckState("idle");
                      }}
                      placeholder="例如 https://my-proxy.example.com/v1"
                      autoComplete="off"
                      required
                      maxLength={256}
                      aria-invalid={Boolean(baseUrl.trim() && baseUrlValidation.error)}
                      aria-describedby="base-url-help"
                    />
                    <span
                      className={`field-help ${baseUrl.trim() && baseUrlValidation.error ? "error" : ""}`}
                      id="base-url-help"
                    >
                      {baseUrl.trim()
                        ? baseUrlValidation.error ?? "HTTPS 地址格式正确，保存时后端还会做私网与重定向检查。"
                        : "复制中转站文档中的 OpenAI 兼容地址，通常以 /v1 结尾。"}
                    </span>
                  </label>
                )}

                {!providerOption.suggestedModel && (
                  <label className="form-field" htmlFor="model">
                    <span className="field-label">模型 ID</span>
                    <input
                      id="model"
                      type="text"
                      value={model}
                      onChange={(event) => {
                        setModel(event.target.value);
                        setAiCheckState("idle");
                      }}
                      placeholder={providerOption.modelPlaceholder}
                      autoComplete="off"
                    />
                  </label>
                )}

                {providerOption.suggestedModel && (
                  <div className="automatic-model">
                    <span>已自动选择模型</span>
                    <strong>{model || providerOption.suggestedModel}</strong>
                  </div>
                )}

                <label className="form-field" htmlFor="api-key">
                  <span className="field-label">
                    {needsNewAiKey
                      ? `${providerOption.label} API Key`
                      : "API Key（已保存，不更换可留空）"}
                  </span>
                  <input
                    id="api-key"
                    type="password"
                    value={apiKey}
                    onChange={(event) => {
                      setApiKey(event.target.value);
                      setAiCheckState("idle");
                    }}
                    placeholder={needsNewAiKey ? "粘贴 Key" : "留空表示继续使用已保存的 Key"}
                    autoComplete="new-password"
                    autoCapitalize="off"
                    spellCheck={false}
                  />
                </label>
                {providerOption.keyPortalUrl && needsNewAiKey && (
                  <a
                    className="key-portal-link"
                    href={providerOption.keyPortalUrl}
                    target="_blank"
                    rel="noreferrer"
                  >
                    还没有 Key？打开 {providerOption.label} 官方页面 ↗
                  </a>
                )}
              </div>
            </section>

            <section className="ai-flow-step" aria-labelledby="ai-step-check">
              <span className="ai-step-number">3</span>
              <div className="ai-step-content">
                <h3 id="ai-step-check">保存并自动检测</h3>
                <p>系统会验证 Key、模型响应和结构化输出，不会扫描市场或创建订单。</p>

                {mfaLevel !== "aal2" && (
                  <div className="ai-prerequisite">
                    <div>
                      <strong>还差一步安全验证</strong>
                      <span>先绑定验证器，才能安全保存密钥。</span>
                    </div>
                    <a className="secondary-button" href="#mfa">去完成双因素验证</a>
                  </div>
                )}

                <button
                  className="primary-button full-width"
                  type="button"
                  onClick={() => void saveAndTestAi()}
                  disabled={
                    (needsNewAiKey && !apiKey.trim()) ||
                    !model.trim() ||
                    (provider === "custom" && !baseUrlValidation.normalized) ||
                    mfaLevel !== "aal2" ||
                    busy !== null
                  }
                >
                  {busy === "ai"
                    ? "正在加密保存…"
                    : busy === "ai-check" || aiCheckState === "checking"
                      ? "正在检测连接…"
                      : status?.aiConfigured
                        ? "保存并重新检测"
                        : "保存并检测连接"}
                </button>

                {aiCheckState === "passed" && (
                  <div className="ai-validation-summary" role="status">
                    <strong>连接完成，可以开始 Paper 模拟</strong>
                    <span>✓ 密钥已加密保存</span>
                    <span>✓ 模型成功响应</span>
                    <span>✓ 决策格式验证通过</span>
                  </div>
                )}
              </div>
            </section>
          </div>

          <details className="advanced-panel ai-advanced-settings">
            <summary>高级设置：模型、调用额度与凭证管理</summary>
            <div className="form-stack">
              {providerOption.suggestedModel && (
                <label className="form-field" htmlFor="model">
                  <span className="field-label">自定义模型 ID</span>
                  <input
                    id="model"
                    type="text"
                    value={model}
                    onChange={(event) => {
                      setModel(event.target.value);
                      setAiCheckState("idle");
                    }}
                    placeholder={providerOption.modelPlaceholder}
                    autoComplete="off"
                  />
                  <span className="field-help">
                    新手建议保留推荐值；模型不可用时，连接检测会明确报错。
                  </span>
                </label>
              )}

              <div className="budget-control">
                <label className="form-field" htmlFor="ai-budget">
                  <span className="field-label">每日 AI 请求上限</span>
                  <select
                    id="ai-budget"
                    value={aiBudgetLimit}
                    onChange={(event) => setAiBudgetLimit(Number(event.target.value))}
                  >
                    <option value={100}>100 · 新手推荐</option>
                    <option value={500}>500 · Paper 观察</option>
                    <option value={2000}>2,000 · 高频自动周期</option>
                    <option value={5000}>5,000 · 高额度（谨慎）</option>
                  </select>
                </label>
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => void saveAiBudget()}
                  disabled={mfaLevel !== "aal2" || busy !== null}
                >
                  {busy === "ai-budget" ? "保存中…" : "保存调用上限"}
                </button>
                <p className="field-help">
                  今日已预留 {status?.aiUsageUsed ?? 0} / {status?.aiUsageLimit ?? 100}
                  请求单位；达到上限后自动停止新 AI 分析。
                </p>
              </div>

              {status?.aiConfigured && (
                <div className="button-row">
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => void testAiCredential()}
                    disabled={mfaLevel !== "aal2" || busy !== null}
                  >
                    {busy === "ai-check" ? "检查中…" : "仅重新检测"}
                  </button>
                  <button
                    className="danger-button"
                    type="button"
                    onClick={() => void revokeAiCredential()}
                    disabled={mfaLevel !== "aal2" || busy !== null}
                  >
                    撤销凭证
                  </button>
                </div>
              )}
            </div>
          </details>
        </section>

        <section className="panel" id="mfa">
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

          {mfaLevel === "aal2" && (
            <div className="mfa-success-callout">
              <div>
                <strong>本次会话已经验证</strong>
                <span>现在可以保存 AI Key，并继续完成新手向导。</span>
              </div>
              <div className="button-row">
                <a className="primary-button" href="#ai">继续配置 AI</a>
                <Link className="secondary-button" href="/setup">返回启动向导</Link>
              </div>
            </div>
          )}

          {mfaLevel !== "aal2" && mfaFactorStatus === "verified" && (
            <div className="mfa-state-callout">
              <strong>验证器已经绑定</strong>
              <span>无需重复绑定，直接输入验证器里当前的 6 位动态码。</span>
            </div>
          )}

          {mfaLevel !== "aal2" &&
            mfaFactorStatus === "unverified" &&
            !mfaEnrollment && (
              <div className="mfa-recovery-callout">
                <div>
                  <strong>检测到上次未完成的绑定</strong>
                  <span>
                    如果你已经扫描过旧二维码，可以直接输入动态码；否则重新生成二维码。
                  </span>
                </div>
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => void beginMfaEnrollment()}
                  disabled={loading || busy !== null}
                >
                  {busy === "mfa-enroll" ? "正在重新生成…" : "重新生成二维码"}
                </button>
              </div>
            )}

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
              <button
                className="secondary-button"
                type="button"
                onClick={() => void beginMfaEnrollment()}
                disabled={loading || busy !== null}
              >
                二维码失效？重新生成
              </button>
            </div>
          )}
          {mfaLevel !== "aal2" && (
            <div className="form-stack">
              {mfaFactorStatus === "none" && !mfaEnrollment && (
                <button
                  className="secondary-button"
                  type="button"
                  onClick={() => void beginMfaEnrollment()}
                  disabled={loading || busy !== null}
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
                    {busy === "mfa-verify"
                      ? "验证中…"
                      : mfaFactorStatus === "verified"
                        ? "验证当前会话"
                        : "完成绑定并继续"}
                  </button>
                </>
              )}
            </div>
          )}
        </section>

        <section className="panel" id="risk">
          <div className="section-heading">
            <div>
              <p className="eyebrow">RISK PRESETS</p>
              <h2>选择能承受的风险</h2>
            </div>
            <span className="pill degraded">版本化保存</span>
          </div>
          <p className="field-help">
            每次切换都会创建不可篡改的新风控版本；已有任务仍使用入队时的旧快照。
          </p>
          <div className="risk-preset-grid">
            <button type="button" className="secondary-button" disabled={busy !== null || mfaLevel !== "aal2"} onClick={() => void applyRiskPreset("conservative")}>
              保守：单笔 $2 / 最小优势 6%
            </button>
            <button type="button" className="secondary-button" disabled={busy !== null || mfaLevel !== "aal2"} onClick={() => void applyRiskPreset("balanced")}>
              均衡：单笔 $5 / 最小优势 4%
            </button>
            <button type="button" className="warning-button" disabled={busy !== null || mfaLevel !== "aal2"} onClick={() => void applyRiskPreset("advanced")}>
              进阶：单笔 $10 / 最小优势 3%
            </button>
          </div>
        </section>

        <section className="panel" id="automation">
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
            <button
              className="secondary-button full-width"
              type="button"
              onClick={applySafePaperPreset}
              disabled={busy !== null || loading}
            >
              套用新手推荐：Paper / 每 5 分钟
            </button>
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
            {["canary", "live"].includes(desiredMode) && (
              <div className="notice error">
                这是实际资金模式。自动周期并不等于持续实盘授权；仍需 AAL2
                短时解锁，并且你应先完成 Paper、Shadow 和小额旁观验证。
              </div>
            )}
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

        <section className="panel" id="wallet">
          <div className="section-heading">
            <div>
              <p className="eyebrow">TRADING WALLET</p>
              <h2>机器人专属钱包</h2>
            </div>
            <span
              className={`pill ${
                status?.walletStatus === "active"
                  ? "online"
                  : status?.walletConfigured
                    ? "degraded"
                    : "offline"
              }`}
            >
              {walletStatusLabel(status?.walletStatus, status?.walletConfigured)}
            </span>
          </div>

          <dl className="detail-list credential-status">
            <div>
              <dt>公开地址</dt>
              <dd title={status?.walletAddress}>{status?.walletAddress ?? "—"}</dd>
            </div>
            <div><dt>签名类型</dt><dd>{status?.signatureType ?? "—"}</dd></div>
            <div><dt>状态</dt><dd>{walletStatusLabel(status?.walletStatus, status?.walletConfigured)}</dd></div>
            <div><dt>网络 Chain ID</dt><dd>{status?.chainId ?? "等待 Worker 返回"}</dd></div>
            <div><dt>抵押资产合约</dt><dd title={status?.collateralToken}>{status?.collateralToken ?? "等待 Worker 返回"}</dd></div>
            <div><dt>pUSD 可用余额</dt><dd>{status?.collateralBalance === undefined ? "等待 Worker 检查" : `$${status.collateralBalance.toFixed(2)}`}</dd></div>
            <div><dt>交易授权</dt><dd>{status?.allowancesReady ? "已就绪" : "入金后由实盘 Worker 安全初始化"}</dd></div>
          </dl>

          {!status?.walletConfigured && (
            <div className="wallet-actions">
              <button
                className="primary-button full-width"
                type="button"
                onClick={generateDedicatedWallet}
                disabled={busy !== null}
              >
                一键生成全新专用钱包
              </button>
              <p className="field-help">
                私钥只在当前页面内存中生成；下载一次离线备份后再加密提交。
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
                  onChange={(event) => {
                    setPrivateKey(event.target.value);
                    setGeneratedWallet(false);
                    setBackupConfirmed(false);
                  }}
                  placeholder="0x…（提交后立即清空）"
                  autoComplete="off"
                  autoCapitalize="off"
                  spellCheck={false}
                />
              </label>
              {generatedWallet && (
                <div className="deposit-callout generated-wallet-backup">
                  <strong>只显示这一次：先保存离线备份</strong>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={downloadWalletBackup}
                  >
                    下载私钥备份
                  </button>
                  <label className="confirm-row">
                    <input
                      type="checkbox"
                      checked={backupConfirmed}
                      onChange={(event) => setBackupConfirmed(event.target.checked)}
                    />
                    <span>我已把备份保存在离线安全位置，并理解丢失后无法恢复。</span>
                  </label>
                </div>
              )}
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
                disabled={
                  !privateKey.trim() ||
                  !importConfirmed ||
                  (generatedWallet && !backupConfirmed) ||
                  mfaLevel !== "aal2" ||
                  busy !== null
                }
              >
                {busy === "import-wallet" ? "导入中…" : "加密导入专用钱包"}
              </button>
            </div>
          </details>

          {status?.walletAddress && (
            <div className="deposit-callout">
              <strong>启动资金地址</strong>
              <code>{status.walletAddress}</code>
              <button
                className="text-button"
                type="button"
                onClick={() => void copyWalletAddress()}
              >
                {addressCopied ? "已复制" : "复制地址"}
              </button>
              <p>
                仅在 Chain ID 和抵押资产都已明确后入金，并先使用可完全承受损失的小额资金。
              </p>
            </div>
          )}
          {status?.walletId && (
            <button
              className="danger-button full-width"
              type="button"
              onClick={() => void revokeWallet()}
              disabled={mfaLevel !== "aal2" || busy !== null}
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
