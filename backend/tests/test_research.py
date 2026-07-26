from decimal import Decimal

from polybot.models import BookLevel, OrderBookSnapshot
from polybot.research import export_order_books, inspect_nautilus_runtime


def test_research_export_is_versioned_jsonl(tmp_path) -> None:
    target = tmp_path / "books.jsonl"
    snapshot = OrderBookSnapshot(
        token_id="token",
        market_id="market",
        bids=[BookLevel(price=Decimal("0.4"), size=Decimal("2"))],
        asks=[BookLevel(price=Decimal("0.5"), size=Decimal("3"))],
    )
    assert export_order_books(target, [snapshot]) == 1
    content = target.read_text(encoding="utf-8")
    assert '"schema_version":1' in content
    assert '"kind":"polymarket_order_book"' in content


def test_research_runtime_probe_is_safe_when_optional_extra_is_absent() -> None:
    runtime = inspect_nautilus_runtime()
    assert runtime.package == "nautilus_trader"
    assert "signer" in runtime.boundary
