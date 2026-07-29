from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import unquote, urlsplit, urlunsplit

import idna

AddressResolver = Callable[[str, int], Awaitable[Sequence[str]]]

_BLOCKED_HOST_SUFFIXES = (
    ".internal",
    ".lan",
    ".local",
    ".localhost",
    ".localdomain",
    ".onion",
)


class UnsafeAIBaseURLError(ValueError):
    """The tenant supplied an endpoint that is unsafe for credential-bearing calls."""


def normalize_ai_base_url(value: str) -> str:
    """Return a canonical public-HTTPS candidate without performing DNS I/O."""

    candidate = value.strip()
    if not 8 <= len(candidate) <= 256:
        raise UnsafeAIBaseURLError("AI Base URL must contain 8 to 256 characters")
    if any(character.isspace() or ord(character) < 32 for character in candidate):
        raise UnsafeAIBaseURLError("AI Base URL cannot contain whitespace or control characters")

    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeAIBaseURLError("AI Base URL is malformed") from exc

    if parsed.scheme.lower() != "https":
        raise UnsafeAIBaseURLError("custom AI providers must use HTTPS")
    if not parsed.hostname:
        raise UnsafeAIBaseURLError("AI Base URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeAIBaseURLError("AI Base URL cannot contain credentials")
    if parsed.query or parsed.fragment:
        raise UnsafeAIBaseURLError("AI Base URL cannot contain a query string or fragment")
    if "\\" in parsed.path:
        raise UnsafeAIBaseURLError("AI Base URL path cannot contain backslashes")

    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or any(
        segment in {".", ".."} for segment in decoded_path.split("/")
    ):
        raise UnsafeAIBaseURLError("AI Base URL path cannot contain traversal segments")

    host = parsed.hostname.rstrip(".").lower()
    ip = _parse_address(host)
    if ip is None:
        try:
            host = idna.encode(host, uts46=True).decode("ascii").lower()
        except idna.IDNAError as exc:
            raise UnsafeAIBaseURLError("AI Base URL hostname is invalid") from exc
        if "." not in host or host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
            raise UnsafeAIBaseURLError("local and single-label AI hostnames are forbidden")
    else:
        _assert_global_address(ip)

    effective_port = port or 443
    if not 1 <= effective_port <= 65535:
        raise UnsafeAIBaseURLError("AI Base URL port is invalid")

    if ":" in host:
        netloc = f"[{host}]"
    else:
        netloc = host
    if effective_port != 443:
        netloc = f"{netloc}:{effective_port}"

    path = parsed.path.rstrip("/")
    return urlunsplit(("https", netloc, path, "", ""))


async def validate_public_ai_base_url(
    value: str,
    *,
    allowed_hosts: str = "",
    resolver: AddressResolver | None = None,
    timeout_seconds: float = 3.0,
) -> str:
    """Validate syntax, optional host allowlist, and every current DNS answer."""

    normalized = normalize_ai_base_url(value)
    parsed = urlsplit(normalized)
    host = parsed.hostname
    if host is None:
        raise UnsafeAIBaseURLError("AI Base URL must include a hostname")

    if allowed_hosts.strip() and not _host_is_allowed(host, allowed_hosts):
        raise UnsafeAIBaseURLError("AI Base URL host is not in the worker allowlist")

    literal = _parse_address(host)
    if literal is None:
        address_resolver = resolver or _system_resolver
        try:
            addresses = await asyncio.wait_for(
                address_resolver(host, parsed.port or 443),
                timeout=timeout_seconds,
            )
        except TimeoutError as exc:
            raise UnsafeAIBaseURLError("AI Base URL DNS lookup timed out") from exc
        except OSError as exc:
            raise UnsafeAIBaseURLError("AI Base URL hostname could not be resolved") from exc
        if not addresses:
            raise UnsafeAIBaseURLError("AI Base URL hostname returned no addresses")
        for address in addresses:
            resolved = _parse_address(address)
            if resolved is None:
                raise UnsafeAIBaseURLError("AI Base URL DNS returned an invalid address")
            _assert_global_address(resolved)
    else:
        _assert_global_address(literal)

    return normalized


async def _system_resolver(host: str, port: int) -> Sequence[str]:
    def resolve() -> list[str]:
        records = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        return sorted({str(record[4][0]) for record in records})

    return await asyncio.to_thread(resolve)


def _assert_global_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if not address.is_global:
        raise UnsafeAIBaseURLError(
            "AI Base URL must not resolve to private, loopback, link-local, "
            "reserved, multicast, or unspecified addresses"
        )


def _parse_address(
    value: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _host_is_allowed(host: str, allowed_hosts: str) -> bool:
    normalized_host = host.rstrip(".").lower()
    for item in allowed_hosts.split(","):
        rule = item.strip().rstrip(".").lower()
        if not rule:
            continue
        if rule.startswith("*."):
            suffix = rule[1:]
            if normalized_host.endswith(suffix) and normalized_host != suffix[1:]:
                return True
        elif normalized_host == rule:
            return True
    return False
