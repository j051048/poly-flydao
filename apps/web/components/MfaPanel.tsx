"use client";

import { useCallback, useEffect, useState } from "react";
import { getSupabaseBrowserClient } from "../lib/supabase/browser";
import {
  POLYBOT_TOTP_FRIENDLY_NAME, readableMfaError, selectTotpFactors,
  type MfaFactorSelection,
} from "../lib/mfa";

/** Enrollment secrets stay in component memory and are cleared after verification. */
export default function MfaPanel({ onAssuranceChange }: {
  onAssuranceChange?: (verified: boolean) => void;
}) {
  const [factors, setFactors] = useState<MfaFactorSelection | null>(null);
  const [verified, setVerified] = useState(false);
  const [enrollment, setEnrollment] = useState<{ id: string; qr: string; secret: string } | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const client = getSupabaseBrowserClient();
    if (!client) {
      setError("登录服务未配置，请先完成 Supabase 配置。");
      onAssuranceChange?.(false);
      return;
    }
    try {
      const [level, listed] = await Promise.all([
        client.auth.mfa.getAuthenticatorAssuranceLevel(), client.auth.mfa.listFactors(),
      ]);
      if (level.error) throw level.error;
      if (listed.error) throw listed.error;
      const ready = level.data.currentLevel === "aal2";
      setFactors(selectTotpFactors(listed.data.all));
      setVerified(ready);
      onAssuranceChange?.(ready);
      setError(null);
    } catch (caught) {
      setVerified(false);
      onAssuranceChange?.(false);
      setError(readableMfaError(caught));
    }
  }, [onAssuranceChange]);

  useEffect(() => { void refresh(); }, [refresh]);

  async function enroll() {
    const client = getSupabaseBrowserClient();
    if (!client || busy) return;
    setBusy(true);
    setError(null);
    setEnrollment(null);
    setCode("");
    try {
      // Re-read before cleanup. Only incomplete enrollments owned by this UI
      // may be replaced; verified or unrelated factors must never be removed.
      const listed = await client.auth.mfa.listFactors();
      if (listed.error) throw listed.error;
      const current = selectTotpFactors(listed.data.all);
      setFactors(current);
      if (current.verified) return;
      for (const factorId of current.pendingIds) {
        const removed = await client.auth.mfa.unenroll({ factorId });
        if (removed.error) throw removed.error;
      }
      const result = await client.auth.mfa.enroll({
        factorType: "totp", friendlyName: POLYBOT_TOTP_FRIENDLY_NAME,
      });
      if (result.error) throw result.error;
      setEnrollment({ id: result.data.id, qr: result.data.totp.qr_code, secret: result.data.totp.secret });
    } catch (caught) {
      setError(readableMfaError(caught));
    } finally { setBusy(false); }
  }

  async function verify() {
    const client = getSupabaseBrowserClient();
    const factorId = enrollment?.id ?? factors?.verified?.id;
    if (!client || !factorId || !/^\d{6}$/.test(code) || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await client.auth.mfa.challengeAndVerify({ factorId, code });
      if (result.error) throw result.error;
      setEnrollment(null);
      setCode("");
      await refresh();
    } catch (caught) {
      setCode("");
      setError(readableMfaError(caught));
    } finally { setBusy(false); }
  }

  return <section className="panel" id="mfa" aria-label="双因素验证">
    <div className="section-heading">
      <div><p className="eyebrow">ACCOUNT SECURITY</p><h2>双因素验证</h2></div>
      <span className={`pill ${verified ? "online" : "degraded"}`}>{verified ? "会话已验证" : "需要验证"}</span>
    </div>
    <p className="field-help">修改风控参数前，请使用验证器完成本次登录的双因素验证。</p>
    {error && <p className="notice error" role="alert">{error}</p>}
    {verified ? <p role="status">当前会话已通过双因素验证，可以提交风控配置。</p> : <>
      {!factors?.verified && !enrollment && <button className="secondary-button" type="button" onClick={() => void enroll()} disabled={busy || !factors}>
        {busy ? "准备中…" : factors?.pending ? "重新生成二维码" : "绑定验证器"}
      </button>}
      {enrollment && <div className="mfa-enrollment">
        {/* SDK-generated data URL rendered as an image, never executable SVG markup. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={enrollment.qr} alt="使用验证器扫描此二维码" width={200} height={200} />
        <div><p>用验证器扫描二维码，或手动填写下方密钥。请勿分享此密钥。</p><code>{enrollment.secret}</code></div>
      </div>}
      {(enrollment || factors?.verified) && <form className="form-stack" onSubmit={(event) => { event.preventDefault(); void verify(); }}>
        <label className="form-field"><span className="field-label">验证器六位验证码</span>
          <input value={code} onChange={(event) => setCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
            inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" maxLength={6} required disabled={busy} />
        </label>
        <button className="primary-button" type="submit" disabled={busy || !/^\d{6}$/.test(code)}>{busy ? "验证中…" : "验证本次会话"}</button>
      </form>}
    </>}
    <button className="text-button" type="button" onClick={() => void refresh()} disabled={busy}>刷新验证状态</button>
  </section>;
}
