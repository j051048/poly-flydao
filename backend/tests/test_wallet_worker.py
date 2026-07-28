from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from polybot.credentials import (
    EncryptedEnvelope,
    SecretKind,
    WalletLifecycleClaim,
    WorkerCredential,
)
from polybot.wallet_worker import WalletLifecycleWorker


class Items:
    def __init__(self, values):
        self.values = values

    def iter_items(self):
        yield from self.values


class Client:
    def __init__(self, open_orders=()):
        self.signer = "0x" + "11" * 20
        self.wallet = "0x" + "22" * 20
        self.open_orders = list(open_orders)
        self.approved = False
        self.cancelled = False
        self.closed = False

    def setup_trading_approvals(self):
        self.approved = True

    def cancel_all(self):
        self.cancelled = True

    def list_open_orders(self):
        return Items(self.open_orders)

    def close(self):
        self.closed = True


class Decryptor:
    def __init__(self, plaintext: bytes):
        self.plaintext = plaintext

    def decrypt(self, envelope, *, account_id, kind, provider):
        return self.plaintext


class Repository:
    def __init__(self, credential):
        self.credential = credential
        self.verified = None
        self.revoked = False
        self.failed = None

    async def get_pending_envelope_for_worker(self, **kwargs):
        return self.credential

    async def get_revocation_envelope_for_worker(self, **kwargs):
        return self.credential

    async def heartbeat_wallet_lifecycle(self, **kwargs):
        return True

    async def complete_wallet_verification(self, **kwargs):
        self.verified = kwargs
        return SimpleNamespace()

    async def fail_wallet_verification(self, **kwargs):
        self.failed = kwargs
        return SimpleNamespace()

    async def complete_wallet_revocation(self, **kwargs):
        self.revoked = True
        return SimpleNamespace()


def _claim(status: str = "pending_verification"):
    now = datetime.now(UTC)
    account_id = uuid4()
    credential_id = uuid4()
    claim = WalletLifecycleClaim(
        id=uuid4(),
        account_id=account_id,
        status=status,
        signer_credential_id=credential_id,
        signature_type=3,
        lifecycle_claimed_by="wallet-worker",
        lifecycle_fencing_token=4,
        lifecycle_lease_expires_at=now + timedelta(seconds=60),
        lifecycle_attempt_count=1,
    )
    credential = WorkerCredential(
        id=credential_id,
        account_id=account_id,
        kind=SecretKind.EVM_SIGNER_KEY,
        provider="polymarket",
        status=(
            "pending_verification"
            if status == "pending_verification"
            else "revocation_pending"
        ),
        version=1,
        envelope=EncryptedEnvelope(
            algorithm="test",
            key_version=1,
            aad_version=1,
            nonce="nonce",
            ciphertext="ciphertext",
        ),
    )
    return claim, credential


async def test_wallet_verification_derives_addresses_before_funding_or_approvals() -> None:
    claim, credential = _claim()
    repository = Repository(credential)
    client = Client()
    worker = WalletLifecycleWorker(
        repository=repository,
        decryptor=Decryptor(bytes.fromhex("ab" * 32)),
        owner_id="wallet-worker",
        client_factory=lambda private_key: client,
    )

    await worker.process_claim(claim)

    assert not client.approved
    assert client.closed
    assert repository.verified["signer_address"] == client.signer
    assert repository.verified["deposit_wallet_address"] == client.wallet
    assert repository.failed is None


async def test_invalid_wallet_key_is_terminal_without_constructing_client() -> None:
    claim, credential = _claim()
    repository = Repository(credential)
    constructed = False

    def factory(private_key):
        nonlocal constructed
        constructed = True
        return Client()

    worker = WalletLifecycleWorker(
        repository=repository,
        decryptor=Decryptor(b"too-short"),
        owner_id="wallet-worker",
        client_factory=factory,
    )

    await worker.process_claim(claim)

    assert not constructed
    assert repository.failed["retryable"] is False
    assert repository.failed["error_code"] == "verification_valueerror"


async def test_revocation_cancels_and_verifies_zero_open_orders() -> None:
    claim, credential = _claim("revocation_pending")
    repository = Repository(credential)
    client = Client()
    worker = WalletLifecycleWorker(
        repository=repository,
        decryptor=Decryptor(bytes.fromhex("cd" * 32)),
        owner_id="wallet-worker",
        client_factory=lambda private_key: client,
    )

    await worker.process_claim(claim)

    assert client.cancelled
    assert client.closed
    assert repository.revoked


async def test_revocation_remains_pending_when_open_orders_remain() -> None:
    claim, credential = _claim("revocation_pending")
    repository = Repository(credential)
    client = Client(open_orders=[SimpleNamespace(id="still-open")])
    worker = WalletLifecycleWorker(
        repository=repository,
        decryptor=Decryptor(bytes.fromhex("ef" * 32)),
        owner_id="wallet-worker",
        client_factory=lambda private_key: client,
    )

    await worker.process_claim(claim)

    assert client.cancelled
    assert not repository.revoked
