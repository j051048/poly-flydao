begin;

-- Add ai_base_url column to account_runtime_profiles
alter table public.account_runtime_profiles
  add column if not exists ai_base_url text default null;

-- Expand the provider constraint to allow 'custom'
alter table public.account_runtime_profiles
  drop constraint if exists account_runtime_profiles_provider_allowed;
alter table public.account_runtime_profiles
  add constraint account_runtime_profiles_provider_allowed
    check (ai_provider in (
      'platform', 'openai', 'anthropic', 'openrouter', 'litellm', 'custom', 'mock'
    ));

-- Add a constraint: ai_base_url is required when provider is 'custom',
-- and must be null otherwise.
alter table public.account_runtime_profiles
  add constraint account_runtime_profiles_custom_base_url
    check (
      (ai_provider = 'custom' and ai_base_url is not null
        and length(ai_base_url) between 8 and 256
        and ai_base_url ~ '^https?://')
      or (ai_provider <> 'custom' and ai_base_url is null)
    );

-- Drop old 10-param function (different signature = different function in PG)
drop function if exists public.update_account_runtime_profile(
  uuid, bigint, text, text, uuid, uuid, uuid, text, boolean, integer
);

-- Create new 11-param function with p_ai_base_url
create or replace function public.update_account_runtime_profile(
  p_account_id uuid,
  p_expected_version bigint,
  p_ai_provider text,
  p_ai_base_url text,
  p_forecast_model text,
  p_ai_credential_id uuid,
  p_trading_wallet_id uuid,
  p_risk_policy_id uuid,
  p_desired_mode text,
  p_auto_run_enabled boolean,
  p_cycle_interval_seconds integer
)
returns setof public.account_runtime_profiles
language plpgsql
security definer
set search_path = ''
as $$
begin
  perform 1 from public.ensure_account_runtime_profile(p_account_id);

  if p_ai_provider not in (
    'platform', 'openai', 'anthropic', 'openrouter', 'litellm', 'custom', 'mock'
  )
    or p_desired_mode not in ('paper', 'shadow', 'canary', 'live')
    or p_cycle_interval_seconds not between 30 and 3600
    or p_forecast_model !~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$'
  then
    raise exception 'invalid runtime profile' using errcode = '22023';
  end if;

  -- custom provider requires a valid base_url
  if p_ai_provider = 'custom' then
    if p_ai_base_url is null
       or length(p_ai_base_url) not between 8 and 256
       or p_ai_base_url !~ '^https?://'
    then
      raise exception 'custom provider requires a valid base URL'
        using errcode = '22023';
    end if;
  elsif p_ai_base_url is not null then
    raise exception 'ai_base_url must be null for non-custom providers'
      using errcode = '22023';
  end if;

  if p_ai_provider in ('platform', 'mock') then
    if p_ai_credential_id is not null then
      raise exception 'platform/mock profiles cannot reference tenant AI keys'
        using errcode = '22023';
    end if;
  elsif not exists (
    select 1 from public.credential_refs as credential
    where credential.id = p_ai_credential_id
      and credential.account_id = p_account_id
      and credential.kind = 'ai_api_key'
      and credential.provider = p_ai_provider
      and credential.status = 'active'
  ) then
    raise exception 'AI credential is missing, inactive, or cross-tenant'
      using errcode = '23503';
  end if;

  if p_trading_wallet_id is not null and not exists (
    select 1 from public.trading_wallets as wallet
    where wallet.id = p_trading_wallet_id
      and wallet.account_id = p_account_id
      and wallet.status = 'active'
  ) then
    raise exception 'trading wallet is missing, inactive, or cross-tenant'
      using errcode = '23503';
  end if;

  if not exists (
    select 1 from public.risk_policies as risk
    where risk.id = p_risk_policy_id
      and risk.account_id = p_account_id
      and risk.status = 'active'
  ) then
    raise exception 'risk policy is missing, inactive, or cross-tenant'
      using errcode = '23503';
  end if;

  if p_desired_mode in ('canary', 'live')
    and (
      p_ai_provider in ('platform', 'mock')
      or p_ai_credential_id is null
      or p_trading_wallet_id is null
    )
  then
    raise exception 'real-money profiles require tenant AI and wallet credentials'
      using errcode = '23514';
  end if;

  return query
  update public.account_runtime_profiles as profile
  set
    ai_provider = p_ai_provider,
    ai_base_url = p_ai_base_url,
    forecast_model = p_forecast_model,
    ai_credential_id = p_ai_credential_id,
    trading_wallet_id = p_trading_wallet_id,
    risk_policy_id = p_risk_policy_id,
    desired_mode = p_desired_mode,
    auto_run_enabled = p_auto_run_enabled,
    cycle_interval_seconds = p_cycle_interval_seconds,
    next_run_at = case
      when p_auto_run_enabled and not profile.auto_run_enabled then now()
      when p_auto_run_enabled then coalesce(profile.next_run_at, now())
      else null
    end,
    version = profile.version + 1
  where profile.account_id = p_account_id
    and profile.version = p_expected_version
  returning profile.*;
end;
$$;

-- Grant execute on the new 11-param signature
revoke all on function public.update_account_runtime_profile(
  uuid, bigint, text, text, text, uuid, uuid, uuid, text, boolean, integer
) from public, anon, authenticated, service_role;

grant execute on function public.update_account_runtime_profile(
  uuid, bigint, text, text, text, uuid, uuid, uuid, text, boolean, integer
) to authenticated, service_role;

commit;
