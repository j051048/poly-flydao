from __future__ import annotations

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet

from polybot.tenant_crypto import (
    EncryptionContext,
    TenantAeadCipher,
)


def test_tenant_aead_round_trip_and_context_binding() -> None:
    cipher = TenantAeadCipher(Fernet.generate_key().decode())
    context = EncryptionContext(
        account_id="11111111-1111-1111-1111-111111111111",
        purpose="signed-order",
        object_id="intent-a",
        key_version=1,
    )
    envelope = cipher.encrypt(b'{"signature":"sensitive"}', context)

    assert cipher.decrypt(envelope, context) == b'{"signature":"sensitive"}'

    other_tenant = EncryptionContext(
        account_id="22222222-2222-2222-2222-222222222222",
        purpose=context.purpose,
        object_id=context.object_id,
        key_version=context.key_version,
    )
    with pytest.raises(InvalidTag):
        cipher.decrypt(envelope, other_tenant)


def test_tenant_aead_rejects_ciphertext_swap_between_objects() -> None:
    cipher = TenantAeadCipher(Fernet.generate_key().decode())
    first = EncryptionContext("account-a", "credential", "credential-a", 3)
    second = EncryptionContext("account-a", "credential", "credential-b", 3)

    envelope = cipher.encrypt(b"secret", first)

    with pytest.raises(InvalidTag):
        cipher.decrypt(envelope, second)


def test_tenant_aead_validates_master_key_and_envelope() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        TenantAeadCipher("c2hvcnQ=")

    cipher = TenantAeadCipher(Fernet.generate_key().decode())
    context = EncryptionContext("account-a", "credential", "credential-a", 1)
    with pytest.raises(ValueError, match="unsupported"):
        cipher.decrypt(b"legacy-ciphertext", context)
