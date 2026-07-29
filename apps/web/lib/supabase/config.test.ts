import { afterEach, describe, expect, it } from "vitest";

import { getPublicSupabaseConfig } from "./config";

const originalUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
const originalAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
const originalPublishableKey =
  process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;

function restoreEnvironmentValue(name: string, value: string | undefined) {
  if (value === undefined) {
    delete process.env[name];
  } else {
    process.env[name] = value;
  }
}

afterEach(() => {
  restoreEnvironmentValue("NEXT_PUBLIC_SUPABASE_URL", originalUrl);
  restoreEnvironmentValue("NEXT_PUBLIC_SUPABASE_ANON_KEY", originalAnonKey);
  restoreEnvironmentValue(
    "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY",
    originalPublishableKey,
  );
});

describe("getPublicSupabaseConfig", () => {
  it("returns null instead of constructing a client with empty values", () => {
    delete process.env.NEXT_PUBLIC_SUPABASE_URL;
    delete process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
    delete process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;

    expect(getPublicSupabaseConfig()).toBeNull();
  });

  it("rejects example placeholders", () => {
    process.env.NEXT_PUBLIC_SUPABASE_URL = "your_supabase_project_url";
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY = "your_supabase_anon_key";

    expect(getPublicSupabaseConfig()).toBeNull();
  });

  it("rejects stringified missing environment values", () => {
    process.env.NEXT_PUBLIC_SUPABASE_URL =
      "https://project-ref.supabase.co";
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY = "undefined";
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY = "null";

    expect(getPublicSupabaseConfig()).toBeNull();
  });

  it("accepts a valid HTTPS project and publishable key", () => {
    process.env.NEXT_PUBLIC_SUPABASE_URL =
      "https://project-ref.supabase.co";
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY = "sb_publishable_test";
    delete process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

    expect(getPublicSupabaseConfig()).toEqual({
      url: "https://project-ref.supabase.co",
      anonKey: "sb_publishable_test",
    });
  });

  it("falls back to a valid legacy anon key when publishable is a placeholder", () => {
    process.env.NEXT_PUBLIC_SUPABASE_URL =
      "https://project-ref.supabase.co";
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY =
      "sb_publishable_REPLACE_ME";
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY = "valid-legacy-anon-key";

    expect(getPublicSupabaseConfig(false)).toEqual({
      url: "https://project-ref.supabase.co",
      anonKey: "valid-legacy-anon-key",
    });
  });

  it("never accepts loopback Supabase Auth in production", () => {
    process.env.NEXT_PUBLIC_SUPABASE_URL = "http://localhost:54321";
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY = "sb_publishable_test";

    expect(getPublicSupabaseConfig(false)).toBeNull();
    expect(getPublicSupabaseConfig(true)).toEqual({
      url: "http://localhost:54321",
      anonKey: "sb_publishable_test",
    });
  });

  it("rejects credentials, paths, query strings and fragments", () => {
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY = "sb_publishable_test";
    for (const url of [
      "https://user:pass@project-ref.supabase.co",
      "https://project-ref.supabase.co/auth/v1",
      "https://project-ref.supabase.co?key=value",
      "https://project-ref.supabase.co#fragment",
    ]) {
      process.env.NEXT_PUBLIC_SUPABASE_URL = url;
      expect(getPublicSupabaseConfig(false)).toBeNull();
    }
  });

  it("rejects secret and service-role keys from the browser bundle", () => {
    process.env.NEXT_PUBLIC_SUPABASE_URL =
      "https://project-ref.supabase.co";
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY = "sb_secret_never_public";
    expect(getPublicSupabaseConfig(false)).toBeNull();

    const encode = (value: object) =>
      btoa(JSON.stringify(value))
        .replace(/\+/g, "-")
        .replace(/\//g, "_")
        .replace(/=+$/g, "");
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY = [
      encode({ alg: "HS256", typ: "JWT" }),
      encode({ role: "service_role" }),
      "signature",
    ].join(".");
    expect(getPublicSupabaseConfig(false)).toBeNull();

    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY = [
      encode({ alg: "HS256", typ: "JWT" }),
      encode({ role: "anon" }),
      "signature",
    ].join(".");
    expect(getPublicSupabaseConfig(false)?.anonKey).toContain(".");
  });
});
