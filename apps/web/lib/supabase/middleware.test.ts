import { NextResponse } from "next/server";
import { describe, expect, it } from "vitest";

import { copyResponseCookies, isPublicPath } from "./middleware";

describe("isPublicPath", () => {
  it.each([
    "/login",
    "/register",
    "/diagnostics",
    "/api/deployment-check",
    "/auth/callback",
  ])("keeps the deployment and authentication recovery path public: %s", (path) => {
    expect(isPublicPath(path)).toBe(true);
  });

  it("keeps tenant control routes protected", () => {
    expect(isPublicPath("/settings")).toBe(false);
    expect(isPublicPath("/api/tenant-secrets")).toBe(false);
  });
});

describe("copyResponseCookies", () => {
  it("preserves refreshed Supabase cookies on an auth redirect", () => {
    const refreshed = NextResponse.next();
    refreshed.cookies.set("sb-session", "rotated", {
      httpOnly: true,
      sameSite: "lax",
      secure: true,
    });
    const redirect = NextResponse.redirect(new URL("https://app.example/login"));

    const result = copyResponseCookies(refreshed, redirect);

    expect(result.cookies.get("sb-session")?.value).toBe("rotated");
    expect(result.headers.get("location")).toBe("https://app.example/login");
  });
});
