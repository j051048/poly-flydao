from __future__ import annotations

from supabase.lib.client_options import SyncClientOptions

from supabase import Client, create_client

# Every store call runs inside ``asyncio.to_thread``. The SDK default of 120s
# means one stalled request can pin a worker thread - and any shutdown that
# joins the executor - for two minutes. A trading loop needs a much tighter
# budget so a cycle is skipped and retried instead of freezing.
DEFAULT_SUPABASE_TIMEOUT_SECONDS = 15.0


def create_supabase_client(
    url: str,
    service_role_key: str,
    *,
    timeout_seconds: float = DEFAULT_SUPABASE_TIMEOUT_SECONDS,
) -> Client:
    if timeout_seconds <= 0:
        raise ValueError("supabase timeout must be positive")
    return create_client(
        url,
        service_role_key,
        options=SyncClientOptions(
            postgrest_client_timeout=timeout_seconds,
            storage_client_timeout=timeout_seconds,
            function_client_timeout=timeout_seconds,
        ),
    )
