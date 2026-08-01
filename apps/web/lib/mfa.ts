export const POLYBOT_TOTP_FRIENDLY_NAME = "Polybot Control";

export interface MfaFactorRecord {
  id: string;
  factor_type: string;
  status: string;
  friendly_name?: string;
}

export interface MfaFactorSelection {
  verified: MfaFactorRecord | null;
  pending: MfaFactorRecord | null;
  pendingIds: string[];
}

function isMfaFactorRecord(value: unknown): value is MfaFactorRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const factor = value as Record<string, unknown>;
  return (
    typeof factor.id === "string" &&
    typeof factor.factor_type === "string" &&
    typeof factor.status === "string"
  );
}

export function selectTotpFactors(values: readonly unknown[]): MfaFactorSelection {
  const totpFactors = values.filter(
    (value): value is MfaFactorRecord =>
      isMfaFactorRecord(value) && value.factor_type === "totp",
  );
  const sameName = (factor: MfaFactorRecord) =>
    factor.friendly_name?.trim().toLowerCase() ===
    POLYBOT_TOTP_FRIENDLY_NAME.toLowerCase();
  const verifiedFactors = totpFactors.filter(
    (factor) => factor.status === "verified",
  );
  const pendingFactors = totpFactors.filter(
    (factor) => factor.status === "unverified" && sameName(factor),
  );

  return {
    verified:
      verifiedFactors.find((factor) => sameName(factor)) ??
      verifiedFactors[0] ??
      null,
    pending: pendingFactors[0] ?? null,
    pendingIds: pendingFactors.map((factor) => factor.id),
  };
}

export function readableMfaError(error: unknown): string {
  const details =
    typeof error === "object" && error !== null
      ? (error as Record<string, unknown>)
      : {};
  const code = typeof details.code === "string" ? details.code : "";
  const message = error instanceof Error ? error.message : "";

  if (
    code === "mfa_factor_name_conflict" ||
    message.toLowerCase().includes("friendly name")
  ) {
    return "检测到上次未完成的双因素绑定。请点击“重新生成二维码”继续。";
  }
  if (code === "mfa_verification_failed") {
    return "验证码不正确，请等待验证器生成新码后重试。";
  }
  if (code === "mfa_challenge_expired") {
    return "验证码挑战已过期，请输入当前验证码后重新提交。";
  }
  if (code === "mfa_factor_not_found") {
    return "原双因素记录已失效，请刷新状态后重新绑定。";
  }
  return message || "双因素验证暂时失败，请稍后重试。";
}
