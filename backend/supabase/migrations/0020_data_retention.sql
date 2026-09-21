begin;

-- Bounded-growth retention for the append-only history tables.
--
-- ``ai_usage_ledger``, ``equity_history`` and ``snapshots`` grow forever on a
-- long-running deployment. Nothing about the trading logic depends on rows
-- older than the retention windows below, so the worker prunes them on a daily
-- schedule through this single, service-role-only entry point.
--
-- Design notes:
--   * Deleting is bounded per call so the transaction cannot lock the database
--     for an unbounded time on a large backlog.
--   * The live, decision-critical tables (orders, fills, forecasts, positions,
--     reconciliation ledger) are deliberately NOT pruned: they are the audit
--     trail that proves what the bot did with real money.
--   * Each call returns a per-table count so the caller can log exactly what
--     was removed.

create or replace function public.prune_polybot_history(
  p_ai_usage_days integer default 90,
  p_equity_days integer default 365,
  p_snapshot_days integer default 30,
  p_batch_limit integer default 20000
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_ai_usage integer := 0;
  v_equity integer := 0;
  v_snapshots integer := 0;
begin
  if p_ai_usage_days < 7 or p_equity_days < 30 or p_snapshot_days < 1 then
    raise exception 'retention window below the supported minimum'
      using errcode = '22023';
  end if;
  if p_batch_limit < 100 or p_batch_limit > 200000 then
    raise exception 'retention batch limit out of range'
      using errcode = '22023';
  end if;

  with doomed as (
    select id from public.ai_usage_ledger
    where created_at < now() - make_interval(days => p_ai_usage_days)
    order by created_at
    limit p_batch_limit
  )
  delete from public.ai_usage_ledger as ledger
  using doomed
  where ledger.id = doomed.id;
  get diagnostics v_ai_usage = row_count;

  with doomed as (
    select id from public.equity_history
    where recorded_at < now() - make_interval(days => p_equity_days)
    order by recorded_at
    limit p_batch_limit
  )
  delete from public.equity_history as history
  using doomed
  where history.id = doomed.id;
  get diagnostics v_equity = row_count;

  with doomed as (
    select id from public.snapshots
    where captured_at < now() - make_interval(days => p_snapshot_days)
    order by captured_at
    limit p_batch_limit
  )
  delete from public.snapshots as snapshot
  using doomed
  where snapshot.id = doomed.id;
  get diagnostics v_snapshots = row_count;

  return jsonb_build_object(
    'ai_usage_ledger', v_ai_usage,
    'equity_history', v_equity,
    'snapshots', v_snapshots
  );
end;
$$;

revoke all on function public.prune_polybot_history(integer, integer, integer, integer)
  from public, anon, authenticated;
grant execute on function public.prune_polybot_history(integer, integer, integer, integer)
  to service_role;

-- 0018 revoked every privilege on the AI usage ledger from service_role and
-- only gave SELECT back, while the worker writes rows through PostgREST. The
-- insert therefore failed and the whole AI usage trail (plus the per-cycle cost
-- alerting built on it) was silently empty. Restore exactly the write path the
-- worker needs, nothing more.
grant insert, select on table public.ai_usage_ledger to service_role;
grant usage, select on sequence public.ai_usage_ledger_id_seq to service_role;

create or replace function public.polybot_schema_version()
returns integer
language sql
stable
security definer
set search_path = ''
as $$ select 20; $$;

revoke all on function public.polybot_schema_version() from public, anon, authenticated;
grant execute on function public.polybot_schema_version() to service_role;

notify pgrst, 'reload schema';

commit;
