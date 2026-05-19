from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .types import Market
from .util import norm_text


@dataclass(frozen=True)
class ThresholdSpec:
    market_id: str
    event_id: str
    underlying_key: str
    metric: str
    operator: str
    threshold: Decimal


_METRIC_PATTERNS = [
    ("temperature", re.compile(r"\b(temp|temperature|high|low|degrees?|fahrenheit|°f|\bf\b)\b", re.I)),
    ("precipitation", re.compile(r"\b(rain|rainfall|precip|precipitation|snow|snowfall|inches?|inch|\")\b", re.I)),
    ("wind", re.compile(r"\b(wind|mph|gust)\b", re.I)),
]

# Lower-bound phrases: "at least 85", "above 85", "85 or higher", ">=85"
_LOWER_PATTERNS = [
    re.compile(r"(?:at\s+least|above|over|more\s+than|greater\s+than|>=|>)\s*\$?(-?\d+(?:\.\d+)?)", re.I),
    re.compile(r"\$?(-?\d+(?:\.\d+)?)\s*(?:or\s+higher|or\s+more|\+|and\s+above|and\s+over)", re.I),
]
_UPPER_PATTERNS = [
    re.compile(r"(?:at\s+most|below|under|less\s+than|fewer\s+than|<=|<)\s*\$?(-?\d+(?:\.\d+)?)", re.I),
    re.compile(r"\$?(-?\d+(?:\.\d+)?)\s*(?:or\s+lower|or\s+less|and\s+below|and\s+under)", re.I),
]


def extract_threshold_spec(market: Market) -> Optional[ThresholdSpec]:
    text = norm_text(market.event_title, market.question, market.description, market.market_slug)
    metric = None
    for name, pat in _METRIC_PATTERNS:
        if pat.search(text):
            metric = name
            break
    if metric is None:
        return None

    for pat in _LOWER_PATTERNS:
        match = pat.search(text)
        if match:
            return ThresholdSpec(
                market_id=market.market_id,
                event_id=market.event_id,
                underlying_key=f"{market.event_id}:{metric}",
                metric=metric,
                operator=">=",
                threshold=Decimal(match.group(1)),
            )
    for pat in _UPPER_PATTERNS:
        match = pat.search(text)
        if match:
            return ThresholdSpec(
                market_id=market.market_id,
                event_id=market.event_id,
                underlying_key=f"{market.event_id}:{metric}",
                metric=metric,
                operator="<=",
                threshold=Decimal(match.group(1)),
            )
    return None


def implies(a: ThresholdSpec, b: ThresholdSpec) -> bool:
    """Return True if event A logically implies event B under simple threshold rules."""
    if a.underlying_key != b.underlying_key or a.market_id == b.market_id:
        return False
    if a.operator == ">=" and b.operator == ">=":
        return a.threshold > b.threshold
    if a.operator == "<=" and b.operator == "<=":
        return a.threshold < b.threshold
    return False
