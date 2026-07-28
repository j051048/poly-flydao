import re
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


def test_secure_multitenant_migration_is_fenced_and_service_role_only() -> None:
    sql = (
        Path("supabase/migrations/0007_secure_multitenant.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    for table in (
        "account_runtime_profiles",
        "credential_refs",
        "trading_wallets",
        "risk_policies",
        "cycle_jobs",
        "credential_audit_events",
    ):
        assert f"create table public.{table}" in sql
        assert f"alter table public.{table} enable row level security" in sql
        assert f"alter table public.{table} force row level security" in sql

    assert "rsa-oaep-256+a256gcm" in sql
    assert "unique (account_id, id)" in sql
    assert "risk_policy_version bigint" in sql
    assert "risk_policies_immutable" in sql
    assert "max_bucket_exposure_pct" in sql
    assert "cycle_jobs_one_active_per_account_uidx" in sql
    assert "for update skip locked" in sql
    assert "validate_cycle_job_lease" in sql
    assert "get_wallet_lifecycle_envelope" in sql
    assert "complete_trading_wallet_verification" in sql
    assert "complete_trading_wallet_revocation" in sql
    assert "'live'," in sql and "'cancel_pending'" in sql
    assert "grant select, insert, update on table" not in sql
    assert "from service_role;" in sql
    assert "to service_role;" in sql

    credential_grant = sql.split(
        "-- authenticated dashboards receive metadata only.", 1
    )[1].split(") on public.credential_refs to authenticated;", 1)[0]
    assert "fingerprint" not in credential_grant
    assert "last_four" not in credential_grant

    function_segments = sql.split("create or replace function public.")[1:]
    security_definers: set[str] = set()
    for segment in function_segments:
        name = re.match(r"([a-z0-9_]+)", segment)
        assert name is not None
        if "security definer" in segment.split(
            "create or replace function public.", 1
        )[0]:
            security_definers.add(name.group(1))
    assert security_definers
    for function in security_definers:
        assert f"revoke all on function public.{function}" in sql
        assert f"grant execute on function public.{function}" in sql


def test_p2_migration_persists_non_atomic_groups_and_pair_inventory() -> None:
    sql = (
        Path("supabase/migrations/0008_order_groups_pair_inventory.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    for table in (
        "order_groups",
        "order_legs",
        "pair_inventory",
        "pair_inventory_events",
    ):
        assert f"create table public.{table}" in sql
        assert f"alter table public.{table} enable row level security" in sql
    assert "paired_size = least(yes_size, no_size)" in sql
    assert "not (directional_yes_size > 0 and directional_no_size > 0)" in sql
    assert "order_groups_research_gate" in sql
    assert "not execution_enabled or not research_only" in sql
    assert "order_legs_maker_post_only" in sql
    assert "unique index pair_inventory_events_trade_uidx" in sql
    assert "foreign key (trading_wallet_id, account_id)" in sql


def test_p2_transaction_rpcs_are_service_role_only_and_append_only() -> None:
    sql = (
        Path("supabase/migrations/0009_pair_execution_rpcs.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    for table in (
        "order_groups",
        "order_legs",
        "pair_inventory",
        "pair_inventory_events",
    ):
        assert f"alter table public.{table} force row level security" in sql
        assert f"revoke all privileges on table public.{table}" in sql
    for function in (
        "persist_pair_order_plan",
        "commit_pair_group_transition",
        "commit_pair_fill",
    ):
        assert f"create or replace function public.{function}" in sql
        assert "security definer" in sql
        assert f"grant execute on function public.{function}" in sql
    assert "set search_path = ''" in sql
    assert "for update" in sql
    assert "on conflict (account_id, clob_trade_id) do nothing" in sql
    assert "return 'duplicate'" in sql
    assert "return 'conflict'" in sql
    assert "pair_inventory_events_append_only" in sql
    assert "revoke update, delete, truncate, references, trigger" in sql
    assert "from public, anon, authenticated, service_role" in sql
    assert "grant select, insert" not in sql
    assert "grant all on table public.order_" not in sql
    assert "grant usage, select on sequence public.pair_inventory_events" not in sql


def test_atomic_tenant_submission_gate_checks_every_live_authority() -> None:
    sql = (
        Path("supabase/migrations/0010_atomic_tenant_submission_gate.sql")
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "create or replace function public.mark_tenant_order_submitting" in sql
    assert "security definer" in sql
    assert "set search_path = ''" in sql
    for required in (
        "worker_lease.expires_at > now()",
        "job.lease_expires_at > now()",
        "job.status = 'running'",
        "control.version = p_control_version",
        "control.armed_until > now()",
        "not control.kill_switch",
        "not control.cancellation_pending",
        "profile.version = p_profile_version",
        "profile.status = 'active'",
        "risk.version = p_risk_policy_version",
        "risk.status = 'active'",
        "wallet.status = 'active'",
        "ai_credential.status = 'active'",
        "signer_credential.status = 'active'",
    ):
        assert required in sql
    assert "revoke all on function public.mark_tenant_order_submitting" in sql
    assert ") from public, anon, authenticated;" in sql
    assert "grant execute on function public.mark_tenant_order_submitting" in sql
    assert ") to service_role;" in sql
