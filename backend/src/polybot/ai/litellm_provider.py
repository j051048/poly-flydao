from __future__ import annotations

import json

from litellm import BadRequestError, UnsupportedParamsError, acompletion

from polybot.ai.openai_provider import SYSTEM_PROMPT, request_json
from polybot.models import Forecast, ForecastPayload, ForecastRequest


class LiteLLMForecastProvider:
    """Provider-neutral gateway integration using a strict JSON schema."""

    def __init__(self, *, api_key: str, api_base: str, timeout_seconds: float = 45):
        self.api_key = api_key
        self.api_base = api_base
        self.timeout_seconds = timeout_seconds
        self._json_schema_unsupported_models: set[str] = set()

    async def forecast(self, request: ForecastRequest, *, model: str) -> Forecast:
        schema = ForecastPayload.model_json_schema()
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Perspective: {request.perspective}\nDATA:\n{request_json(request)}\n"
                    "Return exactly one JSON object with no markdown, matching this JSON "
                    f"Schema:\n{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}"
                ),
            },
        ]
        request_options = {
            "model": model,
            "api_key": self.api_key,
            "api_base": self.api_base,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 1800,
            "timeout": self.timeout_seconds,
        }
        if model in self._json_schema_unsupported_models:
            response = await acompletion(**request_options)
        else:
            try:
                response = await acompletion(
                    **request_options,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": "forecast", "strict": True, "schema": schema},
                    },
                )
            except Exception as exc:
                if not _structured_output_unsupported(exc):
                    raise
                # Cache only an explicit provider capability mismatch. Auth,
                # rate-limit, timeout, transport and content-policy failures are
                # never retried without the server-side schema constraint.
                self._json_schema_unsupported_models.add(model)
                response = await acompletion(**request_options)
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("LiteLLM returned an empty forecast")
        payload = json.loads(content) if isinstance(content, str) else content
        parsed = ForecastPayload.model_validate(payload)
        allowed_ids = {item.id for item in request.evidence}
        return Forecast(
            market_id=request.market.id,
            **parsed.model_dump(exclude={"source_ids"}),
            source_ids=[source_id for source_id in parsed.source_ids if source_id in allowed_ids],
            model=model,
        )


def _structured_output_unsupported(exc: Exception) -> bool:
    # ContentPolicyViolationError and other semantic/policy failures subclass
    # BadRequestError. Exact types make the fallback a narrow capability
    # adapter instead of a generic second-chance request.
    if type(exc) not in {UnsupportedParamsError, BadRequestError}:
        return False
    if getattr(exc, "status_code", 400) not in {400, 422}:
        return False
    message = str(exc).lower()
    capability_markers = (
        "response_format",
        "json_schema",
        "structured output",
        "structured-output",
    )
    unsupported_markers = (
        "does not support",
        "doesn't support",
        "not supported",
        "unsupported",
        "unknown parameter",
        "unrecognized parameter",
        "not available",
    )
    forbidden_markers = (
        "authentication",
        "api key",
        "unauthorized",
        "forbidden",
        "rate limit",
        "too many requests",
        "timeout",
        "timed out",
        "content policy",
        "content-policy",
        "safety policy",
        "blocked content",
    )
    return (
        not any(marker in message for marker in forbidden_markers)
        and any(marker in message for marker in capability_markers)
        and any(marker in message for marker in unsupported_markers)
    )
