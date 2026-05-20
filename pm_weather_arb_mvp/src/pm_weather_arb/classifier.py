from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .types import Market
from .util import norm_text


@dataclass(frozen=True)
class MarketClassification:
    market_class: str
    reason: str


_CLASS_PATTERNS: list[tuple[str, list[str]]] = [
    (
        "weather",
        [
            r"\bweather\b",
            r"\btemperature\b",
            r"\btemp\b",
            r"\bhigh temperature\b",
            r"\blow temperature\b",
            r"\brain\b",
            r"\brainfall\b",
            r"\bprecip\b",
            r"\bprecipitation\b",
            r"\bsnow\b",
            r"\bsnowfall\b",
            r"\bhurricane\b",
            r"\bstorm\b",
            r"\btornado\b",
            r"\bwind\b",
            r"\bheat\b",
            r"\bcold\b",
            r"\bclimate\b",
            r"\bfahrenheit\b",
            r"°f",
            r"\b\d+(?:\.\d+)?\s*f\b",
            r"\binches?\b",
        ],
    ),
    (
        "politics",
        [
            r"\btrump\b",
            r"\bputin\b",
            r"\bbiden\b",
            r"\belection\b",
            r"\bpresident\b",
            r"\bcongress\b",
            r"\bsenate\b",
            r"\bwhite house\b",
            r"\bcabinet\b",
            r"\bminister\b",
            r"\bwar\b",
            r"\bukraine\b",
            r"\bisrael\b",
            r"\bceasefire\b",
            r"\btariff\b",
        ],
    ),
    (
        "sports",
        [
            r"\bnba\b",
            r"\bnfl\b",
            r"\bmlb\b",
            r"\bnhl\b",
            r"\bufc\b",
            r"\bsoccer\b",
            r"\bfootball\b",
            r"\btennis\b",
            r"\bwin the game\b",
            r"\bchampionship\b",
        ],
    ),
    (
        "crypto",
        [
            r"\bbitcoin\b",
            r"\bbtc\b",
            r"\beth\b",
            r"\bethereum\b",
            r"\bsolana\b",
            r"\bcrypto\b",
            r"\btoken\b",
        ],
    ),
    (
        "macro",
        [
            r"\bfed\b",
            r"\brate cut\b",
            r"\binflation\b",
            r"\bcpi\b",
            r"\bgdp\b",
            r"\bunemployment\b",
            r"\bjobs report\b",
            r"\btariff\b",
        ],
    ),
    (
        "entertainment",
        [
            r"\boscar\b",
            r"\bgrammy\b",
            r"\bemmy\b",
            r"\bmovie\b",
            r"\balbum\b",
            r"\bsong\b",
            r"\bstreaming\b",
        ],
    ),
]


def _haystack(market: Market) -> str:
    return norm_text(
        market.event_title,
        market.event_slug,
        market.question,
        market.description,
        market.market_slug,
        " ".join(market.outcomes),
    )


def classify_market(market: Market) -> MarketClassification:
    text = _haystack(market)
    for market_class, patterns in _CLASS_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, text, re.I):
                return MarketClassification(market_class=market_class, reason=f"matched:{pattern}")
    return MarketClassification(market_class="other", reason="no_keyword_match")


def is_weather_market(market: Market) -> bool:
    return classify_market(market).market_class == "weather"


def class_counts(markets: Iterable[Market]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for market in markets:
        cls = classify_market(market).market_class
        counts[cls] = counts.get(cls, 0) + 1
    return counts
