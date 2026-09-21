from __future__ import annotations

from dataclasses import dataclass

from polybot.stores.supabase_client import (
    DEFAULT_SUPABASE_TIMEOUT_SECONDS,
    create_supabase_client,
)
from polybot.stores.supabase_store import SupabaseStore
from supabase import Client


@dataclass(frozen=True)
class AccountStoreFactory:
    """Create immutable account-scoped stores from server-only credentials."""

    url: str
    service_role_key: str
    client: Client | None = None
    timeout_seconds: float = DEFAULT_SUPABASE_TIMEOUT_SECONDS

    def for_account(self, account_id: str) -> SupabaseStore:
        if not account_id:
            raise ValueError("account_id is required")
        client = self.client or create_supabase_client(
            self.url,
            self.service_role_key,
            timeout_seconds=self.timeout_seconds,
        )
        return SupabaseStore(
            self.url,
            self.service_role_key,
            account_id=account_id,
            client=client,
        )
