import { NextResponse } from "next/server";
import { describe, expect, it } from "vitest";

import { copyResponseCookies } from "./middleware";

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
