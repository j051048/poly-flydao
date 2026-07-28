"use client";

import { createBrowserClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";

import { getPublicSupabaseConfig } from "./config";

let browserClient: SupabaseClient | null | undefined;

export function isSupabaseBrowserConfigured(): boolean {
  return getPublicSupabaseConfig() !== null;
}

export function getSupabaseBrowserClient(): SupabaseClient | null {
  if (browserClient !== undefined) return browserClient;

  const config = getPublicSupabaseConfig();
  browserClient = config
    ? createBrowserClient(config.url, config.anonKey)
    : null;
  return browserClient;
}

export function resetSupabaseBrowserClientForTests(): void {
  browserClient = undefined;
}
