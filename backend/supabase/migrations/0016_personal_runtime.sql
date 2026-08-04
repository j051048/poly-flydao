begin;

-- A personal deployment keeps its signer and AI credential in one Zeabur
-- process.  Only public wallet identity/readiness is persisted here.  The EVM
-- private key and AI API key must never enter Supabase.
create table public.personal_runtime_bindings (
  account_id uuid primary key references auth.users(id) on delete cascade,
  signer_address text not null,
  deposit_wallet_address text not null,
  chain_id integer not null,
  collateral_token text not null,
  binding_version bigint not null default 1,
  paused boolean not null default false,
  collateral_balance_pusd numeric(38, 18),
  allowances_ready boolean not null default false,
  readiness_checked_at timestamptz,
  readiness_owner_id text,
  readiness_fencing_token bigint,
  last_seen_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint personal_runtime_bindings_signer_shape
    check (signer_address ~ '^0x[0-9a-f]{40}$'),
  constraint personal_runtime_bindings_deposit_shape
    check (deposit_wallet_address ~ '^0x[0-9a-f]{40}$'),
  constraint personal_runtime_bindings_chain_shape check (chain_id > 0),
  constraint personal_runtime_bindings_collateral_shape
    check (collateral_token ~ '^0x[0-9a-f]{40}$'),
  constraint personal_runtime_bindings_version_positive check (binding_version > 0),
  constraint personal_runtime_bindings_balance_nonnegative
    check (collateral_balance_pusd is null or collateral_balance_pusd >= 0),
  constraint personal_runtime_bindings_readiness_shape
    check (
      (
        readiness_checked_at is null
        and collateral_balance_pusd is null
        and not allowances_ready
        and readiness_owner_id is null
        and readiness_fencing_token is null
      )
      or (
        readiness_checked_at is not null
        and collateral_balance_pusd is not null
        and readiness_owner_id is not null
        and length(btrim(readiness_owner_id)) >= 8
        and readiness_fencing_token is not null
        and readiness_fencing_token > 0
      )
    )
);

create unique index personal_runtime_bindings_deposit_uidx
  on public.personal_runtime_bindings(deposit_wallet_address);

-- Bind public identity to the worker currently holding the account lease.
-- Repeating the same identity is a heartbeat.  A wallet rotation is accepted
-- only while the runtime is fully stopped and no potentially-live order exists.
create or replace function public.bind_personal_runtime_wallet(
  p_account_id uuid,
  p_owner_id text,
  p_fencing_token bigint,
  p_signer_address text,
  p_deposit_wallet_address text,
  p_chain_id integer,
  p_collateral_token text
)
returns setof public.personal_runtime_bindings
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_signer text := lower(btrim(p_signer_address));
  v_deposit text := lower(btrim(p_deposit_wallet_address));
  v_collateral text := lower(btrim(p_collateral_token));
  v_current public.personal_runtime_bindings%rowtype;
  v_identity_changed boolean;
begin
  if p_account_id is null
     or p_owner_id is null
     or length(btrim(p_owner_id)) < 8
     or p_fencing_token is null
     or p_fencing_token <= 0
     or p_signer_address is null
     or p_deposit_wallet_address is null
     or p_collateral_token is null
     or p_chain_id is null
     or v_signer !~ '^0x[0-9a-f]{40}$'
     or v_deposit !~ '^0x[0-9a-f]{40}$'
     or v_collateral !~ '^0x[0-9a-f]{40}$'
     or p_chain_id <= 0
  then
    raise exception 'invalid personal wallet binding' using errcode = '22023';
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended(p_account_id::text || ':personal-runtime', 0)
  );

  if not exists (
    select 1 from public.worker_leases as lease
    where lease.account_id = p_account_id
      and lease.owner_id = p_owner_id
      and lease.fencing_token = p_fencing_token
      and lease.expires_at > now()
  ) then
    return;
  end if;

  select * into v_current
  from public.personal_runtime_bindings as binding
  where binding.account_id = p_account_id
  for update;

  if not found then
    if exists (
      select 1 from public.runtime_controls as control
      where control.account_id = p_account_id
        and (
          control.armed
          or control.accept_new_intents
          or not control.kill_switch
          or control.cancellation_pending
        )
    ) or exists (
      select 1 from public.orders as order_row
      where order_row.account_id = p_account_id
        and order_row.status in (
          'created', 'signed', 'submitting', 'submitted', 'live',
          'partially_filled', 'cancel_pending', 'unknown'
        )
    ) then
      return;
    end if;

    insert into public.personal_runtime_bindings (
      account_id, signer_address, deposit_wallet_address, chain_id,
      collateral_token, last_seen_at
    ) values (
      p_account_id, v_signer, v_deposit, p_chain_id, v_collateral, now()
    );
  else
    v_identity_changed :=
      v_current.signer_address is distinct from v_signer
      or v_current.deposit_wallet_address is distinct from v_deposit
      or v_current.chain_id is distinct from p_chain_id
      or v_current.collateral_token is distinct from v_collateral;

    if v_identity_changed and (
      exists (
        select 1 from public.runtime_controls as control
        where control.account_id = p_account_id
          and (
            control.armed
            or control.accept_new_intents
            or not control.kill_switch
            or control.cancellation_pending
          )
      )
      or exists (
        select 1 from public.orders as order_row
        where order_row.account_id = p_account_id
          and order_row.status in (
            'created', 'signed', 'submitting', 'submitted', 'live',
            'partially_filled', 'cancel_pending', 'unknown'
          )
      )
    ) then
      return;
    end if;

    update public.personal_runtime_bindings as binding
    set signer_address = v_signer,
        deposit_wallet_address = v_deposit,
        chain_id = p_chain_id,
        collateral_token = v_collateral,
        binding_version = case
          when v_identity_changed then binding.binding_version + 1
          else binding.binding_version
        end,
        collateral_balance_pusd = case
          when v_identity_changed then null
          else binding.collateral_balance_pusd
        end,
        allowances_ready = case
          when v_identity_changed then false
          else binding.allowances_ready
        end,
        readiness_checked_at = case
          when v_identity_changed then null
          else binding.readiness_checked_at
        end,
        readiness_owner_id = case
          when v_identity_changed then null
          else binding.readiness_owner_id
        end,
        readiness_fencing_token = case
          when v_identity_changed then null
          else binding.readiness_fencing_token
        end,
        last_seen_at = now(),
        updated_at = now()
    where binding.account_id = p_account_id;
  end if;

  return query
  select binding.*
  from public.personal_runtime_bindings as binding
  where binding.account_id = p_account_id;
end;
$$;

-- This is intentionally distinct from tenant wallet readiness: its only
-- credential is an already-running leased worker, and only public values are
-- persisted.  Binding version prevents a stale wallet from refreshing a newly
-- rotated identity.
create or replace function public.record_personal_wallet_readiness(
  p_account_id uuid,
  p_owner_id text,
  p_fencing_token bigint,
  p_binding_version bigint,
  p_balance_pusd numeric,
  p_allowances_ready boolean
)
returns setof public.personal_runtime_bindings
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_account_id is null
     or p_owner_id is null
     or length(btrim(p_owner_id)) < 8
     or p_fencing_token is null
     or p_fencing_token <= 0
     or p_binding_version is null
     or p_binding_version <= 0
     or p_balance_pusd is null
     or p_allowances_ready is null
     or p_balance_pusd < 0
  then
    raise exception 'invalid personal wallet readiness' using errcode = '22023';
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended(p_account_id::text || ':personal-runtime', 0)
  );

  return query
  update public.personal_runtime_bindings as binding
  set collateral_balance_pusd = p_balance_pusd,
      allowances_ready = p_allowances_ready,
      readiness_checked_at = now(),
      readiness_owner_id = p_owner_id,
      readiness_fencing_token = p_fencing_token,
      last_seen_at = now(),
      updated_at = now()
  where binding.account_id = p_account_id
    and binding.binding_version = p_binding_version
    and exists (
      select 1 from public.worker_leases as lease
      where lease.account_id = p_account_id
        and lease.owner_id = p_owner_id
        and lease.fencing_token = p_fencing_token
        and lease.expires_at > now()
    )
  returning binding.*;
end;
$$;

-- Personal arming bypasses tenant credential/profile references, but not the
-- version CAS, 15-minute limit, cancellation latch, public wallet binding, or
-- active worker lease. Balance/readiness/allowance are deliberately deferred
-- to the final submission gate so a newly bound wallet can bootstrap approvals.
create or replace function public.arm_personal_runtime_control(
  p_account_id uuid,
  p_owner_id text,
  p_fencing_token bigint,
  p_mode text,
  p_armed_until timestamptz,
  p_expected_version bigint
)
returns setof public.runtime_controls
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_account_id is null
     or p_owner_id is null
     or length(btrim(p_owner_id)) < 8
     or p_fencing_token is null
     or p_fencing_token <= 0
     or p_mode is null
     or p_mode not in ('canary', 'live')
  then
    raise exception 'personal runtime can only be armed in canary or live mode'
      using errcode = '22023';
  end if;
  if p_expected_version is null or p_expected_version < 1 then
    raise exception 'expected runtime control version must be positive'
      using errcode = '22023';
  end if;
  if p_armed_until is null
     or p_armed_until <= now()
     or p_armed_until > now() + interval '15 minutes'
  then
    raise exception 'runtime arm expiry must be within the next 15 minutes'
      using errcode = '22023';
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended(p_account_id::text || ':personal-runtime', 0)
  );
  if not exists (
    select 1 from public.personal_runtime_bindings as binding
    where binding.account_id = p_account_id
      and not binding.paused
      and exists (
        select 1 from public.worker_leases as lease
        where lease.account_id = p_account_id
          and lease.owner_id = p_owner_id
          and lease.fencing_token = p_fencing_token
          and lease.expires_at > now()
      )
  ) then
    return;
  end if;

  insert into public.runtime_controls(account_id)
  values (p_account_id)
  on conflict (account_id) do nothing;

  return query
  update public.runtime_controls as control
  set mode = p_mode,
      kill_switch = false,
      accept_new_intents = true,
      armed = true,
      armed_until = p_armed_until,
      version = control.version + 1
  where control.account_id = p_account_id
    and control.version = p_expected_version
    and not control.cancellation_pending
    and (
      (
        not control.armed
        and control.kill_switch
        and not control.accept_new_intents
        and control.armed_until is null
      )
      or (
        control.armed
        and control.mode = p_mode
        and not control.kill_switch
        and control.accept_new_intents
        and control.armed_until > now()
      )
    )
  returning control.*;
end;
$$;

-- The pause latch survives worker restarts and arm-renewal cycles. Every pause
-- request idempotently re-establishes the stopped control invariant. If a
-- worker has already acknowledged cancellation, a later repeated pause safely
-- requests another verified cancel-all before any future resume.
create or replace function public.set_personal_runtime_paused(
  p_account_id uuid,
  p_paused boolean
)
returns setof public.personal_runtime_bindings
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_account_id is null or p_paused is null or not p_paused then
    raise exception 'invalid personal runtime pause request' using errcode = '22023';
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended(p_account_id::text || ':personal-runtime', 0)
  );
  perform 1
  from public.personal_runtime_bindings as binding
  where binding.account_id = p_account_id
  for update;
  if not found then
    return;
  end if;

  update public.personal_runtime_bindings as binding
  set paused = p_paused,
      updated_at = case when binding.paused is distinct from p_paused then now()
                        else binding.updated_at end
  where binding.account_id = p_account_id;

  insert into public.runtime_controls(account_id)
  values (p_account_id)
  on conflict (account_id) do nothing;

  update public.runtime_controls as control
  set kill_switch = true,
      accept_new_intents = false,
      armed = false,
      armed_until = null,
      cancellation_pending = control.mode in ('canary', 'live'),
      version = control.version + 1
  where control.account_id = p_account_id
    and (
      not control.kill_switch
      or control.accept_new_intents
      or control.armed
      or control.armed_until is not null
      or control.cancellation_pending is distinct from (
        control.mode in ('canary', 'live')
      )
    );

  return query
  select binding.*
  from public.personal_runtime_bindings as binding
  where binding.account_id = p_account_id;
end;
$$;

-- Resume is a control-version CAS in the same advisory-lock transaction that
-- clears the durable pause latch. It deliberately does not arm trading; the
-- leased personal worker performs the short-lived arm/readiness sequence.
create or replace function public.resume_personal_runtime(
  p_account_id uuid,
  p_expected_version bigint
)
returns setof public.runtime_controls
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_account_id is null
     or p_expected_version is null
     or p_expected_version <= 0
  then
    raise exception 'invalid personal runtime resume request' using errcode = '22023';
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended(p_account_id::text || ':personal-runtime', 0)
  );
  if not exists (
    select 1 from public.personal_runtime_bindings as binding
    where binding.account_id = p_account_id
  ) then
    return;
  end if;

  insert into public.runtime_controls(account_id)
  values (p_account_id)
  on conflict (account_id) do nothing;

  perform 1
  from public.runtime_controls as control
  where control.account_id = p_account_id
    and control.version = p_expected_version
  for update;
  if not found then
    return;
  end if;

  update public.personal_runtime_bindings as binding
  set paused = false,
      updated_at = case when binding.paused then now() else binding.updated_at end
  where binding.account_id = p_account_id;

  return query
  select control.*
  from public.runtime_controls as control
  where control.account_id = p_account_id
    and control.version = p_expected_version;
end;
$$;

-- The final personal-order transition checks all mutable authority in the same
-- UPDATE: signed-order identity, active account lease, control version/arm, and
-- a wallet balance/allowance observation made by that same leased worker.
create or replace function public.mark_personal_order_submitting(
  p_account_id uuid,
  p_intent_hash text,
  p_owner_id text,
  p_worker_fencing_token bigint,
  p_control_version bigint,
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
  if p_account_id is null
     or p_intent_hash is null
     or length(btrim(p_intent_hash)) = 0
     or length(p_intent_hash) > 256
     or p_owner_id is null
     or p_mode is null
     or p_mode not in ('canary', 'live')
     or length(btrim(p_owner_id)) < 8
     or p_worker_fencing_token is null
     or p_worker_fencing_token <= 0
     or p_control_version is null
     or p_control_version <= 0
  then
    return false;
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended(p_account_id::text || ':personal-runtime', 0)
  );

  update public.orders as order_row
  set status = 'submitting',
      attempt_count = order_row.attempt_count + 1,
      response_payload = coalesce(order_row.response_payload, '{}'::jsonb)
        || jsonb_build_object('submission_scope', 'personal')
  where order_row.account_id = p_account_id
    and order_row.client_order_id = p_intent_hash
    and order_row.status = 'signed'
    and order_row.environment = p_mode
    and order_row.worker_fencing_token = p_worker_fencing_token
    and exists (
      select 1
      from public.worker_leases as lease
      join public.runtime_controls as control
        on control.account_id = p_account_id
      join public.personal_runtime_bindings as binding
        on binding.account_id = p_account_id
      where lease.account_id = p_account_id
        and lease.owner_id = p_owner_id
        and lease.fencing_token = p_worker_fencing_token
        and lease.expires_at > now()
        and control.version = p_control_version
        and control.mode = p_mode
        and control.armed
        and control.accept_new_intents
        and not control.kill_switch
        and not control.cancellation_pending
        and control.armed_until > now()
        and not binding.paused
        and binding.collateral_balance_pusd > 0
        and binding.allowances_ready
        and binding.readiness_checked_at > now() - interval '15 minutes'
        and binding.readiness_owner_id = p_owner_id
        and binding.readiness_fencing_token = p_worker_fencing_token
    );

  get diagnostics transitioned_count = row_count;
  return transitioned_count = 1;
end;
$$;

-- Keep the 0015 defense-in-depth trigger.  Tenant orders retain their original
-- profile/trading_wallet gate; only orders atomically marked by the personal RPC
-- use the personal binding branch.
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
  then
    if coalesce(new.response_payload ->> 'submission_scope', '') = 'personal' then
      if not exists (
        select 1
        from public.personal_runtime_bindings as binding
        join public.worker_leases as lease
          on lease.account_id = binding.account_id
        where binding.account_id = new.account_id
          and binding.collateral_balance_pusd > 0
          and not binding.paused
          and binding.allowances_ready
          and binding.readiness_checked_at > now() - interval '15 minutes'
          and binding.readiness_owner_id = lease.owner_id
          and binding.readiness_fencing_token = lease.fencing_token
          and lease.fencing_token = new.worker_fencing_token
          and lease.expires_at > now()
      ) then
        raise exception 'personal live wallet readiness gate rejected submission'
          using errcode = '23514';
      end if;
    elsif not exists (
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
    ) then
      raise exception 'live wallet readiness gate rejected submission'
        using errcode = '23514';
    end if;
  end if;
  return new;
end;
$$;

-- Personal paper state uses the existing bounded document, but replaces the
-- tenant cycle-job fence with the active single-account worker lease.
create or replace function public.load_personal_paper_account_state(
  p_account_id uuid,
  p_owner_id text,
  p_fencing_token bigint
)
returns table (state jsonb, version bigint, updated_at timestamptz)
language sql
stable
security definer
set search_path = ''
as $$
  select paper.state, paper.version, paper.updated_at
  from public.paper_account_states as paper
  where paper.account_id = p_account_id
    and length(btrim(p_owner_id)) >= 8
    and p_fencing_token > 0
    and exists (
      select 1 from public.worker_leases as lease
      where lease.account_id = p_account_id
        and lease.owner_id = p_owner_id
        and lease.fencing_token = p_fencing_token
        and lease.expires_at > now()
    );
$$;

create or replace function public.save_personal_paper_account_state(
  p_account_id uuid,
  p_owner_id text,
  p_fencing_token bigint,
  p_state jsonb
)
returns setof public.paper_account_states
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_account_id is null
     or p_owner_id is null
     or length(btrim(p_owner_id)) < 8
     or p_fencing_token is null
     or p_fencing_token <= 0
     or p_state is null
     or jsonb_typeof(p_state) <> 'object'
     or length(p_state::text) > 524288
  then
    raise exception 'invalid personal paper account state' using errcode = '22023';
  end if;
  if not exists (
    select 1 from public.worker_leases as lease
    where lease.account_id = p_account_id
      and lease.owner_id = p_owner_id
      and lease.fencing_token = p_fencing_token
      and lease.expires_at > now()
  ) then
    return;
  end if;

  return query
  insert into public.paper_account_states(account_id, state)
  values (p_account_id, p_state)
  on conflict (account_id) do update set
    state = excluded.state,
    version = public.paper_account_states.version + 1,
    updated_at = now()
  returning public.paper_account_states.*;
end;
$$;

create or replace function public.polybot_schema_version()
returns integer
language sql
stable
security definer
set search_path = ''
as $$ select 16; $$;

alter table public.personal_runtime_bindings enable row level security;
alter table public.personal_runtime_bindings force row level security;

create policy personal_runtime_bindings_owner_read
  on public.personal_runtime_bindings for select to authenticated
  using (account_id = (select auth.uid()));

revoke all on public.personal_runtime_bindings from public, anon, authenticated;
grant select on public.personal_runtime_bindings to authenticated;
grant all on public.personal_runtime_bindings to service_role;

revoke all on function public.bind_personal_runtime_wallet(
  uuid, text, bigint, text, text, integer, text
), public.record_personal_wallet_readiness(
  uuid, text, bigint, bigint, numeric, boolean
), public.set_personal_runtime_paused(
  uuid, boolean
), public.resume_personal_runtime(
  uuid, bigint
), public.arm_personal_runtime_control(
  uuid, text, bigint, text, timestamptz, bigint
), public.mark_personal_order_submitting(
  uuid, text, text, bigint, bigint, text
), public.load_personal_paper_account_state(
  uuid, text, bigint
), public.save_personal_paper_account_state(
  uuid, text, bigint, jsonb
), public.polybot_schema_version()
from public, anon, authenticated, service_role;

grant execute on function public.bind_personal_runtime_wallet(
  uuid, text, bigint, text, text, integer, text
), public.record_personal_wallet_readiness(
  uuid, text, bigint, bigint, numeric, boolean
), public.set_personal_runtime_paused(
  uuid, boolean
), public.resume_personal_runtime(
  uuid, bigint
), public.arm_personal_runtime_control(
  uuid, text, bigint, text, timestamptz, bigint
), public.mark_personal_order_submitting(
  uuid, text, text, bigint, bigint, text
), public.load_personal_paper_account_state(
  uuid, text, bigint
), public.save_personal_paper_account_state(
  uuid, text, bigint, jsonb
), public.polybot_schema_version()
to service_role;

notify pgrst, 'reload schema';

commit;
