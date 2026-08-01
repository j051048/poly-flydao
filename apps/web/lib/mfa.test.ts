import { describe, expect, it } from "vitest";

import { readableMfaError, selectTotpFactors } from "./mfa";

describe("selectTotpFactors", () => {
  it("prefers a verified TOTP factor even when an unfinished factor also exists", () => {
    const selection = selectTotpFactors([
      {
        id: "pending-1",
        factor_type: "totp",
        status: "unverified",
        friendly_name: "Polybot Control",
      },
      {
        id: "verified-1",
        factor_type: "totp",
        status: "verified",
        friendly_name: "My authenticator",
      },
    ]);

    expect(selection.verified?.id).toBe("verified-1");
    expect(selection.pendingIds).toEqual(["pending-1"]);
  });

  it("finds all stale Polybot enrollments without touching unrelated factors", () => {
    const selection = selectTotpFactors([
      {
        id: "pending-1",
        factor_type: "totp",
        status: "unverified",
        friendly_name: "Polybot Control",
      },
      {
        id: "pending-other",
        factor_type: "totp",
        status: "unverified",
        friendly_name: "Another app",
      },
      {
        id: "phone-1",
        factor_type: "phone",
        status: "verified",
      },
    ]);

    expect(selection.pending?.id).toBe("pending-1");
    expect(selection.pendingIds).toEqual(["pending-1"]);
    expect(selection.verified).toBeNull();
  });
});

describe("readableMfaError", () => {
  it("turns the Supabase friendly-name conflict into a recovery instruction", () => {
    expect(
      readableMfaError({
        code: "mfa_factor_name_conflict",
        message: "A factor with the friendly name already exists",
      }),
    ).toContain("重新生成二维码");
  });
});
