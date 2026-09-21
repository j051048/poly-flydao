"""Credential domain types: enums, models, errors, and envelope protocols.

Split out of :mod:`polybot.credentials` so the encryption primitives and the
persistence layer can import the contracts without importing each other.
``polybot.credentials`` re-exports every name here.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CredentialConfigurationError(RuntimeError):
    """Credential encryption or persistence is unavailable; fail closed."""


class CredentialConflictError(RuntimeError):
    """A credential operation conflicts with its current lifecycle state."""


class CredentialNotFoundError(LookupError):
    """The requested tenant-owned credential or wallet does not exist."""


class SecretKind(StrEnum):
    AI_API_KEY = "ai_api_key"
    EVM_SIGNER_KEY = "evm_signer_key"


class AIProvider(StrEnum):
    PLATFORM = "platform"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"
    LITELLM = "litellm"
    CUSTOM = "custom"
    MOCK = "mock"


class EncryptedEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    algorithm: str
    key_version: int
    aad_version: int
    nonce: str
    ciphertext: str
    encrypted_data_key: str | None = None


class CredentialMetadata(BaseModel):
    id: UUID
    kind: SecretKind
    provider: str
    label: str | None = None
    status: str
    fingerprint: str
    last_four: str
    version: int
    created_at: datetime
    rotated_at: datetime | None = None
    revoked_at: datetime | None = None


class TradingWalletMetadata(BaseModel):
    id: UUID
    label: str | None = None
    owner_address: str | None = None
    deposit_wallet_address: str | None = None
    signer_address: str | None = None
    chain_id: int | None = None
    collateral_token: str | None = None
    signature_type: int = 3
    status: str
    signer_credential_id: UUID | None = None
    collateral_balance_pusd: Decimal | None = None
    allowances_ready: bool = False
    readiness_checked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CredentialStatus(BaseModel):
    ai_credentials: list[CredentialMetadata]
    wallets: list[TradingWalletMetadata]


class WorkerCredential(BaseModel):
    """Encrypted worker input; never used as an API response model."""

    id: UUID
    account_id: UUID
    kind: SecretKind
    provider: str
    status: str
    version: int
    envelope: EncryptedEnvelope


class WalletLifecycleClaim(BaseModel):
    """Worker-only wallet verification/revocation lease."""

    id: UUID
    account_id: UUID
    status: str
    signer_credential_id: UUID | None = None
    signature_type: int
    lifecycle_claimed_by: str
    lifecycle_fencing_token: int
    lifecycle_lease_expires_at: datetime
    lifecycle_attempt_count: int


class EnvelopeEncryptor(Protocol):
    def encrypt(
        self,
        plaintext: bytes,
        *,
        account_id: str,
        kind: SecretKind,
        provider: str,
        version: int,
    ) -> EncryptedEnvelope: ...


class EnvelopeDecryptor(Protocol):
    def decrypt(
        self,
        envelope: EncryptedEnvelope,
        *,
        account_id: str,
        kind: SecretKind,
        provider: str,
    ) -> bytes: ...
