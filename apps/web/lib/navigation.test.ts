import { describe, expect, it } from "vitest";

import { safeInternalPath } from "./navigation";

describe("safeInternalPath", () => {
  it("keeps normal application-relative destinations", () => {
    expect(safeInternalPath("/settings?tab=wallet#status")).toBe(
      "/settings?tab=wallet#status",
    );
  });

  it.each([
    "https://evil.example",
    "//evil.example",
    "/\\evil.example",
    "/%5cevil.example",
    "/%2fevil.example",
    "/\u0000evil",
  ])("rejects an external or ambiguous redirect: %s", (value) => {
    expect(safeInternalPath(value)).toBe("/");
  });
});
