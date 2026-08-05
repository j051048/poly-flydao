from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from openai import AsyncOpenAI, BadRequestError

from polybot.ai.openai_provider import SYSTEM_PROMPT, request_json
from polybot.ai.usage import Stopwatch, build_usage
from polybot.ai_endpoint import AddressResolver, validate_public_ai_base_url
from polybot.models import Forecast, ForecastPayload, ForecastRequest

EndpointValidator = Callable[[str], Awaitable[str]]


class PublicAIEndpointTransport(httpx.AsyncBaseTransport):
    """Re-check the destination immediately before every credential-bearing request."""

    def __init__(
        self,
        *,
        allowed_hosts: str = "",
        resolver: AddressResolver | None = None,
    ):
        self._allowed_hosts = allowed_hosts
        self._resolver = resolver
        self._transport = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await validate_public_ai_base_url(
            str(request.url),
            allowed_hosts=self._allowed_hosts,
            resolver=self._resolver,
        )
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


class OpenAICompatibleForecastProvider:
    """OpenAI-compatible relay with fail-closed endpoint and redirect handling."""

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str,
        timeout_seconds: float = 45,
        allowed_hosts: str = "",
        resolver: AddressResolver | None = None,
        client: AsyncOpenAI | None = None,
        endpoint_validator: EndpointValidator | None = None,
    ):
        self.api_base = api_base
        self._json_schema_unsupported_models: set[str] = set()
        self._usage_log: list = []
        self._endpoint_validator = endpoint_validator or (
            lambda value: validate_public_ai_base_url(
                value,
                allowed_hosts=allowed_hosts,
                resolver=resolver,
            )
        )
        self._owns_client = client is None
        if client is not None:
            self.client = client
            return

        http_client = httpx.AsyncClient(
            transport=PublicAIEndpointTransport(
                allowed_hosts=allowed_hosts,
                resolver=resolver,
            ),
            follow_redirects=False,
            timeout=timeout_seconds,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=timeout_seconds,
            max_retries=0,
            http_client=http_client,
        )

    async def forecast(self, request: ForecastRequest, *, model: str) -> Forecast:
        # This second check runs after profile validation and immediately before
        # the SDK call. The HTTP transport performs the same check once more.
        await self._endpoint_validator(self.api_base)
        stopwatch = Stopwatch()
        schema = ForecastPayload.model_json_schema()
        serialized_schema = json.dumps(
            schema,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        options: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Perspective: {request.perspective}\n"
                        f"DATA:\n{request_json(request)}\n"
                        "Return exactly one JSON object with no markdown, matching "
                        f"this JSON Schema:\n{serialized_schema}"
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": 1800,
        }
        if model in self._json_schema_unsupported_models:
            response = await self.client.chat.completions.create(**options)
        else:
            try:
                response = await self.client.chat.completions.create(
                    **options,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "forecast",
                            "strict": True,
                            "schema": schema,
                        },
                    },
                )
            except BadRequestError as exc:
                if not _structured_output_unsupported(exc):
                    raise
                self._json_schema_unsupported_models.add(model)
                response = await self.client.chat.completions.create(**options)

        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        self._usage_log.append(build_usage(
            provider="openai_compatible",
            model=model,
            market_id=request.market.id,
            request_id=getattr(response, "id", None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=stopwatch.elapsed_ms(),
        ))
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("OpenAI-compatible relay returned an empty forecast")
        payload = ForecastPayload.model_validate(json.loads(content))
        allowed_ids = {item.id for item in request.evidence}
        return Forecast(
            market_id=request.market.id,
            **payload.model_dump(exclude={"source_ids"}),
            source_ids=[
                source_id for source_id in payload.source_ids if source_id in allowed_ids
            ],
            model=model,
        )

    def last_usage(self):
        return self._usage_log[-1] if self._usage_log else None

    def drain_usage(self) -> list:
        records = self._usage_log
        self._usage_log = []
        return records

    async def close(self) -> None:
        if self._owns_client:
            await self.client.close()


def _structured_output_unsupported(exc: BadRequestError) -> bool:
    if exc.status_code not in {400, 422}:
        return False
    message = str(exc).lower()
    return (
        any(
            marker in message
            for marker in ("response_format", "json_schema", "structured output")
        )
        and any(
            marker in message
            for marker in (
                "does not support",
                "doesn't support",
                "not supported",
                "unsupported",
                "unknown parameter",
                "unrecognized parameter",
            )
        )
        and not any(
            marker in message
            for marker in (
                "api key",
                "authentication",
                "unauthorized",
                "forbidden",
                "rate limit",
                "timeout",
                "content policy",
            )
        )
    )
