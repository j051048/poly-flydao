"""Envelope encryption for tenant secrets, plus the environment key material.

Split out of :mod:`polybot.credentials`. Everything here is pure: it turns
plaintext into an authenticated envelope and back, and reads deployment key
material from the environment. It never touches the database.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from uuid import UUID

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from polybot.credentials_types import (
    CredentialConfigurationError,
    EncryptedEnvelope,
    EnvelopeDecryptor,
    EnvelopeEncryptor,
    SecretKind,
)


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
        if envelope.algorithm != "RSA-OAEP-256+A256GCM" or envelope.encrypted_data_key is None:
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
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
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
            raise CredentialConfigurationError("invalid credential private keyring") from exc
    if private_pem:
        try:
            return RSAEnvelopeDecryptor({version: private_pem.replace("\\n", "\n").encode()})
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
