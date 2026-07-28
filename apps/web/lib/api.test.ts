import { describe, expect, it } from "vitest";

import { buildApiHeaders, validatedApiBaseUrl } from "./api";

describe("buildApiHeaders", () => {
  it("uses only the session token and non-secret request metadata", () => {
    const headers = buildApiHeaders("jwt-value", true, "job-123");

    expect(headers).toEqual({
      Accept: "application/json",
      Authorization: "Bearer jwt-value",
      "Content-Type": "application/json",
      "Idempotency-Key": "job-123",
    });
    expect(Object.keys(headers).some((name) => name.startsWith("X-"))).toBe(false);
  });

  it("does not create an Authorization header for public health checks", () => {
    expect(buildApiHeaders(null, false)).toEqual({
      Accept: "application/json",
    });
  });
});

describe("validatedApiBaseUrl", () => {
  it("accepts HTTPS origins and local development HTTP", () => {
    expect(validatedApiBaseUrl("https://api.example.com/")).toBe(
      "https://api.example.com",
    );
    expect(validatedApiBaseUrl("http://localhost:8080", true)).toBe(
      "http://localhost:8080",
    );
  });

  it("fails closed when production API configuration is missing or loopback", () => {
    expect(validatedApiBaseUrl(undefined, false)).toBeNull();
    expect(validatedApiBaseUrl("", false)).toBeNull();
    expect(validatedApiBaseUrl("http://localhost:8080", false)).toBeNull();
    expect(validatedApiBaseUrl("http://127.0.0.1:8080", false)).toBeNull();
  });

  it.each([
    "http://api.example.com",
    "https://user:pass@api.example.com",
    "https://api.example.com/path",
    "https://api.example.com/?token=secret",
    "not-a-url",
  ])("rejects an unsafe API target: %s", (value) => {
    expect(validatedApiBaseUrl(value)).toBeNull();
  });
});
