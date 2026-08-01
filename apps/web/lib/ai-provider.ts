export interface AIBaseUrlValidation {
  normalized: string | null;
  error: string | null;
}

export type TenantAIProvider =
  | "openrouter"
  | "openai"
  | "anthropic"
  | "litellm"
  | "custom";

export interface AIProviderOption {
  value: TenantAIProvider;
  label: string;
  description: string;
  suggestedModel: string | null;
  modelPlaceholder: string;
  keyPortalUrl: string | null;
}

export const AI_PROVIDER_OPTIONS: readonly AIProviderOption[] = [
  {
    value: "openrouter",
    label: "OpenRouter",
    description: "一个 Key 自动选择合适模型，最适合第一次使用。",
    suggestedModel: "openrouter/auto",
    modelPlaceholder: "例如 openrouter/auto",
    keyPortalUrl: "https://openrouter.ai/settings/keys",
  },
  {
    value: "openai",
    label: "OpenAI",
    description: "直接使用 OpenAI 官方 API Key。",
    suggestedModel: "gpt-5-mini",
    modelPlaceholder: "例如 gpt-5-mini",
    keyPortalUrl: "https://platform.openai.com/api-keys",
  },
  {
    value: "anthropic",
    label: "Anthropic",
    description: "直接使用 Anthropic 官方 API Key。",
    suggestedModel: "anthropic/claude-sonnet-5",
    modelPlaceholder: "例如 anthropic/claude-sonnet-5",
    keyPortalUrl: "https://console.anthropic.com/settings/keys",
  },
  {
    value: "litellm",
    label: "LiteLLM",
    description: "使用部署方统一管理的 LiteLLM 网关。",
    suggestedModel: null,
    modelPlaceholder: "输入网关中已配置的模型 ID",
    keyPortalUrl: null,
  },
  {
    value: "custom",
    label: "自定义中转站",
    description: "连接任意公开 HTTPS、OpenAI 兼容的第三方中转站。",
    suggestedModel: null,
    modelPlaceholder: "输入中转站要求的模型 ID",
    keyPortalUrl: null,
  },
] as const;

export function isTenantAIProvider(value: unknown): value is TenantAIProvider {
  return AI_PROVIDER_OPTIONS.some((option) => option.value === value);
}

export function normalizeTenantAIProvider(value: unknown): TenantAIProvider {
  return isTenantAIProvider(value) ? value : "openrouter";
}

export function getAIProviderOption(provider: TenantAIProvider): AIProviderOption {
  return (
    AI_PROVIDER_OPTIONS.find((option) => option.value === provider) ??
    AI_PROVIDER_OPTIONS[0]
  );
}

export function initialModelForProvider(
  provider: TenantAIProvider,
  currentModel: string | undefined,
  hasConfiguredCredential: boolean,
): string {
  const current = currentModel?.trim() ?? "";
  const isPlatformDefault = current === "gpt-5.6-terra";
  if (current && (hasConfiguredCredential || !isPlatformDefault)) return current;
  return getAIProviderOption(provider).suggestedModel ?? current;
}

const blockedSuffixes = [
  ".internal",
  ".lan",
  ".local",
  ".localhost",
  ".localdomain",
  ".onion",
];

export function validateCustomAIBaseUrl(value: string): AIBaseUrlValidation {
  const candidate = value.trim();
  if (candidate.length < 8 || candidate.length > 256) {
    return {
      normalized: null,
      error: "Base URL 长度必须在 8 到 256 个字符之间。",
    };
  }
  if ([...candidate].some((character) => /\s/.test(character))) {
    return { normalized: null, error: "Base URL 不能包含空白字符。" };
  }

  try {
    const decodedCandidate = decodeURIComponent(candidate);
    if (
      decodedCandidate
        .split("/")
        .some((segment) => segment === "." || segment === "..")
    ) {
      return { normalized: null, error: "Base URL 路径不能包含跳转片段。" };
    }
    const parsed = new URL(candidate);
    const host = parsed.hostname.replace(/^\[|\]$/g, "").toLowerCase();
    const localHostname =
      host === "localhost" ||
      !host.includes(".") ||
      blockedSuffixes.some((suffix) => host.endsWith(suffix));
    const privateIpv4 = isPrivateIpv4(host);
    const ipv6Literal = host.includes(":");

    if (parsed.protocol !== "https:") {
      return { normalized: null, error: "第三方中转站必须使用 HTTPS。" };
    }
    if (parsed.username || parsed.password || parsed.search || parsed.hash) {
      return {
        normalized: null,
        error: "Base URL 不能包含账号密码、查询参数或片段。",
      };
    }
    if (localHostname || privateIpv4 || ipv6Literal) {
      return {
        normalized: null,
        error: "Base URL 必须使用可公开访问的域名，不能指向本机或私网。",
      };
    }
    if (
      parsed.pathname
        .split("/")
        .map((segment) => decodeURIComponent(segment))
        .some((segment) => segment === "." || segment === "..")
    ) {
      return { normalized: null, error: "Base URL 路径不能包含跳转片段。" };
    }

    const path = parsed.pathname.replace(/\/+$/, "");
    return {
      normalized: `${parsed.origin}${path}`,
      error: null,
    };
  } catch {
    return { normalized: null, error: "请输入有效的 HTTPS Base URL。" };
  }
}

function isPrivateIpv4(host: string): boolean {
  if (!/^\d{1,3}(?:\.\d{1,3}){3}$/.test(host)) return false;
  const octets = host.split(".").map(Number);
  if (octets.some((value) => value < 0 || value > 255)) return true;
  const [first, second] = octets;
  return (
    first === 0 ||
    first === 10 ||
    first === 127 ||
    (first === 100 && second >= 64 && second <= 127) ||
    (first === 169 && second === 254) ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168) ||
    first >= 224
  );
}
