import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import MfaPanel from "./MfaPanel";

const { mfa } = vi.hoisted(() => ({ mfa: {
  getAuthenticatorAssuranceLevel: vi.fn(), listFactors: vi.fn(), enroll: vi.fn(),
  unenroll: vi.fn(), challengeAndVerify: vi.fn(),
} }));
vi.mock("../lib/supabase/browser", () => ({ getSupabaseBrowserClient: () => ({ auth: { mfa } }) }));
afterEach(() => { cleanup(); Object.values(mfa).forEach((mock) => mock.mockReset()); });

it("replaces only stale Polybot enrollments, verifies and clears the enrollment secret", async () => {
  let verified = false;
  mfa.getAuthenticatorAssuranceLevel.mockImplementation(async () => ({ data: { currentLevel: verified ? "aal2" : "aal1" }, error: null }));
  mfa.listFactors.mockResolvedValue({ data: { all: [
    { id: "our-pending", factor_type: "totp", status: "unverified", friendly_name: "Polybot Control" },
    { id: "other-pending", factor_type: "totp", status: "unverified", friendly_name: "Other app" },
  ] }, error: null });
  mfa.unenroll.mockResolvedValue({ error: null });
  mfa.enroll.mockResolvedValue({ data: { id: "new-factor", totp: { qr_code: "data:image/svg+xml,test", secret: "test-enrollment-secret" } }, error: null });
  mfa.challengeAndVerify.mockImplementation(async () => { verified = true; return { data: {}, error: null }; });
  const onAssuranceChange = vi.fn();
  await act(async () => { render(<MfaPanel onAssuranceChange={onAssuranceChange} />); });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "重新生成二维码" })); });
  expect(mfa.unenroll).toHaveBeenCalledExactlyOnceWith({ factorId: "our-pending" });
  expect(screen.getByText("test-enrollment-secret")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("验证器六位验证码"), { target: { value: "123456" } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "验证本次会话" })); });
  expect(mfa.challengeAndVerify).toHaveBeenCalledWith({ factorId: "new-factor", code: "123456" });
  expect(screen.queryByText("test-enrollment-secret")).toBeNull();
  expect(onAssuranceChange).toHaveBeenLastCalledWith(true);
});

it("challenges an existing factor and remains unverified on failure", async () => {
  mfa.getAuthenticatorAssuranceLevel.mockResolvedValue({ data: { currentLevel: "aal1" }, error: null });
  mfa.listFactors.mockResolvedValue({ data: { all: [
    { id: "verified-factor", factor_type: "totp", status: "verified", friendly_name: "Existing" },
  ] }, error: null });
  mfa.challengeAndVerify.mockResolvedValue({ error: { code: "mfa_verification_failed" } });
  const onAssuranceChange = vi.fn();
  await act(async () => { render(<MfaPanel onAssuranceChange={onAssuranceChange} />); });
  fireEvent.change(screen.getByLabelText("验证器六位验证码"), { target: { value: "654321" } });
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "验证本次会话" })); });
  expect(screen.getByRole("alert")).toHaveTextContent("验证码不正确");
  expect(screen.getByLabelText("验证器六位验证码")).toHaveValue("");
  expect(onAssuranceChange).not.toHaveBeenCalledWith(true);
  expect(mfa.enroll).not.toHaveBeenCalled();
  expect(mfa.unenroll).not.toHaveBeenCalled();
});
