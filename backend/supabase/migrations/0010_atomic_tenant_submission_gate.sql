begin;

-- This is the last durable authorization gate before an already-signed order
-- can leave a multi-tenant worker.  It deliberately re-checks every mutable
-- authority in the same statement that moves the order to `submitting`.
create or replace function public.mark_tenant_order_submitting(
  p_account_id uuid,
  p_intent_hash text,
  p_worker_fencing_token bigint,
  p_control_version bigint,
  p_job_id uuid,
  p_claimed_by text,
  p_job_fencing_token bigint,
  p_profile_version bigint,
  p_risk_policy_id uuid,
  p_risk_policy_version bigint,
  p_mode text
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  transitioned_count integer;
begin
  if p_mode not in ('canary', 'live') then
    return false;
  end if;

  update public.orders as order_row
     set status = 'submitting',
         attempt_count = order_row.attempt_count + 1
   where order_row.account_id = p_account_id
     and order_row.client_order_id = p_intent_hash
     and order_row.status = 'signed'
     and order_row.environment = p_mode
     and order_row.worker_fencing_token = p_worker_fencing_token
     and exists (
       select 1
         from public.worker_leases as worker_lease
         join public.cycle_jobs as job
           on job.id = p_job_id
          and job.account_id = p_account_id
         join public.runtime_controls as control
           on control.account_id = p_account_id
         join public.account_runtime_profiles as profile
           on profile.account_id = p_account_id
         join public.risk_policies as risk
           on risk.id = p_risk_policy_id
          and risk.account_id = p_account_id
         join public.trading_wallets as wallet
           on wallet.id = job.trading_wallet_id
          and wallet.account_id = p_account_id
         join public.credential_refs as ai_credential
           on ai_credential.id = job.ai_credential_id
          and ai_credential.account_id = p_account_id
         join public.credential_refs as signer_credential
           on signer_credential.id = wallet.signer_credential_id
          and signer_credential.account_id = p_account_id
        where worker_lease.account_id = p_account_id
          and worker_lease.fencing_token = p_worker_fencing_token
          and worker_lease.expires_at > now()
          and job.claimed_by = p_claimed_by
          and job.fencing_token = p_job_fencing_token
          and job.status = 'running'
          and job.lease_expires_at > now()
          and job.mode = p_mode
          and job.risk_policy_id = p_risk_policy_id
          and job.risk_policy_version = p_risk_policy_version
          and control.version = p_control_version
          and control.mode = p_mode
          and control.armed
          and control.accept_new_intents
          and not control.kill_switch
          and not control.cancellation_pending
          and control.armed_until > now()
          and profile.version = p_profile_version
          and profile.status = 'active'
          and profile.desired_mode = p_mode
          and profile.ai_credential_id = job.ai_credential_id
          and profile.trading_wallet_id = job.trading_wallet_id
          and profile.risk_policy_id = job.risk_policy_id
          and risk.version = p_risk_policy_version
          and risk.status = 'active'
          and wallet.status = 'active'
          and ai_credential.kind = 'ai_api_key'
          and ai_credential.status = 'active'
          and signer_credential.kind = 'evm_signer_key'
          and signer_credential.status = 'active'
     );

  get diagnostics transitioned_count = row_count;
  return transitioned_count = 1;
end;
$$;

revoke all on function public.mark_tenant_order_submitting(
  uuid, text, bigint, bigint, uuid, text, bigint, bigint, uuid, bigint, text
) from public, anon, authenticated;
grant execute on function public.mark_tenant_order_submitting(
  uuid, text, bigint, bigint, uuid, text, bigint, bigint, uuid, bigint, text
) to service_role;

commit;
