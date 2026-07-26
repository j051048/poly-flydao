begin;

-- Exchange-side GTD expiry is a second line of defence if the worker is
-- offline when a short-lived runtime arm expires.
alter table public.orders
  add column if not exists expires_at timestamptz;

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'orders_gtd_has_expiry'
      and conrelid = 'public.orders'::regclass
  ) then
    alter table public.orders
      add constraint orders_gtd_has_expiry
      check (order_type <> 'GTD' or expires_at is not null);
  end if;
end
$$;

create index if not exists orders_account_expiry_idx
  on public.orders (account_id, expires_at)
  where status in ('signed', 'submitting', 'submitted', 'live', 'partially_filled')
    and expires_at is not null;

-- The orders table deliberately uses a column-level authenticated grant so
-- encrypted payloads and raw exchange responses stay server-only. Extend that
-- safe projection with the non-secret expiry metadata added above.
grant select (expires_at) on public.orders to authenticated;

commit;
