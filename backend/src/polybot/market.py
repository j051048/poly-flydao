from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from polybot.models import BookLevel, MarketSpec, OrderBookSnapshot, Outcome, utc_now


class MarketData(Protocol):
    async def list_markets(self, limit: int) -> list[MarketSpec]: ...

    async def get_market_by_condition(self, condition_id: str) -> MarketSpec: ...

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBookSnapshot: ...


def _get(obj: Any, *path: str, default: Any = None) -> Any:
    current = obj
    for part in path:
        if current is None:
            return default
        if isinstance(current, Mapping):
            current = current.get(part, default)
        else:
            current = getattr(current, part, default)
    return current


def _decimal(value: Any, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


def _datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
        epoch = float(value)
        if epoch > 10_000_000_000:
            epoch /= 1000
        return datetime.fromtimestamp(epoch, tz=UTC)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _resolved_outcome(raw: Any, yes: Any, no: Any, *, closed: bool) -> Outcome | None:
    """Read an explicit Gamma resolution without guessing from question text."""

    if not closed:
        return None
    yes_label = str(_get(yes, "label", default="Yes") or "Yes").casefold()
    no_label = str(_get(no, "label", default="No") or "No").casefold()
    resolution = _get(raw, "resolution", default={})
    candidates = (
        _get(resolution, "outcome"),
        _get(resolution, "result"),
        _get(resolution, "winning_outcome"),
        _get(raw, "resolved_outcome"),
        _get(raw, "winning_outcome"),
    )
    for value in candidates:
        normalized = str(value or "").strip().casefold()
        if normalized in {"yes", yes_label}:
            return Outcome.YES
        if normalized in {"no", no_label}:
            return Outcome.NO
    yes_winner = _get(yes, "winner")
    no_winner = _get(no, "winner")
    if yes_winner is True and no_winner is not True:
        return Outcome.YES
    if no_winner is True and yes_winner is not True:
        return Outcome.NO
    try:
        yes_price = _decimal(_get(yes, "price"), "-1")
        no_price = _decimal(_get(no, "price"), "-1")
    except Exception:
        return None
    if yes_price == 1 and no_price == 0:
        return Outcome.YES
    if no_price == 1 and yes_price == 0:
        return Outcome.NO
    return None


class PolymarketMarketData:
    """Thin adapter around the official unified `polymarket-client` SDK."""

    def __init__(self, client: Any | None = None):
        if client is None:
            from polymarket import PublicClient

            client = PublicClient()
        self.client = client

    async def list_markets(self, limit: int) -> list[MarketSpec]:
        return await asyncio.to_thread(self._list_markets_sync, limit)

    async def get_market_by_condition(self, condition_id: str) -> MarketSpec:
        return await asyncio.to_thread(self._get_market_by_condition_sync, condition_id)

    def _list_markets_sync(self, limit: int) -> list[MarketSpec]:
        paginator = self.client.list_markets(
            closed=False,
            # The unified SDK passes sort names through to Gamma's keyset
            # endpoint.  Its live service currently accepts the JSON response
            # field ``liquidityNum``; ``liquidity`` sorts the legacy string
            # field lexicographically, while the documented snake-case name is
            # rejected.  The numeric JSON field is therefore intentional.
            order="liquidityNum",
            ascending=False,
            page_size=min(limit, 100),
        )
        items: Iterable[Any]
        if hasattr(paginator, "iter_items"):
            items = paginator.iter_items()
        else:
            page = paginator.first_page()
            items = _get(page, "items", default=page)
        markets: list[MarketSpec] = []
        for raw in items:
            try:
                market = self._map_market(raw)
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
            markets.append(market)
            if len(markets) >= limit:
                break
        return markets

    def _get_market_by_condition_sync(self, condition_id: str) -> MarketSpec:
        """Resolve a CTF condition to its Gamma id, then fetch that exact market.

        The official Position model does not expose the Gamma market id. The
        condition-filtered list call supplies that id and `get_market(id=...)`
        supplies the authoritative single-market response.
        """

        paginator = self.client.list_markets(
            condition_ids=[condition_id],
            page_size=1,
        )
        if hasattr(paginator, "iter_items"):
            raw_reference = next(iter(paginator.iter_items()), None)
        else:
            page = paginator.first_page()
            items = _get(page, "items", default=page) or []
            raw_reference = next(iter(items), None)
        if raw_reference is None:
            raise LookupError(f"no market found for condition {condition_id}")

        market_id = _get(raw_reference, "id")
        if market_id is None:
            raise ValueError("condition lookup returned a market without an id")
        raw = self.client.get_market(id=str(market_id))
        market = self._map_market(raw)
        if market.condition_id != condition_id:
            raise ValueError("condition lookup returned a different market")
        return market

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBookSnapshot:
        raw = await asyncio.to_thread(self.client.get_order_book, token_id=token_id)
        return self._map_book(raw, market_id=market_id, token_id=token_id)

    @staticmethod
    def _map_market(raw: Any) -> MarketSpec:
        yes = _get(raw, "outcomes", "yes")
        no = _get(raw, "outcomes", "no")
        trading = _get(raw, "trading", default={})
        fee = _get(trading, "fee_schedule", default={})
        state = _get(raw, "state", default={})
        metrics = _get(raw, "metrics", default={})
        resolution = _get(raw, "resolution", default={})
        events = _get(raw, "events", default=[]) or []
        first_event = events[0] if events else None
        raw_tags = _get(raw, "tags", default=[]) or []
        raw_market_id = _get(raw, "id", default=_get(raw, "condition_id"))
        yes_token_id = _get(yes, "token_id")
        no_token_id = _get(no, "token_id")
        if raw_market_id is None or not yes_token_id or not no_token_id:
            raise ValueError("binary market is missing an id or outcome token")
        market_id = str(raw_market_id)
        description = str(_get(raw, "description", default=""))
        closed = bool(_get(state, "closed", default=False))
        return MarketSpec(
            id=market_id,
            condition_id=_get(raw, "condition_id"),
            event_id=(
                str(_get(raw, "event_id") or _get(first_event, "id"))
                if (_get(raw, "event_id") or _get(first_event, "id")) is not None
                else None
            ),
            slug=_get(raw, "slug"),
            event_slug=_get(first_event, "slug") or _get(raw, "event_slug"),
            question=str(_get(raw, "question", default=_get(raw, "title", default=""))),
            description=description,
            category=str(_get(raw, "category", default="other") or "other"),
            tags=tuple(
                str(value)
                for tag in raw_tags
                if (value := (_get(tag, "label") or _get(tag, "slug"))) is not None
            ),
            resolution_rules=str(_get(raw, "rules", default=description)),
            resolution_source=(_get(resolution, "source") or _get(raw, "resolution_source")),
            yes_token_id=str(yes_token_id),
            no_token_id=str(no_token_id),
            yes_label=str(_get(yes, "label", default="Yes") or "Yes"),
            no_label=str(_get(no, "label", default="No") or "No"),
            active=bool(_get(state, "active", default=True)),
            closed=closed,
            resolved_outcome=_resolved_outcome(raw, yes, no, closed=closed),
            accepting_orders=bool(_get(state, "accepting_orders", default=True)),
            neg_risk=bool(_get(state, "neg_risk", default=False)),
            liquidity_usd=_decimal(_get(metrics, "liquidity")),
            volume_24h_usd=_decimal(_get(metrics, "volume_24hr")),
            minimum_order_size=_decimal(_get(trading, "minimum_order_size"), "1"),
            tick_size=_decimal(_get(trading, "minimum_tick_size"), "0.01"),
            fees_enabled=bool(_get(trading, "fees_enabled", default=False)),
            fee_rate=_decimal(_get(fee, "rate")),
            fee_exponent=_decimal(_get(fee, "exponent"), "1"),
            fee_taker_only=bool(_get(fee, "taker_only", default=True)),
            maker_rebate_rate=_decimal(_get(fee, "rebate_rate")),
            start_at=_datetime(_get(state, "start_date") or _get(raw, "start_date")),
            end_at=_datetime(_get(state, "end_date") or _get(raw, "end_date")),
            updated_at=utc_now(),
        )

    @staticmethod
    def _map_book(raw: Any, *, market_id: str, token_id: str) -> OrderBookSnapshot:
        def levels(name: str) -> list[BookLevel]:
            source = _get(raw, name, default=[]) or []
            return [
                BookLevel(price=_decimal(_get(level, "price")), size=_decimal(_get(level, "size")))
                for level in source
            ]

        timestamp = _datetime(_get(raw, "timestamp")) or utc_now()
        return OrderBookSnapshot(
            market_id=market_id,
            token_id=token_id,
            bids=levels("bids"),
            asks=levels("asks"),
            tick_size=_decimal(_get(raw, "tick_size"), "0.01"),
            minimum_order_size=_decimal(_get(raw, "min_order_size"), "1"),
            captured_at=timestamp,
        )


class StaticMarketData:
    """Deterministic source used by tests, demos, and offline replay."""

    def __init__(self, markets: list[MarketSpec], books: Mapping[str, OrderBookSnapshot]):
        self.markets = markets
        self.books = dict(books)

    async def list_markets(self, limit: int) -> list[MarketSpec]:
        return self.markets[:limit]

    async def get_market_by_condition(self, condition_id: str) -> MarketSpec:
        for market in self.markets:
            if market.condition_id == condition_id or market.id == condition_id:
                return market
        raise LookupError(f"no static market found for condition {condition_id}")

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBookSnapshot:
        return self.books[token_id].model_copy(update={"market_id": market_id})


class StreamingPolymarketMarketData(PolymarketMarketData):
    """Official CLOB WebSocket cache with REST snapshot/recovery fallback."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        async_client: Any | None = None,
        max_cache_age_seconds: int = 5,
    ):
        super().__init__(client)
        if async_client is None:
            from polymarket import AsyncPublicClient

            async_client = AsyncPublicClient()
        self.async_client = async_client
        self.max_cache_age_seconds = max_cache_age_seconds
        self.rest_validation_seconds = 30
        self._cache: dict[str, OrderBookSnapshot] = {}
        self._rest_validated_at: dict[str, datetime] = {}
        self._token_markets: dict[str, str] = {}
        self._desired_tokens: tuple[str, ...] = ()
        self._subscription_task: asyncio.Task[None] | None = None
        self._handle: Any | None = None
        self._change = asyncio.Event()
        self._closed = False

    async def list_markets(self, limit: int) -> list[MarketSpec]:
        markets = await super().list_markets(limit)
        tokens: list[str] = []
        for market in markets:
            self._token_markets[market.yes_token_id] = market.id
            self._token_markets[market.no_token_id] = market.id
            tokens.extend((market.yes_token_id, market.no_token_id))
        await self.ensure_tokens(tokens)
        return markets

    async def get_market_by_condition(self, condition_id: str) -> MarketSpec:
        market = await super().get_market_by_condition(condition_id)
        self._token_markets[market.yes_token_id] = market.id
        self._token_markets[market.no_token_id] = market.id
        await self.ensure_tokens((*self._desired_tokens, market.yes_token_id, market.no_token_id))
        return market

    async def ensure_tokens(self, tokens: Iterable[str]) -> None:
        desired = tuple(sorted(set(tokens)))
        if desired == self._desired_tokens:
            return
        self._desired_tokens = desired
        self._change.set()
        if self._subscription_task is None or self._subscription_task.done():
            self._subscription_task = asyncio.create_task(
                self._subscription_loop(), name="polymarket-market-stream"
            )
        elif self._handle is not None:
            # Closing causes the loop to resubscribe atomically with the expanded token set.
            await self._handle.close()

    async def get_order_book(self, market_id: str, token_id: str) -> OrderBookSnapshot:
        cached = self._cache.get(token_id)
        if cached is not None:
            age = (utc_now() - cached.captured_at).total_seconds()
            validated_at = self._rest_validated_at.get(token_id)
            validation_age = (
                (utc_now() - validated_at).total_seconds()
                if validated_at is not None
                else float("inf")
            )
            if age <= self.max_cache_age_seconds and validation_age <= self.rest_validation_seconds:
                return cached.model_copy(update={"market_id": market_id})
        snapshot = await super().get_order_book(market_id, token_id)
        self._cache[token_id] = snapshot
        self._rest_validated_at[token_id] = utc_now()
        return snapshot

    async def _subscription_loop(self) -> None:
        from polymarket.streams import MarketSpec as StreamMarketSpec

        backoff = 1.0
        while not self._closed:
            tokens = self._desired_tokens
            self._change.clear()
            if not tokens:
                await self._change.wait()
                continue
            try:
                self._handle = await self.async_client.subscribe(StreamMarketSpec(token_ids=tokens))
                backoff = 1.0
                async for event in self._handle:
                    if self._closed:
                        break
                    self._apply_event(event)
                if tokens != self._desired_tokens:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                # REST remains authoritative while the stream reconnects.
                try:
                    await asyncio.wait_for(self._change.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 30)
            finally:
                handle, self._handle = self._handle, None
                if handle is not None:
                    with contextlib.suppress(Exception):
                        await handle.close()

    def _apply_event(self, event: Any) -> None:
        event_type = getattr(event, "type", None)
        payload = getattr(event, "payload", None)
        if event_type == "book" and payload is not None:
            token_id = str(payload.token_id)
            market_id = self._token_markets.get(token_id, str(payload.market))
            current = self._cache.get(token_id)
            tick_size = getattr(payload, "tick_size", None)
            minimum_order_size = getattr(payload, "min_order_size", None)
            self._cache[token_id] = OrderBookSnapshot(
                token_id=token_id,
                market_id=market_id,
                bids=[BookLevel(price=level.price, size=level.size) for level in payload.bids],
                asks=[BookLevel(price=level.price, size=level.size) for level in payload.asks],
                tick_size=(
                    tick_size or (current.tick_size if current is not None else Decimal("0.01"))
                ),
                minimum_order_size=(
                    minimum_order_size
                    or (current.minimum_order_size if current is not None else Decimal("1"))
                ),
                captured_at=payload.timestamp or utc_now(),
            )
            # The SDK's official market-book WebSocket shape makes both
            # constraints optional. Never label a stream event as REST
            # validated; when either value is absent, force the next consumer
            # read through REST before the cached book can be used.
            if tick_size is None or minimum_order_size is None:
                self._rest_validated_at.pop(token_id, None)
            return
        if event_type == "tick_size_change" and payload is not None:
            token_id = str(payload.token_id)
            current = self._cache.get(token_id)
            if current is not None:
                self._cache[token_id] = current.model_copy(
                    update={
                        "tick_size": payload.new_tick_size,
                        "captured_at": payload.timestamp or utc_now(),
                    }
                )
                self._rest_validated_at.pop(token_id, None)
            return
        if event_type == "market_resolved" and payload is not None:
            for token_id in payload.token_ids or ():
                self._cache.pop(str(token_id), None)
                self._rest_validated_at.pop(str(token_id), None)
            return
        if event_type != "price_change" or payload is None:
            return
        timestamp = payload.timestamp or utc_now()
        for change in payload.price_changes:
            token_id = str(change.token_id)
            current = self._cache.get(token_id)
            if current is None:
                continue
            field = "bids" if change.side == "BUY" else "asks"
            levels = [level for level in getattr(current, field) if level.price != change.price]
            if change.size > 0:
                levels.append(BookLevel(price=change.price, size=change.size))
            self._cache[token_id] = current.model_copy(
                update={field: levels, "captured_at": timestamp}
            )

    async def close(self) -> None:
        self._closed = True
        self._change.set()
        if self._handle is not None:
            with contextlib.suppress(Exception):
                await self._handle.close()
        if self._subscription_task is not None:
            self._subscription_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._subscription_task
        await self.async_client.close()
