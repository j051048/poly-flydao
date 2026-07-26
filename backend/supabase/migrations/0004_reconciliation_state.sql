-- Durable evidence for orders that disappear from the authenticated open set.
-- A single snapshot miss is ambiguous (fill, cancel, or eventual consistency);
-- the reconciler confirms the order id directly and repairs the full trade
-- history before incrementing this counter.

alter table public.orders
  add column if not exists open_snapshot_miss_count integer not null default 0,
  add column if not exists open_snapshot_missing_since timestamptz;

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'orders_open_snapshot_miss_count_nonnegative'
      and conrelid = 'public.orders'::regclass
  ) then
    alter table public.orders
      add constraint orders_open_snapshot_miss_count_nonnegative
      check (open_snapshot_miss_count >= 0);
  end if;
end
$$;

create index if not exists orders_account_missing_reconcile_idx
  on public.orders (account_id, open_snapshot_miss_count, updated_at desc)
  where status in ('submitted', 'live', 'partially_filled', 'unknown', 'cancel_pending')
    and clob_order_id is not null;
