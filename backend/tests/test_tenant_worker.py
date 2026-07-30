from __future__ import annotations

import asyncio
from uuid import uuid4

from polybot.config import TradingMode
from polybot.jobs import (
    CycleJobRequest,
    CycleJobStatus,
    InMemoryJobRepository,
)
from polybot.models import EngineCycleResult
from polybot.tenant_worker import (
    JobLeaseGuard,
    TenantJobExecutionError,
    TenantJobRunner,
)


async def _claimed_job(repository: InMemoryJobRepository, account_id: str):
    queued = await repository.enqueue(
        account_id=account_id,
        idempotency_key=str(uuid4()),
        request=CycleJobRequest(mode=TradingMode.PAPER),
    )
    claimed = await repository.claim_next_job(
        claimed_by="worker-test",
        lease_seconds=10,
        account_id=account_id,
    )
    assert claimed is not None
    assert claimed.id == queued.id
    return claimed


async def test_runner_completes_a_fenced_job() -> None:
    repository = InMemoryJobRepository()
    account_id = str(uuid4())
    claimed = await _claimed_job(repository, account_id)
    observed: list[tuple[str, bool]] = []

    async def execute(job, lease: JobLeaseGuard) -> EngineCycleResult:
        observed.append((str(job.account_id), await lease.execution_allowed()))
        return EngineCycleResult(
            run_id="paper-run-1",
            mode=TradingMode.PAPER,
            markets_scanned=12,
            forecasts_created=2,
            candidates_created=1,
            skipped={"no_positive_value_candidate": 1},
        )

    runner = TenantJobRunner(
        repository=repository,
        executor=execute,
        owner_id="worker-test",
        lease_seconds=10,
        heartbeat_interval_seconds=0.05,
    )
    await runner.execute_claimed_for_test(claimed)

    saved = await repository.get_job(account_id=account_id, job_id=claimed.id)
    assert saved is not None
    assert saved.status is CycleJobStatus.SUCCEEDED
    assert saved.result_summary == {
        "run_id": "paper-run-1",
        "mode": "paper",
        "markets_scanned": 12,
        "forecasts_created": 2,
        "candidates_created": 1,
        "intents_approved": 0,
        "executions": 0,
        "skipped": {"no_positive_value_candidate": 1},
        "started_at": saved.result_summary["started_at"],
        "completed_at": None,
    }
    assert observed == [(account_id, True)]


async def test_lost_heartbeat_fences_completion() -> None:
    class LostHeartbeatRepository(InMemoryJobRepository):
        async def heartbeat(self, **kwargs) -> bool:
            return False

    repository = LostHeartbeatRepository()
    account_id = str(uuid4())
    claimed = await _claimed_job(repository, account_id)

    async def execute(job, lease: JobLeaseGuard) -> None:
        await lease.wait_invalidated()
        assert not await lease.execution_allowed()

    runner = TenantJobRunner(
        repository=repository,
        executor=execute,
        owner_id="worker-test",
        lease_seconds=10,
        heartbeat_interval_seconds=0.01,
    )
    await runner.execute_claimed_for_test(claimed)

    saved = await repository.get_job(account_id=account_id, job_id=claimed.id)
    assert saved is not None
    assert saved.status is CycleJobStatus.RUNNING


async def test_classified_failure_is_safely_persisted() -> None:
    repository = InMemoryJobRepository()
    account_id = str(uuid4())
    claimed = await _claimed_job(repository, account_id)

    async def execute(job, lease: JobLeaseGuard) -> None:
        raise TenantJobExecutionError("Provider timed/out: secret=do-not-store", retryable=False)

    runner = TenantJobRunner(
        repository=repository,
        executor=execute,
        owner_id="worker-test",
        lease_seconds=10,
        heartbeat_interval_seconds=0.05,
    )
    await runner.execute_claimed_for_test(claimed)

    saved = await repository.get_job(account_id=account_id, job_id=claimed.id)
    assert saved is not None
    assert saved.status is CycleJobStatus.FAILED
    assert saved.error_code == "provider_timed_out_secret_do_not_store"


async def test_serve_runs_different_accounts_concurrently() -> None:
    repository = InMemoryJobRepository()
    account_ids = [str(uuid4()), str(uuid4())]
    for account_id in account_ids:
        await repository.enqueue(
            account_id=account_id,
            idempotency_key=str(uuid4()),
            request=CycleJobRequest(mode=TradingMode.PAPER),
        )

    both_started = asyncio.Event()
    release = asyncio.Event()
    started: set[str] = set()

    async def execute(job, lease: JobLeaseGuard) -> None:
        started.add(str(job.account_id))
        if len(started) == 2:
            both_started.set()
        await release.wait()

    runner = TenantJobRunner(
        repository=repository,
        executor=execute,
        owner_id="worker-test",
        max_concurrency=2,
        lease_seconds=10,
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=0.05,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runner.serve(stop))
    await asyncio.wait_for(both_started.wait(), timeout=1)
    release.set()

    for _ in range(100):
        jobs = [
            job
            for job in repository._jobs.values()
            if job.status is CycleJobStatus.SUCCEEDED
        ]
        if len(jobs) == 2:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert started == set(account_ids)
    assert all(
        job.status is CycleJobStatus.SUCCEEDED for job in repository._jobs.values()
    )
