begin;

-- Batch CLOB submission is explicitly non-atomic. These tables persist the
-- group/leg state machine and keep complete pairs separate from directional
-- leftovers. No credential or signed payload is stored here.

create unique index if not exists trading_wallets_id_account_uidx
  on public.trading_wallets (id, account_id);

create table public.order_groups (
  id uuid primary key default gen_random_uuid(),
  account_id uuid not null references auth.users(id) on delete restrict,
  trading_wallet_id uuid not null,
  market_id uuid not null references public.markets(id) on delete restrict,
  strategy text not null default 'pair_accumulator_v1',
  target_pair_size numeric(38, 18) not null,
  paired_size numeric(38, 18) not null default 0,
  directional_yes_size numeric(38, 18) not null default 0,
  directional_no_size numeric(38, 18) not null default 0,
  expected_net_edge_pusd numeric(38, 18) not null,
  leg_deadline_at timestamptz not null,
  status text not null default 'planned',
  research_only boolean not null default true,
  execution_enabled boolean not null default false,
  version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, account_id),
  foreign key (trading_wallet_id, account_id)
    references public.trading_wallets(id, account_id) on delete restrict,
  constraint order_groups_strategy_nonempty
    check (length(btrim(strategy)) > 0),
  constraint order_groups_sizes_valid
    check (
      target_pair_size > 0
      and paired_size >= 0
      and paired_size <= target_pair_size
      and directional_yes_size >= 0
      and directional_no_size >= 0
      and not (directional_yes_size > 0 and directional_no_size > 0)
    ),
  constraint order_groups_status_allowed
    check (
      status in (
        'planned', 'submitting', 'working', 'imbalanced', 'cancelling',
        'hedging', 'paired', 'cancelled', 'frozen', 'failed'
      )
    ),
  constraint order_groups_version_positive check (version > 0),
  constraint order_groups_research_gate
    check (not execution_enabled or not research_only)
);

create index order_groups_account_wallet_status_idx
  on public.order_groups (account_id, trading_wallet_id, status, updated_at desc);
create index order_groups_deadline_idx
  on public.order_groups (leg_deadline_at)
  where status in ('working', 'imbalanced', 'cancelling', 'hedging');

create table public.order_legs (
  id uuid primary key default gen_random_uuid(),
  group_id uuid not null,
  account_id uuid not null references auth.users(id) on delete restrict,
  outcome text not null,
  token_id text not null,
  side text not null default 'BUY',
  purpose text not null default 'pair_entry',
  liquidity_role text not null,
  post_only boolean not null,
  price numeric(20, 10) not null,
  size numeric(38, 18) not null,
  filled_size numeric(38, 18) not null default 0,
  average_fill_price numeric(20, 10),
  fee_paid_pusd numeric(38, 18) not null default 0,
  status text not null default 'planned',
  clob_order_id text,
  deadline_at timestamptz not null,
  version bigint not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, account_id),
  foreign key (group_id, account_id)
    references public.order_groups(id, account_id) on delete cascade,
  constraint order_legs_outcome_allowed check (outcome in ('YES', 'NO')),
  constraint order_legs_side_allowed check (side in ('BUY', 'SELL')),
  constraint order_legs_purpose_allowed
    check (purpose in ('pair_entry', 'imbalance_hedge', 'directional_overlay')),
  constraint order_legs_liquidity_role_allowed
    check (liquidity_role in ('maker', 'taker')),
  constraint order_legs_maker_post_only
    check (
      (liquidity_role = 'maker' and post_only)
      or (liquidity_role = 'taker' and not post_only)
    ),
  constraint order_legs_price_size_valid
    check (
      price > 0 and price < 1
      and size > 0
      and filled_size >= 0 and filled_size <= size
      and fee_paid_pusd >= 0
      and (
        (filled_size = 0 and average_fill_price is null)
        or (
          filled_size > 0
          and average_fill_price > 0
          and average_fill_price < 1
        )
      )
    ),
  constraint order_legs_status_allowed
    check (
      status in (
        'planned', 'submitting', 'open', 'partially_filled', 'filled',
        'cancel_pending', 'cancelled', 'rejected', 'failed'
      )
    ),
  constraint order_legs_version_positive check (version > 0)
);

create unique index order_legs_clob_order_uidx
  on public.order_legs (account_id, clob_order_id)
  where clob_order_id is not null;
create index order_legs_group_status_idx
  on public.order_legs (account_id, group_id, status);

create table public.pair_inventory (
  account_id uuid not null references auth.users(id) on delete restrict,
  trading_wallet_id uuid not null,
  market_id uuid not null references public.markets(id) on delete restrict,
  yes_size numeric(38, 18) not null default 0,
  no_size numeric(38, 18) not null default 0,
  yes_cost_pusd numeric(38, 18) not null default 0,
  no_cost_pusd numeric(38, 18) not null default 0,
  paired_size numeric(38, 18) not null default 0,
  paired_cost_pusd numeric(38, 18) not null default 0,
  directional_yes_size numeric(38, 18) not null default 0,
  directional_no_size numeric(38, 18) not null default 0,
  directional_cost_pusd numeric(38, 18) not null default 0,
  realized_pnl_pusd numeric(38, 18) not null default 0,
  version bigint not null default 1,
  updated_at timestamptz not null default now(),
  primary key (account_id, trading_wallet_id, market_id),
  foreign key (trading_wallet_id, account_id)
    references public.trading_wallets(id, account_id) on delete restrict,
  constraint pair_inventory_values_valid
    check (
      yes_size >= 0 and no_size >= 0
      and yes_cost_pusd >= 0 and no_cost_pusd >= 0
      and paired_size >= 0 and paired_size = least(yes_size, no_size)
      and paired_cost_pusd >= 0
      and directional_yes_size = greatest(yes_size - no_size, 0)
      and directional_no_size = greatest(no_size - yes_size, 0)
      and directional_cost_pusd >= 0
      and version > 0
    )
);

create index pair_inventory_account_wallet_idx
  on public.pair_inventory (account_id, trading_wallet_id, updated_at desc);

create table public.pair_inventory_events (
  id bigint generated always as identity primary key,
  account_id uuid not null references auth.users(id) on delete restrict,
  trading_wallet_id uuid not null,
  market_id uuid not null references public.markets(id) on delete restrict,
  order_group_id uuid,
  order_leg_id uuid,
  clob_trade_id text not null,
  outcome text not null,
  side text not null,
  liquidity_role text not null,
  price numeric(20, 10) not null,
  size numeric(38, 18) not null,
  fee_paid_pusd numeric(38, 18) not null default 0,
  matched_at timestamptz not null,
  created_at timestamptz not null default now(),
  foreign key (trading_wallet_id, account_id)
    references public.trading_wallets(id, account_id) on delete restrict,
  foreign key (order_group_id, account_id)
    references public.order_groups(id, account_id) on delete restrict,
  foreign key (order_leg_id, account_id)
    references public.order_legs(id, account_id) on delete restrict,
  constraint pair_inventory_events_trade_nonempty
    check (length(btrim(clob_trade_id)) > 0),
  constraint pair_inventory_events_outcome_allowed check (outcome in ('YES', 'NO')),
  constraint pair_inventory_events_side_allowed check (side in ('BUY', 'SELL')),
  constraint pair_inventory_events_role_allowed check (liquidity_role in ('maker', 'taker')),
  constraint pair_inventory_events_values_valid
    check (
      price > 0 and price < 1 and size > 0 and fee_paid_pusd >= 0
    )
);

create unique index pair_inventory_events_trade_uidx
  on public.pair_inventory_events (account_id, clob_trade_id);
create index pair_inventory_events_wallet_market_idx
  on public.pair_inventory_events (
    account_id, trading_wallet_id, market_id, matched_at desc
  );

create trigger order_groups_set_updated_at
before update on public.order_groups
for each row execute function public.set_updated_at();

create trigger order_legs_set_updated_at
before update on public.order_legs
for each row execute function public.set_updated_at();

create trigger pair_inventory_set_updated_at
before update on public.pair_inventory
for each row execute function public.set_updated_at();

alter table public.order_groups enable row level security;
alter table public.order_legs enable row level security;
alter table public.pair_inventory enable row level security;
alter table public.pair_inventory_events enable row level security;

create policy order_groups_owner_read
  on public.order_groups for select to authenticated
  using ((select auth.uid()) = account_id);
create policy order_legs_owner_read
  on public.order_legs for select to authenticated
  using ((select auth.uid()) = account_id);
create policy pair_inventory_owner_read
  on public.pair_inventory for select to authenticated
  using ((select auth.uid()) = account_id);
create policy pair_inventory_events_owner_read
  on public.pair_inventory_events for select to authenticated
  using ((select auth.uid()) = account_id);

revoke insert, update, delete on table public.order_groups from authenticated;
revoke insert, update, delete on table public.order_legs from authenticated;
revoke insert, update, delete on table public.pair_inventory from authenticated;
revoke insert, update, delete on table public.pair_inventory_events from authenticated;
revoke update, delete on table public.pair_inventory_events from service_role;

grant select on table public.order_groups to authenticated;
grant select on table public.order_legs to authenticated;
grant select on table public.pair_inventory to authenticated;
grant select on table public.pair_inventory_events to authenticated;
grant all on table public.order_groups to service_role;
grant all on table public.order_legs to service_role;
grant all on table public.pair_inventory to service_role;
grant insert, select on table public.pair_inventory_events to service_role;
grant usage, select on sequence public.pair_inventory_events_id_seq to service_role;

commit;
