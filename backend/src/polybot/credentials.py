from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from postgrest.exceptions import APIError
from pydantic import BaseModel, ConfigDict

from polybot.models import utc_now

_EVM_PRIVATE_KEY = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SAFE_LABEL = re.compile(r"^[\w .:/@+-]{1,80}$", re.UNICODE)
_SAFE_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:@+-]{16,128}$")
_SECP256K1_ORDER = int(
    "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141",
    16,
)


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


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


def _aad(*, account_id: str, kind: SecretKind, provider: str, version: int) -> bytes:
    try:
        normalized_account = str(UUID(account_id))
    except ValueError as exc:
        raise ValueError("account_id must be a UUID") from exc
    return json.dumps(
        {
            "account_id": normalized_account,
            "kind": kind.value,
            "provider": provider,
            "schema": 1,
            "version": version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class AesGcmEnvelopeEncryptor:
    """Symmetric envelope writer for isolated API/worker deployments.

    The API only receives this encrypt-only object. A worker may separately
    construct :class:`AesGcmEnvelopeDecryptor`. Deployments that cannot keep
    those components separate must use the RSA public-key writer instead.
    """

    def __init__(self, master_key: bytes, *, key_version: int = 1):
        if len(master_key) != 32:
            raise ValueError("credential master key must contain exactly 32 bytes")
        if key_version < 1:
            raise ValueError("credential key version must be positive")
        self._aead = AESGCM(master_key)
        self._key_version = key_version

    def encrypt(
        self,
        plaintext: bytes,
        *,
        account_id: str,
        kind: SecretKind,
        provider: str,
        version: int,
    ) -> EncryptedEnvelope:
        nonce = os.urandom(12)
        associated_data = _aad(
            account_id=account_id,
            kind=kind,
            provider=provider,
            version=version,
        )
        ciphertext = self._aead.encrypt(nonce, plaintext, associated_data)
        return EncryptedEnvelope(
            algorithm="A256GCM",
            key_version=self._key_version,
            aad_version=version,
            nonce=_b64encode(nonce),
            ciphertext=_b64encode(ciphertext),
        )


class AesGcmEnvelopeDecryptor:
    def __init__(self, master_keys: Mapping[int, bytes]):
        if not master_keys:
            raise ValueError("at least one credential master key is required")
        self._aead = {
            version: AESGCM(key)
            for version, key in master_keys.items()
            if version > 0 and len(key) == 32
        }
        if len(self._aead) != len(master_keys):
            raise ValueError("credential keyring contains an invalid key")

    def decrypt(
        self,
        envelope: EncryptedEnvelope,
        *,
        account_id: str,
        kind: SecretKind,
        provider: str,
    ) -> bytes:
        if envelope.algorithm != "A256GCM" or envelope.encrypted_data_key is not None:
            raise CredentialConfigurationError("unsupported credential envelope")
        try:
            aead = self._aead[envelope.key_version]
        except KeyError as exc:
            raise CredentialConfigurationError("credential key version is unavailable") from exc
        return aead.decrypt(
            _b64decode(envelope.nonce),
            _b64decode(envelope.ciphertext),
            _aad(
                account_id=account_id,
                kind=kind,
                provider=provider,
                version=envelope.aad_version,
            ),
        )


class RSAEnvelopeEncryptor:
    """Encrypt-only hybrid envelope: RSA-OAEP wraps a random AES-256 data key."""

    def __init__(self, public_key_pem: bytes, *, key_version: int = 1):
        public_key = serialization.load_pem_public_key(public_key_pem)
        if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size < 3072:
            raise ValueError("credential public key must be an RSA key of at least 3072 bits")
        if key_version < 1:
            raise ValueError("credential key version must be positive")
        self._public_key = public_key
        self._key_version = key_version

    def encrypt(
        self,
        plaintext: bytes,
        *,
        account_id: str,
        kind: SecretKind,
        provider: str,
        version: int,
    ) -> EncryptedEnvelope:
        associated_data = _aad(
            account_id=account_id,
            kind=kind,
            provider=provider,
            version=version,
        )
        data_key = bytearray(os.urandom(32))
        nonce = os.urandom(12)
        try:
            ciphertext = AESGCM(bytes(data_key)).encrypt(nonce, plaintext, associated_data)
            encrypted_data_key = self._public_key.encrypt(
                bytes(data_key),
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=associated_data,
                ),
            )
        finally:
            data_key[:] = b"\x00" * len(data_key)
        return EncryptedEnvelope(
            algorithm="RSA-OAEP-256+A256GCM",
            key_version=self._key_version,
            aad_version=version,
            nonce=_b64encode(nonce),
            ciphertext=_b64encode(ciphertext),
            encrypted_data_key=_b64encode(encrypted_data_key),
        )


class RSAEnvelopeDecryptor:
    """Worker-only counterpart. The public API never constructs this class."""

    def __init__(self, private_keys: Mapping[int, bytes]):
        self._private_keys: dict[int, rsa.RSAPrivateKey] = {}
        for version, pem in private_keys.items():
            key = serialization.load_pem_private_key(pem, password=None)
            if version < 1 or not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 3072:
                raise ValueError("credential private keyring contains an invalid key")
            self._private_keys[version] = key
        if not self._private_keys:
            raise ValueError("at least one credential private key is required")

    def decrypt(
        self,
        envelope: EncryptedEnvelope,
        *,
        account_id: str,
        kind: SecretKind,
        provider: str,
    ) -> bytes:
        if (
            envelope.algorithm != "RSA-OAEP-256+A256GCM"
            or envelope.encrypted_data_key is None
        ):
            raise CredentialConfigurationError("unsupported credential envelope")
        try:
            private_key = self._private_keys[envelope.key_version]
        except KeyError as exc:
            raise CredentialConfigurationError("credential key version is unavailable") from exc
        associated_data = _aad(
            account_id=account_id,
            kind=kind,
            provider=provider,
            version=envelope.aad_version,
        )
        data_key = bytearray(
            private_key.decrypt(
                _b64decode(envelope.encrypted_data_key),
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=associated_data,
                ),
            )
        )
        try:
            return AESGCM(bytes(data_key)).decrypt(
                _b64decode(envelope.nonce),
                _b64decode(envelope.ciphertext),
                associated_data,
            )
        finally:
            data_key[:] = b"\x00" * len(data_key)


def _environment_key_version() -> int:
    try:
        version = int(os.getenv("POLYBOT_CREDENTIAL_KEY_VERSION", "1"))
    except ValueError as exc:
        raise CredentialConfigurationError("invalid credential key version") from exc
    if version < 1:
        raise CredentialConfigurationError("invalid credential key version")
    return version


def generate_rsa_credential_keypair(*, key_size: int = 3072) -> tuple[str, str]:
    """Return ``(public_pem, private_pem)`` for API/worker environment variables."""

    if key_size < 3072:
        raise ValueError("credential RSA keys must be at least 3072 bits")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return public_pem, private_pem


def build_api_encryptor_from_environment() -> EnvelopeEncryptor:
    """Build the API's write-only encryption capability.

    Preferred production mode supplies only ``POLYBOT_CREDENTIAL_PUBLIC_KEY_PEM``
    to the API and the matching private key only to workers. Symmetric mode is
    deliberately opt-in and is rejected for the legacy combined component.
    """

    version = _environment_key_version()
    public_pem = os.getenv("POLYBOT_CREDENTIAL_PUBLIC_KEY_PEM")
    private_pem = os.getenv("POLYBOT_CREDENTIAL_PRIVATE_KEY_PEM")
    private_keyring = os.getenv("POLYBOT_CREDENTIAL_PRIVATE_KEYS_JSON")
    encoded_master = os.getenv("POLYBOT_CREDENTIAL_MASTER_KEY")
    if private_pem or private_keyring:
        raise CredentialConfigurationError(
            "the API process must never receive a credential private key"
        )
    if public_pem and encoded_master:
        raise CredentialConfigurationError(
            "configure either the API public key or symmetric key, never both"
        )
    if public_pem:
        try:
            return RSAEnvelopeEncryptor(
                public_pem.replace("\\n", "\n").encode(),
                key_version=version,
            )
        except (TypeError, ValueError) as exc:
            raise CredentialConfigurationError("invalid credential public key") from exc

    allow_symmetric = os.getenv("POLYBOT_ALLOW_API_SYMMETRIC_CREDENTIAL_KEY", "").lower()
    component = os.getenv("POLYBOT_COMPONENT", "all").lower()
    if encoded_master and allow_symmetric in {"1", "true", "yes"} and component == "api":
        try:
            master_key = _b64decode(encoded_master)
            return AesGcmEnvelopeEncryptor(master_key, key_version=version)
        except (ValueError, TypeError) as exc:
            raise CredentialConfigurationError("invalid credential master key") from exc
    if encoded_master and component != "api":
        raise CredentialConfigurationError(
            "combined processes may not hold the API credential encryption key"
        )
    raise CredentialConfigurationError(
        "credential ingestion is disabled until an API encryption key is configured"
    )


def build_worker_decryptor_from_environment() -> EnvelopeDecryptor:
    """Build worker-only decryption capability from a PEM private-key keyring."""

    component = os.getenv("POLYBOT_COMPONENT", "all").lower()
    if component != "worker":
        raise CredentialConfigurationError(
            "credential decryption is available only in a worker-only process"
        )
    version = _environment_key_version()
    private_pem = os.getenv("POLYBOT_CREDENTIAL_PRIVATE_KEY_PEM")
    private_keyring = os.getenv("POLYBOT_CREDENTIAL_PRIVATE_KEYS_JSON")
    if private_pem and private_keyring:
        raise CredentialConfigurationError(
            "configure either one worker private key or a versioned keyring"
        )
    if private_keyring:
        try:
            parsed = json.loads(private_keyring)
            if not isinstance(parsed, dict):
                raise ValueError("keyring must be an object")
            keys = {
                int(key_version): str(pem).replace("\\n", "\n").encode()
                for key_version, pem in parsed.items()
            }
            return RSAEnvelopeDecryptor(keys)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CredentialConfigurationError(
                "invalid credential private keyring"
            ) from exc
    if private_pem:
        try:
            return RSAEnvelopeDecryptor(
                {version: private_pem.replace("\\n", "\n").encode()}
            )
        except (TypeError, ValueError) as exc:
            raise CredentialConfigurationError("invalid credential private key") from exc
    encoded_master = os.getenv("POLYBOT_CREDENTIAL_MASTER_KEY")
    if encoded_master:
        try:
            return AesGcmEnvelopeDecryptor({version: _b64decode(encoded_master)})
        except (TypeError, ValueError) as exc:
            raise CredentialConfigurationError("invalid credential master key") from exc
    raise CredentialConfigurationError("worker credential decryption key is not configured")


def fingerprint_key_from_environment() -> bytes:
    encoded = os.getenv("POLYBOT_CREDENTIAL_FINGERPRINT_KEY")
    if not encoded:
        raise CredentialConfigurationError("credential fingerprint key is not configured")
    try:
        key = _b64decode(encoded)
    except (TypeError, ValueError) as exc:
        raise CredentialConfigurationError("invalid credential fingerprint key") from exc
    if len(key) != 32:
        raise CredentialConfigurationError(
            "credential fingerprint key must contain exactly 32 bytes"
        )
    return key


class CredentialRepository(Protocol):
    async def status(self, account_id: str) -> CredentialStatus: ...

    async def store_ai(
        self,
        *,
        account_id: str,
        provider: AIProvider,
        label: str | None,
        envelope: EncryptedEnvelope,
        fingerprint: str,
        last_four: str,
    ) -> CredentialMetadata: ...

    async def revoke_ai(
        self, *, account_id: str, provider: AIProvider
    ) -> CredentialMetadata | None: ...

    async def provision_wallet(
        self,
        *,
        account_id: str,
        label: str | None,
        owner_address: str | None,
    ) -> TradingWalletMetadata: ...

    async def import_wallet(
        self,
        *,
        account_id: str,
        label: str | None,
        owner_address: str | None,
        signature_type: int,
        idempotency_key: str,
        envelope: EncryptedEnvelope,
        fingerprint: str,
        last_four: str,
    ) -> TradingWalletMetadata: ...

    async def request_wallet_revoke(
        self, *, account_id: str, wallet_id: UUID
    ) -> TradingWalletMetadata | None: ...


class WorkerCredentialRepository(Protocol):
    async def get_envelope_for_worker(
        self,
        *,
        account_id: str,
        credential_id: UUID,
        kind: SecretKind,
    ) -> WorkerCredential | None: ...

    async def claim_wallet_lifecycle(
        self, *, claimed_by: str, lease_seconds: int
    ) -> WalletLifecycleClaim | None: ...

    async def get_pending_envelope_for_worker(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> WorkerCredential | None: ...

    async def get_wallet_lifecycle_envelope(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> WorkerCredential | None: ...

    async def get_revocation_envelope_for_worker(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> WorkerCredential | None: ...

    async def heartbeat_wallet_lifecycle(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
        lease_seconds: int,
    ) -> bool: ...

    async def complete_wallet_verification(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
        signer_address: str,
        deposit_wallet_address: str,
        chain_id: int,
        collateral_token: str,
    ) -> TradingWalletMetadata | None: ...

    async def fail_wallet_verification(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
        error_code: str,
        retryable: bool,
    ) -> TradingWalletMetadata | None: ...

    async def complete_wallet_revocation(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> TradingWalletMetadata | None: ...


def _credential_from_row(row: Mapping[str, Any]) -> CredentialMetadata:
    return CredentialMetadata(
        id=row["id"],
        kind=row["kind"],
        provider=row["provider"],
        label=row.get("label"),
        status=row["status"],
        fingerprint=str(row["fingerprint"])[:16],
        last_four=row["last_four"],
        version=row["version"],
        created_at=row["created_at"],
        rotated_at=row.get("rotated_at"),
        revoked_at=row.get("revoked_at"),
    )


def _wallet_from_row(row: Mapping[str, Any]) -> TradingWalletMetadata:
    return TradingWalletMetadata(
        id=row["id"],
        label=row.get("label"),
        owner_address=row.get("owner_address"),
        deposit_wallet_address=row.get("deposit_wallet_address"),
        signer_address=row.get("signer_address"),
        chain_id=row.get("chain_id"),
        collateral_token=row.get("collateral_token"),
        signature_type=row["signature_type"],
        status=row["status"],
        signer_credential_id=row.get("signer_credential_id"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class SupabaseCredentialRepository:
    """Service-role repository whose every operation carries a JWT-derived account."""

    def __init__(self, client: Any):
        self._client = client

    async def _execute(self, builder: Any) -> Any:
        try:
            return await asyncio.to_thread(builder.execute)
        except APIError as exc:
            if str(exc.code) == "23505":
                raise CredentialConflictError(
                    "credential idempotency or uniqueness conflict"
                ) from exc
            raise

    @staticmethod
    def _first(response: Any) -> dict[str, Any] | None:
        data = getattr(response, "data", None)
        if isinstance(data, list):
            return data[0] if data and isinstance(data[0], dict) else None
        return data if isinstance(data, dict) else None

    async def status(self, account_id: str) -> CredentialStatus:
        credentials_response, wallets_response = await asyncio.gather(
            self._execute(
                self._client.table("credential_refs")
                .select(
                    "id,kind,provider,label,status,fingerprint,last_four,version,"
                    "created_at,rotated_at,revoked_at"
                )
                .eq("account_id", account_id)
                .order("created_at", desc=True)
            ),
            self._execute(
                self._client.table("trading_wallets")
                .select(
                    "id,label,owner_address,deposit_wallet_address,signer_address,"
                    "chain_id,collateral_token,signature_type,status,"
                    "signer_credential_id,created_at,updated_at"
                )
                .eq("account_id", account_id)
                .order("created_at", desc=True)
            ),
        )
        credential_rows = getattr(credentials_response, "data", []) or []
        wallet_rows = getattr(wallets_response, "data", []) or []
        return CredentialStatus(
            ai_credentials=[
                _credential_from_row(row)
                for row in credential_rows
                if isinstance(row, dict) and row.get("kind") == SecretKind.AI_API_KEY
            ],
            wallets=[
                _wallet_from_row(row) for row in wallet_rows if isinstance(row, dict)
            ],
        )

    async def claim_wallet_lifecycle(
        self, *, claimed_by: str, lease_seconds: int
    ) -> WalletLifecycleClaim | None:
        response = await self._execute(
            self._client.rpc(
                "claim_trading_wallet_lifecycle",
                {
                    "p_claimed_by": claimed_by,
                    "p_lease_seconds": lease_seconds,
                },
            )
        )
        row = self._first(response)
        return WalletLifecycleClaim.model_validate(row) if row else None

    async def get_pending_envelope_for_worker(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> WorkerCredential | None:
        return await self.get_wallet_lifecycle_envelope(
            account_id=account_id,
            wallet_id=wallet_id,
            claimed_by=claimed_by,
            fencing_token=fencing_token,
        )

    async def get_wallet_lifecycle_envelope(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> WorkerCredential | None:
        response = await self._execute(
            self._client.rpc(
                "get_wallet_lifecycle_envelope",
                {
                    "p_account_id": account_id,
                    "p_wallet_id": str(wallet_id),
                    "p_claimed_by": claimed_by,
                    "p_fencing_token": fencing_token,
                },
            )
        )
        row = self._first(response)
        if row is None:
            return None
        return WorkerCredential(
            id=row["id"],
            account_id=row["account_id"],
            kind=row["kind"],
            provider=row["provider"],
            status=row["status"],
            version=row["version"],
            envelope=EncryptedEnvelope(
                algorithm=row["cipher_algorithm"],
                key_version=row["key_version"],
                aad_version=row["aad_version"],
                nonce=row["nonce"],
                ciphertext=row["ciphertext"],
                encrypted_data_key=row.get("encrypted_data_key"),
            ),
        )

    async def get_revocation_envelope_for_worker(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> WorkerCredential | None:
        return await self.get_wallet_lifecycle_envelope(
            account_id=account_id,
            wallet_id=wallet_id,
            claimed_by=claimed_by,
            fencing_token=fencing_token,
        )

    async def heartbeat_wallet_lifecycle(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
        lease_seconds: int,
    ) -> bool:
        response = await self._execute(
            self._client.rpc(
                "heartbeat_trading_wallet_lifecycle",
                {
                    "p_account_id": account_id,
                    "p_wallet_id": str(wallet_id),
                    "p_claimed_by": claimed_by,
                    "p_fencing_token": fencing_token,
                    "p_lease_seconds": lease_seconds,
                },
            )
        )
        data = getattr(response, "data", False)
        if isinstance(data, list):
            data = data[0] if data else False
        if isinstance(data, dict):
            data = data.get(
                "heartbeat_trading_wallet_lifecycle",
                data.get("ok", False),
            )
        return data is True

    async def complete_wallet_verification(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
        signer_address: str,
        deposit_wallet_address: str,
        chain_id: int,
        collateral_token: str,
    ) -> TradingWalletMetadata | None:
        response = await self._execute(
            self._client.rpc(
                "complete_trading_wallet_verification",
                {
                    "p_account_id": account_id,
                    "p_wallet_id": str(wallet_id),
                    "p_claimed_by": claimed_by,
                    "p_fencing_token": fencing_token,
                    "p_signer_address": signer_address,
                    "p_deposit_wallet_address": deposit_wallet_address,
                    "p_chain_id": chain_id,
                    "p_collateral_token": collateral_token,
                },
            )
        )
        row = self._first(response)
        return _wallet_from_row(row) if row else None

    async def fail_wallet_verification(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
        error_code: str,
        retryable: bool,
    ) -> TradingWalletMetadata | None:
        response = await self._execute(
            self._client.rpc(
                "fail_trading_wallet_verification",
                {
                    "p_account_id": account_id,
                    "p_wallet_id": str(wallet_id),
                    "p_claimed_by": claimed_by,
                    "p_fencing_token": fencing_token,
                    "p_error_code": error_code,
                    "p_retryable": retryable,
                },
            )
        )
        row = self._first(response)
        return _wallet_from_row(row) if row else None

    async def complete_wallet_revocation(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> TradingWalletMetadata | None:
        response = await self._execute(
            self._client.rpc(
                "complete_trading_wallet_revocation",
                {
                    "p_account_id": account_id,
                    "p_wallet_id": str(wallet_id),
                    "p_claimed_by": claimed_by,
                    "p_fencing_token": fencing_token,
                },
            )
        )
        row = self._first(response)
        return _wallet_from_row(row) if row else None

    async def store_ai(
        self,
        *,
        account_id: str,
        provider: AIProvider,
        label: str | None,
        envelope: EncryptedEnvelope,
        fingerprint: str,
        last_four: str,
    ) -> CredentialMetadata:
        response = await self._execute(
            self._client.rpc(
                "store_ai_credential",
                {
                    "p_account_id": account_id,
                    "p_provider": provider.value,
                    "p_label": label,
                    "p_algorithm": envelope.algorithm,
                    "p_key_version": envelope.key_version,
                    "p_aad_version": envelope.aad_version,
                    "p_nonce": envelope.nonce,
                    "p_ciphertext": envelope.ciphertext,
                    "p_encrypted_data_key": envelope.encrypted_data_key,
                    "p_fingerprint": fingerprint,
                    "p_last_four": last_four,
                },
            )
        )
        row = self._first(response)
        if row is None:
            raise CredentialConflictError("credential could not be stored")
        return _credential_from_row(row)

    async def revoke_ai(
        self, *, account_id: str, provider: AIProvider
    ) -> CredentialMetadata | None:
        response = await self._execute(
            self._client.rpc(
                "revoke_ai_credential",
                {"p_account_id": account_id, "p_provider": provider.value},
            )
        )
        row = self._first(response)
        return _credential_from_row(row) if row else None

    async def provision_wallet(
        self,
        *,
        account_id: str,
        label: str | None,
        owner_address: str | None,
    ) -> TradingWalletMetadata:
        del account_id, label, owner_address
        raise CredentialConfigurationError(
            "wallet provisioning requires a worker-side KMS implementation"
        )

    async def import_wallet(
        self,
        *,
        account_id: str,
        label: str | None,
        owner_address: str | None,
        signature_type: int,
        idempotency_key: str,
        envelope: EncryptedEnvelope,
        fingerprint: str,
        last_four: str,
    ) -> TradingWalletMetadata:
        response = await self._execute(
            self._client.rpc(
                "import_trading_wallet",
                {
                    "p_account_id": account_id,
                    "p_label": label,
                    "p_owner_address": owner_address,
                    "p_signature_type": signature_type,
                    "p_idempotency_key": idempotency_key,
                    "p_algorithm": envelope.algorithm,
                    "p_key_version": envelope.key_version,
                    "p_aad_version": envelope.aad_version,
                    "p_nonce": envelope.nonce,
                    "p_ciphertext": envelope.ciphertext,
                    "p_encrypted_data_key": envelope.encrypted_data_key,
                    "p_fingerprint": fingerprint,
                    "p_last_four": last_four,
                },
            )
        )
        row = self._first(response)
        if row is None:
            raise CredentialConflictError("wallet import could not be stored")
        return _wallet_from_row(row)

    async def request_wallet_revoke(
        self, *, account_id: str, wallet_id: UUID
    ) -> TradingWalletMetadata | None:
        response = await self._execute(
            self._client.rpc(
                "request_trading_wallet_revocation",
                {"p_account_id": account_id, "p_wallet_id": str(wallet_id)},
            )
        )
        row = self._first(response)
        return _wallet_from_row(row) if row else None

    async def get_envelope_for_worker(
        self,
        *,
        account_id: str,
        credential_id: UUID,
        kind: SecretKind,
    ) -> WorkerCredential | None:
        response = await self._execute(
            self._client.table("credential_refs")
            .select(
                "id,account_id,kind,provider,status,version,cipher_algorithm,"
                "key_version,aad_version,nonce,ciphertext,encrypted_data_key"
            )
            .eq("account_id", account_id)
            .eq("id", str(credential_id))
            .eq("kind", kind.value)
            .eq("status", "active")
            .limit(1)
        )
        row = self._first(response)
        if row is None:
            return None
        return WorkerCredential(
            id=row["id"],
            account_id=row["account_id"],
            kind=row["kind"],
            provider=row["provider"],
            status=row["status"],
            version=row["version"],
            envelope=EncryptedEnvelope(
                algorithm=row["cipher_algorithm"],
                key_version=row["key_version"],
                aad_version=row["aad_version"],
                nonce=row["nonce"],
                ciphertext=row["ciphertext"],
                encrypted_data_key=row.get("encrypted_data_key"),
            ),
        )


class InMemoryCredentialRepository:
    """Ciphertext-only deterministic test repository with tenant filtering."""

    def __init__(self):
        self._credentials: dict[UUID, dict[str, Any]] = {}
        self._wallets: dict[UUID, dict[str, Any]] = {}

    async def status(self, account_id: str) -> CredentialStatus:
        return CredentialStatus(
            ai_credentials=[
                _credential_from_row(row)
                for row in self._credentials.values()
                if row["account_id"] == account_id and row["kind"] == SecretKind.AI_API_KEY
            ],
            wallets=[
                _wallet_from_row(row)
                for row in self._wallets.values()
                if row["account_id"] == account_id
            ],
        )

    async def store_ai(
        self,
        *,
        account_id: str,
        provider: AIProvider,
        label: str | None,
        envelope: EncryptedEnvelope,
        fingerprint: str,
        last_four: str,
    ) -> CredentialMetadata:
        now = utc_now()
        existing = next(
            (
                row
                for row in self._credentials.values()
                if row["account_id"] == account_id
                and row["kind"] == SecretKind.AI_API_KEY
                and row["provider"] == provider
                and row["status"] == "active"
            ),
            None,
        )
        if existing is not None:
            existing["status"] = "revoked"
            existing["revoked_at"] = now
        row: dict[str, Any] = {
            "id": uuid4(),
            "account_id": account_id,
            "kind": SecretKind.AI_API_KEY,
            "provider": provider,
            "label": label,
            "status": "active",
            "fingerprint": fingerprint,
            "last_four": last_four,
            "version": (int(existing["version"]) + 1) if existing else 1,
            "envelope": envelope.model_dump(),
            "created_at": now,
            "rotated_at": now if existing else None,
            "revoked_at": None,
        }
        self._credentials[row["id"]] = row
        return _credential_from_row(row)

    async def revoke_ai(
        self, *, account_id: str, provider: AIProvider
    ) -> CredentialMetadata | None:
        for row in self._credentials.values():
            if (
                row["account_id"] == account_id
                and row["kind"] == SecretKind.AI_API_KEY
                and row["provider"] == provider
                and row["status"] == "active"
            ):
                row["status"] = "revoked"
                row["revoked_at"] = utc_now()
                return _credential_from_row(row)
        return None

    async def provision_wallet(
        self,
        *,
        account_id: str,
        label: str | None,
        owner_address: str | None,
    ) -> TradingWalletMetadata:
        now = utc_now()
        row: dict[str, Any] = {
            "id": uuid4(),
            "account_id": account_id,
            "label": label,
            "owner_address": owner_address,
            "deposit_wallet_address": None,
            "signer_address": None,
            "chain_id": None,
            "collateral_token": None,
            "signature_type": 3,
            "status": "provisioning",
            "signer_credential_id": None,
            "lifecycle_claimed_by": None,
            "lifecycle_fencing_token": 0,
            "lifecycle_lease_expires_at": None,
            "lifecycle_attempt_count": 0,
            "last_error_code": None,
            "created_at": now,
            "updated_at": now,
        }
        self._wallets[row["id"]] = row
        return _wallet_from_row(row)

    async def import_wallet(
        self,
        *,
        account_id: str,
        label: str | None,
        owner_address: str | None,
        signature_type: int,
        idempotency_key: str,
        envelope: EncryptedEnvelope,
        fingerprint: str,
        last_four: str,
    ) -> TradingWalletMetadata:
        idempotent = next(
            (
                row
                for row in self._wallets.values()
                if row["account_id"] == account_id
                and row.get("enrollment_idempotency_key") == idempotency_key
            ),
            None,
        )
        if idempotent is not None:
            credential = self._credentials.get(idempotent.get("signer_credential_id"))
            if (
                credential is None
                or credential["fingerprint"] != fingerprint
                or idempotent["signature_type"] != signature_type
                or idempotent.get("owner_address") != owner_address
            ):
                raise CredentialConflictError(
                    "wallet enrollment idempotency key was reused with different input"
                )
            return _wallet_from_row(idempotent)

        duplicate = next(
            (
                row
                for row in self._wallets.values()
                if row["account_id"] == account_id
                and row.get("signer_credential_id") in self._credentials
                and self._credentials[row["signer_credential_id"]]["fingerprint"]
                == fingerprint
                and row["status"]
                in {
                    "pending_verification",
                    "active",
                    "revocation_pending",
                }
            ),
            None,
        )
        if duplicate is not None:
            if (
                duplicate["signature_type"] != signature_type
                or duplicate.get("owner_address") != owner_address
            ):
                raise CredentialConflictError(
                    "signer key is already enrolled with different wallet metadata"
                )
            return _wallet_from_row(duplicate)
        now = utc_now()
        credential_id = uuid4()
        self._credentials[credential_id] = {
            "id": credential_id,
            "account_id": account_id,
            "kind": SecretKind.EVM_SIGNER_KEY,
            "provider": "polymarket",
            "label": label,
            "status": "pending_verification",
            "fingerprint": fingerprint,
            "last_four": last_four,
            "version": 1,
            "envelope": envelope.model_dump(),
            "created_at": now,
            "rotated_at": None,
            "revoked_at": None,
        }
        row: dict[str, Any] = {
            "id": uuid4(),
            "account_id": account_id,
            "label": label,
            "owner_address": owner_address,
            "deposit_wallet_address": None,
            "signer_address": None,
            "chain_id": None,
            "collateral_token": None,
            "enrollment_idempotency_key": idempotency_key,
            "signature_type": signature_type,
            "status": "pending_verification",
            "signer_credential_id": credential_id,
            "lifecycle_claimed_by": None,
            "lifecycle_fencing_token": 0,
            "lifecycle_lease_expires_at": None,
            "lifecycle_attempt_count": 0,
            "last_error_code": None,
            "created_at": now,
            "updated_at": now,
        }
        self._wallets[row["id"]] = row
        return _wallet_from_row(row)

    async def request_wallet_revoke(
        self, *, account_id: str, wallet_id: UUID
    ) -> TradingWalletMetadata | None:
        row = self._wallets.get(wallet_id)
        if row is None or row["account_id"] != account_id:
            return None
        now = utc_now()
        credential_id = row.get("signer_credential_id")
        if row["status"] in {"active", "revocation_pending"}:
            row["status"] = "revocation_pending"
            if credential_id in self._credentials:
                self._credentials[credential_id]["status"] = "revocation_pending"
        else:
            row.update(
                {
                    "status": "revoked",
                    "deposit_wallet_address": None,
                    "signer_address": None,
                    "chain_id": None,
                    "collateral_token": None,
                    "revoked_at": now,
                    "lifecycle_claimed_by": None,
                    "lifecycle_lease_expires_at": None,
                }
            )
            if credential_id in self._credentials:
                self._credentials[credential_id]["status"] = "revoked"
                self._credentials[credential_id]["revoked_at"] = now
        row["updated_at"] = now
        return _wallet_from_row(row)

    async def get_envelope_for_worker(
        self,
        *,
        account_id: str,
        credential_id: UUID,
        kind: SecretKind,
    ) -> WorkerCredential | None:
        row = self._credentials.get(credential_id)
        if (
            row is None
            or row["account_id"] != account_id
            or row["kind"] != kind
            or row["status"] != "active"
        ):
            return None
        return WorkerCredential(
            id=row["id"],
            account_id=row["account_id"],
            kind=row["kind"],
            provider=row["provider"],
            status=row["status"],
            version=row["version"],
            envelope=EncryptedEnvelope.model_validate(row["envelope"]),
        )

    async def claim_wallet_lifecycle(
        self, *, claimed_by: str, lease_seconds: int
    ) -> WalletLifecycleClaim | None:
        if not claimed_by or not 10 <= lease_seconds <= 300:
            raise ValueError("invalid wallet lifecycle lease")
        now = utc_now()
        eligible = sorted(
            (
                row
                for row in self._wallets.values()
                if row["status"] in {"pending_verification", "revocation_pending"}
                and row["lifecycle_attempt_count"] < 5
                and (
                    row["lifecycle_claimed_by"] is None
                    or row["lifecycle_lease_expires_at"] is None
                    or row["lifecycle_lease_expires_at"] <= now
                )
            ),
            key=lambda row: (
                0 if row["status"] == "revocation_pending" else 1,
                row["created_at"],
            ),
        )
        if not eligible:
            return None
        row = eligible[0]
        row["lifecycle_claimed_by"] = claimed_by
        row["lifecycle_fencing_token"] += 1
        row["lifecycle_lease_expires_at"] = now + timedelta(seconds=lease_seconds)
        if row["status"] == "pending_verification":
            row["lifecycle_attempt_count"] += 1
        row["updated_at"] = now
        return WalletLifecycleClaim.model_validate(row)

    def _claimed_wallet(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> dict[str, Any] | None:
        row = self._wallets.get(wallet_id)
        if (
            row is None
            or row["account_id"] != account_id
            or row["lifecycle_claimed_by"] != claimed_by
            or row["lifecycle_fencing_token"] != fencing_token
            or row["lifecycle_lease_expires_at"] is None
            or row["lifecycle_lease_expires_at"] <= utc_now()
        ):
            return None
        return row

    async def get_pending_envelope_for_worker(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> WorkerCredential | None:
        return await self.get_wallet_lifecycle_envelope(
            account_id=account_id,
            wallet_id=wallet_id,
            claimed_by=claimed_by,
            fencing_token=fencing_token,
        )

    async def get_wallet_lifecycle_envelope(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> WorkerCredential | None:
        wallet = self._claimed_wallet(
            account_id=account_id,
            wallet_id=wallet_id,
            claimed_by=claimed_by,
            fencing_token=fencing_token,
        )
        if wallet is None or wallet["status"] not in {
            "pending_verification",
            "revocation_pending",
        }:
            return None
        credential = self._credentials.get(wallet["signer_credential_id"])
        if credential is None or credential["status"] != wallet["status"]:
            return None
        return WorkerCredential(
            id=credential["id"],
            account_id=credential["account_id"],
            kind=credential["kind"],
            provider=credential["provider"],
            status=credential["status"],
            version=credential["version"],
            envelope=EncryptedEnvelope.model_validate(credential["envelope"]),
        )

    async def get_revocation_envelope_for_worker(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> WorkerCredential | None:
        return await self.get_wallet_lifecycle_envelope(
            account_id=account_id,
            wallet_id=wallet_id,
            claimed_by=claimed_by,
            fencing_token=fencing_token,
        )

    async def heartbeat_wallet_lifecycle(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
        lease_seconds: int,
    ) -> bool:
        wallet = self._claimed_wallet(
            account_id=account_id,
            wallet_id=wallet_id,
            claimed_by=claimed_by,
            fencing_token=fencing_token,
        )
        if wallet is None or not 10 <= lease_seconds <= 300:
            return False
        wallet["lifecycle_lease_expires_at"] = utc_now() + timedelta(
            seconds=lease_seconds
        )
        wallet["updated_at"] = utc_now()
        return True

    async def complete_wallet_verification(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
        signer_address: str,
        deposit_wallet_address: str,
        chain_id: int,
        collateral_token: str,
    ) -> TradingWalletMetadata | None:
        wallet = self._claimed_wallet(
            account_id=account_id,
            wallet_id=wallet_id,
            claimed_by=claimed_by,
            fencing_token=fencing_token,
        )
        if wallet is None or wallet["status"] != "pending_verification":
            return None
        credential = self._credentials.get(wallet["signer_credential_id"])
        if credential is None or credential["status"] != "pending_verification":
            return None
        if chain_id <= 0:
            raise ValueError("chain_id must be positive")
        normalized_collateral = collateral_token.lower()
        if not _EVM_ADDRESS.fullmatch(normalized_collateral):
            raise ValueError("collateral_token must be an EVM address")
        now = utc_now()
        credential["status"] = "active"
        wallet.update(
            {
                "signer_address": signer_address,
                "deposit_wallet_address": deposit_wallet_address,
                "chain_id": chain_id,
                "collateral_token": normalized_collateral,
                "status": "active",
                "verified_at": now,
                "lifecycle_claimed_by": None,
                "lifecycle_lease_expires_at": None,
                "updated_at": now,
            }
        )
        return _wallet_from_row(wallet)

    async def fail_wallet_verification(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
        error_code: str,
        retryable: bool,
    ) -> TradingWalletMetadata | None:
        wallet = self._claimed_wallet(
            account_id=account_id,
            wallet_id=wallet_id,
            claimed_by=claimed_by,
            fencing_token=fencing_token,
        )
        if wallet is None or wallet["status"] != "pending_verification":
            return None
        terminal = not retryable or wallet["lifecycle_attempt_count"] >= 5
        wallet["status"] = "failed" if terminal else "pending_verification"
        wallet["last_error_code"] = error_code
        wallet["lifecycle_claimed_by"] = None
        wallet["lifecycle_lease_expires_at"] = None
        wallet["updated_at"] = utc_now()
        if terminal:
            credential = self._credentials.get(wallet["signer_credential_id"])
            if credential:
                credential["status"] = "revoked"
                credential["revoked_at"] = utc_now()
        return _wallet_from_row(wallet)

    async def complete_wallet_revocation(
        self,
        *,
        account_id: str,
        wallet_id: UUID,
        claimed_by: str,
        fencing_token: int,
    ) -> TradingWalletMetadata | None:
        wallet = self._claimed_wallet(
            account_id=account_id,
            wallet_id=wallet_id,
            claimed_by=claimed_by,
            fencing_token=fencing_token,
        )
        if wallet is None or wallet["status"] != "revocation_pending":
            return None
        now = utc_now()
        wallet["status"] = "revoked"
        wallet["revoked_at"] = now
        wallet["lifecycle_claimed_by"] = None
        wallet["lifecycle_lease_expires_at"] = None
        wallet["updated_at"] = now
        credential = self._credentials.get(wallet["signer_credential_id"])
        if credential:
            credential["status"] = "revoked"
            credential["revoked_at"] = now
        return _wallet_from_row(wallet)


class CredentialService:
    def __init__(
        self,
        repository: CredentialRepository,
        encryptor: EnvelopeEncryptor,
        *,
        fingerprint_key: bytes,
    ):
        if len(fingerprint_key) != 32:
            raise ValueError("credential fingerprint key must contain exactly 32 bytes")
        self._repository = repository
        self._encryptor = encryptor
        self._fingerprint_key = fingerprint_key

    @staticmethod
    def _aad_version() -> int:
        # This immutable value travels with the envelope and is intentionally
        # independent from the row lifecycle version, which may increment when
        # concurrent rotations revoke/replace a record.
        return max(1, int.from_bytes(os.urandom(8), "big") & ((1 << 63) - 1))

    def _fingerprint(self, secret: bytes | bytearray) -> str:
        return hmac.new(self._fingerprint_key, secret, hashlib.sha256).hexdigest()

    @staticmethod
    def _label(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not _SAFE_LABEL.fullmatch(normalized):
            raise ValueError("label contains unsupported characters")
        return normalized

    @staticmethod
    def _address(value: str | None) -> str | None:
        if value is None:
            return None
        if not _EVM_ADDRESS.fullmatch(value):
            raise ValueError("invalid EVM address")
        return value.lower()

    async def status(self, account_id: str) -> CredentialStatus:
        return await self._repository.status(account_id)

    async def put_ai(
        self,
        *,
        account_id: str,
        provider: AIProvider,
        api_key: str,
        label: str | None,
    ) -> CredentialMetadata:
        if provider in {AIProvider.PLATFORM, AIProvider.MOCK}:
            raise ValueError("the selected provider does not accept a tenant API key")
        normalized = api_key.strip()
        if not 16 <= len(normalized) <= 512 or any(
            character.isspace() or ord(character) < 33 for character in normalized
        ):
            raise ValueError("API key has an invalid format")
        secret = bytearray(normalized.encode())
        try:
            fingerprint = self._fingerprint(secret)
            envelope = self._encryptor.encrypt(
                bytes(secret),
                account_id=account_id,
                kind=SecretKind.AI_API_KEY,
                provider=provider.value,
                version=self._aad_version(),
            )
        finally:
            secret[:] = b"\x00" * len(secret)
        return await self._repository.store_ai(
            account_id=account_id,
            provider=provider,
            label=self._label(label),
            envelope=envelope,
            fingerprint=fingerprint,
            last_four=normalized[-4:],
        )

    async def revoke_ai(
        self, *, account_id: str, provider: AIProvider
    ) -> CredentialMetadata:
        record = await self._repository.revoke_ai(account_id=account_id, provider=provider)
        if record is None:
            raise CredentialNotFoundError("credential not found")
        return record

    async def provision_wallet(
        self,
        *,
        account_id: str,
        label: str | None,
        owner_address: str | None,
    ) -> TradingWalletMetadata:
        return await self._repository.provision_wallet(
            account_id=account_id,
            label=self._label(label),
            owner_address=self._address(owner_address),
        )

    async def import_wallet(
        self,
        *,
        account_id: str,
        private_key: str,
        label: str | None,
        owner_address: str | None,
        signature_type: int,
        idempotency_key: str,
    ) -> TradingWalletMetadata:
        normalized_key = private_key.strip()
        if not _EVM_PRIVATE_KEY.fullmatch(normalized_key):
            raise ValueError("private key must be a 32-byte hexadecimal value")
        if signature_type not in {0, 1, 2, 3}:
            raise ValueError("unsupported Polymarket signature type")
        if not _SAFE_IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise ValueError("invalid wallet enrollment idempotency key")
        canonical_key = normalized_key.removeprefix("0x").lower()
        scalar = int(canonical_key, 16)
        if scalar == 0 or scalar >= _SECP256K1_ORDER:
            raise ValueError("private key is outside the secp256k1 scalar range")
        secret = bytearray.fromhex(canonical_key)
        try:
            fingerprint = self._fingerprint(secret)
            envelope = self._encryptor.encrypt(
                bytes(secret),
                account_id=account_id,
                kind=SecretKind.EVM_SIGNER_KEY,
                provider="polymarket",
                version=self._aad_version(),
            )
        finally:
            secret[:] = b"\x00" * len(secret)
        return await self._repository.import_wallet(
            account_id=account_id,
            label=self._label(label),
            owner_address=self._address(owner_address),
            signature_type=signature_type,
            idempotency_key=idempotency_key,
            envelope=envelope,
            fingerprint=fingerprint,
            # A private key fingerprint is enough for internal duplicate
            # detection; persisting even 16 raw key bits is unnecessary.
            last_four="****",
        )

    async def revoke_wallet(
        self, *, account_id: str, wallet_id: UUID
    ) -> TradingWalletMetadata:
        record = await self._repository.request_wallet_revoke(
            account_id=account_id,
            wallet_id=wallet_id,
        )
        if record is None:
            raise CredentialNotFoundError("wallet not found")
        return record
