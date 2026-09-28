begin;

-- Preserve both market identities and the risk-approved maximum obligation.
-- Existing rows retain NULL: historical expected costs must not be relabelled
-- as a verified maximum signed commitment.
alter table public.order_intents
  add column if not exists condition_id text,
  add column if not exists max_commitment_usd numeric
    check (max_commitment_usd is null or (
      max_commitment_usd > 0
      and max_commitment_usd < 'Infinity'::numeric
      and (side <> 'BUY' or max_commitment_usd >= price * size)
    ));

create or replace function public.polybot_schema_version()
returns integer
language sql
stable
security definer
set search_path = ''
as $$ select 22; $$;

revoke all on function public.polybot_schema_version() from public, anon, authenticated;
grant execute on function public.polybot_schema_version() to service_role;
notify pgrst, 'reload schema';

commit;
