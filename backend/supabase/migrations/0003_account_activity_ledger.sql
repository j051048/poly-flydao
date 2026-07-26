begin;

create table if not exists public.account_activities (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references auth.users(id) on delete restrict,
  activity_key text not null,
  activity_type text not null,
  condition_id text,
  amount_pusd numeric(38, 18) not null default 0,
  transaction_hash text,
  occurred_at timestamptz not null,
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint account_activities_type_allowed
    check (activity_type in ('REDEEM', 'SPLIT', 'MERGE', 'CONVERSION')),
  constraint account_activities_amount_nonnegative
    check (amount_pusd >= 0),
  unique (account_id, activity_key)
);

create index if not exists account_activities_account_occurred_idx
  on public.account_activities (account_id, occurred_at);

drop trigger if exists account_activities_set_updated_at
  on public.account_activities;
create trigger account_activities_set_updated_at
before update on public.account_activities
for each row execute function public.set_updated_at();

alter table public.account_activities enable row level security;

drop policy if exists account_activities_owner_read
  on public.account_activities;
create policy account_activities_owner_read
  on public.account_activities for select to authenticated
  using ((select auth.uid()) = account_id);

revoke all on table public.account_activities from anon, authenticated;
grant select (
  id, account_id, activity_key, activity_type, condition_id, amount_pusd,
  transaction_hash, occurred_at, created_at, updated_at
) on public.account_activities to authenticated;
grant select, insert, update, delete on table public.account_activities
  to service_role;
revoke delete on table public.account_activities from service_role;

commit;
