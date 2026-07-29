import { describe, expect, it } from "vitest";

import { validateCustomAIBaseUrl } from "./ai-provider";

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
