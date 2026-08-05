begin;

-- Durable per-cycle equity history for net-worth and drawdown charts.
-- The trading system only ever writes through the service-role backend; the
-- owner dashboard may read its own rows through RLS.

create table if not exists public.equity_history (
  id bigint generated always as identity primary key,
  account_id uuid not null references auth.users(id) on delete cascade,
  recorded_at timestamptz not null,
  equity_usd numeric(38, 18) not null,
  source text not null default 'cycle',
  created_at timestamptz not null default now(),
  constraint equity_history_equity_nonnegative
    check (equity_usd >= 0),
  constraint equity_history_source_nonempty
    check (length(btrim(source)) > 0)
);

create index if not exists equity_history_account_recorded_idx
  on public.equity_history (account_id, recorded_at desc);

alter table public.equity_history enable row level security;
alter table public.equity_history force row level security;

create policy equity_history_owner_select
  on public.equity_history for select to authenticated
  using ((select auth.uid()) = account_id);

revoke all on table public.equity_history from public, anon, authenticated;
grant select on table public.equity_history to authenticated;

revoke all on table public.equity_history from service_role;
grant select on table public.equity_history to service_role;

create or replace function public.polybot_schema_version()
returns integer
language sql
stable
security definer
set search_path = ''
as $$ select 17; $$;

revoke all on function public.polybot_schema_version() from public, anon, authenticated;
grant execute on function public.polybot_schema_version() to service_role;

notify pgrst, 'reload schema';

commit;
