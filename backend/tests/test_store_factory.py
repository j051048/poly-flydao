from __future__ import annotations

import pytest

from polybot.stores.factory import AccountStoreFactory
from polybot.stores.supabase_store import TenantScopeError


class FakeClient:
    pass


async def test_factory_returns_independently_scoped_stores() -> None:
    client = FakeClient()
    factory = AccountStoreFactory(
        "https://example.supabase.co",
        "service-role",
        client=client,  # type: ignore[arg-type]
    )
    account_a = factory.for_account("account-a")
    account_b = factory.for_account("account-b")

    assert account_a.account_id == "account-a"
    assert account_b.account_id == "account-b"
    assert account_a.client is client
    assert account_b.client is client
    with pytest.raises(TenantScopeError):
        await account_a.get_runtime_control(account_b.account_id)


def test_factory_rejects_an_empty_account() -> None:
    factory = AccountStoreFactory(
        "https://example.supabase.co",
        "service-role",
        client=FakeClient(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="account_id"):
        factory.for_account("")
