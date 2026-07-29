from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from uuid import uuid4

import pytest

from polybot.config import TradingMode
from polybot.credentials import (
    AesGcmEnvelopeDecryptor,
    AesGcmEnvelopeEncryptor,
    AIProvider,
    CredentialService,
    EncryptedEnvelope,
    InMemoryCredentialRepository,
    SecretKind,
)
from polybot.jobs import (
    CycleJob,
    CycleJobStatus,
    InMemoryJobRepository,
    RuntimeProfilePatch,
    SupabaseJobRepository,
)
from polybot.models import utc_now

ACCOUNT = "11111111-1111-4111-8111-111111111111"


@pytest.mark.asyncio
async def test_envelope_rotation_has_independent_aad_and_hmac_fingerprint() -> None:
    repository = InMemoryCredentialRepository()
    encryption_key = b"e" * 32
    service = CredentialService(
        repository,
        AesGcmEnvelopeEncryptor(encryption_key),
        fingerprint_key=b"f" * 32,
    )
    first_secret = "sk-first-secret-value-0001"
    second_secret = "sk-second-secret-value-0002"
    first, second = await asyncio.gather(
        service.put_ai(
            account_id=ACCOUNT,
            provider=AIProvider.OPENAI,
            api_key=first_secret,
            label="first",
        ),
        service.put_ai(
            account_id=ACCOUNT,
            provider=AIProvider.OPENAI,
            api_key=second_secret,
            label="second",
        ),
    )
    assert {first.version, second.version} == {1, 2}

    decryptor = AesGcmEnvelopeDecryptor({1: encryption_key})
    plaintexts = set()
    aad_versions = set()
    for row in repository._credentials.values():
        envelope = row["envelope"]
        aad_versions.add(envelope["aad_version"])
        plaintext = decryptor.decrypt(
            EncryptedEnvelope.model_validate(envelope),
            account_id=ACCOUNT,
            kind=SecretKind.AI_API_KEY,
            provider="openai",
        ).decode()
        plaintexts.add(plaintext)
        assert row["fingerprint"] != hashlib.sha256(plaintext.encode()).hexdigest()
    assert plaintexts == {first_secret, second_secret}
    assert len(aad_versions) == 2


@pytest.mark.asyncio
async def test_import_verify_use_and_revoke_wallet_lifecycle_is_fenced() -> None:
    repository = InMemoryCredentialRepository()
    encryption_key = b"k" * 32
    service = CredentialService(
        repository,
        AesGcmEnvelopeEncryptor(encryption_key),
        fingerprint_key=b"h" * 32,
    )
    wallet = await service.import_wallet(
        account_id=ACCOUNT,
        private_key="0x" + ("ab" * 32),
        label="bot",
        owner_address=None,
        signature_type=3,
        idempotency_key="wallet:test:00000001",
    )
    assert wallet.status == "pending_verification"
    retry = await service.import_wallet(
        account_id=ACCOUNT,
        private_key="0x" + ("ab" * 32),
        label="bot retry",
        owner_address=None,
        signature_type=3,
        idempotency_key="wallet:test:00000001",
    )
    assert retry.id == wallet.id
    credential = repository._credentials[wallet.signer_credential_id]
    assert credential["last_four"] == "****"

    claim = await repository.claim_wallet_lifecycle(
        claimed_by="wallet-worker-1",
        lease_seconds=30,
    )
    assert claim is not None
    envelope_record = await repository.get_wallet_lifecycle_envelope(
        account_id=ACCOUNT,
        wallet_id=wallet.id,
        claimed_by=claim.lifecycle_claimed_by,
        fencing_token=claim.lifecycle_fencing_token,
    )
    assert envelope_record is not None
    decrypted = AesGcmEnvelopeDecryptor({1: encryption_key}).decrypt(
        envelope_record.envelope,
        account_id=ACCOUNT,
        kind=SecretKind.EVM_SIGNER_KEY,
        provider="polymarket",
    )
    assert decrypted == bytes.fromhex("ab" * 32)

    active = await repository.complete_wallet_verification(
        account_id=ACCOUNT,
        wallet_id=wallet.id,
        claimed_by=claim.lifecycle_claimed_by,
        fencing_token=claim.lifecycle_fencing_token,
        signer_address="0x" + ("1" * 40),
        deposit_wallet_address="0x" + ("2" * 40),
        chain_id=137,
        collateral_token="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    )
    assert active is not None and active.status == "active"
    assert (
        await repository.get_envelope_for_worker(
            account_id=ACCOUNT,
            credential_id=active.signer_credential_id,
            kind=SecretKind.EVM_SIGNER_KEY,
        )
        is not None
    )

    pending = await repository.request_wallet_revoke(
        account_id=ACCOUNT,
        wallet_id=wallet.id,
    )
    assert pending is not None and pending.status == "revocation_pending"
    revoke_claim = await repository.claim_wallet_lifecycle(
        claimed_by="wallet-worker-2",
        lease_seconds=30,
    )
    assert revoke_claim is not None
    revoke_envelope = await repository.get_wallet_lifecycle_envelope(
        account_id=ACCOUNT,
        wallet_id=wallet.id,
        claimed_by=revoke_claim.lifecycle_claimed_by,
        fencing_token=revoke_claim.lifecycle_fencing_token,
    )
    assert revoke_envelope is not None
    revoked = await repository.complete_wallet_revocation(
        account_id=ACCOUNT,
        wallet_id=wallet.id,
        claimed_by=revoke_claim.lifecycle_claimed_by,
        fencing_token=revoke_claim.lifecycle_fencing_token,
    )
    assert revoked is not None and revoked.status == "revoked"


@pytest.mark.asyncio
async def test_auto_scheduler_and_job_lease_fencing() -> None:
    repository = InMemoryJobRepository()
    profile = await repository.get_or_create_profile(ACCOUNT)
    await repository.update_profile(
        ACCOUNT,
        RuntimeProfilePatch(
            expected_version=profile.version,
            ai_provider=AIProvider.PLATFORM,
            forecast_model="gpt-5.6-terra",
            risk_policy_id=profile.risk_policy_id,
            desired_mode=TradingMode.PAPER,
            auto_run_enabled=True,
            cycle_interval_seconds=30,
        ),
    )
    assert await repository.enqueue_due_jobs() == 1
    claim = await repository.claim_next_job(
        claimed_by="cycle-worker-1",
        lease_seconds=30,
    )
    assert claim is not None
    assert claim.risk_policy_version == 1
    assert await repository.validate_lease(
        account_id=ACCOUNT,
        job_id=claim.id,
        claimed_by="cycle-worker-1",
        fencing_token=claim.fencing_token,
    )
    assert await repository.heartbeat(
        account_id=ACCOUNT,
        job_id=claim.id,
        claimed_by="cycle-worker-1",
        fencing_token=claim.fencing_token,
        lease_seconds=30,
    )
    assert not await repository.validate_lease(
        account_id=ACCOUNT,
        job_id=claim.id,
        claimed_by="cycle-worker-1",
        fencing_token=claim.fencing_token + 1,
    )


def test_custom_runtime_profile_requires_safe_https_base_url() -> None:
    common = {
        "expected_version": 1,
        "ai_provider": AIProvider.CUSTOM,
        "forecast_model": "relay-model",
    }
    profile = RuntimeProfilePatch(
        **common,
        ai_base_url="https://Relay.Example.com:443/v1/",
    )
    assert profile.ai_base_url == "https://relay.example.com/v1"

    with pytest.raises(ValueError, match="HTTPS"):
        RuntimeProfilePatch(**common, ai_base_url="http://relay.example.com/v1")

    with pytest.raises(ValueError, match="requires ai_base_url"):
        RuntimeProfilePatch(**common)


def test_non_custom_runtime_profile_rejects_base_url() -> None:
    with pytest.raises(ValueError, match="only allowed"):
        RuntimeProfilePatch(
            expected_version=1,
            ai_provider=AIProvider.OPENAI,
            ai_base_url="https://relay.example.com/v1",
            forecast_model="gpt-test",
        )


class _Response:
    def __init__(self, data: object):
        self.data = data


class _Builder:
    def __init__(self, data: object):
        self._data = data

    def execute(self) -> _Response:
        return _Response(self._data)


class _ScalarRPCClient:
    def rpc(self, name: str, payload: object) -> _Builder:
        del payload
        assert name in {"heartbeat_cycle_job", "validate_cycle_job_lease"}
        return _Builder(True)


@pytest.mark.asyncio
async def test_supabase_scalar_boolean_rpc_results_are_not_dropped() -> None:
    repository = SupabaseJobRepository(_ScalarRPCClient())
    job_id = uuid4()
    assert await repository.heartbeat(
        account_id=ACCOUNT,
        job_id=job_id,
        claimed_by="worker",
        fencing_token=1,
        lease_seconds=30,
    )
    assert await repository.validate_lease(
        account_id=ACCOUNT,
        job_id=job_id,
        claimed_by="worker",
        fencing_token=1,
    )


def test_cycle_job_preserves_risk_policy_version_from_database_row() -> None:
    now = utc_now()
    job = CycleJob.model_validate(
        {
            "id": uuid4(),
            "account_id": ACCOUNT,
            "risk_policy_id": uuid4(),
            "risk_policy_version": 7,
            "mode": "paper",
            "idempotency_key": "roundtrip:test:0001",
            "status": CycleJobStatus.QUEUED,
            "run_after": now,
            "created_at": now - timedelta(seconds=1),
            "updated_at": now,
        }
    )
    assert job.risk_policy_version == 7
