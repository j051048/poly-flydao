from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "private_key",
    "secret",
    "client_secret",
    "service_role_key",
    "signed_payload",
    "signed_payload_ciphertext",
    "credential",
    "credential_value",
}
_SAFE_TOKEN_KEYS = {
    "token_id",
    "yes_token_id",
    "no_token_id",
    "outcome_token_id",
    "fencing_token",
}
_TEXT_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:eyJ[A-Za-z0-9_-]{8,}\.){2}[A-Za-z0-9_-]{8,}\b"),
    # A raw EVM private key is 32 bytes.  Require a 0x prefix to avoid
    # redacting ordinary SHA-256 hashes used as public identifiers.
    re.compile(r"\b0x[a-fA-F0-9]{64}\b"),
)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    if normalized in _SAFE_TOKEN_KEYS:
        return False
    return normalized in _SENSITIVE_KEYS or normalized.endswith(("_api_key", "_private_key"))


def redact_value(value: Any, *, key: str | None = None) -> Any:
    """Return a log-safe copy without mutating the original value."""

    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_value(item) for item in value]
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        redacted = value
        for pattern in _TEXT_PATTERNS:
            redacted = pattern.sub(REDACTED, redacted)
        return redacted
    return value


class SecretRedactingFormatter(logging.Formatter):
    """Last-line defence for application and third-party log messages."""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return str(redact_value(rendered))


def configure_secure_logging(level: str | int = logging.INFO) -> None:
    """Configure a single structured, redacting stderr handler.

    Uvicorn may replace handlers when launched from its CLI.  Application code
    should still log dictionaries through ``safe_json`` and deployments must
    keep request-body/APM capture disabled on credential routes.
    """

    handler = logging.StreamHandler()
    handler.setFormatter(
        SecretRedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def safe_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(redact_value(payload), ensure_ascii=False, separators=(",", ":"), default=str)
