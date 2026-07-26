from pathlib import Path


def test_migration_has_required_safety_invariants() -> None:
    sql = Path("supabase/migrations/0001_initial.sql").read_text(encoding="utf-8").lower()
    for table in (
        "markets",
        "snapshots",
        "evidence",
        "forecasts",
        "order_intents",
        "orders",
        "fills",
        "account_activities",
        "positions",
        "risk_events",
        "runtime_controls",
        "audit_events",
        "worker_leases",
        "account_risk_state",
    ):
        assert f"create table public.{table}" in sql
        assert f"alter table public.{table} enable row level security" in sql
    assert "intent_hash text not null unique" in sql
    assert "claim_worker_lease" in sql
    assert "mark_order_submitting" in sql
    assert "create or replace function public.arm_runtime_control" in sql
    assert "and control.version = p_expected_version" in sql
    assert "create or replace function public.disarm_runtime_control" in sql
    assert "create or replace function public.acknowledge_runtime_cancellation" in sql
    assert "cancellation_pending boolean not null" in sql
    assert "version = control.version + 1" in sql
    assert "grant execute on function public.arm_runtime_control" in sql
    assert "grant execute on function public.disarm_runtime_control" in sql
    assert "grant execute on function public.acknowledge_runtime_cancellation" in sql
    assert "worker_fencing_token = $3" in sql
    assert "day_start_equity_pusd numeric(38, 18) not null" in sql
    assert "risk_day date not null" in sql
    assert "create or replace function public.record_equity_state" in sql
    assert "then public.account_risk_state.latest_equity_pusd" in sql
    assert "grant execute on function public.record_equity_state" in sql
    assert "create unique index fills_clob_trade_id" not in sql
    assert "audit_events_append_only" in sql


def test_upgrade_migration_adds_daily_equity_state() -> None:
    sql = Path("supabase/migrations/0002_daily_equity_risk.sql").read_text(encoding="utf-8").lower()
    assert "add column if not exists day_start_equity_pusd" in sql
    assert "drop function if exists public.record_equity_peak" in sql
    assert "create or replace function public.record_equity_state" in sql
    assert "to service_role" in sql


def test_upgrade_migration_adds_account_activity_ledger() -> None:
    sql = (
        Path("supabase/migrations/0003_account_activity_ledger.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "create table if not exists public.account_activities" in sql
    assert "activity_type in ('redeem', 'split', 'merge', 'conversion')" in sql
    assert "enable row level security" in sql
    assert "revoke delete on table public.account_activities" in sql


def test_upgrade_migration_adds_durable_order_absence_evidence() -> None:
    sql = (
        Path("supabase/migrations/0004_reconciliation_state.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "add column if not exists open_snapshot_miss_count" in sql
    assert "add column if not exists open_snapshot_missing_since" in sql
    assert "orders_open_snapshot_miss_count_nonnegative" in sql
    assert "orders_account_missing_reconcile_idx" in sql


def test_upgrade_migration_makes_arm_expiry_atomic() -> None:
    sql = (
        Path("supabase/migrations/0005_runtime_control_expiry.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "create or replace function public.expire_runtime_control" in sql
    assert "control.armed_until <= now()" in sql
    assert "control.version = p_expected_version" in sql
    assert "cancellation_pending = true" in sql
    assert "p_armed_until <= now()" in sql
    assert "control.armed_until > now()" in sql
    assert "grant execute on function public.expire_runtime_control" in sql


def test_upgrade_migration_adds_exchange_side_order_expiry() -> None:
    sql = (
        Path("supabase/migrations/0006_order_expiry.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "add column if not exists expires_at" in sql
    assert "orders_gtd_has_expiry" in sql
    assert "order_type <> 'gtd' or expires_at is not null" in sql
    assert "orders_account_expiry_idx" in sql
    assert "grant select (expires_at) on public.orders to authenticated" in sql
