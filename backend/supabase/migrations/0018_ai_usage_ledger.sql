begin;

-- Per-forecast AI usage ledger: tokens, latency, provider response id, and
-- estimated cost. The dashboard needs this to keep model spend visible and
-- bounded; the worker writes it best-effort and never blocks trading on it.

create table if not exists public.ai_usage_ledger (
  id bigint generated always as identity primary key,
  account_id uuid not null references auth.users(id) on delete cascade,
  market_id text,
  provider text not null,
  model text not null,
  request_id text,
  input_tokens integer not null default 0,
  output_tokens integer not null default 0,
  total_tokens integer not null default 0,
  latency_ms integer,
  cost_usd numeric(20, 10),
  created_at timestamptz not null default now(),
  constraint ai_usage_ledger_tokens_nonnegative
    check (
      input_tokens >= 0
      and output_tokens >= 0
      and total_tokens >= 0
      and (latency_ms is null or latency_ms >= 0)
      and (cost_usd is null or cost_usd >= 0)
    ),
  constraint ai_usage_ledger_model_nonempty
    check (length(btrim(provider)) > 0 and length(btrim(model)) > 0)
);

create index if not exists ai_usage_ledger_account_created_idx
  on public.ai_usage_ledger (account_id, created_at desc);
create index if not exists ai_usage_ledger_account_market_idx
  on public.ai_usage_ledger (account_id, market_id, created_at desc)
  where market_id is not null;

alter table public.ai_usage_ledger enable row level security;
alter table public.ai_usage_ledger force row level security;

create policy ai_usage_ledger_owner_select
  on public.ai_usage_ledger for select to authenticated
  using ((select auth.uid()) = account_id);

revoke all on table public.ai_usage_ledger from public, anon, authenticated;
grant select on table public.ai_usage_ledger to authenticated;

revoke all on table public.ai_usage_ledger from service_role;
grant select on table public.ai_usage_ledger to service_role;

create or replace function public.polybot_schema_version()
returns integer
language sql
stable
security definer
set search_path = ''
as $$ select 18; $$;

revoke all on function public.polybot_schema_version() from public, anon, authenticated;
grant execute on function public.polybot_schema_version() to service_role;

notify pgrst, 'reload schema';

commit;
