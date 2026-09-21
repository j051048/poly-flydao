begin;

-- 0019: reconciliation quarantine and a self-service baseline.
--
-- Root cause this removes: reconciliation was a single global gate. One
-- pre-existing manual trade on a dedicated wallet meant the gate could never
-- pass, the worker was never ready, and the wallet never bound - with no
-- operator-visible reason and no in-product recovery path.
--
-- Quarantine records activity the bot provably did not create. It is never
-- counted as bot inventory, never feeds cost basis, and never enters the
-- durable positions table. Activity that *does* touch the bot's own token
-- footprint still fails closed.

create table if not exists public.reconciliation_quarantine (
  id bigint generated always as identity primary key,
  account_id uuid not null references auth.users(id) on delete cascade,
  kind text not null,
  external_key text not null,
  reason text not null,
  condition_id text,
  token_id text,
  side text,
  size numeric(38, 18),
  price numeric(20, 10),
  notional_usd numeric(38, 18),
  occurred_at timestamptz,
  detail jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint reconciliation_quarantine_kind_allowed
    check (kind in ('trade', 'position')),
  constraint reconciliation_quarantine_reason_shape
    check (length(btrim(reason)) between 1 and 64),
  constraint reconciliation_quarantine_key_shape
    check (length(btrim(external_key)) between 1 and 256),
  constraint reconciliation_quarantine_size_nonnegative
    check (size is null or size >= 0),
  constraint reconciliation_quarantine_price_range
    check (price is null or (price >= 0 and price <= 1)),
  constraint reconciliation_quarantine_notional_nonnegative
    check (notional_usd is null or notional_usd >= 0),
  unique (account_id, kind, external_key)
);

create index if not exists reconciliation_quarantine_account_created_idx
  on public.reconciliation_quarantine (account_id, created_at desc);

drop trigger if exists reconciliation_quarantine_set_updated_at
  on public.reconciliation_quarantine;
create trigger reconciliation_quarantine_set_updated_at
before update on public.reconciliation_quarantine
for each row execute function public.set_updated_at();

alter table public.reconciliation_quarantine enable row level security;
alter table public.reconciliation_quarantine force row level security;

drop policy if exists reconciliation_quarantine_owner_select
  on public.reconciliation_quarantine;
create policy reconciliation_quarantine_owner_select
  on public.reconciliation_quarantine for select to authenticated
  using ((select auth.uid()) = account_id);

revoke all on table public.reconciliation_quarantine from public, anon, authenticated;
grant select on table public.reconciliation_quarantine to authenticated;

revoke all on table public.reconciliation_quarantine from service_role;
grant select, insert, update on table public.reconciliation_quarantine to service_role;
revoke delete, truncate on table public.reconciliation_quarantine from service_role;

-- Durable ignore-before timestamp so an operator can adopt a wallet that was
-- used manually before the bot existed, without a redeploy.
alter table public.personal_runtime_bindings
  add column if not exists reconcile_baseline_at timestamptz;

-- Setting the baseline is only safe while no real-money exposure can exist:
-- nothing armed, no new intents, no cancellation in flight, and no order in a
-- potentially-live state.
create or replace function public.set_personal_reconcile_baseline(
  p_account_id uuid,
  p_baseline timestamptz
)
returns setof public.personal_runtime_bindings
language plpgsql
security definer
set search_path = ''
as $$
begin
  if p_account_id is null
     or p_baseline is null
     or p_baseline > now() + interval '1 minute'
  then
    raise exception 'invalid personal reconcile baseline' using errcode = '22023';
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended(p_account_id::text || ':personal-runtime', 0)
  );

  if exists (
    select 1 from public.runtime_controls as control
    where control.account_id = p_account_id
      and (
        control.armed
        or control.accept_new_intents
        or control.cancellation_pending
      )
  ) then
    raise exception 'refusing to move the reconcile baseline while the runtime is armed'
      using errcode = '55000';
  end if;

  if exists (
    select 1 from public.orders as order_row
    where order_row.account_id = p_account_id
      and order_row.status not in (
        'confirmed', 'simulated', 'cancelled', 'expired', 'rejected', 'failed'
      )
  ) then
    raise exception 'refusing to move the reconcile baseline with non-terminal orders'
      using errcode = '55000';
  end if;

  return query
  update public.personal_runtime_bindings as binding
  set reconcile_baseline_at = p_baseline,
      updated_at = now()
  where binding.account_id = p_account_id
  returning binding.*;
end;
$$;

revoke all on function public.set_personal_reconcile_baseline(uuid, timestamptz)
  from public, anon, authenticated;
grant execute on function public.set_personal_reconcile_baseline(uuid, timestamptz)
  to service_role;

create or replace function public.polybot_schema_version()
returns integer
language sql
stable
security definer
set search_path = ''
as $$ select 19; $$;

revoke all on function public.polybot_schema_version() from public, anon, authenticated;
grant execute on function public.polybot_schema_version() to service_role;

notify pgrst, 'reload schema';

commit;
