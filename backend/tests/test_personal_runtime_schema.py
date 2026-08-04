from pathlib import Path


def test_personal_runtime_migration_is_fenced_paused_and_service_role_only() -> None:
    sql = Path("supabase/migrations/0016_personal_runtime.sql").read_text(encoding="utf-8").lower()

    for required in (
        "create table public.personal_runtime_bindings",
        "paused boolean not null default false",
        "create or replace function public.bind_personal_runtime_wallet",
        "create or replace function public.record_personal_wallet_readiness",
        "create or replace function public.set_personal_runtime_paused",
        "create or replace function public.resume_personal_runtime",
        "create or replace function public.arm_personal_runtime_control",
        "create or replace function public.mark_personal_order_submitting",
        "create or replace function public.load_personal_paper_account_state",
        "create or replace function public.save_personal_paper_account_state",
        "alter table public.personal_runtime_bindings enable row level security",
        "alter table public.personal_runtime_bindings force row level security",
        "from public, anon, authenticated, service_role;",
        "to service_role;",
        "notify pgrst, 'reload schema';",
        "select 16;",
    ):
        assert required in sql

    arm = sql.split("create or replace function public.arm_personal_runtime_control", 1)[1].split(
        "create or replace function public.set_personal_runtime_paused", 1
    )[0]
    for required in (
        "and not binding.paused",
        "lease.owner_id = p_owner_id",
        "lease.fencing_token = p_fencing_token",
        "lease.expires_at > now()",
        "control.version = p_expected_version",
        "not control.cancellation_pending",
        "control.armed_until > now()",
    ):
        assert required in arm
    for deferred_submission_check in (
        "binding.collateral_balance_pusd",
        "binding.allowances_ready",
        "binding.readiness_checked_at",
        "binding.readiness_owner_id",
        "binding.readiness_fencing_token",
    ):
        assert deferred_submission_check not in arm

    submit = sql.split("create or replace function public.mark_personal_order_submitting", 1)[
        1
    ].split("create or replace function public.enforce_submitting_wallet_readiness", 1)[0]
    for required in (
        "lease.owner_id = p_owner_id",
        "lease.fencing_token = p_worker_fencing_token",
        "lease.expires_at > now()",
        "control.version = p_control_version",
        "control.mode = p_mode",
        "not control.kill_switch",
        "not control.cancellation_pending",
        "not binding.paused",
        "binding.collateral_balance_pusd > 0",
        "binding.allowances_ready",
        "binding.readiness_owner_id = p_owner_id",
        "binding.readiness_fencing_token = p_worker_fencing_token",
    ):
        assert required in submit

    pause = sql.split("create or replace function public.set_personal_runtime_paused", 1)[1].split(
        "create or replace function public.resume_personal_runtime", 1
    )[0]
    assert "kill_switch = true" in pause
    assert "accept_new_intents = false" in pause
    assert "cancellation_pending = control.mode in ('canary', 'live')" in pause
    assert "control.cancellation_pending is distinct from" in pause
    assert "version = control.version + 1" in pause

    resume = sql.split("create or replace function public.resume_personal_runtime", 1)[1].split(
        "create or replace function public.mark_personal_order_submitting", 1
    )[0]
    assert "hashtextextended(p_account_id::text || ':personal-runtime', 0)" in resume
    assert "control.version = p_expected_version" in resume
    assert "set paused = false" in resume
    assert "armed = true" not in resume
    assert "accept_new_intents = true" not in resume
    assert "public.resume_personal_runtime" in sql.split("from public, anon, authenticated", 1)[0]


def test_personal_trigger_preserves_tenant_wallet_readiness_branch() -> None:
    sql = Path("supabase/migrations/0016_personal_runtime.sql").read_text(encoding="utf-8").lower()
    trigger = sql.split("create or replace function public.enforce_submitting_wallet_readiness", 1)[
        1
    ].split("create or replace function public.load_personal_paper_account_state", 1)[0]

    assert "new.response_payload ->> 'submission_scope'" in trigger
    assert "from public.account_runtime_profiles as profile" in trigger
    assert "join public.trading_wallets as wallet" in trigger
    assert "wallet.collateral_balance_pusd > 0" in trigger
    assert "wallet.allowances_ready" in trigger


def test_personal_paper_state_requires_the_active_worker_lease() -> None:
    sql = Path("supabase/migrations/0016_personal_runtime.sql").read_text(encoding="utf-8").lower()
    paper = sql.split("create or replace function public.load_personal_paper_account_state", 1)[
        1
    ].split("create or replace function public.polybot_schema_version", 1)[0]

    assert paper.count("from public.worker_leases as lease") == 2
    assert paper.count("lease.owner_id = p_owner_id") == 2
    assert paper.count("lease.fencing_token = p_fencing_token") == 2
    assert paper.count("lease.expires_at > now()") == 2
    assert "length(p_state::text) > 524288" in paper
