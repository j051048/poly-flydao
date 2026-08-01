from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal

from polybot.ai.litellm_provider import LiteLLMForecastProvider
from polybot.ai.openai_compatible_provider import OpenAICompatibleForecastProvider
from polybot.ai.openai_provider import OpenAIForecastProvider
from polybot.ai_endpoint import UnsafeAIBaseURLError, validate_public_ai_base_url
from polybot.config import Settings
from polybot.credentials import (
    AIProvider,
    EnvelopeDecryptor,
    SecretKind,
    WorkerCredentialRepository,
)
from polybot.jobs import AIDiagnosticJob, SupabaseJobRepository
from polybot.models import ForecastRequest, MarketSpec

_PROVIDER_ENDPOINTS = {
    AIProvider.OPENROUTER: "https://openrouter.ai/api/v1",
    AIProvider.ANTHROPIC: "https://api.anthropic.com",
}


class AIDiagnosticWorker:
    """Worker-only credential/model check that never creates a trade intent."""

    def __init__(
        self,
        *,
        jobs: SupabaseJobRepository,
        credentials: WorkerCredentialRepository,
        decryptor: EnvelopeDecryptor,
        settings: Settings,
        owner_id: str,
        poll_interval_seconds: float = 2,
        logger: logging.Logger | None = None,
    ):
        self.jobs = jobs
        self.credentials = credentials
        self.decryptor = decryptor
        self.settings = settings
        self.owner_id = owner_id
        self.poll_interval_seconds = poll_interval_seconds
        self.logger = logger or logging.getLogger("polybot.ai_diagnostics")

    async def serve(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            job = await self.jobs.claim_ai_diagnostic(
                claimed_by=self.owner_id,
                lease_seconds=300,
            )
            if job is None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.poll_interval_seconds)
                except TimeoutError:
                    pass
                continue
            await self.process(job)

    async def process(self, job: AIDiagnosticJob) -> None:
        secret = bytearray()
        provider = None
        started = time.monotonic()
        try:
            allowed, _, _ = await self.jobs.consume_ai_budget(
                account_id=str(job.account_id),
                units=1,
            )
            if not allowed:
                raise ValueError("daily_budget_exhausted")
            credential = await self.credentials.get_envelope_for_worker(
                account_id=str(job.account_id),
                credential_id=job.credential_id,
                kind=SecretKind.AI_API_KEY,
            )
            if credential is None or credential.provider != job.provider.value:
                raise ValueError("credential_inactive")
            secret = bytearray(
                self.decryptor.decrypt(
                    credential.envelope,
                    account_id=str(job.account_id),
                    kind=SecretKind.AI_API_KEY,
                    provider=credential.provider,
                )
            )
            api_key = secret.decode("utf-8")
            provider = await self._provider(job, api_key)
            request = ForecastRequest(
                market=MarketSpec(
                    id="polybot-ai-diagnostic",
                    question="Will this diagnostic request produce valid structured JSON?",
                    description="Connectivity test only. No market or order is involved.",
                    resolution_rules="Return a calibrated probability using only this prompt.",
                    yes_token_id="diagnostic-yes",
                    no_token_id="diagnostic-no",
                    liquidity_usd=Decimal("0"),
                    accepting_orders=False,
                ),
                perspective="structured-output connectivity diagnostic",
            )
            result = await provider.forecast(request, model=job.model)
            latency_ms = int((time.monotonic() - started) * 1000)
            saved = await self.jobs.finish_ai_diagnostic(
                job=job,
                ok=True,
                result_summary={
                    "provider": job.provider.value,
                    "model": job.model,
                    "structured_output": True,
                    "latency_ms": latency_ms,
                    "checked_at": datetime.now(UTC).isoformat(),
                    "probability_valid": Decimal("0") <= result.probability_yes <= Decimal("1"),
                },
            )
            if saved is None:
                self.logger.warning("AI diagnostic completion lost its fence")
        except Exception as exc:
            self.logger.info("AI diagnostic failed (%s)", type(exc).__name__)
            await self.jobs.finish_ai_diagnostic(
                job=job,
                ok=False,
                result_summary={
                    "provider": job.provider.value,
                    "model": job.model,
                    "checked_at": datetime.now(UTC).isoformat(),
                },
                error_code=_error_code(exc),
            )
        finally:
            secret[:] = b"\x00" * len(secret)
            if provider is not None:
                with suppress(Exception):
                    await provider.close()

    async def _provider(self, job: AIDiagnosticJob, api_key: str):
        if job.provider is AIProvider.OPENAI:
            return OpenAIForecastProvider(
                api_key,
                timeout_seconds=self.settings.ai_timeout_seconds,
            )
        if job.provider in {
            AIProvider.ANTHROPIC,
            AIProvider.OPENROUTER,
            AIProvider.LITELLM,
        }:
            return LiteLLMForecastProvider(
                api_key=api_key,
                api_base=_PROVIDER_ENDPOINTS.get(
                    job.provider,
                    self.settings.litellm_base_url,
                ),
                timeout_seconds=self.settings.ai_timeout_seconds,
            )
        if job.provider is AIProvider.CUSTOM:
            try:
                base_url = await validate_public_ai_base_url(
                    job.ai_base_url or "",
                    allowed_hosts=self.settings.custom_ai_allowed_hosts,
                )
            except UnsafeAIBaseURLError as exc:
                raise ValueError("custom_endpoint_unsafe") from exc
            return OpenAICompatibleForecastProvider(
                api_key=api_key,
                api_base=base_url,
                timeout_seconds=self.settings.ai_timeout_seconds,
                allowed_hosts=self.settings.custom_ai_allowed_hosts,
            )
        raise ValueError("provider_unsupported")


def _error_code(exc: Exception) -> str:
    message = str(exc).lower()
    if "auth" in message or "api key" in message or "unauthorized" in message:
        return "ai_authentication_failed"
    if "rate limit" in message:
        return "ai_rate_limited"
    if "timeout" in message:
        return "ai_timeout"
    if isinstance(exc, UnicodeDecodeError):
        return "ai_credential_invalid"
    if isinstance(exc, ValueError) and message in {
        "credential_inactive",
        "custom_endpoint_unsafe",
        "provider_unsupported",
        "daily_budget_exhausted",
    }:
        return message
    return f"ai_{type(exc).__name__.lower()}"[:64]
