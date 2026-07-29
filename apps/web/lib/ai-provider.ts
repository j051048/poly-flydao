export interface AIBaseUrlValidation {
  normalized: string | null;
  error: string | null;
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
