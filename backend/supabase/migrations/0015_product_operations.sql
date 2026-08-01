begin;

-- Product/operations closure for the tenant queue.  This migration keeps all
-- secret material behind the existing worker-only envelope boundary; every
-- new tenant-visible table contains metadata or bounded public summaries only.

alter table public.trading_wallets
  add column collateral_balance_pusd numeric(38, 18),
  add column allowances_ready boolean not null default false,
  add column readiness_checked_at timestamptz;

alter table public.trading_wallets
  add constraint trading_wallets_collateral_balance_nonnegative
  check (collateral_balance_pusd is null or collateral_balance_pusd >= 0);

alter table public.markets
  add column resolved_outcome text,
  add column resolution_confirmed_at timestamptz;

alter table public.markets
  add constraint markets_resolved_outcome_allowed
  check (resolved_outcome is null or resolved_outcome in ('YES', 'NO')),
  add constraint markets_resolution_shape
  check (
    (resolved_outcome is null and resolution_confirmed_at is null)
    or (resolved_outcome is not null and resolution_confirmed_at is not null and closed)
  );

create table public.worker_heartbeats (
  owner_id text primary key,
  release text not null,
  status text not null default 'starting',
  active_jobs integer not null default 0,
  queue_lag_seconds numeric(20, 3),
  started_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  details jsonb not null default '{}'::jsonb,
  constraint worker_heartbeats_owner_shape
    check (length(btrim(owner_id)) between 8 and 160),
  constraint worker_heartbeats_release_shape
    check (length(btrim(release)) between 1 and 80),
  constraint worker_heartbeats_status_allowed
    check (status in ('starting', 'ready', 'degraded', 'stopping')),
  constraint worker_heartbeats_active_jobs_range
    check (active_jobs between 0 and 32),
  constraint worker_heartbeats_queue_lag_nonnegative
    check (queue_lag_seconds is null or queue_lag_seconds >= 0),
  constraint worker_heartbeats_details_shape
    check (jsonb_typeof(details) = 'object' and length(details::text) <= 4096)
);

create index worker_heartbeats_last_seen_idx
  on public.worker_heartbeats(last_seen_at desc);

create table public.account_notifications (
  id bigint generated always as identity primary key,
  account_id uuid not null references auth.users(id) on delete cascade,
  severity text not null,
  code text not null,
  title text not null,
  message text not null,
  details jsonb not null default '{}'::jsonb,
  read_at timestamptz,
  created_at timestamptz not null default now(),
  constraint account_notifications_severity_allowed
    check (severity in ('info', 'warning', 'critical')),
  constraint account_notifications_code_shape
    check (code ~ '^[a-z0-9_]{1,64}$'),
  constraint account_notifications_text_shape
    check (length(title) between 1 and 120 and length(message) between 1 and 500),
  constraint account_notifications_details_shape
    check (jsonb_typeof(details) = 'object' and length(details::text) <= 8192)
);

create index account_notifications_account_created_idx
  on public.account_notifications(account_id, created_at desc);

create or replace function public.notify_wallet_failure()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.status = 'failed' and old.status is distinct from new.status then
    insert into public.account_notifications(
      account_id, severity, code, title, message, details
    ) values (
      new.account_id, 'critical', 'wallet_verification_failed',
      '机器人钱包验证失败',
      '钱包未进入可用状态，自动实盘已保持锁定。请检查 Worker 日志后重新配置。',
      jsonb_build_object('wallet_id', new.id)
    );
  end if;
  return new;
end;
$$;

create trigger trading_wallets_failure_notification
after update on public.trading_wallets
for each row execute function public.notify_wallet_failure();

create or replace function public.notify_critical_risk_event()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.severity = 'critical' or new.action in ('halt', 'cancel_all') then
    insert into public.account_notifications(
      account_id, severity, code, title, message, details
    ) values (
      new.account_id, 'critical', 'risk_control_triggered',
      '风控已阻止或停止交易',
      left(new.message, 500),
      jsonb_build_object('risk_event_id', new.id, 'risk_code', new.code)
    );
  end if;
  return new;
end;
$$;

create trigger risk_events_critical_notification
after insert on public.risk_events
for each row execute function public.notify_critical_risk_event();

-- Paper state is one bounded JSON document so a transient per-job runtime can
-- resume positions without mixing simulation holdings with real positions.
create table public.paper_account_states (
  account_id uuid primary key references auth.users(id) on delete cascade,
  state jsonb not null default '{}'::jsonb,
  version bigint not null default 1,
  updated_at timestamptz not null default now(),
  constraint paper_account_states_object
    check (jsonb_typeof(state) = 'object' and length(state::text) <= 524288),
  constraint paper_account_states_version_positive check (version > 0)
);

create table public.ai_diagnostic_jobs (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references auth.users(id) on delete cascade,
  credential_id uuid not null,
  provider text not null,
  ai_base_url text,
  model text not null,
  status text not null default 'queued',
  claimed_by text,
  fencing_token bigint not null default 0,
  lease_expires_at timestamptz,
  result_summary jsonb,
  error_code text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  completed_at timestamptz,
  updated_at timestamptz not null default now(),
  foreign key (account_id, credential_id)
    references public.credential_refs(account_id, id) on delete restrict,
  constraint ai_diagnostic_jobs_provider_allowed
    check (provider in ('openai', 'anthropic', 'openrouter', 'litellm', 'custom')),
  constraint ai_diagnostic_jobs_model_shape
    check (length(model) between 1 and 128 and model ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]*$'),
  constraint ai_diagnostic_jobs_status_allowed
    check (status in ('queued', 'claimed', 'running', 'succeeded', 'failed')),
  constraint ai_diagnostic_jobs_fence_shape
    check (fencing_token >= 0),
  constraint ai_diagnostic_jobs_result_shape
    check (
      result_summary is null
      or (jsonb_typeof(result_summary) = 'object' and length(result_summary::text) <= 8192)
    ),
  constraint ai_diagnostic_jobs_error_shape
    check (error_code is null or error_code ~ '^[a-z0-9_]{1,64}$')
);

create unique index ai_diagnostic_jobs_one_active_per_account
  on public.ai_diagnostic_jobs(account_id)
  where status in ('queued', 'claimed', 'running');
create index ai_diagnostic_jobs_queue_idx
  on public.ai_diagnostic_jobs(created_at)
  where status = 'queued';

create table public.forecast_outcomes (
  forecast_id uuid primary key references public.forecasts(id) on delete cascade,
  account_id uuid not null references auth.users(id) on delete cascade,
  market_id uuid not null references public.markets(id) on delete restrict,
  probability_yes numeric(10, 9) not null,
  resolved_yes boolean not null,
  resolved_at timestamptz not null,
  brier_score numeric(20, 18) not null,
  log_loss numeric(20, 18) not null,
  created_at timestamptz not null default now(),
  constraint forecast_outcomes_scores_nonnegative
    check (brier_score >= 0 and log_loss >= 0),
  constraint forecast_outcomes_probability_range
    check (probability_yes between 0 and 1)
);

create table public.ai_usage_daily (
  account_id uuid not null references auth.users(id) on delete cascade,
  usage_day date not null default ((now() at time zone 'utc')::date),
  request_units integer not null default 0,
  request_limit integer not null default 100,
  updated_at timestamptz not null default now(),
  primary key (account_id, usage_day),
  constraint ai_usage_daily_units_range
    check (request_units >= 0 and request_limit between 1 and 10000)
);

create table public.ai_budget_settings (
  account_id uuid primary key references auth.users(id) on delete cascade,
  request_limit integer not null default 100,
  updated_at timestamptz not null default now(),
  constraint ai_budget_settings_limit_range
    check (request_limit between 20 and 10000)
);

create index forecast_outcomes_account_resolved_idx
  on public.forecast_outcomes(account_id, resolved_at desc);

create or replace function public.record_worker_heartbeat(
  p_owner_id text,
  p_release text,
  p_status text,
  p_active_jobs integer,
  p_queue_lag_seconds numeric default null,
  p_details jsonb default '{}'::jsonb
)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_status not in ('starting', 'ready', 'degraded', 'stopping')
     or p_active_jobs not between 0 and 32
     or jsonb_typeof(coalesce(p_details, '{}'::jsonb)) <> 'object'
  then
    raise exception 'invalid worker heartbeat' using errcode = '22023';
  end if;
  insert into public.worker_heartbeats (
    owner_id, release, status, active_jobs, queue_lag_seconds, details,
    started_at, last_seen_at
  ) values (
    p_owner_id, p_release, p_status, p_active_jobs, p_queue_lag_seconds,
    coalesce(p_details, '{}'::jsonb), now(), now()
  )
  on conflict (owner_id) do update set
    release = excluded.release,
    status = excluded.status,
    active_jobs = excluded.active_jobs,
    queue_lag_seconds = excluded.queue_lag_seconds,
    details = excluded.details,
    last_seen_at = now();
end;
$$;

create or replace function public.record_wallet_readiness(
  p_account_id uuid,
  p_wallet_id uuid,
  p_balance_pusd numeric,
  p_allowances_ready boolean
)
returns setof public.trading_wallets
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_balance_pusd < 0 then
    raise exception 'wallet balance must be non-negative' using errcode = '22023';
  end if;
  return query
  update public.trading_wallets as wallet
  set collateral_balance_pusd = p_balance_pusd,
      allowances_ready = p_allowances_ready,
      readiness_checked_at = now(),
      updated_at = now()
  where wallet.account_id = p_account_id
    and wallet.id = p_wallet_id
    and wallet.status = 'active'
  returning wallet.*;
end;
$$;

create or replace function public.enforce_live_wallet_readiness()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.mode in ('canary', 'live') and not exists (
    select 1 from public.trading_wallets as wallet
    where wallet.id = new.trading_wallet_id
      and wallet.account_id = new.account_id
      and wallet.status = 'active'
      and wallet.chain_id is not null
      and wallet.collateral_token is not null
  ) then
    raise exception 'live wallet identity has not been verified'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

create trigger cycle_jobs_live_wallet_ready
before insert on public.cycle_jobs
for each row execute function public.enforce_live_wallet_readiness();

create or replace function public.enforce_submitting_wallet_readiness()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.environment in ('canary', 'live')
     and new.status = 'submitting'
     and old.status is distinct from new.status
     and not exists (
       select 1
       from public.account_runtime_profiles as profile
       join public.trading_wallets as wallet
         on wallet.id = profile.trading_wallet_id
        and wallet.account_id = profile.account_id
       where profile.account_id = new.account_id
         and profile.status = 'active'
         and wallet.status = 'active'
         and wallet.chain_id is not null
         and wallet.collateral_token is not null
         and wallet.collateral_balance_pusd > 0
         and wallet.allowances_ready
         and wallet.readiness_checked_at > now() - interval '15 minutes'
     )
  then
    raise exception 'live wallet readiness gate rejected submission'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

create trigger orders_live_wallet_ready
before update on public.orders
for each row execute function public.enforce_submitting_wallet_readiness();

create or replace function public.save_paper_account_state(
  p_account_id uuid,
  p_job_id uuid,
  p_claimed_by text,
  p_fencing_token bigint,
  p_state jsonb
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
begin
  if jsonb_typeof(p_state) <> 'object' or length(p_state::text) > 524288 then
    raise exception 'invalid paper account state' using errcode = '22023';
  end if;
  if not exists (
    select 1 from public.cycle_jobs as job
    where job.id = p_job_id
      and job.account_id = p_account_id
      and job.claimed_by = p_claimed_by
      and job.fencing_token = p_fencing_token
      and job.mode = 'paper'
      and job.status = 'running'
      and job.lease_expires_at > now()
  ) then
    return false;
  end if;
  insert into public.paper_account_states(account_id, state)
  values (p_account_id, p_state)
  on conflict (account_id) do update set
    state = excluded.state,
    version = public.paper_account_states.version + 1,
    updated_at = now();
  return true;
end;
$$;

create or replace function public.create_risk_policy_preset(
  p_account_id uuid,
  p_expected_profile_version bigint,
  p_preset text
)
returns setof public.risk_policies
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_version bigint;
  v_saved public.risk_policies%rowtype;
begin
  if p_preset not in ('conservative', 'balanced', 'advanced') then
    raise exception 'unsupported risk preset' using errcode = '22023';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_account_id::text || ':risk', 0));
  if not exists (
    select 1 from public.account_runtime_profiles as profile
    where profile.account_id = p_account_id
      and profile.version = p_expected_profile_version
      and profile.status = 'active'
  ) then
    raise exception 'runtime profile changed concurrently' using errcode = '40001';
  end if;
  select coalesce(max(risk.version), 0) + 1 into v_version
  from public.risk_policies as risk where risk.account_id = p_account_id;
  update public.risk_policies as risk
  set status = 'superseded', superseded_at = now()
  where risk.account_id = p_account_id and risk.status = 'active';
  insert into public.risk_policies (
    account_id, version, max_order_usd, max_trade_risk_pct,
    max_event_exposure_pct, max_bucket_exposure_pct, max_gross_exposure_pct,
    daily_loss_limit_pct, max_drawdown_pct, min_edge
  ) values (
    p_account_id,
    v_version,
    case p_preset when 'conservative' then 2 when 'balanced' then 5 else 10 end,
    case p_preset when 'conservative' then 0.0025 when 'balanced' then 0.005 else 0.01 end,
    case p_preset when 'conservative' then 0.01 when 'balanced' then 0.02 else 0.03 end,
    case p_preset when 'conservative' then 0.025 when 'balanced' then 0.05 else 0.08 end,
    case p_preset when 'conservative' then 0.05 when 'balanced' then 0.10 else 0.15 end,
    case p_preset when 'conservative' then 0.01 when 'balanced' then 0.02 else 0.03 end,
    case p_preset when 'conservative' then 0.04 when 'balanced' then 0.08 else 0.10 end,
    case p_preset when 'conservative' then 0.06 when 'balanced' then 0.04 else 0.03 end
  ) returning * into v_saved;
  update public.account_runtime_profiles as profile
  set risk_policy_id = v_saved.id,
      version = profile.version + 1,
      updated_at = now()
  where profile.account_id = p_account_id
    and profile.version = p_expected_profile_version;
  return next v_saved;
  return;
end;
$$;

create or replace function public.enqueue_ai_diagnostic(
  p_account_id uuid
)
returns setof public.ai_diagnostic_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_profile public.account_runtime_profiles%rowtype;
  v_saved public.ai_diagnostic_jobs%rowtype;
begin
  select * into v_profile
  from public.account_runtime_profiles as profile
  where profile.account_id = p_account_id and profile.status = 'active';
  if v_profile.ai_credential_id is null
     or v_profile.ai_provider in ('platform', 'mock')
     or not exists (
       select 1 from public.credential_refs as credential
       where credential.account_id = p_account_id
         and credential.id = v_profile.ai_credential_id
         and credential.kind = 'ai_api_key'
         and credential.provider = v_profile.ai_provider
         and credential.status = 'active'
     )
  then
    raise exception 'active AI credential is required' using errcode = '22023';
  end if;
  select * into v_saved
  from public.ai_diagnostic_jobs as job
  where job.account_id = p_account_id
    and job.status in ('queued', 'claimed', 'running')
  order by job.created_at desc limit 1;
  if found then
    return next v_saved;
    return;
  end if;
  insert into public.ai_diagnostic_jobs (
    account_id, credential_id, provider, ai_base_url, model
  ) values (
    p_account_id, v_profile.ai_credential_id, v_profile.ai_provider,
    v_profile.ai_base_url, v_profile.forecast_model
  ) returning * into v_saved;
  return next v_saved;
  return;
end;
$$;

create or replace function public.claim_ai_diagnostic(
  p_claimed_by text,
  p_lease_seconds integer
)
returns setof public.ai_diagnostic_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_id uuid;
begin
  if length(btrim(p_claimed_by)) < 8 or p_lease_seconds not between 10 and 300 then
    raise exception 'invalid diagnostic lease' using errcode = '22023';
  end if;
  select job.id into v_id
  from public.ai_diagnostic_jobs as job
  where job.status = 'queued'
     or (
       job.status in ('claimed', 'running')
       and job.lease_expires_at <= now()
     )
  order by job.created_at
  for update skip locked
  limit 1;
  if v_id is null then return; end if;
  return query
  update public.ai_diagnostic_jobs as job
  set status = 'claimed', claimed_by = p_claimed_by,
      fencing_token = job.fencing_token + 1,
      lease_expires_at = now() + make_interval(secs => p_lease_seconds),
      started_at = coalesce(job.started_at, now()), updated_at = now()
  where job.id = v_id
  returning job.*;
end;
$$;

create or replace function public.finish_ai_diagnostic(
  p_account_id uuid,
  p_job_id uuid,
  p_claimed_by text,
  p_fencing_token bigint,
  p_ok boolean,
  p_result_summary jsonb,
  p_error_code text default null
)
returns setof public.ai_diagnostic_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_count integer;
begin
  if jsonb_typeof(coalesce(p_result_summary, '{}'::jsonb)) <> 'object'
     or length(coalesce(p_result_summary, '{}'::jsonb)::text) > 8192
     or (p_error_code is not null and p_error_code !~ '^[a-z0-9_]{1,64}$')
  then
    raise exception 'invalid diagnostic result' using errcode = '22023';
  end if;
  return query
  update public.ai_diagnostic_jobs as job
  set status = case when p_ok then 'succeeded' else 'failed' end,
      result_summary = coalesce(p_result_summary, '{}'::jsonb),
      error_code = case when p_ok then null else p_error_code end,
      completed_at = now(), lease_expires_at = null, updated_at = now()
  where job.id = p_job_id and job.account_id = p_account_id
    and job.claimed_by = p_claimed_by
    and job.fencing_token = p_fencing_token
    and job.status in ('claimed', 'running')
    and job.lease_expires_at > now()
  returning job.*;
  get diagnostics v_count = row_count;
  if v_count = 1 and not p_ok then
    insert into public.account_notifications(
      account_id, severity, code, title, message, details
    ) values (
      p_account_id, 'warning', 'ai_diagnostic_failed',
      'AI 连接检查失败',
      '模型连接或结构化输出检查未通过，自动周期将保持安全失败。',
      jsonb_build_object('diagnostic_job_id', p_job_id, 'error_code', p_error_code)
    );
  end if;
end;
$$;

create or replace function public.consume_ai_budget(
  p_account_id uuid,
  p_units integer default 1
)
returns table (allowed boolean, used integer, daily_limit integer)
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_today date := (now() at time zone 'utc')::date;
  v_limit integer;
begin
  if p_units not between 1 and 10 then
    raise exception 'invalid AI budget units' using errcode = '22023';
  end if;
  select settings.request_limit into v_limit
  from public.ai_budget_settings as settings
  where settings.account_id = p_account_id;
  v_limit := coalesce(v_limit, 100);
  insert into public.ai_usage_daily(
    account_id, usage_day, request_units, request_limit
  ) values (p_account_id, v_today, 0, v_limit)
  on conflict (account_id, usage_day) do update set
    request_limit = v_limit,
    updated_at = now();
  return query
  update public.ai_usage_daily as usage
  set request_units = usage.request_units + p_units, updated_at = now()
  where usage.account_id = p_account_id and usage.usage_day = v_today
    and usage.request_units + p_units <= usage.request_limit
  returning true, usage.request_units, usage.request_limit;
  if not found then
    return query
    select false, usage.request_units, usage.request_limit
    from public.ai_usage_daily as usage
    where usage.account_id = p_account_id and usage.usage_day = v_today;
  end if;
end;
$$;

create or replace function public.set_ai_budget_limit(
  p_account_id uuid,
  p_request_limit integer
)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_request_limit not between 20 and 10000 then
    raise exception 'AI request limit out of range' using errcode = '22023';
  end if;
  insert into public.ai_budget_settings(account_id, request_limit)
  values (p_account_id, p_request_limit)
  on conflict (account_id) do update set
    request_limit = excluded.request_limit,
    updated_at = now();
  update public.ai_usage_daily as usage
  set request_limit = p_request_limit, updated_at = now()
  where usage.account_id = p_account_id
    and usage.usage_day = (now() at time zone 'utc')::date;
  return p_request_limit;
end;
$$;

create or replace function public.record_market_resolution(
  p_condition_id text,
  p_outcome text
)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_market_id uuid;
  v_count integer;
begin
  if p_outcome not in ('YES', 'NO') then
    raise exception 'invalid resolved outcome' using errcode = '22023';
  end if;
  update public.markets as market
  set closed = true, active = false, accepting_orders = false,
      resolved_outcome = p_outcome,
      resolution_confirmed_at = coalesce(market.resolution_confirmed_at, now()),
      updated_at = now()
  where market.condition_id = p_condition_id
    and (market.resolved_outcome is null or market.resolved_outcome = p_outcome)
  returning market.id into v_market_id;
  if v_market_id is null then return 0; end if;
  insert into public.forecast_outcomes (
    forecast_id, account_id, market_id, probability_yes, resolved_yes, resolved_at,
    brier_score, log_loss
  )
  select forecast.id, forecast.account_id, forecast.market_id, forecast.p_yes,
    p_outcome = 'YES', market.resolution_confirmed_at,
    power(forecast.p_yes - case when p_outcome = 'YES' then 1 else 0 end, 2),
    -ln(greatest(
      0.000000001,
      case when p_outcome = 'YES' then forecast.p_yes else 1 - forecast.p_yes end
    ))
  from public.forecasts as forecast
  join public.markets as market on market.id = forecast.market_id
  where forecast.market_id = v_market_id
  on conflict (forecast_id) do nothing;
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

create or replace function public.polybot_schema_version()
returns integer
language sql
stable
security definer
set search_path = ''
as $$ select 15; $$;

alter table public.worker_heartbeats enable row level security;
alter table public.worker_heartbeats force row level security;
alter table public.account_notifications enable row level security;
alter table public.account_notifications force row level security;
alter table public.paper_account_states enable row level security;
alter table public.paper_account_states force row level security;
alter table public.ai_diagnostic_jobs enable row level security;
alter table public.ai_diagnostic_jobs force row level security;
alter table public.forecast_outcomes enable row level security;
alter table public.forecast_outcomes force row level security;
alter table public.ai_usage_daily enable row level security;
alter table public.ai_usage_daily force row level security;
alter table public.ai_budget_settings enable row level security;
alter table public.ai_budget_settings force row level security;

create policy account_notifications_owner_read
  on public.account_notifications for select to authenticated
  using (account_id = (select auth.uid()));
create policy account_notifications_owner_update
  on public.account_notifications for update to authenticated
  using (account_id = (select auth.uid()))
  with check (account_id = (select auth.uid()));
create policy paper_account_states_owner_read
  on public.paper_account_states for select to authenticated
  using (account_id = (select auth.uid()));
create policy ai_diagnostic_jobs_owner_read
  on public.ai_diagnostic_jobs for select to authenticated
  using (account_id = (select auth.uid()));
create policy forecast_outcomes_owner_read
  on public.forecast_outcomes for select to authenticated
  using (account_id = (select auth.uid()));
create policy ai_usage_daily_owner_read
  on public.ai_usage_daily for select to authenticated
  using (account_id = (select auth.uid()));
create policy ai_budget_settings_owner_read
  on public.ai_budget_settings for select to authenticated
  using (account_id = (select auth.uid()));

revoke all on public.worker_heartbeats, public.account_notifications,
  public.paper_account_states, public.ai_diagnostic_jobs,
  public.forecast_outcomes, public.ai_usage_daily, public.ai_budget_settings
from public, anon, authenticated;
grant select, update(read_at) on public.account_notifications to authenticated;
grant select on public.paper_account_states, public.ai_diagnostic_jobs,
  public.forecast_outcomes to authenticated;
grant select on public.ai_usage_daily to authenticated;
grant select on public.ai_budget_settings to authenticated;
grant all on public.worker_heartbeats, public.account_notifications,
  public.paper_account_states, public.ai_diagnostic_jobs,
  public.forecast_outcomes, public.ai_usage_daily, public.ai_budget_settings
to service_role;
grant usage, select on sequence public.account_notifications_id_seq to service_role;

revoke all on function public.record_worker_heartbeat(text, text, text, integer, numeric, jsonb),
  public.enforce_live_wallet_readiness(),
  public.enforce_submitting_wallet_readiness(),
  public.notify_wallet_failure(),
  public.notify_critical_risk_event(),
  public.record_wallet_readiness(uuid, uuid, numeric, boolean),
  public.save_paper_account_state(uuid, uuid, text, bigint, jsonb),
  public.create_risk_policy_preset(uuid, bigint, text),
  public.enqueue_ai_diagnostic(uuid),
  public.claim_ai_diagnostic(text, integer),
  public.finish_ai_diagnostic(uuid, uuid, text, bigint, boolean, jsonb, text),
  public.consume_ai_budget(uuid, integer),
  public.set_ai_budget_limit(uuid, integer),
  public.record_market_resolution(text, text),
  public.polybot_schema_version()
from public, anon, authenticated, service_role;
grant execute on function public.record_worker_heartbeat(text, text, text, integer, numeric, jsonb),
  public.record_wallet_readiness(uuid, uuid, numeric, boolean),
  public.save_paper_account_state(uuid, uuid, text, bigint, jsonb),
  public.create_risk_policy_preset(uuid, bigint, text),
  public.enqueue_ai_diagnostic(uuid),
  public.claim_ai_diagnostic(text, integer),
  public.finish_ai_diagnostic(uuid, uuid, text, bigint, boolean, jsonb, text),
  public.consume_ai_budget(uuid, integer),
  public.set_ai_budget_limit(uuid, integer),
  public.record_market_resolution(text, text),
  public.polybot_schema_version()
to service_role;

notify pgrst, 'reload schema';

commit;
