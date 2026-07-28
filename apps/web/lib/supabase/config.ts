export interface PublicSupabaseConfig {
  url: string;
  anonKey: string;
}

function isConfiguredValue(value: string | undefined): value is string {
  if (!value) return false;
  const normalized = value.trim().toLowerCase();
  return (
    normalized.length > 0 &&
    !normalized.startsWith("your_") &&
    !normalized.includes("project_ref") &&
    !normalized.includes("replace_me") &&
    !normalized.includes("<")
  );
}

function browserSafeSupabaseKey(value: string | undefined): value is string {
  if (!isConfiguredValue(value)) return false;
  const normalized = value.trim();
  if (
    normalized.toLowerCase().startsWith("sb_secret_") ||
    normalized.toLowerCase().includes("service_role")
  ) {
    return false;
  }
  const segments = normalized.split(".");
  if (segments.length === 3) {
    try {
      const base64 = segments[1].replace(/-/g, "+").replace(/_/g, "/");
      const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
      const payload = JSON.parse(globalThis.atob(padded)) as {
        role?: unknown;
      };
      if (
        payload.role === "service_role" ||
        payload.role === "supabase_admin"
      ) {
        return false;
      }
    } catch {
      // A publishable key can be opaque. Only reject a JWT when its privileged
      // role can be decoded; Supabase will reject malformed values itself.
    }
  }
  return true;
}

export function getPublicSupabaseConfig(
  allowLocalHttp = process.env.NODE_ENV === "development",
): PublicSupabaseConfig | null {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL?.trim();
  const anonKey = [
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY?.trim(),
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY?.trim(),
  ].find(browserSafeSupabaseKey);

  if (!isConfiguredValue(url) || !anonKey) return null;

  try {
    const parsed = new URL(url);
    const loopback =
      parsed.hostname === "localhost" ||
      parsed.hostname === "127.0.0.1" ||
      parsed.hostname === "[::1]";
    if (
      (parsed.protocol !== "https:" &&
        !(allowLocalHttp && parsed.protocol === "http:" && loopback)) ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash ||
      !["", "/"].includes(parsed.pathname)
    ) {
      return null;
    }
    return { url: parsed.origin, anonKey };
  } catch {
    return null;
  }
}
