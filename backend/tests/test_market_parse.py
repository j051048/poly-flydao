from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from polybot.market import PolymarketMarketData, _datetime, _decimal, _resolved_outcome
from polybot.models import Outcome


class FakePaginator:
    def __init__(self, items):
        self._items = items

    def iter_items(self):
        return iter(self._items)

    def first_page(self):
        return {"items": self._items}


class FakeMarketClient:
    def __init__(self, markets, book=None):
        self.markets = markets
        self.book = book or {}

    def list_markets(self, **kwargs):
        condition_ids = kwargs.get("condition_ids")
        if condition_ids:
            items = [
                market
                for market in self.markets
                if market.get("condition_id") in condition_ids
            ]
        else:
            items = self.markets
        return FakePaginator(items)

    def get_market(self, *, id):
        return next(market for market in self.markets if market.get("id") == id)

    def get_order_book(self, *, token_id):
        return self.book[token_id]


def _raw_market(**overrides):
    raw = {
        "id": "gamma-1",
        "condition_id": "condition-1",
        "question": "Will the market resolve?",
        "outcomes": {
            "yes": {"token_id": "yes-1", "label": "Yes"},
            "no": {"token_id": "no-1", "label": "No"},
        },
        "state": {"active": True, "closed": False, "accepting_orders": True},
        "trading": {
            "minimum_order_size": "1",
            "minimum_tick_size": "0.01",
            "fees_enabled": True,
            "fee_schedule": {"rate": "0.01", "exponent": "1", "taker_only": True},
        },
        "metrics": {"liquidity": "50000", "volume_24hr": "12000"},
    }
    raw.update(overrides)
    return raw


def test_map_market_parses_full_gamma_payload() -> None:
    market = PolymarketMarketData._map_market(_raw_market())

    assert market.id == "gamma-1"
    assert market.condition_id == "condition-1"
    assert market.yes_token_id == "yes-1"
    assert market.no_token_id == "no-1"
    assert market.liquidity_usd == Decimal("50000")
    assert market.volume_24h_usd == Decimal("12000")
    assert market.fee_rate == Decimal("0.01")
    assert market.fees_enabled is True


def test_map_market_skips_invalid_rows_in_list() -> None:
    client = FakeMarketClient(
        [
            _raw_market(),
            {"id": "broken", "outcomes": {}},  # invalid: missing tokens
            _raw_market(id="gamma-2", condition_id="condition-2"),
        ]
    )
    data = PolymarketMarketData(client)

    markets = data._list_markets_sync(limit=10)

    assert [market.id for market in markets] == ["gamma-1", "gamma-2"]


def test_resolved_outcome_reads_explicit_winner() -> None:
    raw = _raw_market(
        state={"active": False, "closed": True},
        resolution={"outcome": "NO"},
    )
    yes = {"token_id": "yes-1", "label": "Yes"}
    no = {"token_id": "no-1", "label": "No"}
    assert _resolved_outcome(raw, yes, no, closed=True) is Outcome.NO


def test_resolved_outcome_requires_closed() -> None:
    raw = _raw_market(resolution={"outcome": "YES"})
    yes = {"token_id": "yes-1", "label": "Yes"}
    no = {"token_id": "no-1", "label": "No"}
    assert _resolved_outcome(raw, yes, no, closed=False) is None


def test_get_market_by_condition_resolves_gamma_id() -> None:
    client = FakeMarketClient([_raw_market()])
    data = PolymarketMarketData(client)

    market = data._get_market_by_condition_sync("condition-1")

    assert market.id == "gamma-1"


def test_get_market_by_condition_missing_raises() -> None:
    client = FakeMarketClient([])
    data = PolymarketMarketData(client)
    with pytest.raises(LookupError):
        data._get_market_by_condition_sync("missing")


def test_map_book_parses_levels() -> None:
    raw = {
        "bids": [{"price": "0.40", "size": "100"}],
        "asks": [{"price": "0.41", "size": "50"}],
        "tick_size": "0.01",
        "min_order_size": "1",
        "timestamp": "2026-01-01T00:00:00Z",
    }
    book = PolymarketMarketData._map_book(raw, market_id="gamma-1", token_id="yes-1")

    assert book.best_bid == Decimal("0.40")
    assert book.best_ask == Decimal("0.41")
    assert book.minimum_order_size == Decimal("1")
    assert book.market_id == "gamma-1"


def test_datetime_helper_handles_none_epoch_iso_and_naive() -> None:
    assert _datetime(None) is None
    assert _datetime("") is None
    epoch = _datetime(1700000000)
    assert epoch is not None and epoch.tzinfo == UTC
    millis = _datetime(1700000000000)
    assert millis is not None and millis.year == 2023
    parsed = _datetime("2026-01-01T00:00:00Z")
    assert parsed is not None and parsed.tzinfo == UTC
    naive = _datetime("2026-01-01T00:00:00")
    assert naive is not None and naive.tzinfo == UTC
    aware = _datetime(datetime(2026, 1, 1, tzinfo=UTC))
    assert aware is not None and aware.tzinfo == UTC


def test_decimal_helper_uses_default_for_none() -> None:
    assert _decimal(None, "7") == Decimal("7")
    assert _decimal("1.5") == Decimal("1.5")
