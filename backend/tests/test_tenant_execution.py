from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from polybot.ai_endpoint import UnsafeAIBaseURLError
from polybot.config import Settings, TradingMode
from polybot.credentials import (
    AIProvider,
    EncryptedEnvelope,
    SecretKind,
    WorkerCredential,
)
from polybot.jobs import (
    CycleJob,
    CycleJobStatus,
    RiskPolicySnapshot,
    RuntimeProfile,
)
from polybot.tenant_execution import TenantRuntimeExecutor
from polybot.tenant_worker import JobLeaseGuard, TenantJobExecutionError


class FakeJobs:
    def __init__(self, profile: RuntimeProfile, risk: RiskPolicySnapshot):
        self.profile = profile
        self.risk = risk

    async def get_or_create_profile(self, account_id: str):
        assert account_id == str(self.profile.account_id)
        return self.profile

    async def get_risk_policy(
        self,
        *,
        account_id: str,
        risk_policy_id,
        expected_version: int,
    ):
        if (
            account_id != str(self.risk.account_id)
            or risk_policy_id != self.risk.id
            or expected_version != self.risk.version
        ):
            return None
        return self.risk

    async def get_worker_wallet(self, *, account_id: str, wallet_id):
        return None


class FakeCredentials:
    def __init__(self, credential: WorkerCredential | None):
        self.credential = credential

    async def get_envelope_for_worker(
        self,
        *,
        account_id: str,
        credential_id,
        kind: SecretKind,
    ):
        record = self.credential
        if (
            record is None
            or str(record.account_id) != account_id
            or record.id != credential_id
            or record.kind is not kind
        ):
            return None
        return record


class FakeDecryptor:
    def __init__(self, plaintext: bytes):
        self.plaintext = plaintext
        self.calls = 0

    def decrypt(self, envelope, *, account_id, kind, provider):
        self.calls += 1
        return self.plaintext


class FakeStores:
    def __init__(self):
        self.accounts: list[str] = []

    def for_account(self, account_id: str):
        self.accounts.append(account_id)
        return object()


def _fixture():
    now = datetime.now(UTC)
    account_id = uuid4()
    credential_id = uuid4()
    risk_id = uuid4()
    profile = RuntimeProfile(
        account_id=account_id,
        ai_provider=AIProvider.OPENAI,
        forecast_model="gpt-5-mini",
        ai_credential_id=credential_id,
        risk_policy_id=risk_id,
        desired_mode=TradingMode.PAPER,
        created_at=now,
        updated_at=now,
    )
    risk = RiskPolicySnapshot(
        id=risk_id,
        account_id=account_id,
        version=3,
        status="active",
        max_order_usd=Decimal("2.50"),
        max_trade_risk_pct=Decimal("0.002"),
        max_event_exposure_pct=Decimal("0.01"),
        max_bucket_exposure_pct=Decimal("0.03"),
        max_gross_exposure_pct=Decimal("0.05"),
        daily_loss_limit_pct=Decimal("0.01"),
        max_drawdown_pct=Decimal("0.04"),
        min_edge=Decimal("0.06"),
        created_at=now,
    )
    credential = WorkerCredential(
        id=credential_id,
        account_id=account_id,
        kind=SecretKind.AI_API_KEY,
        provider="openai",
        status="active",
        version=1,
        envelope=EncryptedEnvelope(
            algorithm="test",
            key_version=1,
            aad_version=1,
            nonce="nonce",
            ciphertext="ciphertext",
        ),
    )
    job = CycleJob(
        id=uuid4(),
        account_id=account_id,
        ai_credential_id=credential_id,
        risk_policy_id=risk_id,
        risk_policy_version=risk.version,
        mode=TradingMode.PAPER,
        idempotency_key="12345678-1234-1234-1234-123456789012",
        status=CycleJobStatus.RUNNING,
        claimed_by="worker-test",
        fencing_token=1,
        run_after=now,
        created_at=now,
        updated_at=now,
    )
    return profile, risk, credential, job


async def test_paper_job_builds_an_account_bound_validated_runtime(monkeypatch) -> None:
    profile, risk, credential, job = _fixture()
    jobs = FakeJobs(profile, risk)
    decryptor = FakeDecryptor(b"sk-tenant-only")
    stores = FakeStores()
    captured = {}

    class Engine:
        execution_guard = None

        async def run_cycle(self):
            assert self.execution_guard is not None
            assert await self.execution_guard()

    class Runtime:
        engine = Engine()
        closed = False

        async def close(self):
            self.closed = True

    runtime = Runtime()

    def fake_build(settings, *, store_override):
        captured["settings"] = settings
        captured["store"] = store_override
        return runtime

    monkeypatch.setattr("polybot.tenant_execution.build_runtime", fake_build)
    executor = TenantRuntimeExecutor(
        base_settings=Settings(
            _env_file=None,
            component="worker",
            worker_execution_model="tenant_queue",
        ),
        jobs=jobs,
        credentials=FakeCredentials(credential),
        decryptor=decryptor,
        stores=stores,
    )

    await executor(job, JobLeaseGuard(job))

    settings = captured["settings"]
    assert settings.account_id == str(profile.account_id)
    assert settings.max_order_usd == Decimal("2.50")
    assert settings.min_edge == Decimal("0.06")
    assert settings.forecast_model == "gpt-5-mini"
    assert settings.critic_model == "gpt-5-mini"
    assert settings.openai_api_key.get_secret_value() == "sk-tenant-only"
    assert stores.accounts == [str(profile.account_id)]
    assert decryptor.calls == 1
    assert runtime.closed


async def test_job_rejects_cross_tenant_or_inactive_credential(monkeypatch) -> None:
    profile, risk, _, job = _fixture()
    decryptor = FakeDecryptor(b"must-not-be-used")
    executor = TenantRuntimeExecutor(
        base_settings=Settings(_env_file=None, component="worker"),
        jobs=FakeJobs(profile, risk),
        credentials=FakeCredentials(None),
        decryptor=decryptor,
        stores=FakeStores(),
    )

    with pytest.raises(TenantJobExecutionError, match="ai_credential_inactive"):
        await executor(job, JobLeaseGuard(job))

    assert decryptor.calls == 0


async def test_profile_reference_change_fails_before_secret_decryption() -> None:
    profile, risk, credential, job = _fixture()
    changed = profile.model_copy(update={"ai_credential_id": uuid4(), "version": 2})
    decryptor = FakeDecryptor(b"must-not-be-used")
    executor = TenantRuntimeExecutor(
        base_settings=Settings(_env_file=None, component="worker"),
        jobs=FakeJobs(changed, risk),
        credentials=FakeCredentials(credential),
        decryptor=decryptor,
        stores=FakeStores(),
    )

    with pytest.raises(TenantJobExecutionError, match="runtime_profile_changed"):
        await executor(job, JobLeaseGuard(job))

    assert decryptor.calls == 0


async def test_custom_provider_rejects_unsafe_endpoint_before_secret_decryption(
    monkeypatch,
) -> None:
    profile, risk, credential, job = _fixture()
    profile = profile.model_copy(
        update={
            "ai_provider": AIProvider.CUSTOM,
            "ai_base_url": "https://relay.example.com/v1",
        }
    )
    credential = credential.model_copy(update={"provider": "custom"})
    decryptor = FakeDecryptor(b"must-not-be-used")

    async def reject_endpoint(*args, **kwargs):
        del args, kwargs
        raise UnsafeAIBaseURLError("private endpoint")

    monkeypatch.setattr(
        "polybot.tenant_execution.validate_public_ai_base_url",
        reject_endpoint,
    )
    executor = TenantRuntimeExecutor(
        base_settings=Settings(_env_file=None, component="worker"),
        jobs=FakeJobs(profile, risk),
        credentials=FakeCredentials(credential),
        decryptor=decryptor,
        stores=FakeStores(),
    )

    with pytest.raises(TenantJobExecutionError, match="custom_provider_endpoint_unsafe"):
        await executor(job, JobLeaseGuard(job))

    assert decryptor.calls == 0


def test_tenant_runtime_maps_only_custom_provider_to_hardened_adapter() -> None:
    profile, risk, credential, job = _fixture()
    executor = TenantRuntimeExecutor(
        base_settings=Settings(_env_file=None, component="worker"),
        jobs=FakeJobs(profile, risk),
        credentials=FakeCredentials(credential),
        decryptor=FakeDecryptor(b"unused"),
        stores=FakeStores(),
    )
    shared = {
        "job": job,
        "model": "relay-model",
        "ai_secret": bytearray(b"tenant-api-key"),
        "signer_secret": None,
        "wallet_address": None,
        "risk": risk,
    }

    custom = executor._job_settings(
        **shared,
        provider=AIProvider.CUSTOM,
        ai_base_url="https://relay.example.com/v1",
    )
    openrouter = executor._job_settings(
        **shared,
        provider=AIProvider.OPENROUTER,
        ai_base_url=None,
    )

    assert custom.ai_provider == "openai_compatible"
    assert custom.litellm_base_url == "https://relay.example.com/v1"
    assert custom.forecast_model == custom.critic_model == "relay-model"
    assert openrouter.ai_provider == "litellm"
    assert openrouter.litellm_base_url == "https://openrouter.ai/api/v1"
    assert openrouter.forecast_model == openrouter.critic_model == "relay-model"
