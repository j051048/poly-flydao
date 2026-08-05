from __future__ import annotations

import base64
import json
import time
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from polybot.auth import JWTVerificationError, SupabaseJWTVerifier

ISSUER = "https://project.supabase.co/auth/v1"
SUBJECT = "11111111-1111-4111-8111-111111111111"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _token(private_key: rsa.RSAPrivateKey, claims: dict[str, Any]) -> str:
    header = {"alg": "RS256", "kid": "test-key", "typ": "JWT"}
    encoded_header = _b64(json.dumps(header, separators=(",", ":")).encode())
    encoded_claims = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{encoded_header}.{encoded_claims}".encode()
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{encoded_header}.{encoded_claims}.{_b64(signature)}"


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": "authenticated",
        "sub": SUBJECT,
        "role": "authenticated",
        "aal": "aal2",
        "iat": now - 1,
        "exp": now + 300,
    }
    claims.update(overrides)
    return claims


def _verifier(
    private_key: rsa.RSAPrivateKey,
) -> tuple[SupabaseJWTVerifier, httpx.AsyncClient]:
    public = private_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "test-key",
                "alg": "RS256",
                "use": "sig",
                "n": _b64(public.n.to_bytes((public.n.bit_length() + 7) // 8, "big")),
                "e": _b64(public.e.to_bytes((public.e.bit_length() + 7) // 8, "big")),
            }
        ]
    }
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks))
    )
    return SupabaseJWTVerifier(issuer=ISSUER, client=client), client


@pytest.mark.asyncio
async def test_supabase_jwt_verifies_signature_and_strict_claims() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier, client = _verifier(private_key)
    try:
        principal = await verifier.verify(_token(private_key, _claims()))
        assert principal.account_id == SUBJECT
        assert principal.is_aal2

        for claims in (
            _claims(iss="https://attacker.invalid/auth/v1"),
            _claims(aud="anon"),
            _claims(exp=int(time.time()) - 1),
            _claims(sub="not-a-uuid"),
            _claims(role="service_role"),
        ):
            with pytest.raises(JWTVerificationError):
                await verifier.verify(_token(private_key, claims))
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_supabase_jwt_rejects_tampered_signature() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier, client = _verifier(private_key)
    try:
        token = _token(private_key, _claims())
        prefix, signature = token.rsplit(".", 1)
        replacement = "A" if signature[0] != "A" else "B"
        tampered = f"{prefix}.{replacement}{signature[1:]}"
        with pytest.raises(JWTVerificationError):
            await verifier.verify(tampered)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_supabase_jwt_rejects_unknown_key_id() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "another-key",
                "alg": "RS256",
                "use": "sig",
                "n": _b64(public.n.to_bytes((public.n.bit_length() + 7) // 8, "big")),
                "e": _b64(public.e.to_bytes((public.e.bit_length() + 7) // 8, "big")),
            }
        ]
    }
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=jwks))
    )
    verifier = SupabaseJWTVerifier(issuer=ISSUER, client=client)
    try:
        with pytest.raises(JWTVerificationError):
            await verifier.verify(_token(private_key, _claims()))
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_supabase_jwt_fails_closed_when_jwks_unavailable() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    )
    verifier = SupabaseJWTVerifier(issuer=ISSUER, client=client)
    try:
        with pytest.raises(JWTVerificationError):
            await verifier.verify(_token(private_key, _claims()))
    finally:
        await client.aclose()
