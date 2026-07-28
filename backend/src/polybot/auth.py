from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

_SAFE_KEY_ID = re.compile(r"^[A-Za-z0-9._:@+-]{1,128}$")


class JWTVerificationError(ValueError):
    """Raised for every untrusted bearer-token failure.

    The API deliberately maps all instances to the same public 401 response so
    callers cannot use validation details as an authentication oracle.
    """


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    account_id: str
    aal: str
    role: str
    issuer: str
    audience: tuple[str, ...]

    @property
    def is_aal2(self) -> bool:
        return self.aal == "aal2"


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> AuthPrincipal: ...

    async def close(self) -> None: ...


def _b64url_decode(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise JWTVerificationError("malformed token")
    try:
        return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))
    except (ValueError, TypeError) as exc:
        raise JWTVerificationError("malformed token") from exc


def _strict_json(raw: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise JWTVerificationError("duplicate token field")
            result[key] = value
        return result

    try:
        parsed = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise JWTVerificationError("malformed token") from exc
    if not isinstance(parsed, dict):
        raise JWTVerificationError("malformed token")
    return parsed


def _integer_claim(claims: Mapping[str, Any], name: str, *, required: bool) -> int | None:
    value = claims.get(name)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JWTVerificationError(f"invalid {name} claim")
    if int(value) != value:
        raise JWTVerificationError(f"invalid {name} claim")
    return int(value)


def _audiences(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) and value:
        return (value,)
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, str) and item for item in value)
    ):
        return tuple(value)
    raise JWTVerificationError("invalid aud claim")


def _validated_auth_url(value: str, *, label: str) -> str:
    """Allow remote HTTPS and exact loopback HTTP endpoints only."""

    if not value or any(character.isspace() for character in value):
        raise ValueError(f"{label} must be a valid HTTPS URL")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid HTTPS URL") from exc
    hostname = parsed.hostname
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be a valid HTTPS URL")
    if parsed.scheme.lower() == "http":
        normalized_host = hostname.rstrip(".").lower()
        is_loopback = normalized_host == "localhost"
        if not is_loopback and "%" not in normalized_host:
            try:
                is_loopback = ipaddress.ip_address(normalized_host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            raise ValueError(f"{label} must use HTTPS outside loopback")
    return value.rstrip("/")


class SupabaseJWTVerifier:
    """Verify asymmetric Supabase access tokens against the project's JWKS.

    Legacy shared-secret JWTs are intentionally unsupported in the public API:
    a leaked service-side HMAC secret would also become a token-minting key.
    Supabase projects should use an asymmetric signing key and publish its JWKS.
    """

    _ALGORITHMS = frozenset({"RS256", "ES256"})

    def __init__(
        self,
        *,
        issuer: str,
        audience: str | tuple[str, ...] = "authenticated",
        jwks_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        cache_seconds: int = 300,
        clock_skew_seconds: int = 30,
        refresh_cooldown_seconds: float = 5.0,
        negative_cache_seconds: float = 30.0,
        max_jwks_bytes: int = 262_144,
        max_jwks_keys: int = 32,
    ):
        self.issuer = _validated_auth_url(issuer, label="JWT issuer")
        self.allowed_audiences = frozenset(
            (audience,) if isinstance(audience, str) else audience
        )
        if not self.allowed_audiences or any(not item for item in self.allowed_audiences):
            raise ValueError("at least one JWT audience is required")
        self.jwks_url = _validated_auth_url(
            jwks_url or f"{self.issuer}/.well-known/jwks.json",
            label="JWKS endpoint",
        )
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(5.0),
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )
        self._cache_seconds = max(30, cache_seconds)
        self._clock_skew_seconds = max(0, min(clock_skew_seconds, 60))
        self._refresh_cooldown_seconds = max(1.0, refresh_cooldown_seconds)
        self._negative_cache_seconds = max(1.0, negative_cache_seconds)
        self._max_jwks_bytes = max(1024, min(max_jwks_bytes, 1_048_576))
        self._max_jwks_keys = max(1, min(max_jwks_keys, 128))
        self._cached_keys: tuple[float, tuple[dict[str, Any], ...]] | None = None
        self._last_refresh_at = float("-inf")
        self._refresh_lock = asyncio.Lock()
        self._negative_keys: dict[tuple[str, str], float] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _fetch_jwks(self, *, now: float) -> tuple[dict[str, Any], ...]:
        try:
            async with self._client.stream(
                "GET",
                self.jwks_url,
                follow_redirects=False,
            ) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if (
                    content_length is not None
                    and int(content_length) > self._max_jwks_bytes
                ):
                    raise JWTVerificationError("authentication service unavailable")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > self._max_jwks_bytes:
                        raise JWTVerificationError("authentication service unavailable")
            payload = json.loads(raw)
        except JWTVerificationError:
            raise
        except (httpx.HTTPError, ValueError, UnicodeDecodeError) as exc:
            raise JWTVerificationError("authentication service unavailable") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
            raise JWTVerificationError("authentication service unavailable")
        raw_keys = payload["keys"]
        if (
            not 1 <= len(raw_keys) <= self._max_jwks_keys
            or any(not isinstance(key, dict) for key in raw_keys)
        ):
            raise JWTVerificationError("authentication service unavailable")
        keys = tuple(raw_keys)
        self._cached_keys = (now + self._cache_seconds, keys)
        self._last_refresh_at = now
        self._negative_keys.clear()
        return keys

    async def _jwks(self) -> tuple[dict[str, Any], ...]:
        now = time.monotonic()
        if self._cached_keys is not None and self._cached_keys[0] > now:
            return self._cached_keys[1]
        async with self._refresh_lock:
            now = time.monotonic()
            if self._cached_keys is not None and self._cached_keys[0] > now:
                return self._cached_keys[1]
            return await self._fetch_jwks(now=now)

    @staticmethod
    def _find_matching_jwk(
        keys: tuple[dict[str, Any], ...],
        *,
        kid: str,
        algorithm: str,
    ) -> dict[str, Any] | None:
        matches = [
            key
            for key in keys
            if key.get("kid") == kid
            and key.get("alg", algorithm) == algorithm
            and key.get("use", "sig") == "sig"
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def _is_negative_key(self, key: tuple[str, str], *, now: float) -> bool:
        expiry = self._negative_keys.get(key)
        if expiry is None:
            return False
        if expiry <= now:
            self._negative_keys.pop(key, None)
            return False
        return True

    def _remember_negative_key(self, key: tuple[str, str], *, now: float) -> None:
        if len(self._negative_keys) >= 128:
            self._negative_keys.pop(next(iter(self._negative_keys)))
        self._negative_keys[key] = now + self._negative_cache_seconds

    async def _matching_jwk(self, *, kid: str, algorithm: str) -> dict[str, Any]:
        cache_key = (kid, algorithm)
        now = time.monotonic()
        if self._is_negative_key(cache_key, now=now):
            raise JWTVerificationError("unknown signing key")

        keys = await self._jwks()
        match = self._find_matching_jwk(keys, kid=kid, algorithm=algorithm)
        if match is not None:
            return match

        # A single-flight refresh allows newly rotated keys while preventing
        # attacker-controlled unknown kids from creating a JWKS fetch storm.
        async with self._refresh_lock:
            now = time.monotonic()
            if self._is_negative_key(cache_key, now=now):
                raise JWTVerificationError("unknown signing key")
            if self._cached_keys is not None:
                match = self._find_matching_jwk(
                    self._cached_keys[1],
                    kid=kid,
                    algorithm=algorithm,
                )
                if match is not None:
                    return match
            if now - self._last_refresh_at >= self._refresh_cooldown_seconds:
                keys = await self._fetch_jwks(now=now)
                match = self._find_matching_jwk(
                    keys,
                    kid=kid,
                    algorithm=algorithm,
                )
                if match is not None:
                    return match
            self._remember_negative_key(cache_key, now=now)
        raise JWTVerificationError("unknown signing key")

    @staticmethod
    def _verify_signature(
        *,
        algorithm: str,
        jwk: Mapping[str, Any],
        signing_input: bytes,
        signature: bytes,
    ) -> None:
        try:
            if algorithm == "RS256":
                if jwk.get("kty") != "RSA":
                    raise JWTVerificationError("invalid signing key")
                modulus = int.from_bytes(_b64url_decode(str(jwk["n"])), "big")
                exponent = int.from_bytes(_b64url_decode(str(jwk["e"])), "big")
                public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
                public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
                return
            if algorithm == "ES256":
                if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
                    raise JWTVerificationError("invalid signing key")
                if len(signature) != 64:
                    raise JWTVerificationError("invalid signature")
                x = int.from_bytes(_b64url_decode(str(jwk["x"])), "big")
                y = int.from_bytes(_b64url_decode(str(jwk["y"])), "big")
                public_key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
                r = int.from_bytes(signature[:32], "big")
                s = int.from_bytes(signature[32:], "big")
                public_key.verify(
                    encode_dss_signature(r, s),
                    signing_input,
                    ec.ECDSA(hashes.SHA256()),
                )
                return
        except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
            raise JWTVerificationError("invalid signature") from exc
        raise JWTVerificationError("unsupported signing algorithm")

    async def verify(self, token: str) -> AuthPrincipal:
        if len(token) > 16_384:
            raise JWTVerificationError("token too large")
        parts = token.split(".")
        if len(parts) != 3:
            raise JWTVerificationError("malformed token")
        encoded_header, encoded_claims, encoded_signature = parts
        header = _strict_json(_b64url_decode(encoded_header))
        claims = _strict_json(_b64url_decode(encoded_claims))
        algorithm = header.get("alg")
        kid = header.get("kid")
        if (
            algorithm not in self._ALGORITHMS
            or not isinstance(kid, str)
            or not _SAFE_KEY_ID.fullmatch(kid)
        ):
            raise JWTVerificationError("unsupported token")
        if header.get("typ", "JWT") != "JWT":
            raise JWTVerificationError("unsupported token")
        jwk = await self._matching_jwk(kid=kid, algorithm=algorithm)
        self._verify_signature(
            algorithm=algorithm,
            jwk=jwk,
            signing_input=f"{encoded_header}.{encoded_claims}".encode("ascii"),
            signature=_b64url_decode(encoded_signature),
        )

        now = int(time.time())
        exp = _integer_claim(claims, "exp", required=True)
        nbf = _integer_claim(claims, "nbf", required=False)
        issued_at = _integer_claim(claims, "iat", required=True)
        assert exp is not None
        assert issued_at is not None
        if now >= exp:
            raise JWTVerificationError("token expired")
        if nbf is not None and now + self._clock_skew_seconds < nbf:
            raise JWTVerificationError("token not active")
        if issued_at > now + self._clock_skew_seconds:
            raise JWTVerificationError("token issued in the future")
        if claims.get("iss") != self.issuer:
            raise JWTVerificationError("invalid issuer")
        audiences = _audiences(claims.get("aud"))
        if self.allowed_audiences.isdisjoint(audiences):
            raise JWTVerificationError("invalid audience")
        subject = claims.get("sub")
        if not isinstance(subject, str):
            raise JWTVerificationError("invalid subject")
        try:
            account_id = str(UUID(subject))
        except ValueError as exc:
            raise JWTVerificationError("invalid subject") from exc
        role = claims.get("role")
        if role != "authenticated":
            raise JWTVerificationError("invalid role")
        aal = claims.get("aal", "aal1")
        if aal not in {"aal1", "aal2"}:
            raise JWTVerificationError("invalid assurance level")
        return AuthPrincipal(
            account_id=account_id,
            aal=aal,
            role=role,
            issuer=self.issuer,
            audience=audiences,
        )


class StaticTokenVerifier:
    """Explicit test double; never selected by environment-based app startup."""

    def __init__(self, tokens: Mapping[str, AuthPrincipal]):
        self._tokens = dict(tokens)

    async def close(self) -> None:
        return None

    async def verify(self, token: str) -> AuthPrincipal:
        try:
            return self._tokens[token]
        except KeyError as exc:
            raise JWTVerificationError("invalid token") from exc
