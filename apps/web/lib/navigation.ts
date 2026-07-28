export function safeInternalPath(value: string | null | undefined): string {
  if (
    !value ||
    !value.startsWith("/") ||
    value.startsWith("//") ||
    value.includes("\\") ||
    /%(?:2f|5c)/i.test(value) ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    return "/";
  }
  try {
    const base = "https://polybot.invalid";
    const parsed = new URL(value, base);
    if (parsed.origin !== base) return "/";
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return "/";
  }
}
