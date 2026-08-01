import { describe, expect, it } from "vitest";

import { evaluateDashboardCors } from "./deployment-diagnostics";

describe("evaluateDashboardCors", () => {
  it("accepts the exact dashboard origin and every sensitive request field", () => {
    expect(
      evaluateDashboardCors(
        "https://poly-flydao.vercel.app",
        "https://poly-flydao.vercel.app",
        "GET, POST, PUT, DELETE, OPTIONS",
        "Authorization, Content-Type, Idempotency-Key",
      ),
    ).toEqual({
      ok: true,
      allowsOrigin: true,
      allowsPut: true,
      allowsAuthorization: true,
      allowsContentType: true,
      allowsIdempotencyKey: true,
    });
  });

  it("identifies a stale backend even when public GET endpoints are online", () => {
    expect(
      evaluateDashboardCors(
        "https://poly-flydao.vercel.app",
        null,
        "GET, POST, OPTIONS",
        "Authorization, Content-Type",
      ),
    ).toEqual({
      ok: false,
      allowsOrigin: false,
      allowsPut: false,
      allowsAuthorization: true,
      allowsContentType: true,
      allowsIdempotencyKey: false,
    });
  });
});
