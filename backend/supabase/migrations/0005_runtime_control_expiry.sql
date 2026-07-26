begin;

-- Arming is a versioned transition. An already-expired arm must first pass
-- through expire -> cancel-all -> acknowledgement; it cannot be renewed over
-- the top of exchange orders that survived the previous authorization window.
create or replace function public.arm_runtime_control(
  p_account_id uuid,
  p_mode text,
  p_armed_until timestamptz,
  p_expected_version bigint
)
returns setof public.runtime_controls
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if p_mode not in ('canary', 'live') then
    raise exception 'runtime control can only be armed in canary or live mode'
      using errcode = '22023';
  end if;
  if p_expected_version < 1 then
    raise exception 'expected runtime control version must be positive'
      using errcode = '22023';
  end if;
  if p_armed_until <= now() or p_armed_until > now() + interval '15 minutes' then
    raise exception 'runtime arm expiry must be within the next 15 minutes'
      using errcode = '22023';
  end if;

  insert into public.runtime_controls (account_id)
  values (p_account_id)
  on conflict (account_id) do nothing;

  return query
  update public.runtime_controls as control
  set
    mode = p_mode,
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

-- The database clock is authoritative. Only one worker can win this version
-- CAS; cancel-all itself remains intentionally idempotent across replicas.
create or replace function public.expire_runtime_control(
  p_account_id uuid,
  p_mode text,
  p_expected_version bigint
)
returns setof public.runtime_controls
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if p_mode not in ('canary', 'live') then
    raise exception 'runtime control can only expire in canary or live mode'
      using errcode = '22023';
  end if;

  return query
  update public.runtime_controls as control
  set
    kill_switch = true,
    accept_new_intents = false,
    armed = false,
    armed_until = null,
    cancellation_pending = true,
    version = control.version + 1
  where control.account_id = p_account_id
    and control.mode = p_mode
    and control.version = p_expected_version
    and control.armed
    and control.armed_until <= now()
  returning control.*;
end;
$$;

revoke all on function public.expire_runtime_control(uuid, text, bigint)
  from public, anon, authenticated;
grant execute on function public.expire_runtime_control(uuid, text, bigint)
  to service_role;

commit;
