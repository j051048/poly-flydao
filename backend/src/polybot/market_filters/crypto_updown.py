from __future__ import annotations

import re
from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator

from polybot.models import MarketSpec, Outcome

_ASSET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("BTC", re.compile(r"\b(?:btc|bitcoin)\b", re.IGNORECASE)),
    ("ETH", re.compile(r"\b(?:eth|ethereum)\b", re.IGNORECASE)),
    ("SOL", re.compile(r"\b(?:sol|solana)\b", re.IGNORECASE)),
    ("XRP", re.compile(r"\b(?:xrp|ripple)\b", re.IGNORECASE)),
)
# The only symbols with a real classifier. Configuring anything else would
# silently match nothing, so the filter refuses to be built with it.
SUPPORTED_ASSETS: frozenset[str] = frozenset(symbol for symbol, _ in _ASSET_PATTERNS)
_UP_LABELS = frozenset({"up", "higher", "increase", "above"})
_DOWN_LABELS = frozenset({"down", "lower", "decrease", "below"})
_UP_DOWN_CONTEXT = re.compile(
    r"(?:\bup\b.+\bdown\b|\bdown\b.+\bup\b|\bhigher\b.+\blower\b|\blower\b.+\bhigher\b)",
    re.IGNORECASE,
)


class CryptoUpDownFilterConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed_assets: frozenset[str] = frozenset({"BTC", "ETH"})
    minimum_duration: timedelta = timedelta(minutes=1)
    maximum_duration: timedelta = timedelta(days=1)
    require_resolution_source: bool = True
    allow_neg_risk: bool = False

    @field_validator("allowed_assets")
    @classmethod
    def _validate_allowed_assets(cls, value: frozenset[str]) -> frozenset[str]:
        if not value:
            raise ValueError("allowed_assets must contain at least one symbol")
        unsupported = sorted(value - SUPPORTED_ASSETS)
        if unsupported:
            supported = ", ".join(sorted(SUPPORTED_ASSETS))
            raise ValueError(
                "unsupported crypto Up/Down symbols: "
                + ", ".join(unsupported)
                + f" (supported: {supported})"
            )
        return value


class CryptoUpDownMarket(BaseModel):
    model_config = ConfigDict(frozen=True)

    market: MarketSpec
    asset_symbol: str
    up_outcome: Outcome
    down_outcome: Outcome
    up_token_id: str = Field(min_length=1)
    down_token_id: str = Field(min_length=1)


class CryptoUpDownFilter:
    """Fail-closed classifier for short-horizon crypto Up/Down markets.

    Token direction is derived from the raw Gamma outcome labels. The question
    or slug is never used to guess which token means Up, preventing an inverted
    trade when a market uses non-YES/NO display labels.
    """

    def __init__(self, config: CryptoUpDownFilterConfig | None = None):
        self.config = config or CryptoUpDownFilterConfig()

    def classify(self, market: MarketSpec) -> CryptoUpDownMarket | None:
        if not market.active or market.closed or not market.accepting_orders:
            return None
        if market.neg_risk and not self.config.allow_neg_risk:
            return None
        if self.config.require_resolution_source and not market.resolution_source:
            return None
        if market.start_at is None or market.end_at is None:
            return None
        duration = market.end_at - market.start_at
        if not self.config.minimum_duration <= duration <= self.config.maximum_duration:
            return None

        searchable = " ".join(
            value
            for value in (
                market.question,
                market.slug or "",
                market.event_slug or "",
                " ".join(market.tags),
            )
            if value
        )
        asset = next(
            (
                symbol
                for symbol, pattern in _ASSET_PATTERNS
                if symbol in self.config.allowed_assets and pattern.search(searchable)
            ),
            None,
        )
        if asset is None or not _UP_DOWN_CONTEXT.search(searchable):
            return None

        yes_direction = self._label_direction(market.yes_label)
        no_direction = self._label_direction(market.no_label)
        if {yes_direction, no_direction} != {"up", "down"}:
            return None
        if yes_direction == "up":
            return CryptoUpDownMarket(
                market=market,
                asset_symbol=asset,
                up_outcome=Outcome.YES,
                down_outcome=Outcome.NO,
                up_token_id=market.yes_token_id,
                down_token_id=market.no_token_id,
            )
        return CryptoUpDownMarket(
            market=market,
            asset_symbol=asset,
            up_outcome=Outcome.NO,
            down_outcome=Outcome.YES,
            up_token_id=market.no_token_id,
            down_token_id=market.yes_token_id,
        )

    def filter(self, markets: list[MarketSpec]) -> list[CryptoUpDownMarket]:
        return [
            classified
            for market in markets
            if (classified := self.classify(market)) is not None
        ]

    @staticmethod
    def _label_direction(label: str) -> str | None:
        normalized = re.sub(r"[^a-z]+", " ", label.casefold()).strip()
        if normalized in _UP_LABELS:
            return "up"
        if normalized in _DOWN_LABELS:
            return "down"
        return None
