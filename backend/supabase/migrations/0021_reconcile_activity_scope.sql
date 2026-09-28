begin;

-- Preserve external lifecycle events and their raw payloads for audit while
-- excluding them from bot cost basis. The reconciler classifies all existing
-- rows against the complete bot condition footprint before replay, including
-- history no longer returned by the current REST cursor. Ambiguous events
-- remain in the strict bot ledger; moving a baseline cannot mask bot exposure.
alter table public.reconciliation_quarantine
  drop constraint reconciliation_quarantine_kind_allowed;
alter table public.reconciliation_quarantine
  add constraint reconciliation_quarantine_kind_allowed
  check (kind in ('trade', 'position', 'activity'));

alter table public.account_activities
  add column ledger_scope text not null default 'bot',
  add column scope_reason text;

alter table public.account_activities
  add constraint account_activities_ledger_scope_allowed
    check (ledger_scope in ('bot', 'quarantine')),
  add constraint account_activities_scope_reason_required
    check (
      (ledger_scope = 'bot' and scope_reason is null)
      or (
        ledger_scope = 'quarantine'
        and scope_reason is not null
        and length(btrim(scope_reason)) between 1 and 64
      )
    );

grant select (ledger_scope, scope_reason) on public.account_activities to authenticated;

create or replace function public.polybot_schema_version()
returns integer
language sql
stable
security definer
set search_path = ''
as $$ select 21; $$;

revoke all on function public.polybot_schema_version() from public, anon, authenticated;
grant execute on function public.polybot_schema_version() to service_role;

notify pgrst, 'reload schema';

commit;
