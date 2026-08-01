import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

import { getPublicSupabaseConfig } from "./config";

const PUBLIC_PATHS = new Set([
  "/login",
  "/register",
  "/diagnostics",
  "/api/deployment-check",
  "/auth/callback",
]);

export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.has(pathname);
}

export function copyResponseCookies(
  source: NextResponse,
  target: NextResponse,
): NextResponse {
  source.cookies.getAll().forEach((cookie) => {
    target.cookies.set(cookie);
  });
  return target;
}

export async function refreshAuthSession(request: NextRequest): Promise<NextResponse> {
  const config = getPublicSupabaseConfig();
  const pathname = request.nextUrl.pathname;
  const isPublic = isPublicPath(pathname);
  if (!config) {
    if (isPublic) return NextResponse.next({ request });
    const loginUrl = request.nextUrl.clone();
    loginUrl.pathname = "/login";
    loginUrl.search = "";
    loginUrl.searchParams.set("error", "auth_not_configured");
    return NextResponse.redirect(loginUrl);
  }

  let response = NextResponse.next({ request });
  const supabase = createServerClient(config.url, config.anonKey, {
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      setAll(cookiesToSet) {
        cookiesToSet.forEach(({ name, value }) => request.cookies.set(name, value));
        response = NextResponse.next({ request });
        cookiesToSet.forEach(({ name, value, options }) => {
          response.cookies.set(name, value, options);
        });
      },
    },
  });

  let user: { id: string } | null = null;
  try {
    const result = await supabase.auth.getUser();
    user = result.data.user;
  } catch {
    // Fail closed for protected pages when the identity provider is
    // unavailable, while keeping login/register renderable.
  }

  if (!user && !isPublic) {
    const loginUrl = request.nextUrl.clone();
    loginUrl.pathname = "/login";
    loginUrl.searchParams.set("next", pathname);
    return copyResponseCookies(response, NextResponse.redirect(loginUrl));
  }

  if (user && (pathname === "/login" || pathname === "/register")) {
    const homeUrl = request.nextUrl.clone();
    homeUrl.pathname = "/";
    homeUrl.search = "";
    return copyResponseCookies(response, NextResponse.redirect(homeUrl));
  }

  return response;
}
