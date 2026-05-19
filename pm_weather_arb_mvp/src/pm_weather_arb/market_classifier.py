from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .normalize import extract_threshold_spec
from .types import Market
from .util import norm_text


UNIVERSAL_KINDS = {
    "YES_NO_BUY_BOTH",
    "YES_NO_SPLIT_SELL_BOTH",
    "NEGRISK_BUY_ALL_YES",
    "NEGRISK_BUY_ALL_NO",
}

SEMANTIC_KINDS = {
    "THRESHOLD_NESTED_BUY_SUPER_YES_SUB_NO",
}

WEATHER_TERMS = (
    "weather",
    "temperature",
    "high temp",
    "low temp",
    "rain",
    "rainfall",
    "precip",
    "precipitation",
    "snow",
    "snowfall",
    "hurricane",
    "storm",
    "tornado",
    "wind",
    "fahrenheit",
    "degree",
    "degrees",
    "heat",
    "cold",
    "climate",
    "nws",
    "noaa",
)

POLITICS_TERMS = (
    "trump",
    "putin",
    "biden",
    "president",
    "election",
    "mayoral",
    "primary",
    "senate",
    "congress",
    "democratic",
    "republican",
    "ukraine",
    "israel",
    "tariff",
)

SPORTS_TERMS = (
    "nba",
    "nfl",
    "mlb",
    "nhl",
    "ufc",
    "soccer",
    "football",
    "basketball",
    "baseball",
    "championship",
)

CRYPTO_TERMS = (
    "bitcoin",
    "btc",
    "ethereum",
    "eth",
    "crypto",
    "solana",
    "xrp",
)

MACRO_TERMS = (
    "fed",
    "rate cut",
    "interest rate",
    "inflation",
    "cpi",
    "gdp",
    "recession",
)


@dataclass(frozen=True)
class MarketClassification:
    market_class: str
    has_numeric_threshold: bool
    threshold_strategy_allowed: bool


def classify_market(market: Market) -> MarketClassification:
    text = norm_text(
        market.event_title,
        market.event_slug,
        market.question,
        market.description,
        market.market_slug,
    )
    has_threshold = extract_threshold_spec(market) is not None

    if any(term in text for term in POLITICS_TERMS):
        market_class = "politics"
    elif any(term in text for term in SPORTS_TERMS):
        market_class = "sports"
    elif any(term in text for term in CRYPTO_TERMS):
        market_class = "crypto"
    elif any(term in text for term in MACRO_TERMS):
        market_class = "macro"
    elif has_threshold or any(term in text for term in WEATHER_TERMS):
        market_class = "weather"
    else:
        market_class = "other"

    return MarketClassification(
        market_class=market_class,
        has_numeric_threshold=has_threshold,
        threshold_strategy_allowed=has_threshold and market_class == "weather",
    )


def classify_markets(markets: Sequence[Market]) -> str:
    classes = {classify_market(market).market_class for market in markets}
    if not classes:
        return "other"
    if len(classes) == 1:
        return next(iter(classes))
    return "mixed"


def filter_threshold_markets(markets: Iterable[Market]) -> list[Market]:
    return [market for market in markets if classify_market(market).threshold_strategy_allowed]


def strategy_scope_for_kind(kind: str) -> str:
    if kind in SEMANTIC_KINDS:
        return "semantic"
    return "universal"


def semantic_required_for_kind(kind: str) -> bool:
    return kind in SEMANTIC_KINDS
