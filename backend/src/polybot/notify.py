from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from polybot.ai_endpoint import validate_public_ai_base_url

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotificationMessage:
    title: str
    message: str
    severity: str = "info"


class Notifier:
    """Best-effort external alert channel (Telegram/Discord-style webhook).

    Notifications never block or crash the trading loop: every send is
    fire-and-forget with a bounded timeout. The webhook URL is validated as a
    public HTTPS endpoint and is never logged.
    """

    def __init__(
        self,
        webhook_url: str | None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10,
    ):
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)

    async def send(self, notification: NotificationMessage) -> bool:
        if not self.webhook_url:
            return False
        try:
            url = await validate_public_ai_base_url(self.webhook_url)
        except Exception:
            LOGGER.warning("notification webhook URL rejected; alerts disabled")
            return False
        payload = {
            "text": (
                f"[{notification.severity.upper()}] {notification.title}\n"
                f"{notification.message}"
            ),
            "title": notification.title,
            "message": notification.message,
            "severity": notification.severity,
        }
        try:
            response = await self._client.post(url, json=payload)
            response.raise_for_status()
            return True
        except Exception:
            LOGGER.warning("notification delivery failed", exc_info=True)
            return False

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def build_notifier(webhook_url: str | None) -> Notifier | None:
    if not webhook_url or not webhook_url.strip():
        return None
    return Notifier(webhook_url)
