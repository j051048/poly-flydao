begin;

-- P0/P1 secure multi-tenant control plane. Secret plaintext is never stored.
-- The API writes hybrid-encrypted envelopes; only a worker-only process receives
-- the RSA private key required to unwrap them.

create table public.credential_refs (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references auth.users(id) on delete restrict,
  kind text not null,
  provider text not null,
  label text,
  status text not null default 'active',
  cipher_algorithm text not null,
  key_version integer not null,
  aad_version bigint not null,
  nonce text not null,
  ciphertext text not null,
  encrypted_data_key text,
  fingerprint text not null,
  last_four text not null,
  version bigint not null default 1,
  created_at timestamptz not null default now(),
  rotated_at timestamptz,
  revoked_at timestamptz,
  updated_at timestamptz not null default now(),
  constraint credential_refs_kind_allowed
    check (kind in ('ai_api_key', 'evm_signer_key')),
  constraint credential_refs_provider_nonempty
    check (length(btrim(provider)) between 1 and 40),
  constraint credential_refs_label_length
    check (label is null or length(label) between 1 and 80),
  constraint credential_refs_status_allowed
    check (status in ('active', 'pending_verification', 'revocation_pending', 'revoked')),
  constraint credential_refs_algorithm_allowed
    check (cipher_algorithm in ('A256GCM', 'RSA-OAEP-256+A256GCM')),
  constraint credential_refs_key_versions_positive
    check (key_version > 0 and aad_version > 0 and version > 0),
  constraint credential_refs_envelope_shape
    check (
      length(nonce) between 16 and 64
      and length(ciphertext) between 20 and 16384
      and (
        (cipher_algorithm = 'A256GCM' and encrypted_data_key is null)
        or
        (
          cipher_algorithm = 'RSA-OAEP-256+A256GCM'
          and encrypted_data_key is not null
          and length(encrypted_data_key) between 256 and 2048
        )
      )
    ),
  constraint credential_refs_fingerprint_shape
    check (fingerprint ~ '^[0-9a-f]{64}$'),
  constraint credential_refs_last_four_shape
    check (length(last_four) = 4),
  constraint credential_refs_revocation_shape
    check (
      (status = 'revoked' and revoked_at is not null)
      or status <> 'revoked'
    ),
  unique (account_id, id)
);

create unique index credential_refs_one_active_ai_provider_uidx
  on public.credential_refs (account_id, provider)
  where kind = 'ai_api_key' and status = 'active';
create unique index credential_refs_active_signer_fingerprint_uidx
  on public.credential_refs (account_id, kind, fingerprint)
  where kind = 'evm_signer_key'
    and status in ('pending_verification', 'active', 'revocation_pending');
create index credential_refs_account_status_idx
  on public.credential_refs (account_id, status, created_at desc);

create table public.trading_wallets (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references auth.users(id) on delete restrict,
  label text,
  enrollment_idempotency_key text,
  owner_address text,
  deposit_wallet_address text,
  signer_address text,
  chain_id integer,
  collateral_token text,
  signer_credential_id uuid,
  signature_type smallint not null default 3,
  status text not null default 'provisioning',
  version bigint not null default 1,
  lifecycle_claimed_by text,
  lifecycle_fencing_token bigint not null default 0,
  lifecycle_lease_expires_at timestamptz,
  lifecycle_attempt_count integer not null default 0,
  last_error_code text,
  verified_at timestamptz,
  revoked_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint trading_wallets_label_length
    check (label is null or length(label) between 1 and 80),
  constraint trading_wallets_idempotency_shape
    check (
      enrollment_idempotency_key is null
      or (
        length(enrollment_idempotency_key) between 16 and 128
        and enrollment_idempotency_key ~ '^[A-Za-z0-9._:@+-]+$'
      )
    ),
  constraint trading_wallets_owner_address_shape
    check (owner_address is null or owner_address ~ '^0x[0-9a-f]{40}$'),
  constraint trading_wallets_deposit_address_shape
    check (
      deposit_wallet_address is null
      or deposit_wallet_address ~ '^0x[0-9a-f]{40}$'
    ),
  constraint trading_wallets_signer_address_shape
    check (signer_address is null or signer_address ~ '^0x[0-9a-f]{40}$'),
  constraint trading_wallets_chain_id_shape
    check (chain_id is null or chain_id > 0),
  constraint trading_wallets_collateral_token_shape
    check (
      collateral_token is null
      or collateral_token ~ '^0x[0-9a-f]{40}$'
    ),
  constraint trading_wallets_signature_type_allowed
    check (signature_type in (0, 1, 2, 3)),
  constraint trading_wallets_status_allowed
    check (
      status in (
        'provisioning',
        'pending_verification',
        'active',
        'revocation_pending',
        'revoked',
        'failed'
      )
    ),
  constraint trading_wallets_version_positive check (version > 0),
  constraint trading_wallets_lifecycle_values
    check (
      lifecycle_fencing_token >= 0
      and lifecycle_attempt_count >= 0
      and (
        lifecycle_claimed_by is null
        or (
          lifecycle_lease_expires_at is not null
          and lifecycle_fencing_token > 0
        )
      )
    ),
  constraint trading_wallets_active_shape
    check (
      status <> 'active'
      or (
        deposit_wallet_address is not null
        and signer_address is not null
        and chain_id is not null
        and collateral_token is not null
        and signer_credential_id is not null
        and verified_at is not null
      )
    ),
  foreign key (account_id, signer_credential_id)
    references public.credential_refs(account_id, id) on delete restrict,
  unique (account_id, id)
);

create unique index trading_wallets_deposit_address_uidx
  on public.trading_wallets (deposit_wallet_address)
  where deposit_wallet_address is not null
    and status in ('active', 'revocation_pending');
create unique index trading_wallets_enrollment_idempotency_uidx
  on public.trading_wallets (account_id, enrollment_idempotency_key)
  where enrollment_idempotency_key is not null;
create unique index trading_wallets_signer_credential_uidx
  on public.trading_wallets (signer_credential_id)
  where signer_credential_id is not null;
create index trading_wallets_account_status_idx
  on public.trading_wallets (account_id, status, created_at desc);

create table public.risk_policies (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references auth.users(id) on delete restrict,
  version bigint not null default 1,
  status text not null default 'active',
  max_order_usd numeric(38, 18) not null default 5,
  max_trade_risk_pct numeric(10, 9) not null default 0.005,
  max_event_exposure_pct numeric(10, 9) not null default 0.02,
  max_bucket_exposure_pct numeric(10, 9) not null default 0.05,
  max_gross_exposure_pct numeric(10, 9) not null default 0.10,
  daily_loss_limit_pct numeric(10, 9) not null default 0.02,
  max_drawdown_pct numeric(10, 9) not null default 0.08,
  min_edge numeric(10, 9) not null default 0.04,
  created_at timestamptz not null default now(),
  superseded_at timestamptz,
  constraint risk_policies_version_positive check (version > 0),
  constraint risk_policies_status_allowed check (status in ('active', 'superseded')),
  constraint risk_policies_max_order_positive check (max_order_usd > 0),
  constraint risk_policies_percentages_in_range
    check (
      max_trade_risk_pct > 0 and max_trade_risk_pct <= 1
      and max_event_exposure_pct > 0 and max_event_exposure_pct <= 1
      and max_bucket_exposure_pct > 0 and max_bucket_exposure_pct <= 1
      and max_gross_exposure_pct > 0 and max_gross_exposure_pct <= 1
      and daily_loss_limit_pct > 0 and daily_loss_limit_pct <= 1
      and max_drawdown_pct > 0 and max_drawdown_pct <= 1
      and min_edge >= 0 and min_edge <= 1
    ),
  unique (account_id, id),
  unique (account_id, id, version)
);

create unique index risk_policies_one_active_uidx
  on public.risk_policies (account_id)
  where status = 'active';
create unique index risk_policies_account_version_uidx
  on public.risk_policies (account_id, version);

create table public.account_runtime_profiles (
  account_id uuid primary key references auth.users(id) on delete restrict,
  ai_provider text not null default 'platform',
  forecast_model text not null default 'gpt-5.6-terra',
  ai_credential_id uuid,
  trading_wallet_id uuid,
  risk_policy_id uuid,
  desired_mode text not null default 'paper',
  auto_run_enabled boolean not null default false,
  cycle_interval_seconds integer not null default 60,
  next_run_at timestamptz,
  status text not null default 'active',
  version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint account_runtime_profiles_provider_allowed
    check (ai_provider in ('platform', 'openai', 'anthropic', 'openrouter', 'litellm', 'mock')),
  constraint account_runtime_profiles_model_shape
    check (
      length(forecast_model) between 1 and 128
      and forecast_model ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]*$'
    ),
  constraint account_runtime_profiles_mode_allowed
    check (desired_mode in ('paper', 'shadow', 'canary', 'live')),
  constraint account_runtime_profiles_interval_range
    check (cycle_interval_seconds between 30 and 3600),
  constraint account_runtime_profiles_status_allowed
    check (status in ('active', 'suspended')),
  constraint account_runtime_profiles_version_positive check (version > 0),
  constraint account_runtime_profiles_schedule_shape
    check (
      (auto_run_enabled and next_run_at is not null)
      or (not auto_run_enabled)
    ),
  foreign key (account_id, ai_credential_id)
    references public.credential_refs(account_id, id) on delete restrict,
  foreign key (account_id, trading_wallet_id)
    references public.trading_wallets(account_id, id) on delete restrict,
  foreign key (account_id, risk_policy_id)
    references public.risk_policies(account_id, id) on delete restrict
);

create index account_runtime_profiles_due_idx
  on public.account_runtime_profiles (next_run_at)
  where auto_run_enabled and status = 'active';

create table public.cycle_jobs (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references auth.users(id) on delete restrict,
  trading_wallet_id uuid,
  ai_credential_id uuid,
  risk_policy_id uuid,
  risk_policy_version bigint,
  mode text not null,
  idempotency_key text not null,
  status text not null default 'queued',
  run_after timestamptz not null default now(),
  requested_run_after timestamptz,
  claimed_by text,
  fencing_token bigint not null default 0,
  heartbeat_at timestamptz,
  lease_expires_at timestamptz,
  attempt_count integer not null default 0,
  max_attempts integer not null default 3,
  error_code text,
  started_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint cycle_jobs_mode_allowed
    check (mode in ('paper', 'shadow', 'canary', 'live')),
  constraint cycle_jobs_idempotency_shape
    check (
      length(idempotency_key) between 16 and 128
      and idempotency_key ~ '^[A-Za-z0-9._:@+-]+$'
    ),
  constraint cycle_jobs_status_allowed
    check (status in ('queued', 'claimed', 'running', 'succeeded', 'failed', 'cancelled')),
  constraint cycle_jobs_attempts_valid
    check (
      attempt_count >= 0
      and max_attempts between 1 and 10
      and fencing_token >= 0
    ),
  constraint cycle_jobs_worker_shape
    check (
      (
        status in ('claimed', 'running')
        and claimed_by is not null
        and heartbeat_at is not null
        and lease_expires_at is not null
        and fencing_token > 0
      )
      or status not in ('claimed', 'running')
    ),
  constraint cycle_jobs_live_refs_required
    check (
      mode not in ('canary', 'live')
      or (
        trading_wallet_id is not null
        and ai_credential_id is not null
        and risk_policy_id is not null
      )
    ),
  foreign key (account_id, trading_wallet_id)
    references public.trading_wallets(account_id, id) on delete restrict,
  foreign key (account_id, ai_credential_id)
    references public.credential_refs(account_id, id) on delete restrict,
  foreign key (account_id, risk_policy_id, risk_policy_version)
    references public.risk_policies(account_id, id, version) on delete restrict,
  constraint cycle_jobs_risk_snapshot_shape
    check (
      (risk_policy_id is null and risk_policy_version is null)
      or (risk_policy_id is not null and risk_policy_version is not null)
    )
);

create unique index cycle_jobs_account_idempotency_uidx
  on public.cycle_jobs (account_id, idempotency_key);
create unique index cycle_jobs_one_active_per_account_uidx
  on public.cycle_jobs (account_id)
  where status in ('queued', 'claimed', 'running');
create index cycle_jobs_claim_idx
  on public.cycle_jobs (run_after, created_at)
  where status in ('queued', 'claimed', 'running');
create index cycle_jobs_account_created_idx
  on public.cycle_jobs (account_id, created_at desc);

create table public.credential_audit_events (
  id bigint generated always as identity primary key,
  account_id uuid not null references auth.users(id) on delete restrict,
  actor_type text not null default 'system',
  actor_id text,
  action text not null,
  entity_type text not null,
  entity_id uuid,
  request_id text,
  metadata jsonb not null default '{}'::jsonb,
  occurred_at timestamptz not null default now(),
  constraint credential_audit_actor_allowed
    check (actor_type in ('user', 'worker', 'system')),
  constraint credential_audit_action_nonempty
    check (length(btrim(action)) > 0),
  constraint credential_audit_entity_nonempty
    check (length(btrim(entity_type)) > 0)
);

create index credential_audit_account_occurred_idx
  on public.credential_audit_events (account_id, occurred_at desc);

create or replace function public.prevent_credential_envelope_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if (
    new.account_id,
    new.kind,
    new.provider,
    new.cipher_algorithm,
    new.key_version,
    new.aad_version,
    new.nonce,
    new.ciphertext,
    new.encrypted_data_key,
    new.fingerprint,
    new.last_four,
    new.version
  ) is distinct from (
    old.account_id,
    old.kind,
    old.provider,
    old.cipher_algorithm,
    old.key_version,
    old.aad_version,
    old.nonce,
    old.ciphertext,
    old.encrypted_data_key,
    old.fingerprint,
    old.last_four,
    old.version
  ) then
    raise exception 'credential envelope and scope are immutable'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

create or replace function public.prevent_cycle_job_scope_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if (
    new.account_id,
    new.trading_wallet_id,
    new.ai_credential_id,
    new.risk_policy_id,
    new.risk_policy_version,
    new.mode,
    new.idempotency_key,
    new.max_attempts,
    new.created_at
  ) is distinct from (
    old.account_id,
    old.trading_wallet_id,
    old.ai_credential_id,
    old.risk_policy_id,
    old.risk_policy_version,
    old.mode,
    old.idempotency_key,
    old.max_attempts,
    old.created_at
  ) then
    raise exception 'cycle job tenant scope and execution snapshot are immutable'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

create or replace function public.prevent_risk_policy_rewrite()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if (
    new.id,
    new.account_id,
    new.version,
    new.max_order_usd,
    new.max_trade_risk_pct,
    new.max_event_exposure_pct,
    new.max_bucket_exposure_pct,
    new.max_gross_exposure_pct,
    new.daily_loss_limit_pct,
    new.max_drawdown_pct,
    new.min_edge,
    new.created_at
  ) is distinct from (
    old.id,
    old.account_id,
    old.version,
    old.max_order_usd,
    old.max_trade_risk_pct,
    old.max_event_exposure_pct,
    old.max_bucket_exposure_pct,
    old.max_gross_exposure_pct,
    old.daily_loss_limit_pct,
    old.max_drawdown_pct,
    old.min_edge,
    old.created_at
  ) then
    raise exception 'risk policy snapshots are immutable'
      using errcode = '23514';
  end if;
  if old.status = 'superseded' or new.status not in ('active', 'superseded') then
    raise exception 'invalid risk policy lifecycle transition'
      using errcode = '23514';
  end if;
  if new.status = 'superseded' and new.superseded_at is null then
    raise exception 'superseded risk policy requires a timestamp'
      using errcode = '23514';
  end if;
  return new;
end;
$$;

create or replace function public.prevent_credential_audit_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  raise exception 'credential audit events are append-only' using errcode = '42501';
end;
$$;

create or replace function public.audit_multitenant_control_change()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_row jsonb := case when tg_op = 'DELETE' then to_jsonb(old) else to_jsonb(new) end;
begin
  insert into public.credential_audit_events (
    account_id,
    actor_type,
    actor_id,
    action,
    entity_type,
    entity_id,
    metadata
  )
  values (
    (v_row ->> 'account_id')::uuid,
    'system',
    current_user::text,
    lower(tg_table_name || '_' || tg_op),
    tg_table_name,
    nullif(v_row ->> 'id', '')::uuid,
    jsonb_strip_nulls(
      jsonb_build_object(
        'status', v_row ->> 'status',
        'version', v_row ->> 'version',
        'source', 'database_trigger'
      )
    )
  );
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

revoke all on function public.prevent_credential_envelope_mutation() from public;
revoke all on function public.prevent_cycle_job_scope_mutation() from public;
revoke all on function public.prevent_risk_policy_rewrite() from public;
revoke all on function public.prevent_credential_audit_mutation() from public;
revoke all on function public.audit_multitenant_control_change() from public;

create trigger credential_refs_immutable
before update on public.credential_refs
for each row execute function public.prevent_credential_envelope_mutation();
create trigger cycle_jobs_scope_immutable
before update on public.cycle_jobs
for each row execute function public.prevent_cycle_job_scope_mutation();
create trigger risk_policies_immutable
before update on public.risk_policies
for each row execute function public.prevent_risk_policy_rewrite();
create trigger credential_audit_append_only
before update or delete on public.credential_audit_events
for each row execute function public.prevent_credential_audit_mutation();

create trigger credential_refs_updated_at
before update on public.credential_refs
for each row execute function public.set_updated_at();
create trigger trading_wallets_updated_at
before update on public.trading_wallets
for each row execute function public.set_updated_at();
create trigger account_runtime_profiles_updated_at
before update on public.account_runtime_profiles
for each row execute function public.set_updated_at();
create trigger cycle_jobs_updated_at
before update on public.cycle_jobs
for each row execute function public.set_updated_at();

create trigger credential_refs_audit
after insert or update on public.credential_refs
for each row execute function public.audit_multitenant_control_change();
create trigger trading_wallets_audit
after insert or update on public.trading_wallets
for each row execute function public.audit_multitenant_control_change();
create trigger account_runtime_profiles_audit
after insert or update on public.account_runtime_profiles
for each row execute function public.audit_multitenant_control_change();
create trigger cycle_jobs_audit
after insert or update on public.cycle_jobs
for each row execute function public.audit_multitenant_control_change();

create or replace function public.ensure_account_runtime_profile(p_account_id uuid)
returns setof public.account_runtime_profiles
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_risk_policy_id uuid;
begin
  select risk.id into v_risk_policy_id
  from public.risk_policies as risk
  where risk.account_id = p_account_id and risk.status = 'active'
  limit 1;

  if v_risk_policy_id is null then
    insert into public.risk_policies (account_id)
    values (p_account_id)
    returning id into v_risk_policy_id;
  end if;

  insert into public.runtime_controls (account_id)
  values (p_account_id)
  on conflict (account_id) do nothing;

  insert into public.account_runtime_profiles (account_id, risk_policy_id)
  values (p_account_id, v_risk_policy_id)
  on conflict (account_id) do nothing;

  return query
  select profile.*
  from public.account_runtime_profiles as profile
  where profile.account_id = p_account_id;
end;
$$;

create or replace function public.store_ai_credential(
  p_account_id uuid,
  p_provider text,
  p_label text,
  p_algorithm text,
  p_key_version integer,
  p_aad_version bigint,
  p_nonce text,
  p_ciphertext text,
  p_encrypted_data_key text,
  p_fingerprint text,
  p_last_four text
)
returns setof public.credential_refs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_version bigint;
  v_old_credential_id uuid;
  v_saved public.credential_refs%rowtype;
begin
  if p_provider not in ('openai', 'anthropic', 'openrouter', 'litellm') then
    raise exception 'unsupported AI provider' using errcode = '22023';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(p_account_id::text || ':ai:' || p_provider, 0));

  select coalesce(max(credential.version), 0) + 1 into v_version
  from public.credential_refs as credential
  where credential.account_id = p_account_id
    and credential.kind = 'ai_api_key'
    and credential.provider = p_provider;

  select credential.id into v_old_credential_id
  from public.credential_refs as credential
  where credential.account_id = p_account_id
    and credential.kind = 'ai_api_key'
    and credential.provider = p_provider
    and credential.status = 'active'
  for update;

  update public.credential_refs as credential
  set status = 'revoked', revoked_at = now()
  where credential.account_id = p_account_id
    and credential.kind = 'ai_api_key'
    and credential.provider = p_provider
    and credential.status = 'active';

  insert into public.credential_refs (
    account_id, kind, provider, label, status, cipher_algorithm,
    key_version, aad_version, nonce, ciphertext, encrypted_data_key,
    fingerprint, last_four, version, rotated_at
  )
  values (
    p_account_id, 'ai_api_key', p_provider, p_label, 'active', p_algorithm,
    p_key_version, p_aad_version, p_nonce, p_ciphertext, p_encrypted_data_key,
    p_fingerprint, p_last_four, v_version,
    case when v_version > 1 then now() else null end
  )
  returning * into v_saved;

  if v_old_credential_id is not null then
    update public.account_runtime_profiles as profile
    set ai_credential_id = v_saved.id, version = profile.version + 1
    where profile.account_id = p_account_id
      and profile.ai_credential_id = v_old_credential_id;
  end if;
  return next v_saved;
  return;
end;
$$;

create or replace function public.revoke_ai_credential(
  p_account_id uuid,
  p_provider text
)
returns setof public.credential_refs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_saved public.credential_refs%rowtype;
  v_mode text;
begin
  update public.credential_refs as credential
  set status = 'revoked', revoked_at = now()
  where credential.account_id = p_account_id
    and credential.kind = 'ai_api_key'
    and credential.provider = p_provider
    and credential.status = 'active'
  returning credential.* into v_saved;
  if not found then
    return;
  end if;

  update public.account_runtime_profiles as profile
  set
    ai_provider = 'platform',
    ai_credential_id = null,
    desired_mode = 'paper',
    auto_run_enabled = false,
    next_run_at = null,
    version = profile.version + 1
  where profile.account_id = p_account_id
    and profile.ai_credential_id = v_saved.id;

  update public.cycle_jobs as job
  set status = 'cancelled', completed_at = now()
  where job.account_id = p_account_id
    and job.ai_credential_id = v_saved.id
    and job.status = 'queued';

  select control.mode into v_mode
  from public.runtime_controls as control
  where control.account_id = p_account_id;
  if v_mode is not null then
    perform 1 from public.disarm_runtime_control(p_account_id, v_mode);
  end if;

  return next v_saved;
  return;
end;
$$;

create or replace function public.import_trading_wallet(
  p_account_id uuid,
  p_label text,
  p_owner_address text,
  p_signature_type smallint,
  p_idempotency_key text,
  p_algorithm text,
  p_key_version integer,
  p_aad_version bigint,
  p_nonce text,
  p_ciphertext text,
  p_encrypted_data_key text,
  p_fingerprint text,
  p_last_four text
)
returns setof public.trading_wallets
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_credential_id uuid;
  v_existing public.trading_wallets%rowtype;
  v_existing_fingerprint text;
  v_idempotency_lock bigint;
  v_fingerprint_lock bigint;
begin
  if length(p_idempotency_key) not between 16 and 128
    or p_idempotency_key !~ '^[A-Za-z0-9._:@+-]+$'
  then
    raise exception 'invalid wallet enrollment idempotency key'
      using errcode = '22023';
  end if;
  if p_signature_type not in (0, 1, 2, 3)
    or p_fingerprint !~ '^[0-9a-f]{64}$'
    or (
      p_owner_address is not null
      and lower(p_owner_address) !~ '^0x[0-9a-f]{40}$'
    )
  then
    raise exception 'invalid wallet enrollment input' using errcode = '22023';
  end if;

  -- Serialize both the request identity and the signer identity. Sorting the
  -- two advisory lock keys prevents lock-order inversions.
  v_idempotency_lock := hashtextextended(
    p_account_id::text || ':wallet-idempotency:' || p_idempotency_key,
    0
  );
  v_fingerprint_lock := hashtextextended(
    p_account_id::text || ':wallet-fingerprint:' || p_fingerprint,
    0
  );
  perform pg_advisory_xact_lock(least(v_idempotency_lock, v_fingerprint_lock));
  if v_idempotency_lock <> v_fingerprint_lock then
    perform pg_advisory_xact_lock(greatest(v_idempotency_lock, v_fingerprint_lock));
  end if;

  select wallet.* into v_existing
  from public.trading_wallets as wallet
  where wallet.account_id = p_account_id
    and wallet.enrollment_idempotency_key = p_idempotency_key;
  if found then
    select credential.fingerprint into v_existing_fingerprint
    from public.credential_refs as credential
    where credential.account_id = p_account_id
      and credential.id = v_existing.signer_credential_id;
    if v_existing_fingerprint is distinct from p_fingerprint
      or v_existing.signature_type is distinct from p_signature_type
      or v_existing.owner_address is distinct from lower(p_owner_address)
    then
      raise exception 'wallet enrollment idempotency conflict'
        using errcode = '23505';
    end if;
    return next v_existing;
    return;
  end if;

  select wallet.* into v_existing
  from public.trading_wallets as wallet
  join public.credential_refs as credential
    on credential.account_id = wallet.account_id
    and credential.id = wallet.signer_credential_id
  where wallet.account_id = p_account_id
    and credential.kind = 'evm_signer_key'
    and credential.fingerprint = p_fingerprint
    and credential.status in ('pending_verification', 'active', 'revocation_pending')
  limit 1;
  if found then
    if v_existing.signature_type is distinct from p_signature_type
      or v_existing.owner_address is distinct from lower(p_owner_address)
    then
      raise exception 'signer key is already enrolled with different wallet metadata'
        using errcode = '23505';
    end if;
    return next v_existing;
    return;
  end if;

  insert into public.credential_refs (
    account_id, kind, provider, label, status, cipher_algorithm,
    key_version, aad_version, nonce, ciphertext, encrypted_data_key,
    fingerprint, last_four
  )
  values (
    p_account_id, 'evm_signer_key', 'polymarket', p_label,
    'pending_verification', p_algorithm, p_key_version, p_aad_version,
    p_nonce, p_ciphertext, p_encrypted_data_key, p_fingerprint, p_last_four
  )
  returning id into v_credential_id;

  insert into public.trading_wallets (
    account_id, label, enrollment_idempotency_key, owner_address,
    signer_credential_id, signature_type, status
  )
  values (
    p_account_id, p_label, p_idempotency_key, lower(p_owner_address),
    v_credential_id, p_signature_type, 'pending_verification'
  )
  returning * into v_existing;
  return next v_existing;
  return;
end;
$$;

create or replace function public.request_trading_wallet_revocation(
  p_account_id uuid,
  p_wallet_id uuid
)
returns setof public.trading_wallets
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_credential_id uuid;
  v_wallet_status text;
  v_mode text;
begin
  select wallet.signer_credential_id, wallet.status
  into v_credential_id, v_wallet_status
  from public.trading_wallets as wallet
  where wallet.id = p_wallet_id
    and wallet.account_id = p_account_id
  for update;
  if not found then
    return;
  end if;

  if v_wallet_status in ('revoked', 'revocation_pending') then
    return query
    select wallet.*
    from public.trading_wallets as wallet
    where wallet.id = p_wallet_id and wallet.account_id = p_account_id;
    return;
  end if;

  if v_wallet_status <> 'active' then
    update public.credential_refs
    set status = 'revoked', revoked_at = coalesce(revoked_at, now())
    where id = v_credential_id and account_id = p_account_id;

    return query
    update public.trading_wallets as wallet
    set
      status = 'revoked',
      deposit_wallet_address = null,
      signer_address = null,
      chain_id = null,
      collateral_token = null,
      revoked_at = now(),
      version = wallet.version + 1,
      lifecycle_claimed_by = null,
      lifecycle_lease_expires_at = null
    where wallet.id = p_wallet_id and wallet.account_id = p_account_id
    returning wallet.*;
    return;
  end if;

  update public.credential_refs
  set status = 'revocation_pending'
  where id = v_credential_id and account_id = p_account_id and status <> 'revoked';

  update public.cycle_jobs
  set status = 'cancelled', completed_at = now()
  where account_id = p_account_id and status = 'queued';

  update public.account_runtime_profiles as profile
  set
    trading_wallet_id = null,
    desired_mode = 'paper',
    auto_run_enabled = false,
    next_run_at = null,
    version = profile.version + 1
  where profile.account_id = p_account_id
    and profile.trading_wallet_id = p_wallet_id;

  select control.mode into v_mode
  from public.runtime_controls as control
  where control.account_id = p_account_id;
  if v_mode is not null then
    perform 1
    from public.disarm_runtime_control(p_account_id, v_mode);
  end if;

  return query
  update public.trading_wallets as wallet
  set status = 'revocation_pending', version = wallet.version + 1
  where wallet.id = p_wallet_id and wallet.account_id = p_account_id
  returning wallet.*;
end;
$$;

create or replace function public.claim_trading_wallet_lifecycle(
  p_claimed_by text,
  p_lease_seconds integer
)
returns setof public.trading_wallets
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_wallet_id uuid;
begin
  if length(p_claimed_by) not between 1 and 120
    or p_lease_seconds not between 10 and 300
  then
    raise exception 'invalid wallet lifecycle lease' using errcode = '22023';
  end if;

  with exhausted as (
    update public.trading_wallets as wallet
    set
      status = 'failed',
      lifecycle_claimed_by = null,
      lifecycle_lease_expires_at = null,
      last_error_code = 'verification_attempts_exhausted'
    where wallet.status = 'pending_verification'
      and wallet.lifecycle_attempt_count >= 5
      and (
        wallet.lifecycle_claimed_by is null
        or wallet.lifecycle_lease_expires_at <= now()
      )
    returning wallet.account_id, wallet.signer_credential_id
  )
  update public.credential_refs as credential
  set status = 'revoked', revoked_at = now()
  from exhausted
  where credential.account_id = exhausted.account_id
    and credential.id = exhausted.signer_credential_id
    and credential.status = 'pending_verification';

  select wallet.id into v_wallet_id
  from public.trading_wallets as wallet
  where wallet.status in ('pending_verification', 'revocation_pending')
    and (
      wallet.lifecycle_claimed_by is null
      or wallet.lifecycle_lease_expires_at <= now()
    )
    and (
      wallet.status = 'revocation_pending'
      or wallet.lifecycle_attempt_count < 5
    )
  order by
    case when wallet.status = 'revocation_pending' then 0 else 1 end,
    wallet.created_at
  for update skip locked
  limit 1;

  if v_wallet_id is null then
    return;
  end if;

  return query
  update public.trading_wallets as wallet
  set
    lifecycle_claimed_by = p_claimed_by,
    lifecycle_fencing_token = wallet.lifecycle_fencing_token + 1,
    lifecycle_lease_expires_at = now() + make_interval(secs => p_lease_seconds),
    lifecycle_attempt_count = wallet.lifecycle_attempt_count
      + case when wallet.status = 'pending_verification' then 1 else 0 end
  where wallet.id = v_wallet_id
  returning wallet.*;
end;
$$;

create or replace function public.get_wallet_lifecycle_envelope(
  p_account_id uuid,
  p_wallet_id uuid,
  p_claimed_by text,
  p_fencing_token bigint
)
returns setof public.credential_refs
language sql
security definer
set search_path = ''
as $$
  select credential.*
  from public.trading_wallets as wallet
  join public.credential_refs as credential
    on credential.account_id = wallet.account_id
    and credential.id = wallet.signer_credential_id
  where wallet.id = p_wallet_id
    and wallet.account_id = p_account_id
    and wallet.status in ('pending_verification', 'revocation_pending')
    and wallet.lifecycle_claimed_by = p_claimed_by
    and wallet.lifecycle_fencing_token = p_fencing_token
    and wallet.lifecycle_lease_expires_at > now()
    and credential.kind = 'evm_signer_key'
    and (
      (wallet.status = 'pending_verification' and credential.status = 'pending_verification')
      or (wallet.status = 'revocation_pending' and credential.status = 'revocation_pending')
    );
$$;

create or replace function public.heartbeat_trading_wallet_lifecycle(
  p_account_id uuid,
  p_wallet_id uuid,
  p_claimed_by text,
  p_fencing_token bigint,
  p_lease_seconds integer
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_lease_seconds not between 10 and 300 then
    return false;
  end if;
  update public.trading_wallets as wallet
  set lifecycle_lease_expires_at = now() + make_interval(secs => p_lease_seconds)
  where wallet.id = p_wallet_id
    and wallet.account_id = p_account_id
    and wallet.lifecycle_claimed_by = p_claimed_by
    and wallet.lifecycle_fencing_token = p_fencing_token
    and wallet.lifecycle_lease_expires_at > now()
    and wallet.status in ('pending_verification', 'revocation_pending');
  return found;
end;
$$;

create or replace function public.complete_trading_wallet_verification(
  p_account_id uuid,
  p_wallet_id uuid,
  p_claimed_by text,
  p_fencing_token bigint,
  p_signer_address text,
  p_deposit_wallet_address text,
  p_chain_id integer,
  p_collateral_token text
)
returns setof public.trading_wallets
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_credential_id uuid;
begin
  if lower(p_signer_address) !~ '^0x[0-9a-f]{40}$'
    or lower(p_deposit_wallet_address) !~ '^0x[0-9a-f]{40}$'
    or p_chain_id is null
    or p_chain_id <= 0
    or lower(p_collateral_token) !~ '^0x[0-9a-f]{40}$'
  then
    raise exception 'invalid verified wallet environment' using errcode = '22023';
  end if;

  select wallet.signer_credential_id into v_credential_id
  from public.trading_wallets as wallet
  where wallet.id = p_wallet_id
    and wallet.account_id = p_account_id
    and wallet.status = 'pending_verification'
    and wallet.lifecycle_claimed_by = p_claimed_by
    and wallet.lifecycle_fencing_token = p_fencing_token
    and wallet.lifecycle_lease_expires_at > now()
  for update;
  if not found then
    return;
  end if;

  update public.credential_refs as credential
  set status = 'active'
  where credential.id = v_credential_id
    and credential.account_id = p_account_id
    and credential.kind = 'evm_signer_key'
    and credential.status = 'pending_verification';
  if not found then
    raise exception 'pending signer credential is unavailable' using errcode = '55000';
  end if;

  return query
  update public.trading_wallets as wallet
  set
    signer_address = lower(p_signer_address),
    deposit_wallet_address = lower(p_deposit_wallet_address),
    chain_id = p_chain_id,
    collateral_token = lower(p_collateral_token),
    status = 'active',
    verified_at = now(),
    version = wallet.version + 1,
    lifecycle_claimed_by = null,
    lifecycle_lease_expires_at = null,
    last_error_code = null
  where wallet.id = p_wallet_id and wallet.account_id = p_account_id
  returning wallet.*;
end;
$$;

create or replace function public.fail_trading_wallet_verification(
  p_account_id uuid,
  p_wallet_id uuid,
  p_claimed_by text,
  p_fencing_token bigint,
  p_error_code text,
  p_retryable boolean
)
returns setof public.trading_wallets
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_credential_id uuid;
  v_terminal boolean;
begin
  if length(p_error_code) not between 1 and 64
    or p_error_code !~ '^[a-z0-9_.:-]+$'
  then
    raise exception 'invalid wallet verification error code' using errcode = '22023';
  end if;
  select
    wallet.signer_credential_id,
    (not p_retryable or wallet.lifecycle_attempt_count >= 5)
  into v_credential_id, v_terminal
  from public.trading_wallets as wallet
  where wallet.id = p_wallet_id
    and wallet.account_id = p_account_id
    and wallet.status = 'pending_verification'
    and wallet.lifecycle_claimed_by = p_claimed_by
    and wallet.lifecycle_fencing_token = p_fencing_token
    and wallet.lifecycle_lease_expires_at > now()
  for update;
  if not found then
    return;
  end if;

  if v_terminal then
    update public.credential_refs
    set status = 'revoked', revoked_at = now()
    where id = v_credential_id and account_id = p_account_id;
  end if;

  return query
  update public.trading_wallets as wallet
  set
    status = case when v_terminal then 'failed' else 'pending_verification' end,
    lifecycle_claimed_by = null,
    lifecycle_lease_expires_at = null,
    last_error_code = p_error_code
  where wallet.id = p_wallet_id and wallet.account_id = p_account_id
  returning wallet.*;
end;
$$;

create or replace function public.complete_trading_wallet_revocation(
  p_account_id uuid,
  p_wallet_id uuid,
  p_claimed_by text,
  p_fencing_token bigint
)
returns setof public.trading_wallets
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_credential_id uuid;
begin
  select wallet.signer_credential_id into v_credential_id
  from public.trading_wallets as wallet
  where wallet.id = p_wallet_id
    and wallet.account_id = p_account_id
    and wallet.status = 'revocation_pending'
    and wallet.lifecycle_claimed_by = p_claimed_by
    and wallet.lifecycle_fencing_token = p_fencing_token
    and wallet.lifecycle_lease_expires_at > now()
    and not exists (
      select 1 from public.orders as open_order
      where open_order.account_id = p_account_id
        and open_order.status in (
          'created', 'signed', 'submitting', 'submitted', 'live',
          'partially_filled', 'cancel_pending', 'unknown'
        )
    )
  for update;
  if not found then
    return;
  end if;

  update public.credential_refs
  set status = 'revoked', revoked_at = now()
  where id = v_credential_id and account_id = p_account_id;

  return query
  update public.trading_wallets as wallet
  set
    status = 'revoked',
    revoked_at = now(),
    version = wallet.version + 1,
    lifecycle_claimed_by = null,
    lifecycle_lease_expires_at = null
  where wallet.id = p_wallet_id and wallet.account_id = p_account_id
  returning wallet.*;
end;
$$;

create or replace function public.update_account_runtime_profile(
  p_account_id uuid,
  p_expected_version bigint,
  p_ai_provider text,
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

  if p_ai_provider not in ('platform', 'openai', 'anthropic', 'openrouter', 'litellm', 'mock')
    or p_desired_mode not in ('paper', 'shadow', 'canary', 'live')
    or p_cycle_interval_seconds not between 30 and 3600
    or p_forecast_model !~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$'
  then
    raise exception 'invalid runtime profile' using errcode = '22023';
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

create or replace function public.enqueue_cycle_job(
  p_account_id uuid,
  p_idempotency_key text,
  p_mode text,
  p_trading_wallet_id uuid default null,
  p_ai_credential_id uuid default null,
  p_risk_policy_id uuid default null,
  p_run_after timestamptz default null
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_profile public.account_runtime_profiles%rowtype;
  v_control public.runtime_controls%rowtype;
  v_existing public.cycle_jobs%rowtype;
  v_job public.cycle_jobs%rowtype;
  v_wallet_id uuid;
  v_ai_id uuid;
  v_risk_id uuid;
  v_risk_version bigint;
begin
  if p_mode not in ('paper', 'shadow', 'canary', 'live')
    or length(p_idempotency_key) not between 16 and 128
    or p_idempotency_key !~ '^[A-Za-z0-9._:@+-]+$'
    or (p_run_after is not null and p_run_after > now() + interval '24 hours')
  then
    raise exception 'invalid cycle job request' using errcode = '22023';
  end if;

  select * into v_profile
  from public.ensure_account_runtime_profile(p_account_id);
  v_wallet_id := coalesce(p_trading_wallet_id, v_profile.trading_wallet_id);
  v_ai_id := coalesce(p_ai_credential_id, v_profile.ai_credential_id);
  v_risk_id := coalesce(p_risk_policy_id, v_profile.risk_policy_id);

  perform pg_advisory_xact_lock(
    hashtextextended(p_account_id::text || ':cycle:' || p_idempotency_key, 0)
  );
  select * into v_existing
  from public.cycle_jobs as job
  where job.account_id = p_account_id and job.idempotency_key = p_idempotency_key;
  if found then
    if v_existing.mode is distinct from p_mode
      or v_existing.trading_wallet_id is distinct from v_wallet_id
      or v_existing.ai_credential_id is distinct from v_ai_id
      or v_existing.risk_policy_id is distinct from v_risk_id
      or v_existing.requested_run_after is distinct from p_run_after
    then
      raise exception 'cycle job idempotency conflict' using errcode = '23505';
    end if;
    return to_jsonb(v_existing) || jsonb_build_object('deduplicated', true);
  end if;

  if v_ai_id is not null and not exists (
    select 1 from public.credential_refs as credential
    where credential.id = v_ai_id
      and credential.account_id = p_account_id
      and credential.kind = 'ai_api_key'
      and credential.status = 'active'
  ) then
    raise exception 'AI credential is inactive or cross-tenant' using errcode = '23503';
  end if;
  if v_risk_id is not null and not exists (
    select 1 from public.risk_policies as risk
    where risk.id = v_risk_id
      and risk.account_id = p_account_id
      and risk.status = 'active'
  ) then
    raise exception 'risk policy is inactive or cross-tenant' using errcode = '23503';
  end if;
  if v_risk_id is not null then
    select risk.version into v_risk_version
    from public.risk_policies as risk
    where risk.id = v_risk_id
      and risk.account_id = p_account_id
      and risk.status = 'active';
  end if;

  if p_mode in ('canary', 'live') then
    if v_wallet_id is null or v_ai_id is null or v_risk_id is null or not exists (
      select 1 from public.trading_wallets as wallet
      where wallet.id = v_wallet_id
        and wallet.account_id = p_account_id
        and wallet.status = 'active'
    ) then
      raise exception 'real-money job prerequisites are incomplete' using errcode = '23514';
    end if;
    select * into v_control
    from public.runtime_controls as control
    where control.account_id = p_account_id;
    if not found
      or not v_control.armed
      or v_control.kill_switch
      or not v_control.accept_new_intents
      or v_control.cancellation_pending
      or v_control.mode <> p_mode
      or v_control.armed_until <= now()
    then
      raise exception 'real-money runtime is not armed' using errcode = '55000';
    end if;
  end if;

  insert into public.cycle_jobs (
    account_id, trading_wallet_id, ai_credential_id, risk_policy_id, risk_policy_version,
    mode, idempotency_key, run_after, requested_run_after
  )
  values (
    p_account_id, v_wallet_id, v_ai_id, v_risk_id, v_risk_version, p_mode,
    p_idempotency_key, coalesce(p_run_after, now()), p_run_after
  )
  returning * into v_job;
  return to_jsonb(v_job) || jsonb_build_object('deduplicated', false);
end;
$$;

create or replace function public.enqueue_due_cycle_jobs(p_limit integer default 100)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_profile public.account_runtime_profiles%rowtype;
  v_enqueued integer := 0;
  v_idempotency text;
begin
  if p_limit not between 1 and 500 then
    raise exception 'enqueue limit out of range' using errcode = '22023';
  end if;

  for v_profile in
    select profile.*
    from public.account_runtime_profiles as profile
    where profile.auto_run_enabled
      and profile.status = 'active'
      and profile.next_run_at <= now()
    order by profile.next_run_at, profile.account_id
    for update skip locked
    limit p_limit
  loop
    update public.account_runtime_profiles as profile
    set next_run_at = greatest(now(), v_profile.next_run_at)
      + make_interval(secs => v_profile.cycle_interval_seconds)
    where profile.account_id = v_profile.account_id;

    if exists (
      select 1 from public.cycle_jobs as active_job
      where active_job.account_id = v_profile.account_id
        and active_job.status in ('queued', 'claimed', 'running')
    ) then
      continue;
    end if;
    if v_profile.desired_mode in ('canary', 'live') and not exists (
      select 1 from public.runtime_controls as control
      where control.account_id = v_profile.account_id
        and control.mode = v_profile.desired_mode
        and control.armed
        and control.accept_new_intents
        and not control.kill_switch
        and not control.cancellation_pending
        and control.armed_until > now()
    ) then
      continue;
    end if;

    v_idempotency := 'auto:' || replace(v_profile.account_id::text, '-', '')
      || ':' || floor(extract(epoch from v_profile.next_run_at))::bigint::text;
    begin
      perform public.enqueue_cycle_job(
        v_profile.account_id,
        v_idempotency,
        v_profile.desired_mode,
        v_profile.trading_wallet_id,
        v_profile.ai_credential_id,
        v_profile.risk_policy_id,
        now()
      );
      v_enqueued := v_enqueued + 1;
    exception
      when unique_violation or check_violation or foreign_key_violation then
        null;
    end;
  end loop;
  return v_enqueued;
end;
$$;

create or replace function public.claim_next_cycle_job(
  p_claimed_by text,
  p_lease_seconds integer,
  p_account_id uuid default null
)
returns setof public.cycle_jobs
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_job_id uuid;
begin
  if length(p_claimed_by) not between 1 and 120
    or p_lease_seconds not between 10 and 300
  then
    raise exception 'invalid cycle job lease' using errcode = '22023';
  end if;

  update public.cycle_jobs as expired
  set
    status = 'failed',
    error_code = 'lease_exhausted',
    completed_at = now(),
    lease_expires_at = null
  where expired.status in ('claimed', 'running')
    and expired.lease_expires_at <= now()
    and expired.attempt_count >= expired.max_attempts
    and (p_account_id is null or expired.account_id = p_account_id);

  select job.id into v_job_id
  from public.cycle_jobs as job
  where (p_account_id is null or job.account_id = p_account_id)
    and job.run_after <= now()
    and (
      job.status = 'queued'
      or (
        job.status in ('claimed', 'running')
        and job.lease_expires_at <= now()
        and job.attempt_count < job.max_attempts
      )
    )
  order by job.run_after, job.created_at
  for update skip locked
  limit 1;

  if v_job_id is null then
    return;
  end if;

  return query
  update public.cycle_jobs as job
  set
    status = 'claimed',
    claimed_by = p_claimed_by,
    fencing_token = job.fencing_token + 1,
    heartbeat_at = now(),
    lease_expires_at = now() + make_interval(secs => p_lease_seconds),
    attempt_count = job.attempt_count + 1,
    error_code = null
  where job.id = v_job_id
  returning job.*;
end;
$$;

create or replace function public.heartbeat_cycle_job(
  p_account_id uuid,
  p_job_id uuid,
  p_claimed_by text,
  p_fencing_token bigint,
  p_lease_seconds integer
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_lease_seconds not between 10 and 300 then
    return false;
  end if;
  update public.cycle_jobs as job
  set
    heartbeat_at = now(),
    lease_expires_at = now() + make_interval(secs => p_lease_seconds)
  where job.id = p_job_id
    and job.account_id = p_account_id
    and job.claimed_by = p_claimed_by
    and job.fencing_token = p_fencing_token
    and job.status in ('claimed', 'running')
    and job.lease_expires_at > now();
  return found;
end;
$$;

create or replace function public.mark_cycle_job_running(
  p_account_id uuid,
  p_job_id uuid,
  p_claimed_by text,
  p_fencing_token bigint
)
returns setof public.cycle_jobs
language sql
security definer
set search_path = ''
as $$
  update public.cycle_jobs as job
  set status = 'running', started_at = coalesce(job.started_at, now())
  where job.id = p_job_id
    and job.account_id = p_account_id
    and job.claimed_by = p_claimed_by
    and job.fencing_token = p_fencing_token
    and job.status = 'claimed'
    and job.lease_expires_at > now()
  returning job.*;
$$;

create or replace function public.validate_cycle_job_lease(
  p_account_id uuid,
  p_job_id uuid,
  p_claimed_by text,
  p_fencing_token bigint
)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.cycle_jobs as job
    where job.id = p_job_id
      and job.account_id = p_account_id
      and job.claimed_by = p_claimed_by
      and job.fencing_token = p_fencing_token
      and job.status in ('claimed', 'running')
      and job.lease_expires_at > now()
  );
$$;

create or replace function public.complete_cycle_job(
  p_account_id uuid,
  p_job_id uuid,
  p_claimed_by text,
  p_fencing_token bigint
)
returns setof public.cycle_jobs
language sql
security definer
set search_path = ''
as $$
  update public.cycle_jobs as job
  set status = 'succeeded', completed_at = now(), lease_expires_at = null
  where job.id = p_job_id
    and job.account_id = p_account_id
    and job.claimed_by = p_claimed_by
    and job.fencing_token = p_fencing_token
    and job.status in ('claimed', 'running')
    and job.lease_expires_at > now()
  returning job.*;
$$;

create or replace function public.fail_cycle_job(
  p_account_id uuid,
  p_job_id uuid,
  p_claimed_by text,
  p_fencing_token bigint,
  p_error_code text,
  p_retryable boolean
)
returns setof public.cycle_jobs
language plpgsql
security definer
set search_path = ''
as $$
begin
  if length(p_error_code) not between 1 and 64
    or p_error_code !~ '^[a-z0-9_.:-]+$'
  then
    raise exception 'invalid worker error code' using errcode = '22023';
  end if;
  return query
  update public.cycle_jobs as job
  set
    status = case
      when p_retryable and job.attempt_count < job.max_attempts then 'queued'
      else 'failed'
    end,
    claimed_by = case
      when p_retryable and job.attempt_count < job.max_attempts then null
      else job.claimed_by
    end,
    lease_expires_at = null,
    run_after = case
      when p_retryable and job.attempt_count < job.max_attempts
        then now() + interval '30 seconds'
      else job.run_after
    end,
    error_code = p_error_code,
    completed_at = case
      when p_retryable and job.attempt_count < job.max_attempts then null
      else now()
    end
  where job.id = p_job_id
    and job.account_id = p_account_id
    and job.claimed_by = p_claimed_by
    and job.fencing_token = p_fencing_token
    and job.status in ('claimed', 'running')
    and job.lease_expires_at > now()
  returning job.*;
end;
$$;

alter table public.credential_refs enable row level security;
alter table public.credential_refs force row level security;
alter table public.trading_wallets enable row level security;
alter table public.trading_wallets force row level security;
alter table public.risk_policies enable row level security;
alter table public.risk_policies force row level security;
alter table public.account_runtime_profiles enable row level security;
alter table public.account_runtime_profiles force row level security;
alter table public.cycle_jobs enable row level security;
alter table public.cycle_jobs force row level security;
alter table public.credential_audit_events enable row level security;
alter table public.credential_audit_events force row level security;

create policy credential_refs_owner_read
  on public.credential_refs for select to authenticated
  using ((select auth.uid()) = account_id);
create policy trading_wallets_owner_read
  on public.trading_wallets for select to authenticated
  using ((select auth.uid()) = account_id);
create policy risk_policies_owner_read
  on public.risk_policies for select to authenticated
  using ((select auth.uid()) = account_id);
create policy account_runtime_profiles_owner_read
  on public.account_runtime_profiles for select to authenticated
  using ((select auth.uid()) = account_id);
create policy cycle_jobs_owner_read
  on public.cycle_jobs for select to authenticated
  using ((select auth.uid()) = account_id);
create policy credential_audit_owner_read
  on public.credential_audit_events for select to authenticated
  using ((select auth.uid()) = account_id);

revoke all on table
  public.credential_refs,
  public.trading_wallets,
  public.risk_policies,
  public.account_runtime_profiles,
  public.cycle_jobs,
  public.credential_audit_events
from public, anon, authenticated;

-- Authenticated dashboards receive metadata only. Ciphertext and signer
-- references stay behind the service-role API/worker boundary.
grant select (
  id, account_id, kind, provider, label, status, version,
  created_at, rotated_at, revoked_at, updated_at
) on public.credential_refs to authenticated;
grant select (
  id, account_id, label, owner_address, deposit_wallet_address, signer_address,
  chain_id, collateral_token, signature_type, status, version, verified_at,
  revoked_at, created_at, updated_at
) on public.trading_wallets to authenticated;
grant select on public.risk_policies to authenticated;
grant select on public.account_runtime_profiles to authenticated;
grant select (
  id, account_id, trading_wallet_id, ai_credential_id, risk_policy_id,
  risk_policy_version, mode, idempotency_key, status, run_after, attempt_count, max_attempts,
  error_code, started_at, completed_at, created_at, updated_at
) on public.cycle_jobs to authenticated;
grant select on public.credential_audit_events to authenticated;

revoke all on table
  public.credential_refs,
  public.trading_wallets,
  public.risk_policies,
  public.account_runtime_profiles,
  public.cycle_jobs,
  public.credential_audit_events
from service_role;
grant select on table
  public.credential_refs,
  public.trading_wallets,
  public.risk_policies,
  public.account_runtime_profiles,
  public.cycle_jobs,
  public.credential_audit_events
to service_role;

revoke all on function public.ensure_account_runtime_profile(uuid)
  from public, anon, authenticated;
revoke all on function public.store_ai_credential(
  uuid, text, text, text, integer, bigint, text, text, text, text, text
) from public, anon, authenticated;
revoke all on function public.revoke_ai_credential(uuid, text)
  from public, anon, authenticated;
revoke all on function public.import_trading_wallet(
  uuid, text, text, smallint, text, text, integer, bigint,
  text, text, text, text, text
) from public, anon, authenticated;
revoke all on function public.request_trading_wallet_revocation(uuid, uuid)
  from public, anon, authenticated;
revoke all on function public.claim_trading_wallet_lifecycle(text, integer)
  from public, anon, authenticated;
revoke all on function public.get_wallet_lifecycle_envelope(uuid, uuid, text, bigint)
  from public, anon, authenticated;
revoke all on function public.heartbeat_trading_wallet_lifecycle(
  uuid, uuid, text, bigint, integer
) from public, anon, authenticated;
revoke all on function public.complete_trading_wallet_verification(
  uuid, uuid, text, bigint, text, text, integer, text
) from public, anon, authenticated;
revoke all on function public.fail_trading_wallet_verification(
  uuid, uuid, text, bigint, text, boolean
) from public, anon, authenticated;
revoke all on function public.complete_trading_wallet_revocation(
  uuid, uuid, text, bigint
) from public, anon, authenticated;
revoke all on function public.update_account_runtime_profile(
  uuid, text, text, uuid, uuid, uuid, text, boolean, integer
) from public, anon, authenticated;
revoke all on function public.enqueue_cycle_job(
  uuid, text, text, uuid, uuid, uuid, timestamptz
) from public, anon, authenticated;
revoke all on function public.enqueue_due_cycle_jobs(integer)
  from public, anon, authenticated;
revoke all on function public.claim_next_cycle_job(text, integer, uuid)
  from public, anon, authenticated;
revoke all on function public.heartbeat_cycle_job(uuid, uuid, text, bigint, integer)
  from public, anon, authenticated;
revoke all on function public.mark_cycle_job_running(uuid, uuid, text, bigint)
  from public, anon, authenticated;
revoke all on function public.validate_cycle_job_lease(uuid, uuid, text, bigint)
  from public, anon, authenticated;
revoke all on function public.complete_cycle_job(uuid, uuid, text, bigint)
  from public, anon, authenticated;
revoke all on function public.fail_cycle_job(uuid, uuid, text, bigint, text, boolean)
  from public, anon, authenticated;

grant execute on function public.ensure_account_runtime_profile(uuid) to service_role;
grant execute on function public.store_ai_credential(
  uuid, text, text, text, integer, bigint, text, text, text, text, text
) to service_role;
grant execute on function public.revoke_ai_credential(uuid, text) to service_role;
grant execute on function public.import_trading_wallet(
  uuid, text, text, smallint, text, text, integer, bigint,
  text, text, text, text, text
) to service_role;
grant execute on function public.request_trading_wallet_revocation(uuid, uuid)
  to service_role;
grant execute on function public.claim_trading_wallet_lifecycle(text, integer)
  to service_role;
grant execute on function public.get_wallet_lifecycle_envelope(uuid, uuid, text, bigint)
  to service_role;
grant execute on function public.heartbeat_trading_wallet_lifecycle(
  uuid, uuid, text, bigint, integer
) to service_role;
grant execute on function public.complete_trading_wallet_verification(
  uuid, uuid, text, bigint, text, text, integer, text
) to service_role;
grant execute on function public.fail_trading_wallet_verification(
  uuid, uuid, text, bigint, text, boolean
) to service_role;
grant execute on function public.complete_trading_wallet_revocation(
  uuid, uuid, text, bigint
) to service_role;
grant execute on function public.update_account_runtime_profile(
  uuid, text, text, uuid, uuid, uuid, text, boolean, integer
) to service_role;
grant execute on function public.enqueue_cycle_job(
  uuid, text, text, uuid, uuid, uuid, timestamptz
) to service_role;
grant execute on function public.enqueue_due_cycle_jobs(integer) to service_role;
grant execute on function public.claim_next_cycle_job(text, integer, uuid) to service_role;
grant execute on function public.heartbeat_cycle_job(uuid, uuid, text, bigint, integer)
  to service_role;
grant execute on function public.mark_cycle_job_running(uuid, uuid, text, bigint)
  to service_role;
grant execute on function public.validate_cycle_job_lease(uuid, uuid, text, bigint)
  to service_role;
grant execute on function public.complete_cycle_job(uuid, uuid, text, bigint)
  to service_role;
grant execute on function public.fail_cycle_job(uuid, uuid, text, bigint, text, boolean)
  to service_role;

comment on table public.credential_refs is
  'Encrypted tenant credential envelopes. Plaintext must never be inserted or returned.';
comment on column public.credential_refs.aad_version is
  'Immutable random envelope version bound into AES-GCM AAD; independent from lifecycle version.';
comment on table public.cycle_jobs is
  'Tenant cycle queue with idempotency, expiring leases, and monotonic fencing tokens.';
comment on function public.enqueue_due_cycle_jobs(integer) is
  'Atomically schedules due opt-in profiles; real-money profiles still require an unexpired arm.';

commit;
