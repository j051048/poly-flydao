from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_MAGIC = b"PBA1"
_NONCE_SIZE = 12


@dataclass(frozen=True)
class EncryptionContext:
    account_id: str
    purpose: str
    object_id: str
    key_version: int

    def aad(self) -> bytes:
        if not self.account_id or not self.purpose or not self.object_id:
            raise ValueError("encryption context fields must be non-empty")
        if self.key_version < 1:
            raise ValueError("key_version must be positive")
        return json.dumps(
            {
                "account_id": self.account_id,
                "purpose": self.purpose,
                "object_id": self.object_id,
                "key_version": self.key_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()


class TenantAeadCipher:
    """Derive a tenant/purpose-specific AES-GCM key from a deployment KEK."""

    def __init__(self, encoded_master_key: str):
        try:
            master_key = base64.urlsafe_b64decode(encoded_master_key.encode())
        except Exception as exc:
            raise ValueError("master key must be URL-safe base64") from exc
        if len(master_key) != 32:
            raise ValueError("master key must decode to exactly 32 bytes")
        self._master_key = master_key

    def _derive(self, context: EncryptionContext) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"polybot-tenant-aead-v1",
            info=f"{context.account_id}:{context.purpose}:{context.key_version}".encode(),
        ).derive(self._master_key)

    def encrypt(self, plaintext: bytes, context: EncryptionContext) -> bytes:
        if not plaintext:
            raise ValueError("refusing to encrypt an empty payload")
        nonce = os.urandom(_NONCE_SIZE)
        ciphertext = AESGCM(self._derive(context)).encrypt(nonce, plaintext, context.aad())
        return _MAGIC + nonce + ciphertext

    def decrypt(self, envelope: bytes, context: EncryptionContext) -> bytes:
        if not envelope.startswith(_MAGIC):
            raise ValueError("unsupported ciphertext envelope")
        nonce_start = len(_MAGIC)
        nonce_end = nonce_start + _NONCE_SIZE
        nonce = envelope[nonce_start:nonce_end]
        ciphertext = envelope[nonce_end:]
        if len(nonce) != _NONCE_SIZE or len(ciphertext) < 16:
            raise ValueError("malformed ciphertext envelope")
        return AESGCM(self._derive(context)).decrypt(nonce, ciphertext, context.aad())
