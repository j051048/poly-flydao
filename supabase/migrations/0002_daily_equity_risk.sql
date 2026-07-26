begin;

alter table public.account_risk_state
  add column if not exists day_start_equity_pusd numeric(38, 18),
  add column if not exists risk_day date;

update public.account_risk_state
set
  day_start_equity_pusd = coalesce(day_start_equity_pusd, latest_equity_pusd),
  risk_day = coalesce(risk_day, (now() at time zone 'utc')::date)
where day_start_equity_pusd is null or risk_day is null;

alter table public.account_risk_state
  alter column day_start_equity_pusd set not null,
  alter column risk_day set not null,
  alter column risk_day set default ((now() at time zone 'utc')::date);

alter table public.account_risk_state
  drop constraint if exists account_risk_state_equity_nonnegative;
alter table public.account_risk_state
  add constraint account_risk_state_equity_nonnegative
  check (
    peak_equity_pusd >= 0
    and latest_equity_pusd >= 0
    and day_start_equity_pusd >= 0
  );

drop function if exists public.record_equity_peak(uuid, numeric);

create or replace function public.record_equity_state(
  p_account_id uuid,
  p_equity_pusd numeric
)
returns table (
  peak_equity_pusd numeric,
  latest_equity_pusd numeric,
  day_start_equity_pusd numeric,
  risk_day date
)
language plpgsql
security invoker
set search_path = ''
as $$
declare
  utc_today date := (now() at time zone 'utc')::date;
begin
  if $2 < 0 then
    raise exception 'equity must be non-negative';
  end if;
  if $2::text in ('NaN', 'Infinity', '-Infinity') then
    raise exception 'equity must be finite';
  end if;

  return query
  insert into public.account_risk_state (
    account_id,
    peak_equity_pusd,
    latest_equity_pusd,
    day_start_equity_pusd,
    risk_day,
    updated_at
  ) values ($1, $2, $2, $2, utc_today, now())
  on conflict on constraint account_risk_state_pkey do update
    set peak_equity_pusd = greatest(
          public.account_risk_state.peak_equity_pusd,
          excluded.latest_equity_pusd
        ),
        day_start_equity_pusd = case
          when public.account_risk_state.risk_day < utc_today
            then public.account_risk_state.latest_equity_pusd
          else public.account_risk_state.day_start_equity_pusd
        end,
        risk_day = utc_today,
        latest_equity_pusd = excluded.latest_equity_pusd,
        updated_at = now()
  returning
    public.account_risk_state.peak_equity_pusd,
    public.account_risk_state.latest_equity_pusd,
    public.account_risk_state.day_start_equity_pusd,
    public.account_risk_state.risk_day;
end;
$$;

revoke all on function public.record_equity_state(uuid, numeric)
  from public, anon, authenticated;
grant execute on function public.record_equity_state(uuid, numeric)
  to service_role;

commit;
