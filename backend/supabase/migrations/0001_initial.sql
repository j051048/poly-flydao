begin;

-- This schema deliberately contains no wallet private keys, API secrets, or
-- provider credentials. Those values belong exclusively in the worker's secret
-- environment. Application-encrypted, already-signed order payloads may be kept
-- for idempotent retries, but their encryption key must never be stored here.

create extension if not exists pgcrypto with schema extensions;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

revoke all on function public.set_updated_at() from public;

create table public.markets (
  id uuid primary key default gen_random_uuid(),
  gamma_market_id text,
  condition_id text not null unique,
  event_id text,
  slug text,
  question text not null,
  description text,
  resolution_source text,
  outcomes jsonb not null default '[]'::jsonb,
  clob_token_ids jsonb not null default '[]'::jsonb,
  active boolean not null default true,
  closed boolean not null default false,
  accepting_orders boolean not null default false,
  neg_risk boolean not null default false,
  tick_size numeric(20, 10),
  minimum_order_size numeric(38, 18),
  end_at timestamptz,
  last_synced_at timestamptz not null default now(),
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint markets_gamma_market_id_nonempty
    check (gamma_market_id is null or length(btrim(gamma_market_id)) > 0),
  constraint markets_condition_id_nonempty
    check (length(btrim(condition_id)) > 0),
  constraint markets_outcomes_array
    check (jsonb_typeof(outcomes) = 'array'),
  constraint markets_token_ids_array
    check (jsonb_typeof(clob_token_ids) = 'array'),
  constraint markets_tick_size_positive
    check (tick_size is null or tick_size > 0),
  constraint markets_minimum_order_size_positive
    check (minimum_order_size is null or minimum_order_size > 0)
);

create unique index markets_gamma_market_id_uidx
  on public.markets (gamma_market_id)
  where gamma_market_id is not null;
create index markets_active_end_at_idx
  on public.markets (active, closed, end_at);
create index markets_event_id_idx
  on public.markets (event_id)
  where event_id is not null;

create table public.snapshots (
  id bigint generated always as identity primary key,
  market_id uuid not null references public.markets(id) on delete cascade,
  source text not null default 'clob_ws',
  source_sequence bigint,
  captured_at timestamptz not null,
  yes_bid numeric(20, 10),
  yes_ask numeric(20, 10),
  no_bid numeric(20, 10),
  no_ask numeric(20, 10),
  midpoint numeric(20, 10),
  spread numeric(20, 10),
  last_trade_price numeric(20, 10),
  book jsonb not null default '{}'::jsonb,
  data_hash text,
  created_at timestamptz not null default now(),
  constraint snapshots_source_nonempty
    check (length(btrim(source)) > 0),
  constraint snapshots_prices_in_range
    check (
      (yes_bid is null or yes_bid between 0 and 1)
      and (yes_ask is null or yes_ask between 0 and 1)
      and (no_bid is null or no_bid between 0 and 1)
      and (no_ask is null or no_ask between 0 and 1)
      and (midpoint is null or midpoint between 0 and 1)
      and (last_trade_price is null or last_trade_price between 0 and 1)
      and (spread is null or spread >= 0)
    ),
  constraint snapshots_yes_book_not_crossed
    check (yes_bid is null or yes_ask is null or yes_bid <= yes_ask),
  constraint snapshots_no_book_not_crossed
    check (no_bid is null or no_ask is null or no_bid <= no_ask)
);

create unique index snapshots_source_dedupe_uidx
  on public.snapshots (market_id, source, source_sequence)
  where source_sequence is not null;
create index snapshots_market_captured_idx
  on public.snapshots (market_id, captured_at desc);
create index snapshots_captured_brin_idx
  on public.snapshots using brin (captured_at);

create table public.evidence (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references auth.users(id) on delete cascade,
  market_id uuid not null references public.markets(id) on delete restrict,
  source_type text not null,
  source_id text,
  source_url text,
  source_title text,
  published_at timestamptz,
  fetched_at timestamptz not null default now(),
  content_hash text not null,
  summary text not null,
  payload jsonb not null default '{}'::jsonb,
  reliability_score numeric(5, 4),
  expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint evidence_source_type_nonempty
    check (length(btrim(source_type)) > 0),
  constraint evidence_summary_nonempty
    check (length(btrim(summary)) > 0),
  constraint evidence_reliability_in_range
    check (reliability_score is null or reliability_score between 0 and 1)
);

create unique index evidence_account_content_hash_uidx
  on public.evidence (account_id, content_hash);
create index evidence_account_market_fetched_idx
  on public.evidence (account_id, market_id, fetched_at desc);

create table public.forecasts (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references auth.users(id) on delete cascade,
  market_id uuid not null references public.markets(id) on delete restrict,
  as_of timestamptz not null,
  provider text not null,
  model text not null,
  prompt_version text not null,
  calibration_version text,
  input_hash text not null,
  p_yes numeric(10, 9) not null,
  p_low numeric(10, 9) not null,
  p_high numeric(10, 9) not null,
  confidence numeric(10, 9),
  evidence_ids uuid[] not null default '{}'::uuid[],
  rationale jsonb not null default '{}'::jsonb,
  invalidation_conditions jsonb not null default '[]'::jsonb,
  status text not null default 'produced',
  expires_at timestamptz,
  created_at timestamptz not null default now(),
  constraint forecasts_provider_nonempty
    check (length(btrim(provider)) > 0),
  constraint forecasts_model_nonempty
    check (length(btrim(model)) > 0),
  constraint forecasts_input_hash_nonempty
    check (length(btrim(input_hash)) >= 16),
  constraint forecasts_probabilities_ordered
    check (
      p_low between 0 and 1
      and p_yes between 0 and 1
      and p_high between 0 and 1
      and p_low <= p_yes
      and p_yes <= p_high
    ),
  constraint forecasts_confidence_in_range
    check (confidence is null or confidence between 0 and 1),
  constraint forecasts_status_allowed
    check (status in ('produced', 'validated', 'rejected', 'expired')),
  constraint forecasts_invalidation_array
    check (jsonb_typeof(invalidation_conditions) = 'array')
);

create unique index forecasts_input_uidx
  on public.forecasts (account_id, market_id, provider, model, input_hash);
create index forecasts_account_market_as_of_idx
  on public.forecasts (account_id, market_id, as_of desc);

create table public.order_intents (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references auth.users(id) on delete restrict,
  market_id uuid not null references public.markets(id) on delete restrict,
  forecast_id uuid references public.forecasts(id) on delete set null,
  intent_hash text not null unique,
  run_id text not null,
  mode text not null default 'paper',
  event_id text,
  bucket text not null,
  outcome text not null,
  token_id text not null,
  side text not null,
  order_type text not null default 'GTC',
  price numeric(20, 10) not null,
  size numeric(38, 18) not null,
  size_unit text not null default 'shares',
  notional_usd numeric(38, 18) not null,
  expected_fee_pusd numeric(38, 18) not null default 0,
  expected_slippage numeric(20, 10) not null default 0,
  edge_after_costs numeric(20, 10) not null,
  strategy text not null,
  decision_key text,
  post_only boolean not null default false,
  expires_at timestamptz,
  status text not null default 'proposed',
  risk_checks jsonb not null default '{}'::jsonb,
  rationale jsonb not null default '{}'::jsonb,
  approved_by uuid references auth.users(id) on delete set null,
  approved_at timestamptz,
  rejected_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint order_intents_hash_length
    check (length(btrim(intent_hash)) between 32 and 128),
  constraint order_intents_run_id_nonempty
    check (length(btrim(run_id)) > 0),
  constraint order_intents_mode_allowed
    check (mode in ('paper', 'shadow', 'canary', 'live')),
  constraint order_intents_bucket_nonempty
    check (length(btrim(bucket)) > 0),
  constraint order_intents_outcome_allowed
    check (outcome in ('YES', 'NO')),
  constraint order_intents_side_allowed
    check (side in ('BUY', 'SELL')),
  constraint order_intents_type_allowed
    check (order_type in ('GTC', 'GTD', 'FOK', 'FAK')),
  constraint order_intents_price_in_range
    check (price > 0 and price < 1),
  constraint order_intents_size_positive
    check (size > 0),
  constraint order_intents_size_unit_allowed
    check (size_unit in ('shares', 'pusd')),
  constraint order_intents_costs_nonnegative
    check (
      notional_usd > 0
      and expected_fee_pusd >= 0
      and expected_slippage >= 0
    ),
  constraint order_intents_strategy_nonempty
    check (length(btrim(strategy)) > 0),
  constraint order_intents_post_only_compatible
    check (not post_only or order_type in ('GTC', 'GTD')),
  constraint order_intents_gtd_has_expiry
    check (order_type <> 'GTD' or expires_at is not null),
  constraint order_intents_status_allowed
    check (
      status in (
        'proposed', 'risk_approved', 'rejected', 'signed', 'submitted',
        'live', 'partially_filled', 'matched', 'mined', 'confirmed',
        'simulated', 'cancelled', 'expired', 'failed'
      )
    )
);

create index order_intents_account_status_created_idx
  on public.order_intents (account_id, status, created_at desc);
create index order_intents_market_created_idx
  on public.order_intents (market_id, created_at desc);
create index order_intents_run_id_idx
  on public.order_intents (account_id, run_id);

create table public.orders (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references auth.users(id) on delete restrict,
  order_intent_id uuid not null references public.order_intents(id) on delete restrict,
  client_order_id text not null,
  clob_order_id text,
  signed_order_hash text,
  signed_payload_ciphertext bytea,
  payload_key_version integer,
  worker_fencing_token bigint,
  environment text not null default 'paper',
  outcome_token_id text not null,
  side text not null,
  order_type text not null,
  limit_price numeric(20, 10) not null,
  original_size numeric(38, 18) not null,
  filled_size numeric(38, 18) not null default 0,
  remaining_size numeric(38, 18) not null,
  average_fill_price numeric(20, 10),
  fee_paid_pusd numeric(38, 18) not null default 0,
  status text not null default 'created',
  attempt_count integer not null default 0,
  last_error_code text,
  last_error_detail text,
  response_payload jsonb not null default '{}'::jsonb,
  submitted_at timestamptz,
  matched_at timestamptz,
  mined_at timestamptz,
  confirmed_at timestamptz,
  cancelled_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint orders_client_order_id_nonempty
    check (length(btrim(client_order_id)) > 0),
  constraint orders_environment_allowed
    check (environment in ('paper', 'shadow', 'canary', 'live')),
  constraint orders_side_allowed
    check (side in ('BUY', 'SELL')),
  constraint orders_type_allowed
    check (order_type in ('GTC', 'GTD', 'FOK', 'FAK')),
  constraint orders_price_in_range
    check (limit_price > 0 and limit_price < 1),
  constraint orders_sizes_valid
    check (
      original_size > 0
      and filled_size >= 0
      and remaining_size >= 0
      and filled_size <= original_size
      and remaining_size <= original_size
    ),
  constraint orders_average_price_in_range
    check (average_fill_price is null or average_fill_price between 0 and 1),
  constraint orders_fee_nonnegative
    check (fee_paid_pusd >= 0),
  constraint orders_attempt_count_nonnegative
    check (attempt_count >= 0),
  constraint orders_status_allowed
    check (
      status in (
        'created', 'signed', 'submitting', 'submitted', 'live',
        'partially_filled', 'matched', 'mined', 'confirmed', 'simulated',
        'cancel_pending', 'cancelled', 'expired', 'rejected', 'failed',
        'unknown'
      )
    ),
  constraint orders_encrypted_payload_key_version
    check (
      (signed_payload_ciphertext is null and payload_key_version is null)
      or (signed_payload_ciphertext is not null and payload_key_version is not null)
    ),
  constraint orders_worker_fencing_token_positive
    check (worker_fencing_token is null or worker_fencing_token > 0)
);

create unique index orders_account_client_order_uidx
  on public.orders (account_id, client_order_id);
create unique index orders_clob_order_id_uidx
  on public.orders (clob_order_id)
  where clob_order_id is not null;
create unique index orders_signed_order_hash_uidx
  on public.orders (signed_order_hash)
  where signed_order_hash is not null;
create index orders_account_status_updated_idx
  on public.orders (account_id, status, updated_at desc);
create index orders_intent_idx
  on public.orders (order_intent_id);

create table public.fills (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references auth.users(id) on delete restrict,
  order_id uuid not null references public.orders(id) on delete restrict,
  market_id uuid not null references public.markets(id) on delete restrict,
  fill_key text not null unique,
  clob_trade_id text,
  outcome_token_id text not null,
  side text not null,
  liquidity_role text,
  price numeric(20, 10) not null,
  size numeric(38, 18) not null,
  fee_pusd numeric(38, 18) not null default 0,
  settlement_status text not null default 'MATCHED',
  transaction_hash text,
  block_number bigint,
  matched_at timestamptz not null,
  mined_at timestamptz,
  confirmed_at timestamptz,
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint fills_key_nonempty
    check (length(btrim(fill_key)) >= 16),
  constraint fills_side_allowed
    check (side in ('BUY', 'SELL')),
  constraint fills_liquidity_role_allowed
    check (liquidity_role is null or liquidity_role in ('maker', 'taker')),
  constraint fills_price_in_range
    check (price > 0 and price < 1),
  constraint fills_size_positive
    check (size > 0),
  constraint fills_fee_nonnegative
    check (fee_pusd >= 0),
  constraint fills_settlement_status_allowed
    check (settlement_status in ('MATCHED', 'MINED', 'CONFIRMED', 'RETRYING', 'FAILED'))
);

-- One taker trade can match more than one maker order owned by the account.
-- `fill_key` (trade + order) is the idempotency key; trade id alone is not unique.
create index fills_clob_trade_id_idx
  on public.fills (clob_trade_id)
  where clob_trade_id is not null;
create index fills_account_market_matched_idx
  on public.fills (account_id, market_id, matched_at desc);
create index fills_order_idx
  on public.fills (order_id);

create table public.account_activities (
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

create index account_activities_account_occurred_idx
  on public.account_activities (account_id, occurred_at);

create table public.positions (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references auth.users(id) on delete restrict,
  market_id uuid not null references public.markets(id) on delete restrict,
  outcome text not null,
  outcome_token_id text not null,
  shares numeric(38, 18) not null default 0,
  average_entry_price numeric(20, 10),
  cost_basis_pusd numeric(38, 18) not null default 0,
  realized_pnl_pusd numeric(38, 18) not null default 0,
  mark_price numeric(20, 10),
  unrealized_pnl_pusd numeric(38, 18) not null default 0,
  as_of timestamptz not null,
  version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint positions_outcome_allowed
    check (outcome in ('YES', 'NO')),
  constraint positions_shares_nonnegative
    check (shares >= 0),
  constraint positions_average_price_in_range
    check (average_entry_price is null or average_entry_price between 0 and 1),
  constraint positions_mark_price_in_range
    check (mark_price is null or mark_price between 0 and 1),
  constraint positions_cost_basis_nonnegative
    check (cost_basis_pusd >= 0),
  constraint positions_version_positive
    check (version > 0),
  unique (account_id, market_id, outcome_token_id)
);

create index positions_account_updated_idx
  on public.positions (account_id, updated_at desc);

create table public.risk_events (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references auth.users(id) on delete restrict,
  market_id uuid references public.markets(id) on delete set null,
  order_intent_id uuid references public.order_intents(id) on delete set null,
  order_id uuid references public.orders(id) on delete set null,
  severity text not null,
  code text not null,
  message text not null,
  action text not null default 'notify',
  details jsonb not null default '{}'::jsonb,
  occurred_at timestamptz not null default now(),
  resolved_at timestamptz,
  resolved_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint risk_events_severity_allowed
    check (severity in ('info', 'warning', 'critical')),
  constraint risk_events_code_nonempty
    check (length(btrim(code)) > 0),
  constraint risk_events_message_nonempty
    check (length(btrim(message)) > 0),
  constraint risk_events_action_allowed
    check (action in ('notify', 'reject', 'reduce', 'halt', 'cancel_all')),
  constraint risk_events_resolution_order
    check (resolved_at is null or resolved_at >= occurred_at)
);

create index risk_events_account_unresolved_idx
  on public.risk_events (account_id, severity, occurred_at desc)
  where resolved_at is null;

create table public.runtime_controls (
  account_id uuid primary key references auth.users(id) on delete restrict,
  mode text not null default 'paper',
  kill_switch boolean not null default true,
  accept_new_intents boolean not null default false,
  armed boolean not null default false,
  armed_until timestamptz,
  cancellation_pending boolean not null default false,
  require_manual_live_confirmation boolean not null default true,
  max_single_order_pusd numeric(38, 18) not null default 10,
  max_position_per_market_pct numeric(10, 9) not null default 0.02,
  max_correlated_exposure_pct numeric(10, 9) not null default 0.05,
  max_gross_exposure_pct numeric(10, 9) not null default 0.10,
  daily_loss_limit_pct numeric(10, 9) not null default 0.02,
  max_drawdown_pct numeric(10, 9) not null default 0.08,
  max_data_age_seconds integer not null default 10,
  version bigint not null default 1,
  live_confirmation_ref text,
  last_live_confirmed_at timestamptz,
  updated_by uuid references auth.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint runtime_controls_mode_allowed
    check (mode in ('paper', 'shadow', 'canary', 'live')),
  constraint runtime_controls_armed_state_valid
    check (
      (not armed and not accept_new_intents and armed_until is null)
      or (
        armed and accept_new_intents and mode in ('canary', 'live')
        and not kill_switch and not cancellation_pending and armed_until is not null
      )
    ),
  constraint runtime_controls_order_limit_positive
    check (max_single_order_pusd > 0),
  constraint runtime_controls_percentages_in_range
    check (
      max_position_per_market_pct > 0 and max_position_per_market_pct <= 1
      and max_correlated_exposure_pct > 0 and max_correlated_exposure_pct <= 1
      and max_gross_exposure_pct > 0 and max_gross_exposure_pct <= 1
      and daily_loss_limit_pct > 0 and daily_loss_limit_pct <= 1
      and max_drawdown_pct > 0 and max_drawdown_pct <= 1
    ),
  constraint runtime_controls_exposure_ordered
    check (
      max_position_per_market_pct <= max_correlated_exposure_pct
      and max_correlated_exposure_pct <= max_gross_exposure_pct
    ),
  constraint runtime_controls_data_age_positive
    check (max_data_age_seconds between 1 and 300),
  constraint runtime_controls_version_positive
    check (version > 0)
);

-- A rolling deployment can briefly run two worker processes. The executor must
-- claim this lease and attach its fencing_token to every state transition;
-- stale owners are not allowed to sign or submit orders.
create table public.worker_leases (
  account_id uuid primary key references auth.users(id) on delete restrict,
  owner_id text not null,
  fencing_token bigint not null default 1,
  acquired_at timestamptz not null default now(),
  expires_at timestamptz not null,
  updated_at timestamptz not null default now(),
  constraint worker_leases_owner_nonempty
    check (length(btrim(owner_id)) >= 8),
  constraint worker_leases_fencing_token_positive
    check (fencing_token > 0),
  constraint worker_leases_time_order
    check (expires_at >= updated_at)
);

create table public.account_risk_state (
  account_id uuid primary key references auth.users(id) on delete restrict,
  peak_equity_pusd numeric(38, 18) not null,
  latest_equity_pusd numeric(38, 18) not null,
  day_start_equity_pusd numeric(38, 18) not null,
  risk_day date not null default ((now() at time zone 'utc')::date),
  updated_at timestamptz not null default now(),
  constraint account_risk_state_equity_nonnegative
    check (
      peak_equity_pusd >= 0
      and latest_equity_pusd >= 0
      and day_start_equity_pusd >= 0
    ),
  constraint account_risk_state_peak_valid
    check (peak_equity_pusd >= latest_equity_pusd)
);

create or replace function public.claim_worker_lease(
  p_account_id uuid,
  p_owner_id text,
  p_ttl_seconds integer default 15
)
returns table (claimed boolean, fencing_token bigint, expires_at timestamptz)
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if length(btrim($2)) < 8 then
    raise exception 'owner_id must contain at least 8 characters';
  end if;
  if $3 < 5 or $3 > 600 then
    raise exception 'lease TTL must be between 5 and 600 seconds';
  end if;

  return query
  with lease_claim as (
    insert into public.worker_leases (
      account_id,
      owner_id,
      fencing_token,
      acquired_at,
      expires_at,
      updated_at
    )
    values (
      $1,
      $2,
      1,
      now(),
      now() + make_interval(secs => $3),
      now()
    )
    on conflict on constraint worker_leases_pkey do update
      set owner_id = excluded.owner_id,
          fencing_token = case
            when public.worker_leases.owner_id = excluded.owner_id
              then public.worker_leases.fencing_token
            else public.worker_leases.fencing_token + 1
          end,
          acquired_at = case
            when public.worker_leases.owner_id = excluded.owner_id
              then public.worker_leases.acquired_at
            else now()
          end,
          expires_at = now() + make_interval(secs => $3),
          updated_at = now()
      where public.worker_leases.owner_id = excluded.owner_id
         or public.worker_leases.expires_at <= now()
    returning public.worker_leases.fencing_token,
              public.worker_leases.expires_at
  )
  select true, lease_claim.fencing_token, lease_claim.expires_at
  from lease_claim;

  if not found then
    return query
    select false, lease.fencing_token, lease.expires_at
    from public.worker_leases as lease
    where lease.account_id = $1;
  end if;
end;
$$;

create or replace function public.release_worker_lease(
  p_account_id uuid,
  p_owner_id text,
  p_fencing_token bigint
)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
  released_count integer;
begin
  update public.worker_leases as lease
     set expires_at = now(),
         updated_at = now()
   where lease.account_id = $1
     and lease.owner_id = $2
     and lease.fencing_token = $3;
  get diagnostics released_count = row_count;
  return released_count = 1;
end;
$$;

create or replace function public.validate_worker_lease(
  p_account_id uuid,
  p_owner_id text,
  p_fencing_token bigint
)
returns boolean
language sql
stable
security invoker
set search_path = ''
as $$
  select exists (
    select 1
    from public.worker_leases as lease
    where lease.account_id = $1
      and lease.owner_id = $2
      and lease.fencing_token = $3
      and lease.expires_at > now()
  );
$$;

-- This transition is the last durable gate before the signed payload leaves the
-- worker. It binds the order to the still-current, unexpired fencing token in a
-- single database statement so a stale rolling-deployment worker fails closed.
create or replace function public.mark_order_submitting(
  p_account_id uuid,
  p_intent_hash text,
  p_fencing_token bigint
)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
  transitioned_count integer;
begin
  update public.orders as order_row
     set status = 'submitting',
         attempt_count = order_row.attempt_count + 1
    from public.worker_leases as lease
   where order_row.account_id = $1
     and order_row.client_order_id = $2
     and order_row.status = 'signed'
     and order_row.worker_fencing_token = $3
     and lease.account_id = $1
     and lease.fencing_token = $3
     and lease.expires_at > now();
  get diagnostics transitioned_count = row_count;
  return transitioned_count = 1;
end;
$$;

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

-- Arming is optimistic: the caller must present the version it just read.
-- PostgreSQL rechecks the version predicate after waiting on a concurrent row
-- lock, so a simultaneous disarm always makes the stale arm a no-op.
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
  returning control.*;
end;
$$;

-- Disarm is an unconditional serialized latch. It never relies on a prior
-- read, and every successful invocation advances the version. A concurrent
-- stale arm therefore cannot overwrite the kill switch after waiting.
create or replace function public.disarm_runtime_control(
  p_account_id uuid,
  p_mode text
)
returns setof public.runtime_controls
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if p_mode not in ('paper', 'shadow', 'canary', 'live') then
    raise exception 'invalid runtime control mode'
      using errcode = '22023';
  end if;

  insert into public.runtime_controls (account_id)
  values (p_account_id)
  on conflict (account_id) do nothing;

  return query
  update public.runtime_controls as control
  set
    mode = p_mode,
    kill_switch = true,
    accept_new_intents = false,
    armed = false,
    armed_until = null,
    cancellation_pending = p_mode in ('canary', 'live'),
    version = control.version + 1
  where control.account_id = p_account_id
  returning control.*;
end;
$$;

-- Only the worker that has verified zero exchange open orders may clear this
-- latch. Version CAS prevents an acknowledgement from clearing a newer
-- disarm request.
create or replace function public.acknowledge_runtime_cancellation(
  p_account_id uuid,
  p_expected_version bigint
)
returns setof public.runtime_controls
language plpgsql
security invoker
set search_path = ''
as $$
begin
  return query
  update public.runtime_controls as control
  set
    cancellation_pending = false,
    version = control.version + 1
  where control.account_id = p_account_id
    and control.version = p_expected_version
    and control.cancellation_pending
    and control.kill_switch
    and not control.armed
    and not exists (
      select 1
      from public.orders as pending_order
      where pending_order.account_id = p_account_id
        and pending_order.status in ('signed', 'submitting', 'unknown')
    )
  returning control.*;
end;
$$;

revoke all on function public.claim_worker_lease(uuid, text, integer)
  from public, anon, authenticated;
revoke all on function public.release_worker_lease(uuid, text, bigint)
  from public, anon, authenticated;
revoke all on function public.validate_worker_lease(uuid, text, bigint)
  from public, anon, authenticated;
revoke all on function public.mark_order_submitting(uuid, text, bigint)
  from public, anon, authenticated;
revoke all on function public.record_equity_state(uuid, numeric)
  from public, anon, authenticated;
revoke all on function public.arm_runtime_control(uuid, text, timestamptz, bigint)
  from public, anon, authenticated;
revoke all on function public.disarm_runtime_control(uuid, text)
  from public, anon, authenticated;
revoke all on function public.acknowledge_runtime_cancellation(uuid, bigint)
  from public, anon, authenticated;
grant execute on function public.claim_worker_lease(uuid, text, integer)
  to service_role;
grant execute on function public.release_worker_lease(uuid, text, bigint)
  to service_role;
grant execute on function public.validate_worker_lease(uuid, text, bigint)
  to service_role;
grant execute on function public.mark_order_submitting(uuid, text, bigint)
  to service_role;
grant execute on function public.record_equity_state(uuid, numeric)
  to service_role;
grant execute on function public.arm_runtime_control(uuid, text, timestamptz, bigint)
  to service_role;
grant execute on function public.disarm_runtime_control(uuid, text)
  to service_role;
grant execute on function public.acknowledge_runtime_cancellation(uuid, bigint)
  to service_role;

create table public.audit_events (
  id bigint generated always as identity primary key,
  account_id uuid not null,
  actor_type text not null,
  actor_id text,
  action text not null,
  entity_type text,
  entity_id text,
  request_id text,
  trace_id text,
  ip_hash text,
  before_state jsonb,
  after_state jsonb,
  metadata jsonb not null default '{}'::jsonb,
  previous_event_hash text,
  event_hash text,
  occurred_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  constraint audit_events_actor_type_allowed
    check (actor_type in ('user', 'worker', 'system', 'ai')),
  constraint audit_events_action_nonempty
    check (length(btrim(action)) > 0)
);

create unique index audit_events_event_hash_uidx
  on public.audit_events (event_hash)
  where event_hash is not null;
create index audit_events_account_occurred_idx
  on public.audit_events (account_id, occurred_at desc);
create index audit_events_entity_idx
  on public.audit_events (entity_type, entity_id, occurred_at desc)
  where entity_type is not null and entity_id is not null;

create or replace function public.validate_runtime_control_change()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if tg_op = 'UPDATE' and new.version <> old.version + 1 then
    raise exception 'runtime_controls.version must increase by exactly one'
      using errcode = '23514';
  end if;

  if new.armed and (
    new.armed_until is null
    or new.armed_until <= now()
    or new.armed_until > now() + interval '15 minutes'
  ) then
    raise exception 'armed_until must be within the next 15 minutes'
      using errcode = '23514';
  end if;

  return new;
end;
$$;

create or replace function public.audit_runtime_control_change()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
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
  values (
    new.account_id,
    'system',
    current_user::text,
    case when tg_op = 'INSERT'
      then 'runtime_control_created'
      else 'runtime_control_changed'
    end,
    'runtime_controls',
    new.account_id::text,
    case when tg_op = 'UPDATE' then to_jsonb(old) else null end,
    to_jsonb(new),
    jsonb_build_object('source', 'database_trigger')
  );
  return new;
end;
$$;

revoke all on function public.validate_runtime_control_change() from public;
revoke all on function public.audit_runtime_control_change() from public;

create or replace function public.prevent_audit_event_mutation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  raise exception 'audit_events is append-only' using errcode = '42501';
end;
$$;

revoke all on function public.prevent_audit_event_mutation() from public;

create trigger audit_events_append_only
before update or delete on public.audit_events
for each row execute function public.prevent_audit_event_mutation();

create trigger markets_set_updated_at
before update on public.markets
for each row execute function public.set_updated_at();
create trigger evidence_set_updated_at
before update on public.evidence
for each row execute function public.set_updated_at();
create trigger order_intents_set_updated_at
before update on public.order_intents
for each row execute function public.set_updated_at();
create trigger orders_set_updated_at
before update on public.orders
for each row execute function public.set_updated_at();
create trigger positions_set_updated_at
before update on public.positions
for each row execute function public.set_updated_at();
create trigger account_activities_set_updated_at
before update on public.account_activities
for each row execute function public.set_updated_at();
create trigger risk_events_set_updated_at
before update on public.risk_events
for each row execute function public.set_updated_at();
create trigger runtime_controls_set_updated_at
before update on public.runtime_controls
for each row execute function public.set_updated_at();
create trigger runtime_controls_validate
before insert or update on public.runtime_controls
for each row execute function public.validate_runtime_control_change();
create trigger runtime_controls_audit
after insert or update on public.runtime_controls
for each row execute function public.audit_runtime_control_change();
create trigger worker_leases_set_updated_at
before update on public.worker_leases
for each row execute function public.set_updated_at();
create trigger account_risk_state_set_updated_at
before update on public.account_risk_state
for each row execute function public.set_updated_at();

-- RLS is enabled on every API-visible table. There are deliberately no client
-- INSERT, UPDATE, or DELETE policies. Writes must pass through a trusted Vercel
-- server route or the Zeabur worker using service credentials.
alter table public.markets enable row level security;
alter table public.snapshots enable row level security;
alter table public.evidence enable row level security;
alter table public.forecasts enable row level security;
alter table public.order_intents enable row level security;
alter table public.orders enable row level security;
alter table public.fills enable row level security;
alter table public.account_activities enable row level security;
alter table public.positions enable row level security;
alter table public.risk_events enable row level security;
alter table public.runtime_controls enable row level security;
alter table public.worker_leases enable row level security;
alter table public.account_risk_state enable row level security;
alter table public.audit_events enable row level security;

create policy markets_authenticated_read
  on public.markets for select to authenticated using (true);
create policy snapshots_authenticated_read
  on public.snapshots for select to authenticated using (true);
create policy evidence_owner_read
  on public.evidence for select to authenticated
  using ((select auth.uid()) = account_id);
create policy forecasts_owner_read
  on public.forecasts for select to authenticated
  using ((select auth.uid()) = account_id);
create policy order_intents_owner_read
  on public.order_intents for select to authenticated
  using ((select auth.uid()) = account_id);
create policy orders_owner_read
  on public.orders for select to authenticated
  using ((select auth.uid()) = account_id);
create policy fills_owner_read
  on public.fills for select to authenticated
  using ((select auth.uid()) = account_id);
create policy account_activities_owner_read
  on public.account_activities for select to authenticated
  using ((select auth.uid()) = account_id);
create policy positions_owner_read
  on public.positions for select to authenticated
  using ((select auth.uid()) = account_id);
create policy risk_events_owner_read
  on public.risk_events for select to authenticated
  using ((select auth.uid()) = account_id);
create policy runtime_controls_owner_read
  on public.runtime_controls for select to authenticated
  using ((select auth.uid()) = account_id);
create policy worker_leases_owner_read
  on public.worker_leases for select to authenticated
  using ((select auth.uid()) = account_id);
create policy account_risk_state_owner_read
  on public.account_risk_state for select to authenticated
  using ((select auth.uid()) = account_id);
create policy audit_events_owner_read
  on public.audit_events for select to authenticated
  using ((select auth.uid()) = account_id);

revoke all on table
  public.markets,
  public.snapshots,
  public.evidence,
  public.forecasts,
  public.order_intents,
  public.orders,
  public.fills,
  public.account_activities,
  public.positions,
  public.risk_events,
  public.runtime_controls,
  public.worker_leases,
  public.account_risk_state,
  public.audit_events
from anon, authenticated;

grant select on table
  public.markets,
  public.snapshots,
  public.evidence,
  public.forecasts,
  public.order_intents,
  public.positions,
  public.risk_events,
  public.runtime_controls,
  public.worker_leases,
  public.account_risk_state,
  public.audit_events
to authenticated;

-- The dashboard cannot select the replayable signed payload or raw exchange
-- responses. Server code may retrieve those fields with service credentials.
grant select (
  id, account_id, order_intent_id, client_order_id, clob_order_id,
  signed_order_hash, payload_key_version, environment, outcome_token_id, side,
  order_type, limit_price, original_size, filled_size, remaining_size,
  average_fill_price, fee_paid_pusd, status, attempt_count, last_error_code,
  last_error_detail, submitted_at, matched_at, mined_at, confirmed_at,
  cancelled_at, created_at, updated_at
) on public.orders to authenticated;

grant select (
  id, account_id, order_id, market_id, fill_key, clob_trade_id,
  outcome_token_id, side, liquidity_role, price, size, fee_pusd,
  settlement_status, transaction_hash, block_number, matched_at, mined_at,
  confirmed_at, created_at
) on public.fills to authenticated;

grant select (
  id, account_id, activity_key, activity_type, condition_id, amount_pusd,
  transaction_hash, occurred_at, created_at, updated_at
) on public.account_activities to authenticated;

grant select, insert, update, delete on table
  public.markets,
  public.snapshots,
  public.evidence,
  public.forecasts,
  public.order_intents,
  public.orders,
  public.fills,
  public.account_activities,
  public.positions,
  public.risk_events,
  public.runtime_controls,
  public.worker_leases,
  public.account_risk_state
to service_role;
revoke delete on table
  public.order_intents,
  public.orders,
  public.fills,
  public.account_activities,
  public.risk_events,
  public.runtime_controls,
  public.worker_leases,
  public.account_risk_state
from service_role;
grant select, insert on table public.audit_events to service_role;
grant usage, select on sequence
  public.snapshots_id_seq,
  public.audit_events_id_seq
to service_role;

comment on table public.runtime_controls is
  'Fail-closed runtime gate. New rows default to paper mode, unarmed, with kill_switch on and no intent acceptance. The worker must also reject an expired armed_until value.';
comment on column public.orders.signed_payload_ciphertext is
  'Optional application-encrypted signed order payload for exact idempotent replay. Never expose to the UI; keep the encryption key only in the worker secret environment.';
comment on table public.audit_events is
  'Append-only audit trail. Callers must redact wallet keys, API secrets, auth tokens, signed payloads, and sensitive evidence before insert.';

commit;
