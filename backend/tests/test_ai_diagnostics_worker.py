from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from polybot.ai_diagnostics import AIDiagnosticWorker
from polybot.config import Settings
from polybot.credentials import AIProvider
from polybot.jobs import AIDiagnosticJob
from polybot.models import Forecast


class FakeJobs:
    def __init__(self) -> None:
        self.finished: list[tuple[bool, str]] = []

    async def consume_ai_budget(self, *, account_id: str, units: int) -> tuple[bool, int, int]:
        return True, 1, 100

    async def finish_ai_diagnostic(
        self,
        *,
        job: AIDiagnosticJob,
        ok: bool,
        result_summary: dict[str, object],
        error_code: str | None = None,
    ):
        self.finished.append((ok, error_code or ""))
        return job


class FakeCredentials:
    def __init__(self) -> None:
        self.envelope = None

    async def get_envelope_for_worker(self, **kwargs):
        return self.envelope


class FakeDecryptor:
    def decrypt(self, envelope, *, account_id: str, kind, provider):
        assert isinstance(envelope, bytes)
        return b"fake-api-key"


class FakeProvider:
    def __init__(self) -> None:
        self.closed = False

    async def forecast(self, request, *, model):
        return Forecast(
            market_id=request.market.id,
            probability_yes=Decimal("0.5"),
            probability_low=Decimal("0.4"),
            probability_high=Decimal("0.6"),
            confidence=Decimal("0.8"),
            model=model,
        )

    async def close(self) -> None:
        self.closed = True


def _job() -> AIDiagnosticJob:
    now = datetime.now(UTC)
    return AIDiagnosticJob(
        id=uuid4(),
        account_id=uuid4(),
        credential_id=uuid4(),
        provider=AIProvider.OPENAI,
        model="gpt-4o-mini",
        status="claimed",
        created_at=now,
        updated_at=now,
    )


async def test_process_success_finishes_ok(monkeypatch) -> None:
    jobs = FakeJobs()
    credentials = FakeCredentials()
    credentials.envelope = SimpleNamespace(provider="openai", envelope=b"envelope-bytes")
    worker = AIDiagnosticWorker(
        jobs=jobs,  # type: ignore[arg-type]
        credentials=credentials,  # type: ignore[arg-type]
        decryptor=FakeDecryptor(),
        settings=Settings(_env_file=None),
        owner_id="worker-1",
    )
    provider = FakeProvider()

    async def fake_provider(job, api_key):
        del api_key
        return provider

    monkeypatch.setattr(worker, "_provider", fake_provider)

    await worker.process(_job())

    assert jobs.finished == [(True, "")]
    assert provider.closed


async def test_process_budget_exhausted_finishes_failed(monkeypatch) -> None:
    class ExhaustedJobs(FakeJobs):
        async def consume_ai_budget(self, *, account_id: str, units: int):
            return False, 100, 100

    jobs = ExhaustedJobs()
    worker = AIDiagnosticWorker(
        jobs=jobs,  # type: ignore[arg-type]
        credentials=FakeCredentials(),  # type: ignore[arg-type]
        decryptor=FakeDecryptor(),
        settings=Settings(_env_file=None),
        owner_id="worker-1",
    )

    await worker.process(_job())

    assert jobs.finished == [(False, "daily_budget_exhausted")]
