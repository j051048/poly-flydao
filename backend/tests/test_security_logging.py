from __future__ import annotations

import logging

from polybot.security_logging import REDACTED, SecretRedactingFormatter, redact_value, safe_json


def test_recursive_redaction_preserves_public_token_ids() -> None:
    private_key = "0x" + "ab" * 32
    payload = {
        "api_key": "sk-super-secret-value",
        "nested": {"private_key": private_key, "token_id": "123456789"},
        "message": f"Authorization: Bearer {'x' * 32}",
    }

    redacted = redact_value(payload)

    assert redacted["api_key"] == REDACTED
    assert redacted["nested"]["private_key"] == REDACTED
    assert redacted["nested"]["token_id"] == "123456789"
    assert "x" * 32 not in redacted["message"]
    assert private_key not in safe_json(payload)


def test_formatter_redacts_exception_and_message_text() -> None:
    formatter = SecretRedactingFormatter("%(levelname)s %(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="provider failed with sk-abcdefghijklmnop",
        args=(),
        exc_info=None,
    )

    rendered = formatter.format(record)

    assert "sk-abcdefghijklmnop" not in rendered
    assert REDACTED in rendered
