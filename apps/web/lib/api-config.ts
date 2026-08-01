export function validatedApiBaseUrl(
  value: string | undefined,
  allowLocalHttp = process.env.NODE_ENV === "development",
): string | null {
  if (!value) return null;
  try {
    const parsed = new URL(value);
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
    return parsed.origin;
  } catch {
    return null;
  }
}

export const API_BASE_URL =
  validatedApiBaseUrl(process.env.NEXT_PUBLIC_API_BASE_URL) ?? "";
