from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from polybot.market import PolymarketMarketData


class _Paginator:
    def __init__(self, items):
        self.items = items

    def iter_items(self):
        yield from self.items


class _RecordingClient:
    def __init__(self, items):
        self.items = items
        self.kwargs = None

    def list_markets(self, **kwargs):
        self.kwargs = kwargs
        return _Paginator(self.items)


def test_official_sdk_shape_maps_to_domain_model() -> None:
    raw = SimpleNamespace(
        id=123,
        condition_id="condition",
        question="Question?",
        description="Description",
        category="politics",
        events=(SimpleNamespace(id=456),),
        state=SimpleNamespace(
            active=True,
            closed=False,
            accepting_orders=True,
            neg_risk=False,
            end_date="2026-12-01T00:00:00Z",
        ),
        outcomes=SimpleNamespace(
            yes=SimpleNamespace(token_id="yes"),
            no=SimpleNamespace(token_id="no"),
        ),
        metrics=SimpleNamespace(liquidity=Decimal("1000"), volume_24hr=Decimal("200")),
        trading=SimpleNamespace(
            minimum_order_size=Decimal("5"),
            minimum_tick_size=Decimal("0.01"),
            fees_enabled=True,
            fee_schedule=SimpleNamespace(rate=Decimal("0.02"), exponent=2, taker_only=True),
        ),
        resolution=SimpleNamespace(source="official source"),
    )
    market = PolymarketMarketData._map_market(raw)
    assert market.id == "123"
    assert market.event_id == "456"
    assert market.yes_token_id == "yes"
    assert market.fee_exponent == Decimal("2")


def test_market_discovery_uses_keyset_numeric_liquidity_sort() -> None:
    client = _RecordingClient([])
    source = PolymarketMarketData(client=client)

    assert source._list_markets_sync(25) == []
    assert client.kwargs == {
        "closed": False,
        "order": "liquidityNum",
        "ascending": False,
        "page_size": 25,
    }
