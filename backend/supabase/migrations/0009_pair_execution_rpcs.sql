begin;

-- P2 remains research-only. These service-role RPCs make each state-machine
-- transition a single PostgreSQL transaction; no browser/authenticated role
-- can mutate the execution tables directly.

alter table public.order_groups force row level security;
alter table public.order_legs force row level security;
alter table public.pair_inventory force row level security;
alter table public.pair_inventory_events force row level security;

create or replace function public.reject_pair_inventory_event_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  raise exception 'pair_inventory_events is append-only' using errcode = '42501';
end;
$$;

revoke all on function public.reject_pair_inventory_event_mutation()
  from public, anon, authenticated, service_role;

drop trigger if exists pair_inventory_events_append_only
  on public.pair_inventory_events;
create trigger pair_inventory_events_append_only
before update or delete on public.pair_inventory_events
for each row execute function public.reject_pair_inventory_event_mutation();

create or replace function public.persist_pair_order_plan(
  p_account_id uuid,
  p_group jsonb,
  p_legs jsonb
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_leg jsonb;
  v_count integer;
  v_distinct_count integer;
begin
  if jsonb_typeof(p_group) <> 'object'
     or jsonb_typeof(p_legs) <> 'array' then
    raise exception 'pair plan payload must contain an object and an array'
      using errcode = '22023';
  end if;
  if p_group ->> 'account_id' <> p_account_id::text then
    raise exception 'pair plan account scope mismatch' using errcode = '42501';
  end if;
  if coalesce((p_group ->> 'research_only')::boolean, false) is not true
     or coalesce((p_group ->> 'execution_enabled')::boolean, false) is true then
    raise exception 'P2 persistence is restricted to disabled research execution'
      using errcode = '42501';
  end if;
  if jsonb_array_length(p_legs) <> 2 then
    raise exception 'an initial pair plan requires exactly two legs'
      using errcode = '23514';
  end if;
  select count(distinct (value ->> 'id')::uuid)
    into v_distinct_count
    from jsonb_array_elements(p_legs);
  if v_distinct_count <> jsonb_array_length(p_legs) then
    raise exception 'pair plan contains duplicate leg ids' using errcode = '23514';
  end if;

  insert into public.order_groups (
    id,
    account_id,
    trading_wallet_id,
    market_id,
    strategy,
    target_pair_size,
    paired_size,
    directional_yes_size,
    directional_no_size,
    expected_net_edge_pusd,
    leg_deadline_at,
    status,
    research_only,
    execution_enabled,
    version,
    created_at,
    updated_at
  )
  values (
    (p_group ->> 'id')::uuid,
    p_account_id,
    (p_group ->> 'trading_wallet_id')::uuid,
    (p_group ->> 'market_id')::uuid,
    p_group ->> 'strategy',
    (p_group ->> 'target_pair_size')::numeric,
    (p_group ->> 'paired_size')::numeric,
    (p_group ->> 'directional_yes_size')::numeric,
    (p_group ->> 'directional_no_size')::numeric,
    (p_group ->> 'expected_net_edge_pusd')::numeric,
    (p_group ->> 'leg_deadline_at')::timestamptz,
    p_group ->> 'status',
    true,
    false,
    (p_group ->> 'version')::bigint,
    (p_group ->> 'created_at')::timestamptz,
    (p_group ->> 'updated_at')::timestamptz
  )
  on conflict (id) do nothing;
  get diagnostics v_count = row_count;
  if v_count = 0 then
    return false;
  end if;

  for v_leg in select value from jsonb_array_elements(p_legs)
  loop
    if v_leg ->> 'group_id' <> p_group ->> 'id' then
      raise exception 'pair leg belongs to a different group' using errcode = '23514';
    end if;
    insert into public.order_legs (
      id,
      group_id,
      account_id,
      outcome,
      token_id,
      side,
      purpose,
      liquidity_role,
      post_only,
      price,
      size,
      filled_size,
      average_fill_price,
      fee_paid_pusd,
      status,
      clob_order_id,
      deadline_at,
      version,
      created_at,
      updated_at
    )
    values (
      (v_leg ->> 'id')::uuid,
      (v_leg ->> 'group_id')::uuid,
      p_account_id,
      v_leg ->> 'outcome',
      v_leg ->> 'token_id',
      v_leg ->> 'side',
      v_leg ->> 'purpose',
      v_leg ->> 'liquidity_role',
      (v_leg ->> 'post_only')::boolean,
      (v_leg ->> 'price')::numeric,
      (v_leg ->> 'size')::numeric,
      (v_leg ->> 'filled_size')::numeric,
      (v_leg ->> 'average_fill_price')::numeric,
      (v_leg ->> 'fee_paid_pusd')::numeric,
      v_leg ->> 'status',
      nullif(v_leg ->> 'clob_order_id', ''),
      (v_leg ->> 'deadline_at')::timestamptz,
      (v_leg ->> 'version')::bigint,
      (v_leg ->> 'created_at')::timestamptz,
      (v_leg ->> 'updated_at')::timestamptz
    );
  end loop;

  return true;
exception
  when unique_violation then
    -- The function-level exception block rolls back the entire attempted plan.
    return false;
end;
$$;

create or replace function public.commit_pair_group_transition(
  p_account_id uuid,
  p_expected_group_version bigint,
  p_group jsonb,
  p_legs jsonb,
  p_reason text
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_before public.order_groups%rowtype;
  v_leg jsonb;
  v_count integer;
  v_existing_count integer;
  v_payload_count integer;
  v_yes_filled numeric;
  v_no_filled numeric;
  v_leg_exists boolean;
  v_new_leg_count integer;
begin
  if p_expected_group_version < 1
     or jsonb_typeof(p_group) <> 'object'
     or jsonb_typeof(p_legs) <> 'array'
     or length(btrim(coalesce(p_reason, ''))) = 0 then
    raise exception 'invalid pair transition payload' using errcode = '22023';
  end if;
  if p_group ->> 'account_id' <> p_account_id::text
     or (p_group ->> 'version')::bigint <> p_expected_group_version + 1 then
    raise exception 'pair transition scope or version is invalid'
      using errcode = '42501';
  end if;

  select *
    into v_before
    from public.order_groups
    where id = (p_group ->> 'id')::uuid
      and account_id = p_account_id
      and version = p_expected_group_version
      and research_only
      and not execution_enabled
    for update;
  if not found then
    return false;
  end if;

  select count(*), count(distinct (value ->> 'id')::uuid)
    into v_payload_count, v_count
    from jsonb_array_elements(p_legs);
  if v_payload_count < 2 or v_count <> v_payload_count then
    raise exception 'pair transition has missing or duplicate legs'
      using errcode = '23514';
  end if;
  select count(*)
    into v_existing_count
    from public.order_legs
    where account_id = p_account_id
      and group_id = v_before.id;
  if exists (
    select 1
    from public.order_legs as durable_leg
    where durable_leg.account_id = p_account_id
      and durable_leg.group_id = v_before.id
      and not exists (
        select 1
        from jsonb_array_elements(p_legs) as payload_leg(value)
        where (payload_leg.value ->> 'id')::uuid = durable_leg.id
      )
  ) then
    raise exception 'pair transition omitted a durable leg'
      using errcode = '23514';
  end if;
  select count(*)
    into v_new_leg_count
    from jsonb_array_elements(p_legs) as payload_leg(value)
    where not exists (
      select 1
      from public.order_legs as durable_leg
      where durable_leg.account_id = p_account_id
        and durable_leg.group_id = v_before.id
        and durable_leg.id = (payload_leg.value ->> 'id')::uuid
    );
  if v_payload_count < v_existing_count
     or v_payload_count > v_existing_count + 1
     or v_new_leg_count > 1 then
    raise exception 'pair transition may append at most one hedge and remove no legs'
      using errcode = '23514';
  end if;
  select
    coalesce(sum((value ->> 'filled_size')::numeric)
      filter (where value ->> 'side' = 'BUY'
        and value ->> 'outcome' = 'YES'), 0),
    coalesce(sum((value ->> 'filled_size')::numeric)
      filter (where value ->> 'side' = 'BUY'
        and value ->> 'outcome' = 'NO'), 0)
  into v_yes_filled, v_no_filled
  from jsonb_array_elements(p_legs);
  if (p_group ->> 'paired_size')::numeric
       <> least(least(v_yes_filled, v_no_filled), v_before.target_pair_size)
     or (p_group ->> 'directional_yes_size')::numeric
       <> greatest(v_yes_filled - v_no_filled, 0)
     or (p_group ->> 'directional_no_size')::numeric
       <> greatest(v_no_filled - v_yes_filled, 0) then
    raise exception 'pair transition group inventory does not match its legs'
      using errcode = '23514';
  end if;

  update public.order_groups
    set paired_size = (p_group ->> 'paired_size')::numeric,
        directional_yes_size = (p_group ->> 'directional_yes_size')::numeric,
        directional_no_size = (p_group ->> 'directional_no_size')::numeric,
        status = p_group ->> 'status',
        version = (p_group ->> 'version')::bigint,
        updated_at = (p_group ->> 'updated_at')::timestamptz
    where id = v_before.id
      and account_id = p_account_id
      and version = p_expected_group_version;
  get diagnostics v_count = row_count;
  if v_count <> 1 then
    return false;
  end if;

  for v_leg in select value from jsonb_array_elements(p_legs)
  loop
    if v_leg ->> 'group_id' <> v_before.id::text then
      raise exception 'pair transition contains a foreign leg'
        using errcode = '23514';
    end if;
    select exists (
      select 1
      from public.order_legs
      where id = (v_leg ->> 'id')::uuid
        and account_id = p_account_id
        and group_id = v_before.id
    ) into v_leg_exists;
    if not v_leg_exists and (
      v_leg ->> 'purpose' <> 'imbalance_hedge'
      or (v_leg ->> 'filled_size')::numeric <> 0
      or (v_leg ->> 'average_fill_price') is not null
      or (v_leg ->> 'fee_paid_pusd')::numeric <> 0
      or v_leg ->> 'status' <> 'planned'
      or (v_leg ->> 'version')::bigint <> 1
    ) then
      raise exception 'only an unfilled planned hedge leg may be appended'
        using errcode = '23514';
    end if;
    insert into public.order_legs as durable_leg (
      id,
      group_id,
      account_id,
      outcome,
      token_id,
      side,
      purpose,
      liquidity_role,
      post_only,
      price,
      size,
      filled_size,
      average_fill_price,
      fee_paid_pusd,
      status,
      clob_order_id,
      deadline_at,
      version,
      created_at,
      updated_at
    )
    values (
      (v_leg ->> 'id')::uuid,
      v_before.id,
      p_account_id,
      v_leg ->> 'outcome',
      v_leg ->> 'token_id',
      v_leg ->> 'side',
      v_leg ->> 'purpose',
      v_leg ->> 'liquidity_role',
      (v_leg ->> 'post_only')::boolean,
      (v_leg ->> 'price')::numeric,
      (v_leg ->> 'size')::numeric,
      (v_leg ->> 'filled_size')::numeric,
      (v_leg ->> 'average_fill_price')::numeric,
      (v_leg ->> 'fee_paid_pusd')::numeric,
      v_leg ->> 'status',
      nullif(v_leg ->> 'clob_order_id', ''),
      (v_leg ->> 'deadline_at')::timestamptz,
      (v_leg ->> 'version')::bigint,
      (v_leg ->> 'created_at')::timestamptz,
      (v_leg ->> 'updated_at')::timestamptz
    )
    on conflict (id) do update
      set status = excluded.status,
          clob_order_id = excluded.clob_order_id,
          deadline_at = excluded.deadline_at,
          version = excluded.version,
          updated_at = excluded.updated_at
      where durable_leg.account_id = p_account_id
        and durable_leg.group_id = v_before.id
        and excluded.outcome = durable_leg.outcome
        and excluded.token_id = durable_leg.token_id
        and excluded.side = durable_leg.side
        and excluded.purpose = durable_leg.purpose
        and excluded.liquidity_role = durable_leg.liquidity_role
        and excluded.post_only = durable_leg.post_only
        and excluded.price = durable_leg.price
        and excluded.size = durable_leg.size
        and excluded.filled_size = durable_leg.filled_size
        and excluded.average_fill_price is not distinct from
          durable_leg.average_fill_price
        and excluded.fee_paid_pusd = durable_leg.fee_paid_pusd
        and excluded.created_at = durable_leg.created_at
        and (
          excluded.version = durable_leg.version + 1
          or (
            excluded.version = durable_leg.version
            and excluded.status = durable_leg.status
            and excluded.clob_order_id is not distinct from
              durable_leg.clob_order_id
            and excluded.deadline_at = durable_leg.deadline_at
          )
        );
    get diagnostics v_count = row_count;
    if v_count <> 1 then
      raise exception 'pair leg scope or version conflict' using errcode = '40001';
    end if;
  end loop;

  select count(*)
    into v_count
    from public.order_legs
    where account_id = p_account_id
      and group_id = v_before.id;
  if v_count <> v_payload_count then
    raise exception 'pair transition did not preserve the complete leg set'
      using errcode = '23514';
  end if;

  insert into public.audit_events (
    account_id,
    actor_type,
    actor_id,
    action,
    entity_type,
    entity_id,
    before_state,
    after_state,
    metadata
  )
  select
    p_account_id,
    'system',
    null,
    'pair_group_transition',
    'order_group',
    v_before.id::text,
    to_jsonb(v_before),
    to_jsonb(g),
    jsonb_build_object(
      'reason', left(p_reason, 256),
      'database_role', current_user::text
    )
  from public.order_groups as g
  where g.id = v_before.id
    and g.account_id = p_account_id;

  return true;
end;
$$;

create or replace function public.commit_pair_fill(
  p_account_id uuid,
  p_expected_group_version bigint,
  p_fill jsonb,
  p_group jsonb,
  p_legs jsonb
)
returns text
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_group public.order_groups%rowtype;
  v_leg_before public.order_legs%rowtype;
  v_leg_durable public.order_legs%rowtype;
  v_existing_event public.pair_inventory_events%rowtype;
  v_leg jsonb;
  v_target_leg jsonb;
  v_fill_id text;
  v_fill_size numeric;
  v_fill_price numeric;
  v_fill_fee numeric;
  v_matched_at timestamptz;
  v_fill_outcome text;
  v_fill_side text;
  v_fill_liquidity_role text;
  v_count integer;
  v_payload_count integer;
  v_distinct_count integer;
  v_yes_size numeric;
  v_no_size numeric;
  v_yes_cost numeric;
  v_no_cost numeric;
  v_paired_size numeric;
  v_paired_cost numeric;
  v_directional_yes numeric;
  v_directional_no numeric;
  v_directional_cost numeric;
  v_realized numeric;
  v_average_cost numeric;
  v_released_basis numeric;
  v_yes_filled numeric;
  v_no_filled numeric;
  v_expected_group_status text;
  v_expected_leg_status text;
  v_nonterminal_hedges integer;
begin
  if p_expected_group_version < 1
     or jsonb_typeof(p_fill) <> 'object'
     or jsonb_typeof(p_group) <> 'object'
     or jsonb_typeof(p_legs) <> 'array'
     or p_group ->> 'account_id' <> p_account_id::text
     or p_fill ->> 'group_id' <> p_group ->> 'id' then
    raise exception 'invalid pair fill payload' using errcode = '22023';
  end if;
  v_fill_id := btrim(coalesce(p_fill ->> 'clob_trade_id', ''));
  v_fill_size := (p_fill ->> 'size')::numeric;
  v_fill_price := (p_fill ->> 'price')::numeric;
  v_fill_fee := (p_fill ->> 'fee_paid_pusd')::numeric;
  v_matched_at := (p_fill ->> 'matched_at')::timestamptz;
  v_fill_outcome := p_fill ->> 'outcome';
  v_fill_side := p_fill ->> 'side';
  v_fill_liquidity_role := p_fill ->> 'liquidity_role';
  if length(v_fill_id) = 0
     or v_fill_size <= 0
     or v_fill_price <= 0
     or v_fill_price >= 1
     or v_fill_fee < 0
     or v_fill_outcome not in ('YES', 'NO')
     or v_fill_side not in ('BUY', 'SELL')
     or v_fill_liquidity_role not in ('maker', 'taker')
     or (p_group ->> 'version')::bigint <> p_expected_group_version + 1 then
    raise exception 'pair fill economics or version are invalid'
      using errcode = '23514';
  end if;

  select *
    into v_existing_event
    from public.pair_inventory_events
    where account_id = p_account_id
      and clob_trade_id = v_fill_id
    limit 1;
  if found then
    if v_existing_event.order_group_id is distinct from
         (p_fill ->> 'group_id')::uuid
       or v_existing_event.order_leg_id is distinct from
         (p_fill ->> 'leg_id')::uuid
       or v_existing_event.price is distinct from v_fill_price
       or v_existing_event.size is distinct from v_fill_size
       or v_existing_event.fee_paid_pusd is distinct from v_fill_fee
       or v_existing_event.matched_at is distinct from v_matched_at
       or v_existing_event.outcome is distinct from v_fill_outcome
       or v_existing_event.side is distinct from v_fill_side
       or v_existing_event.liquidity_role is distinct from
         v_fill_liquidity_role then
      raise exception 'clob trade id is bound to different fill economics'
        using errcode = '23505';
    end if;
    return 'duplicate';
  end if;

  select *
    into v_group
    from public.order_groups
    where id = (p_fill ->> 'group_id')::uuid
      and account_id = p_account_id
      and version = p_expected_group_version
      and research_only
      and not execution_enabled
    for update;
  if not found then
    select *
      into v_existing_event
      from public.pair_inventory_events
      where account_id = p_account_id
        and clob_trade_id = v_fill_id
      limit 1;
    if found then
      if v_existing_event.order_group_id is distinct from
           (p_fill ->> 'group_id')::uuid
         or v_existing_event.order_leg_id is distinct from
           (p_fill ->> 'leg_id')::uuid
         or v_existing_event.price is distinct from v_fill_price
         or v_existing_event.size is distinct from v_fill_size
         or v_existing_event.fee_paid_pusd is distinct from v_fill_fee
         or v_existing_event.matched_at is distinct from v_matched_at
         or v_existing_event.outcome is distinct from v_fill_outcome
         or v_existing_event.side is distinct from v_fill_side
         or v_existing_event.liquidity_role is distinct from
           v_fill_liquidity_role then
        raise exception 'clob trade id is bound to different fill economics'
          using errcode = '23505';
      end if;
      return 'duplicate';
    end if;
    return 'conflict';
  end if;

  select *
    into v_leg_before
    from public.order_legs
    where id = (p_fill ->> 'leg_id')::uuid
      and group_id = v_group.id
      and account_id = p_account_id
    for update;
  if not found then
    raise exception 'pair fill refers to an unknown tenant leg'
      using errcode = '23503';
  end if;
  if v_fill_outcome <> v_leg_before.outcome
     or v_fill_side <> v_leg_before.side
     or v_fill_liquidity_role <> v_leg_before.liquidity_role then
    raise exception 'pair fill identity differs from its durable leg'
      using errcode = '23514';
  end if;
  if v_leg_before.filled_size + v_fill_size > v_leg_before.size then
    raise exception 'pair fill exceeds remaining leg size' using errcode = '23514';
  end if;

  select value
    into v_leg
    from jsonb_array_elements(p_legs)
    where (value ->> 'id')::uuid = v_leg_before.id;
  if v_leg is null
     or (v_leg ->> 'group_id')::uuid <> v_group.id
     or (v_leg ->> 'filled_size')::numeric
        <> v_leg_before.filled_size + v_fill_size
     or (v_leg ->> 'fee_paid_pusd')::numeric
        <> v_leg_before.fee_paid_pusd + v_fill_fee then
    raise exception 'pair fill state does not match the durable leg delta'
      using errcode = '23514';
  end if;
  if (v_leg ->> 'average_fill_price')::numeric
     <> (
       coalesce(v_leg_before.average_fill_price, 0) * v_leg_before.filled_size
       + v_fill_price * v_fill_size
     ) / (v_leg_before.filled_size + v_fill_size) then
    raise exception 'pair fill average price is inconsistent'
      using errcode = '23514';
  end if;
  if (v_leg ->> 'version')::bigint <> v_leg_before.version + 1
     or v_leg ->> 'outcome' <> v_leg_before.outcome
     or v_leg ->> 'token_id' <> v_leg_before.token_id
     or v_leg ->> 'side' <> v_leg_before.side
     or v_leg ->> 'purpose' <> v_leg_before.purpose
     or v_leg ->> 'liquidity_role' <> v_leg_before.liquidity_role
     or (v_leg ->> 'post_only')::boolean <> v_leg_before.post_only
     or (v_leg ->> 'price')::numeric <> v_leg_before.price
     or (v_leg ->> 'size')::numeric <> v_leg_before.size
     or nullif(v_leg ->> 'clob_order_id', '') is distinct from
       v_leg_before.clob_order_id
     or (v_leg ->> 'deadline_at')::timestamptz <> v_leg_before.deadline_at
     or (v_leg ->> 'created_at')::timestamptz <> v_leg_before.created_at then
    raise exception 'pair fill attempted to mutate immutable leg identity'
      using errcode = '23514';
  end if;
  v_expected_leg_status := case
    when v_leg_before.filled_size + v_fill_size = v_leg_before.size
      then 'filled'
    when v_leg_before.status = 'cancelled'
      then 'cancelled'
    when v_leg_before.status = 'cancel_pending'
      then 'cancel_pending'
    else 'partially_filled'
  end;
  if v_leg ->> 'status' <> v_expected_leg_status then
    raise exception 'pair fill leg status is inconsistent with its durable state'
      using errcode = '23514';
  end if;
  v_target_leg := v_leg;

  select count(*), count(distinct (value ->> 'id')::uuid)
    into v_payload_count, v_distinct_count
    from jsonb_array_elements(p_legs);
  select count(*)
    into v_count
    from public.order_legs
    where account_id = p_account_id
      and group_id = v_group.id;
  if v_payload_count <> v_count
     or v_distinct_count <> v_payload_count then
    raise exception 'pair fill must carry the complete unique leg set'
      using errcode = '23514';
  end if;
  if exists (
    select 1
    from public.order_legs as durable_leg
    where durable_leg.account_id = p_account_id
      and durable_leg.group_id = v_group.id
      and not exists (
        select 1
        from jsonb_array_elements(p_legs) as payload_leg(value)
        where (payload_leg.value ->> 'id')::uuid = durable_leg.id
      )
  ) or exists (
    select 1
    from jsonb_array_elements(p_legs) as payload_leg(value)
    where not exists (
      select 1
      from public.order_legs as durable_leg
      where durable_leg.account_id = p_account_id
        and durable_leg.group_id = v_group.id
        and durable_leg.id = (payload_leg.value ->> 'id')::uuid
    )
  ) then
    raise exception 'pair fill leg UUID set differs from durable state'
      using errcode = '23514';
  end if;
  for v_leg in select value from jsonb_array_elements(p_legs)
  loop
    if (v_leg ->> 'id')::uuid = v_leg_before.id then
      continue;
    end if;
    select *
      into v_leg_durable
      from public.order_legs
      where id = (v_leg ->> 'id')::uuid
        and account_id = p_account_id
        and group_id = v_group.id;
    if not found
       or v_leg ->> 'group_id' <> v_group.id::text
       or v_leg ->> 'outcome' is distinct from v_leg_durable.outcome
       or v_leg ->> 'token_id' is distinct from v_leg_durable.token_id
       or v_leg ->> 'side' is distinct from v_leg_durable.side
       or v_leg ->> 'purpose' is distinct from v_leg_durable.purpose
       or v_leg ->> 'liquidity_role' is distinct from
         v_leg_durable.liquidity_role
       or (v_leg ->> 'post_only')::boolean is distinct from
         v_leg_durable.post_only
       or (v_leg ->> 'price')::numeric is distinct from v_leg_durable.price
       or (v_leg ->> 'size')::numeric is distinct from v_leg_durable.size
       or (v_leg ->> 'filled_size')::numeric is distinct from
         v_leg_durable.filled_size
       or (v_leg ->> 'average_fill_price')::numeric is distinct from
         v_leg_durable.average_fill_price
       or (v_leg ->> 'fee_paid_pusd')::numeric is distinct from
         v_leg_durable.fee_paid_pusd
       or v_leg ->> 'status' is distinct from v_leg_durable.status
       or nullif(v_leg ->> 'clob_order_id', '') is distinct from
         v_leg_durable.clob_order_id
       or (v_leg ->> 'deadline_at')::timestamptz is distinct from
         v_leg_durable.deadline_at
       or (v_leg ->> 'version')::bigint is distinct from v_leg_durable.version
       or (v_leg ->> 'created_at')::timestamptz is distinct from
         v_leg_durable.created_at
       or (v_leg ->> 'updated_at')::timestamptz is distinct from
         v_leg_durable.updated_at then
      raise exception 'pair fill attempted to mutate a non-target leg'
        using errcode = '23514';
    end if;
  end loop;
  select
    coalesce(sum((value ->> 'filled_size')::numeric)
      filter (where value ->> 'side' = 'BUY'
        and value ->> 'outcome' = 'YES'), 0),
    coalesce(sum((value ->> 'filled_size')::numeric)
      filter (where value ->> 'side' = 'BUY'
        and value ->> 'outcome' = 'NO'), 0)
  into v_yes_filled, v_no_filled
  from jsonb_array_elements(p_legs);
  if (p_group ->> 'paired_size')::numeric
       <> least(least(v_yes_filled, v_no_filled), v_group.target_pair_size)
     or (p_group ->> 'directional_yes_size')::numeric
       <> greatest(v_yes_filled - v_no_filled, 0)
     or (p_group ->> 'directional_no_size')::numeric
       <> greatest(v_no_filled - v_yes_filled, 0) then
    raise exception 'pair fill group inventory does not match its legs'
      using errcode = '23514';
  end if;
  select count(*)
    into v_nonterminal_hedges
    from jsonb_array_elements(p_legs)
    where value ->> 'purpose' = 'imbalance_hedge'
      and value ->> 'status' not in (
        'filled', 'cancelled', 'rejected', 'failed'
      );
  v_expected_group_status := case
    when v_group.status in ('frozen', 'failed')
      then v_group.status
    when v_group.status = 'cancelling'
      then 'cancelling'
    when v_group.status = 'hedging' and v_nonterminal_hedges > 0
      then 'hedging'
    when v_group.status = 'hedging'
      then case
        when least(v_yes_filled, v_no_filled) > 0
          and v_yes_filled = v_no_filled then 'paired'
        else 'frozen'
      end
    when v_group.status in ('cancelled', 'paired')
      and v_yes_filled <> v_no_filled
      then 'frozen'
    when least(v_yes_filled, v_no_filled) >= v_group.target_pair_size
      and v_yes_filled = v_no_filled
      then 'paired'
    when v_yes_filled <> v_no_filled
      then 'imbalanced'
    else 'working'
  end;
  if p_group ->> 'status' <> v_expected_group_status then
    raise exception 'pair fill group status is inconsistent with its durable phase'
      using errcode = '23514';
  end if;

  insert into public.pair_inventory_events (
    account_id,
    trading_wallet_id,
    market_id,
    order_group_id,
    order_leg_id,
    clob_trade_id,
    outcome,
    side,
    liquidity_role,
    price,
    size,
    fee_paid_pusd,
    matched_at
  )
  values (
    p_account_id,
    v_group.trading_wallet_id,
    v_group.market_id,
    v_group.id,
    v_leg_before.id,
    v_fill_id,
    v_leg_before.outcome,
    v_leg_before.side,
    v_leg_before.liquidity_role,
    v_fill_price,
    v_fill_size,
    v_fill_fee,
    v_matched_at
  )
  on conflict (account_id, clob_trade_id) do nothing;
  get diagnostics v_count = row_count;
  if v_count = 0 then
    select *
      into v_existing_event
      from public.pair_inventory_events
      where account_id = p_account_id
        and clob_trade_id = v_fill_id
      limit 1;
    if not found
       or v_existing_event.order_group_id is distinct from v_group.id
       or v_existing_event.order_leg_id is distinct from v_leg_before.id
       or v_existing_event.price is distinct from v_fill_price
       or v_existing_event.size is distinct from v_fill_size
       or v_existing_event.fee_paid_pusd is distinct from v_fill_fee
       or v_existing_event.matched_at is distinct from v_matched_at
       or v_existing_event.outcome is distinct from v_fill_outcome
       or v_existing_event.side is distinct from v_fill_side
       or v_existing_event.liquidity_role is distinct from
         v_fill_liquidity_role then
      raise exception 'clob trade id collision could not be proven idempotent'
        using errcode = '23505';
    end if;
    return 'duplicate';
  end if;

  update public.order_groups
    set paired_size = (p_group ->> 'paired_size')::numeric,
        directional_yes_size = (p_group ->> 'directional_yes_size')::numeric,
        directional_no_size = (p_group ->> 'directional_no_size')::numeric,
        status = v_expected_group_status,
        version = p_expected_group_version + 1,
        updated_at = (p_group ->> 'updated_at')::timestamptz
    where id = v_group.id
      and account_id = p_account_id
      and version = p_expected_group_version;
  get diagnostics v_count = row_count;
  if v_count <> 1 then
    raise exception 'pair fill lost its group CAS' using errcode = '40001';
  end if;

  update public.order_legs
    set filled_size = (v_target_leg ->> 'filled_size')::numeric,
        average_fill_price = (v_target_leg ->> 'average_fill_price')::numeric,
        fee_paid_pusd = (v_target_leg ->> 'fee_paid_pusd')::numeric,
        status = v_expected_leg_status,
        version = v_leg_before.version + 1,
        updated_at = (v_target_leg ->> 'updated_at')::timestamptz
    where id = v_leg_before.id
      and account_id = p_account_id
      and group_id = v_group.id
      and version = v_leg_before.version;
  get diagnostics v_count = row_count;
  if v_count <> 1 then
    raise exception 'pair fill target leg lost its exact CAS'
      using errcode = '40001';
  end if;

  insert into public.pair_inventory (
    account_id,
    trading_wallet_id,
    market_id
  )
  values (
    p_account_id,
    v_group.trading_wallet_id,
    v_group.market_id
  )
  on conflict (account_id, trading_wallet_id, market_id) do nothing;

  select
    yes_size,
    no_size,
    yes_cost_pusd,
    no_cost_pusd,
    realized_pnl_pusd
  into
    v_yes_size,
    v_no_size,
    v_yes_cost,
    v_no_cost,
    v_realized
  from public.pair_inventory
  where account_id = p_account_id
    and trading_wallet_id = v_group.trading_wallet_id
    and market_id = v_group.market_id
  for update;

  if v_leg_before.side = 'BUY' then
    if v_leg_before.outcome = 'YES' then
      v_yes_size := v_yes_size + v_fill_size;
      v_yes_cost := v_yes_cost + v_fill_size * v_fill_price + v_fill_fee;
    else
      v_no_size := v_no_size + v_fill_size;
      v_no_cost := v_no_cost + v_fill_size * v_fill_price + v_fill_fee;
    end if;
  elsif v_leg_before.outcome = 'YES' then
    if v_fill_size > v_yes_size then
      raise exception 'YES sell fill exceeds pair inventory' using errcode = '23514';
    end if;
    v_average_cost := case when v_yes_size > 0
      then v_yes_cost / v_yes_size else 0 end;
    v_released_basis := v_average_cost * v_fill_size;
    v_yes_size := v_yes_size - v_fill_size;
    v_yes_cost := greatest(0, v_yes_cost - v_released_basis);
    v_realized := v_realized
      + v_fill_size * v_fill_price - v_fill_fee - v_released_basis;
  else
    if v_fill_size > v_no_size then
      raise exception 'NO sell fill exceeds pair inventory' using errcode = '23514';
    end if;
    v_average_cost := case when v_no_size > 0
      then v_no_cost / v_no_size else 0 end;
    v_released_basis := v_average_cost * v_fill_size;
    v_no_size := v_no_size - v_fill_size;
    v_no_cost := greatest(0, v_no_cost - v_released_basis);
    v_realized := v_realized
      + v_fill_size * v_fill_price - v_fill_fee - v_released_basis;
  end if;

  v_paired_size := least(v_yes_size, v_no_size);
  v_paired_cost := v_paired_size * (
    case when v_yes_size > 0 then v_yes_cost / v_yes_size else 0 end
    + case when v_no_size > 0 then v_no_cost / v_no_size else 0 end
  );
  v_directional_yes := greatest(v_yes_size - v_no_size, 0);
  v_directional_no := greatest(v_no_size - v_yes_size, 0);
  v_directional_cost :=
    v_directional_yes
      * case when v_yes_size > 0 then v_yes_cost / v_yes_size else 0 end
    + v_directional_no
      * case when v_no_size > 0 then v_no_cost / v_no_size else 0 end;

  update public.pair_inventory
    set yes_size = v_yes_size,
        no_size = v_no_size,
        yes_cost_pusd = v_yes_cost,
        no_cost_pusd = v_no_cost,
        paired_size = v_paired_size,
        paired_cost_pusd = v_paired_cost,
        directional_yes_size = v_directional_yes,
        directional_no_size = v_directional_no,
        directional_cost_pusd = v_directional_cost,
        realized_pnl_pusd = v_realized,
        version = version + 1,
        updated_at = now()
    where account_id = p_account_id
      and trading_wallet_id = v_group.trading_wallet_id
      and market_id = v_group.market_id;

  return 'applied';
end;
$$;

-- Undo the broad grants in 0008 and grant only the capabilities used by the
-- read UI and the transactional worker adapter.
revoke all privileges on table public.order_groups
  from public, anon, authenticated, service_role;
revoke all privileges on table public.order_legs
  from public, anon, authenticated, service_role;
revoke all privileges on table public.pair_inventory
  from public, anon, authenticated, service_role;
revoke all privileges on table public.pair_inventory_events
  from public, anon, authenticated, service_role;
revoke all privileges on sequence public.pair_inventory_events_id_seq
  from public, anon, authenticated, service_role;

grant select on table public.order_groups to authenticated;
grant select on table public.order_legs to authenticated;
grant select on table public.pair_inventory to authenticated;
grant select on table public.pair_inventory_events to authenticated;

grant select on table public.order_groups to service_role;
grant select on table public.order_legs to service_role;
grant select on table public.pair_inventory to service_role;
grant select on table public.pair_inventory_events to service_role;

revoke delete, truncate, references, trigger on table public.order_groups
  from service_role;
revoke delete, truncate, references, trigger on table public.order_legs
  from service_role;
revoke delete, truncate, references, trigger on table public.pair_inventory
  from service_role;
revoke update, delete, truncate, references, trigger
  on table public.pair_inventory_events from service_role;

revoke all on function public.persist_pair_order_plan(uuid, jsonb, jsonb)
  from public, anon, authenticated, service_role;
revoke all on function public.commit_pair_group_transition(
  uuid, bigint, jsonb, jsonb, text
) from public, anon, authenticated, service_role;
revoke all on function public.commit_pair_fill(
  uuid, bigint, jsonb, jsonb, jsonb
) from public, anon, authenticated, service_role;

grant execute on function public.persist_pair_order_plan(uuid, jsonb, jsonb)
  to service_role;
grant execute on function public.commit_pair_group_transition(
  uuid, bigint, jsonb, jsonb, text
) to service_role;
grant execute on function public.commit_pair_fill(
  uuid, bigint, jsonb, jsonb, jsonb
) to service_role;

commit;
