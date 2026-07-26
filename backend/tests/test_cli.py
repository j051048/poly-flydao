from __future__ import annotations

import json
import sys
from types import SimpleNamespace

from polymarket import SecureClient

from polybot.cli import main
from polybot.config import DEDICATED_WALLET_ACK_TEXT, get_settings


def test_generate_secrets_includes_dedicated_wallet_ack(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["polybot", "generate-secrets"])

    main()

    assert (
        f"POLYBOT_DEDICATED_WALLET_ACK={DEDICATED_WALLET_ACK_TEXT}"
        in capsys.readouterr().out
    )


def test_wallet_bootstrap_sets_approvals_and_never_prints_private_key(
    monkeypatch, capsys
) -> None:
    private_key = "0x" + "11" * 32

    class FakeClient:
        wallet = "0x" + "22" * 20
        signer = "0x" + "33" * 20
        wallet_type = "DEPOSIT"
        environment = SimpleNamespace(chain_id=137, collateral_token="0xCOLLATERAL")

        def __init__(self) -> None:
            self.approval_calls = 0
            self.closed = False

        def setup_trading_approvals(self) -> None:
            self.approval_calls += 1

        def get_balance_allowance(self, **kwargs):
            assert kwargs == {"asset_type": "COLLATERAL"}
            return SimpleNamespace(
                balance=5_000_000,
                allowances={"exchange": 2**256 - 1},
            )

        def close(self) -> None:
            self.closed = True

    client = FakeClient()

    def create(**kwargs):
        assert kwargs == {"private_key": private_key, "wallet": None}
        return client

    monkeypatch.setattr(SecureClient, "create", staticmethod(create))
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", private_key)
    monkeypatch.setenv("POLYBOT_MODE", "canary")
    monkeypatch.setattr(
        sys,
        "argv",
        ["polybot", "wallet-bootstrap", "--confirm-standard-allowances"],
    )
    get_settings.cache_clear()
    try:
        main()
    finally:
        get_settings.cache_clear()

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["trading_approvals"] == "ready"
    assert payload["trading_wallet"] == client.wallet
    assert client.approval_calls == 1
    assert client.closed
    assert private_key not in output
