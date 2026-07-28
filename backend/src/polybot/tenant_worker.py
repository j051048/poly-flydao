from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field

from polybot.jobs import CycleJob, WorkerJobRepository

_ERROR_CODE = re.compile(r"[^a-z0-9_]+")


class TenantJobExecutionError(RuntimeError):
    """A classified failure whose retry policy is safe to persist."""

    def __init__(self, error_code: str, *, retryable: bool):
        super().__init__(error_code)
        self.error_code = _safe_error_code(error_code)
        self.retryable = retryable


@dataclass
class JobLeaseGuard:
    """In-memory half of the database fencing lease.

    Order submission must call :meth:`execution_allowed` immediately before
    signing and again before posting. A false heartbeat permanently invalidates
    this object, so an old worker cannot become valid again after another
    replica has reclaimed the job with a newer fencing token.
    """

    job: CycleJob
    _valid: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _invalidated: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def __post_init__(self) -> None:
        self._valid.set()

    @property
    def is_valid(self) -> bool:
        return self._valid.is_set()

    async def execution_allowed(self) -> bool:
        return self.is_valid

    def invalidate(self) -> None:
        self._valid.clear()
        self._invalidated.set()

    async def wait_invalidated(self) -> None:
        await self._invalidated.wait()


TenantJobExecutor = Callable[[CycleJob, JobLeaseGuard], Awaitable[None]]
DueJobScheduler = Callable[[], Awaitable[int]]


class TenantJobRunner:
    """Bounded, fenced cycle-job dispatcher for a multi-tenant worker."""

    def __init__(
        self,
        *,
        repository: WorkerJobRepository,
        executor: TenantJobExecutor,
        owner_id: str,
        max_concurrency: int = 4,
        lease_seconds: int = 60,
        poll_interval_seconds: float = 1.0,
        heartbeat_interval_seconds: float | None = None,
        schedule_due_jobs: DueJobScheduler | None = None,
        logger: logging.Logger | None = None,
    ):
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        if not 1 <= max_concurrency <= 32:
            raise ValueError("max_concurrency must be between 1 and 32")
        if not 10 <= lease_seconds <= 300:
            raise ValueError("lease_seconds must be between 10 and 300")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        heartbeat_interval = heartbeat_interval_seconds or lease_seconds / 3
        if heartbeat_interval <= 0 or heartbeat_interval >= lease_seconds:
            raise ValueError("heartbeat interval must be positive and shorter than the lease")

        self.repository = repository
        self.executor = executor
        self.owner_id = owner_id
        self.max_concurrency = max_concurrency
        self.lease_seconds = lease_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.heartbeat_interval_seconds = heartbeat_interval
        self.schedule_due_jobs = schedule_due_jobs
        self.logger = logger or logging.getLogger("polybot.tenant_worker")
        self._tasks: set[asyncio.Task[None]] = set()
        self._active_accounts: set[str] = set()

    async def serve(self, stop: asyncio.Event) -> None:
        """Claim and execute jobs until stopped, then run executor cleanup."""

        try:
            while not stop.is_set():
                self._discard_finished()
                if self.schedule_due_jobs is not None:
                    try:
                        await self.schedule_due_jobs()
                    except Exception:
                        self.logger.exception("automatic cycle scheduling failed")

                claimed_any = False
                while len(self._tasks) < self.max_concurrency and not stop.is_set():
                    job = await self.repository.claim_next_job(
                        claimed_by=self.owner_id,
                        lease_seconds=self.lease_seconds,
                    )
                    if job is None:
                        break
                    claimed_any = True
                    account_id = str(job.account_id)
                    if account_id in self._active_accounts:
                        await self.repository.fail(
                            account_id=account_id,
                            job_id=job.id,
                            claimed_by=self.owner_id,
                            fencing_token=job.fencing_token,
                            error_code="duplicate_account_claim",
                            retryable=True,
                        )
                        self.logger.error(
                            "repository returned concurrent jobs for one account; "
                            "the duplicate was fenced and requeued"
                        )
                        continue
                    self._active_accounts.add(account_id)
                    task = asyncio.create_task(
                        self._execute_claimed(job),
                        name=f"tenant-cycle-{job.id}",
                    )
                    self._tasks.add(task)

                if not claimed_any or len(self._tasks) >= self.max_concurrency:
                    await self._wait_for_progress(stop)
        finally:
            for task in self._tasks:
                task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
            self._active_accounts.clear()

    async def execute_claimed_for_test(self, job: CycleJob) -> None:
        """Exercise one already-claimed job without starting the poll loop."""

        account_id = str(job.account_id)
        if account_id in self._active_accounts:
            raise RuntimeError("account already active")
        self._active_accounts.add(account_id)
        await self._execute_claimed(job)

    async def _execute_claimed(self, job: CycleJob) -> None:
        account_id = str(job.account_id)
        lease = JobLeaseGuard(job)
        heartbeat_task: asyncio.Task[None] | None = None
        try:
            running = await self.repository.mark_running(
                account_id=account_id,
                job_id=job.id,
                claimed_by=self.owner_id,
                fencing_token=job.fencing_token,
            )
            if running is None:
                lease.invalidate()
                self.logger.warning("cycle job lost its fence before execution")
                return

            lease.job = running
            heartbeat_task = asyncio.create_task(
                self._maintain_heartbeat(lease),
                name=f"tenant-cycle-heartbeat-{job.id}",
            )
            await self.executor(running, lease)
            if not lease.is_valid:
                self.logger.critical(
                    "cycle execution returned after its fencing lease was lost; "
                    "completion was deliberately not persisted"
                )
                return
            completed = await self.repository.complete(
                account_id=account_id,
                job_id=job.id,
                claimed_by=self.owner_id,
                fencing_token=job.fencing_token,
            )
            if completed is None:
                lease.invalidate()
                self.logger.critical("cycle completion rejected by fencing token")
        except asyncio.CancelledError:
            lease.invalidate()
            raise
        except TenantJobExecutionError as exc:
            await self._record_failure(job, lease, exc.error_code, exc.retryable)
        except Exception as exc:
            self.logger.exception("tenant cycle execution failed")
            error_code = _safe_error_code(f"execution_{type(exc).__name__}")
            await self._record_failure(job, lease, error_code, True)
        finally:
            lease.invalidate()
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
            self._active_accounts.discard(account_id)

    async def _record_failure(
        self,
        job: CycleJob,
        lease: JobLeaseGuard,
        error_code: str,
        retryable: bool,
    ) -> None:
        if not lease.is_valid:
            return
        saved = await self.repository.fail(
            account_id=str(job.account_id),
            job_id=job.id,
            claimed_by=self.owner_id,
            fencing_token=job.fencing_token,
            error_code=_safe_error_code(error_code),
            retryable=retryable,
        )
        if saved is None:
            lease.invalidate()
            self.logger.critical("cycle failure transition rejected by fencing token")

    async def _maintain_heartbeat(self, lease: JobLeaseGuard) -> None:
        while lease.is_valid:
            await asyncio.sleep(self.heartbeat_interval_seconds)
            if not lease.is_valid:
                return
            try:
                healthy = await self.repository.heartbeat(
                    account_id=str(lease.job.account_id),
                    job_id=lease.job.id,
                    claimed_by=self.owner_id,
                    fencing_token=lease.job.fencing_token,
                    lease_seconds=self.lease_seconds,
                )
            except Exception:
                healthy = False
                self.logger.exception("cycle job heartbeat failed")
            if not healthy:
                lease.invalidate()
                self.logger.critical("cycle job fencing lease was lost")
                return

    async def _wait_for_progress(self, stop: asyncio.Event) -> None:
        waiters: set[asyncio.Task[object]] = {
            asyncio.create_task(stop.wait(), name="tenant-worker-stop-wait")
        }
        if self._tasks:
            waiters.update(self._tasks)
        done, pending = await asyncio.wait(
            waiters,
            timeout=self.poll_interval_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for waiter in pending:
            if waiter not in self._tasks:
                waiter.cancel()
        for waiter in done:
            if waiter not in self._tasks:
                with suppress(asyncio.CancelledError):
                    await waiter
        self._discard_finished()

    def _discard_finished(self) -> None:
        finished = {task for task in self._tasks if task.done()}
        self._tasks.difference_update(finished)
        for task in finished:
            with suppress(asyncio.CancelledError):
                error = task.exception()
                if error is not None:
                    self.logger.error(
                        "tenant cycle task terminated unexpectedly",
                        exc_info=(type(error), error, error.__traceback__),
                    )


def _safe_error_code(value: str) -> str:
    normalized = _ERROR_CODE.sub("_", value.lower()).strip("_")
    return (normalized or "unknown_error")[:64]
