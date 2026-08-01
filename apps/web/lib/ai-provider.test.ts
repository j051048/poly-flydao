import { describe, expect, it } from "vitest";

import {
  getAIProviderOption,
  initialModelForProvider,
  normalizeTenantAIProvider,
  validateCustomAIBaseUrl,
} from "./ai-provider";

describe("AI provider presets", () => {
  it("falls back from platform-only profiles to the beginner OpenRouter option", () => {
    expect(normalizeTenantAIProvider("platform")).toBe("openrouter");
    expect(getAIProviderOption("openrouter").suggestedModel).toBe(
      "openrouter/auto",
    );
  });

  it("replaces the server platform default only before a tenant credential exists", () => {
    expect(
      initialModelForProvider("openrouter", "gpt-5.6-terra", false),
    ).toBe("openrouter/auto");
    expect(
      initialModelForProvider("openai", "tenant-custom-model", true),
    ).toBe("tenant-custom-model");
  });
});

describe("validateCustomAIBaseUrl", () => {
  it("normalizes an OpenAI-compatible HTTPS relay", () => {
    expect(
      validateCustomAIBaseUrl(" https://Relay.Example.com/v1/ "),
    ).toEqual({
      normalized: "https://relay.example.com/v1",
      error: null,
    });
  });

  it.each([
    "http://relay.example.com/v1",
    "https://localhost:8443/v1",
    "https://worker.internal/v1",
    "https://127.0.0.1/v1",
    "https://10.0.0.8/v1",
    "https://169.254.169.254/latest",
    "https://user:pass@relay.example.com/v1",
    "https://relay.example.com/v1?token=secret",
    "https://relay.example.com/v1#fragment",
    "https://relay.example.com/v1/%2e%2e/admin",
  ])("rejects an unsafe relay URL: %s", (value) => {
    expect(validateCustomAIBaseUrl(value).normalized).toBeNull();
  });
});
