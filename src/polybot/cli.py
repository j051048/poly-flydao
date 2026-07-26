from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

from polybot.backtest import load_jsonl, report_json, run_backtest
from polybot.config import BETA_SDK_ACK_TEXT, LIVE_ACK_TEXT, TradingMode, get_settings
from polybot.research import inspect_nautilus_runtime
from polybot.runtime import build_runtime


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="polybot")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("config-check", help="validate settings without exposing secrets")
    commands.add_parser(
        "generate-secrets",
        help="generate operational Fernet/admin secrets and acknowledgement values",
    )
    commands.add_parser(
        "wallet-info",
        help="derive/deploy the SDK Deposit Wallet and print its funding address",
    )
    commands.add_parser("cycle", help="run exactly one gated scan cycle")
    commands.add_parser("research-runtime", help="inspect the optional Nautilus runtime")
    backtest = commands.add_parser("backtest", help="replay timestamped JSONL opportunities")
    backtest.add_argument("--input", type=Path, required=True)
    return root


def main() -> None:
    args = parser().parse_args()
    if args.command == "generate-secrets":
        print(
            "\n".join(
                [
                    f"POLYBOT_SIGNED_PAYLOAD_KEY={Fernet.generate_key().decode()}",
                    f"POLYBOT_ADMIN_TOKEN={secrets.token_urlsafe(48)}",
                    f"POLYBOT_LIVE_ACK={LIVE_ACK_TEXT}",
                    f"POLYBOT_BETA_SDK_ACK={BETA_SDK_ACK_TEXT}",
                ]
            )
        )
        return
    settings = get_settings()
    if args.command == "wallet-info":
        if settings.polymarket_private_key is None:
            raise SystemExit("POLYMARKET_PRIVATE_KEY is required")
        from polymarket import SecureClient

        client = SecureClient.create(
            private_key=settings.polymarket_private_key.get_secret_value(),
            wallet=settings.polymarket_deposit_wallet,
        )
        try:
            print(
                json.dumps(
                    {
                        "trading_wallet": str(client.wallet),
                        "signer": str(client.signer),
                        "wallet_type": str(client.wallet_type),
                        "chain_id": client.environment.chain_id,
                        "collateral_token": client.environment.collateral_token,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            client.close()
        return
    if args.command == "config-check":
        print(
            json.dumps(
                {
                    "valid": True,
                    "mode": settings.mode,
                    "ai_provider": settings.ai_provider,
                    "uses_supabase": settings.uses_supabase,
                    "live_locked": settings.mode.value in {"paper", "shadow"},
                },
                ensure_ascii=False,
            )
        )
        return
    if args.command == "backtest":
        print(report_json(run_backtest(load_jsonl(args.input), settings)))
        return
    if args.command == "research-runtime":
        print(inspect_nautilus_runtime().model_dump_json(indent=2))
        return
    if args.command == "cycle":
        if settings.mode in {TradingMode.CANARY, TradingMode.LIVE}:
            raise SystemExit("real-money cycles are restricted to the leased polybot-worker")
        report = asyncio.run(_run_cycle(settings))
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))


async def _run_cycle(settings):
    runtime = build_runtime(settings)
    try:
        return await runtime.engine.run_cycle()
    finally:
        await runtime.close()


if __name__ == "__main__":
    main()
