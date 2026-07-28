from __future__ import annotations

from dataclasses import dataclass

from polybot.stores.supabase_store import SupabaseStore
from supabase import Client, create_client


@dataclass(frozen=True)
class AccountStoreFactory:
    """Create immutable account-scoped stores from server-only credentials."""

    url: str
    service_role_key: str
    client: Client | None = None

    def for_account(self, account_id: str) -> SupabaseStore:
        if not account_id:
            raise ValueError("account_id is required")
        client = self.client or create_client(self.url, self.service_role_key)
        return SupabaseStore(
            self.url,
            self.service_role_key,
            account_id=account_id,
            client=client,
        )
