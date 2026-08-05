"""Step-by-step worker startup preflight diagnosis against a cloud Supabase.

Reads credentials from backend/.env (git-ignored):

    SUPABASE_URL=https://<project-ref>.supabase.co
    SUPABASE_SERVICE_ROLE_KEY=...
    POLYBOT_ACCOUNT_ID=<owner auth user uuid>

Run:  python scripts/diagnose_worker.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "POLYBOT_ACCOUNT_ID",
        "SUPABASE_SERVICE_ROLE_KEY",
    ):
        values.setdefault(key, os.environ.get(key, ""))
    return values


def _ok(label: str, detail: str = "") -> None:
    print(f"  [OK] {label}{(' ' + detail) if detail else ''}")


def _fail(label: str, detail: str = "") -> None:
    print(f"  [FAIL] {label}{(' ' + detail) if detail else ''}")


async def _run(values: dict[str, str]) -> None:
    from polybot.schema import EXPECTED_SCHEMA_VERSION
    from supabase import create_client

    url = values["SUPABASE_URL"].rstrip("/")
    key = values["SUPABASE_SERVICE_ROLE_KEY"]
    account_id = values.get("POLYBOT_ACCOUNT_ID") or ""
    print(f"checking account_id={account_id}")
    client = create_client(url, key)

    # 1. schema version
    try:
        response = await asyncio.to_thread(
            client.rpc("polybot_schema_version", {}).execute
        )
        data = getattr(response, "data", None)
        if isinstance(data, list):
            data = data[0] if data else None
        if isinstance(data, dict):
            data = data.get("polybot_schema_version", data.get("version"))
        print(f"  schema version = {data!r} (expected {EXPECTED_SCHEMA_VERSION})")
        _ok("schema version query") if int(data or -1) == EXPECTED_SCHEMA_VERSION else _fail(
            "schema version",
            f"got {data!r}; apply missing migrations (0017/0018 or full set)",
        )
    except Exception as exc:
        _fail("schema version query", f"{type(exc).__name__}: {exc}")

    # 2. runtime_controls table
    try:
        await asyncio.to_thread(
            client.table("runtime_controls")
            .select("account_id")
            .eq("account_id", account_id)
            .limit(1)
            .execute
        )
        _ok("runtime_controls readable")
    except Exception as exc:
        _fail("runtime_controls readable", f"{type(exc).__name__}: {exc}")

    # 3. admin auth user lookup (the worker-specific gate)
    try:
        user_response = await asyncio.to_thread(
            client.auth.admin.get_user_by_id,
            account_id,
        )
        user = getattr(user_response, "user", None)
        user_id = str(getattr(user, "id", ""))
        if user is None or user_id != account_id:
            _fail(
                "auth user lookup",
                f"no matching Auth user for {account_id}; "
                "go to Supabase > Authentication > Users and copy the exact UUID",
            )
        else:
            _ok("auth user lookup", f"matched {user_id}")
    except Exception as exc:
        _fail("auth user lookup", f"{type(exc).__name__}: {exc}")

    # 4. incremental migration columns
    columns = (
        ("account_risk_state", "risk_day"),
        ("account_activities", "activity_key"),
        ("orders", "open_snapshot_miss_count,expires_at"),
        ("cycle_jobs", "risk_policy_version"),
        ("order_groups", "execution_enabled"),
        ("pair_inventory_events", "clob_trade_id"),
    )
    for table, column in columns:
        try:
            await asyncio.to_thread(
                client.table(table)
                .select(column)
                .eq("account_id", account_id)
                .limit(1)
                .execute
            )
            _ok(f"table {table} column {column}")
        except Exception as exc:
            _fail(f"table {table} column {column}", f"{type(exc).__name__}: {exc}")

    # 5. expire_runtime_control RPC (migration 0005 grant)
    try:
        await asyncio.to_thread(
            client.rpc(
                "expire_runtime_control",
                {
                    "p_account_id": account_id,
                    "p_mode": "canary",
                    "p_expected_version": 0,
                },
            ).execute
        )
        _ok("expire_runtime_control RPC")
    except Exception as exc:
        _fail("expire_runtime_control RPC", f"{type(exc).__name__}: {exc}")


def main() -> None:
    values = _load_env()
    missing = [
        key
        for key in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "POLYBOT_ACCOUNT_ID")
        if not values.get(key)
    ]
    if missing:
        raise SystemExit(
            "missing in backend/.env: " + ", ".join(missing)
        )
    asyncio.run(_run(values))


if __name__ == "__main__":
    main()
