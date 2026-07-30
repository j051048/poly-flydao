-- Persist a deliberately compact, non-secret execution summary so the tenant
-- can understand what the autonomous worker did after a page refresh.

alter table public.cycle_jobs
  add column result_summary jsonb;

alter table public.cycle_jobs
  add constraint cycle_jobs_result_summary_shape
  check (
    result_summary is null
    or (
      jsonb_typeof(result_summary) = 'object'
      and length(result_summary::text) <= 16384
    )
  );

create or replace function public.complete_cycle_job_with_result(
  p_account_id uuid,
  p_job_id uuid,
  p_claimed_by text,
  p_fencing_token bigint,
  p_result_summary jsonb
)
returns setof public.cycle_jobs
language plpgsql
security definer
set search_path = ''
as $$
begin
  if jsonb_typeof(coalesce(p_result_summary, '{}'::jsonb)) <> 'object'
     or length(coalesce(p_result_summary, '{}'::jsonb)::text) > 16384
  then
    raise exception 'invalid cycle result summary' using errcode = '22023';
  end if;

  return query
  update public.cycle_jobs as job
  set
    status = 'succeeded',
    completed_at = now(),
    lease_expires_at = null,
    result_summary = coalesce(p_result_summary, '{}'::jsonb)
  where job.id = p_job_id
    and job.account_id = p_account_id
    and job.claimed_by = p_claimed_by
    and job.fencing_token = p_fencing_token
    and job.status in ('claimed', 'running')
    and job.lease_expires_at > now()
  returning job.*;
end;
$$;

revoke all on function public.complete_cycle_job_with_result(
  uuid, uuid, text, bigint, jsonb
) from public, anon, authenticated;

grant execute on function public.complete_cycle_job_with_result(
  uuid, uuid, text, bigint, jsonb
) to service_role;

comment on column public.cycle_jobs.result_summary is
  'Compact tenant-visible worker result. Must contain counters/reason codes only, never secrets or model prompts.';

comment on function public.complete_cycle_job_with_result(
  uuid, uuid, text, bigint, jsonb
) is
  'Fenced worker completion that persists a bounded, non-secret cycle summary.';
