import { NextResponse } from "next/server";

import { safeInternalPath } from "../../../lib/navigation";
import { createServerSupabaseClient } from "../../../lib/supabase/server";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const code = url.searchParams.get("code");
  const next = safeInternalPath(url.searchParams.get("next"));
  const supabase = await createServerSupabaseClient();

  if (code && supabase) {
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (!error) return NextResponse.redirect(new URL(next, url.origin));
  }

  const loginUrl = new URL("/login", url.origin);
  loginUrl.searchParams.set("error", "认证链接无效或已过期");
  return NextResponse.redirect(loginUrl);
}
