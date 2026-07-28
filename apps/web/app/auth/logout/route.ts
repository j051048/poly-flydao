import { NextResponse } from "next/server";

import { createServerSupabaseClient } from "../../../lib/supabase/server";

export async function POST(request: Request) {
  const supabase = await createServerSupabaseClient();
  if (supabase) await supabase.auth.signOut();
  return NextResponse.redirect(new URL("/login", request.url), 303);
}
