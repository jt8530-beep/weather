from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from .types import Market

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemperatureBucket:
    market_id: str
    token_yes: str
    token_no: str
    question: str
    kind: str          # "upper" | "exact" | "lower"
    value: Decimal
    unit: str          # "C" | "F" | "unknown"


@dataclass(frozen=True)
class TemperatureBucketValidation:
    event_id: str
    event_title: str
    is_valid: bool
    reason: str
    unit: str
    bucket_count: int
    min_exact: Optional[Decimal]
    max_exact: Optional[Decimal]
    buckets: List[TemperatureBucket]


# ---------------------------------------------------------------------------
# Regex fragments  (compiled once)
# ---------------------------------------------------------------------------

_TEMP_UNIT = (
    r"(?:\s*[°\u00b0]\s*[cfCF]"
    r"|\s*degrees?\s*(?:celsius|fahrenheit)"
    r"|\s+celsius\b"
    r"|\s+fahrenheit\b"
    r"|\s*\b[cfCF]\b(?=\s|$|\.|\)))?"
)

# Suffixes that indicate a lower boundary (>= X)
_LOWER_SUFFIX = r"(?:or\s+above|or\s+higher|or\s+more|and\s+above|and\s+over)"
# Suffixes that indicate an upper boundary (<= X)
_UPPER_SUFFIX = r"(?:or\s+below|or\s+lower|or\s+less|and\s+below|and\s+under)"

# Matches "<number><unit> <LOWER_SUFFIX>"  or  "above/over/at least/>= <number><unit>"
_LOWER_RE = re.compile(
    r"(?:^|\s)"
    r"(?P<value>-?\d+(?:\.\d+)?)"
    + _TEMP_UNIT +
    r"\s+" + _LOWER_SUFFIX
    r"|"
    r"(?:above|over|at\s+least|>=)\s+"
    r"(?P<value2>-?\d+(?:\.\d+)?)"
    + _TEMP_UNIT,
    re.I,
)

# Matches "<number><unit> <UPPER_SUFFIX>"  or  "below/under/at most/<= <number><unit>"
_UPPER_RE = re.compile(
    r"(?:^|\s)"
    r"(?P<value>-?\d+(?:\.\d+)?)"
    + _TEMP_UNIT +
    r"\s+" + _UPPER_SUFFIX
    r"|"
    r"(?:below|under|at\s+most|<=)\s+"
    r"(?P<value2>-?\d+(?:\.\d+)?)"
    + _TEMP_UNIT,
    re.I,
)

# Matches "be <number><unit>"  (won't pick up "May 21" because no unit)
_EXACT_RE = re.compile(
    r"\bbe\s+(-?\d+(?:\.\d+)?)"
    + _TEMP_UNIT +
    r"\b",
    re.I,
)

# Quick pre-check: does the text contain any boundary suffix?
_BOUNDARY_SUFFIX_RE = re.compile(
    _LOWER_SUFFIX + r"|" + _UPPER_SUFFIX,
    re.I,
)


# ---------------------------------------------------------------------------
# Unit detection
# ---------------------------------------------------------------------------

def detect_unit(text: str) -> str:
    """Return "C", "F", or "unknown" from a temperature question or bucket text."""
    t = text.lower()

    # Celsius patterns
    if re.search(r"(?:[°\u00b0]\s*c\b|degrees?\s*celsius|\b\d+\s*c\b(?!\s*(?:f|fahrenheit))|celsius\b)", t):
        return "C"

    # Fahrenheit patterns
    if re.search(r"(?:[°\u00b0]\s*f\b|degrees?\s*fahrenheit|\b\d+\s*f\b|fahrenheit\b)", t):
        return "F"

    return "unknown"


# ---------------------------------------------------------------------------
# Non-integer guard
# ---------------------------------------------------------------------------

def is_integral_decimal(x: Decimal) -> bool:
    return x == x.to_integral_value()


# ---------------------------------------------------------------------------
# Temperature bucket parser
# ---------------------------------------------------------------------------

def _text_with_group_title(market: Market) -> str:
    """Combine question and groupItemTitle for parsing."""
    raw = getattr(market, 'raw', None)
    if raw and isinstance(raw, dict):
        group_title = raw.get("groupItemTitle", "")
    else:
        group_title = ""
    return f"{market.question} {group_title}"


def _is_temperature_market(market: Market) -> bool:
    """Check if market or its event is temperature-related."""
    title_low = market.event_title.lower()
    q_low = market.question.lower()
    return any(kw in title_low or kw in q_low for kw in [
        "highest temperature", "lowest temperature", "temperature",
        "high temp", "low temp"
    ])


def parse_temperature_bucket(market: Market) -> Optional[TemperatureBucket]:
    """Parse a single market into a TemperatureBucket, or None if not a temperature bucket."""
    if not market.yes_token or not market.no_token:
        return None

    if not _is_temperature_market(market):
        return None

    text = _text_with_group_title(market)

    # 1. Try upper (<= X)  —  "X°C or below" / "X°C or lower" / "below X°C" / ...
    m = _UPPER_RE.search(text)
    if m:
        value = m.group("value") or m.group("value2")
        if value:
            unit = detect_unit(m.group(0))
            return TemperatureBucket(
                market_id=market.market_id,
                token_yes=market.yes_token.token_id,
                token_no=market.no_token.token_id,
                question=market.question,
                kind="upper",
                value=Decimal(value),
                unit=unit,
            )

    # 2. Try lower (>= X)  —  "X°C or above" / "X°C or higher" / "above X°C" / ...
    m = _LOWER_RE.search(text)
    if m:
        value = m.group("value") or m.group("value2")
        if value:
            unit = detect_unit(m.group(0))
            return TemperatureBucket(
                market_id=market.market_id,
                token_yes=market.yes_token.token_id,
                token_no=market.no_token.token_id,
                question=market.question,
                kind="lower",
                value=Decimal(value),
                unit=unit,
            )

    # 3. Try exact  —  "be X°C"
    # Only attempt exact if the text does NOT contain a boundary suffix,
    # to prevent "12°C or lower" from falling through to exact.
    if not _BOUNDARY_SUFFIX_RE.search(text):
        m = _EXACT_RE.search(text)
        if m:
            value = m.group(1)
            if value:
                unit = detect_unit(m.group(0))
                return TemperatureBucket(
                    market_id=market.market_id,
                    token_yes=market.yes_token.token_id,
                    token_no=market.no_token.token_id,
                    question=market.question,
                    kind="exact",
                    value=Decimal(value),
                    unit=unit,
                )

    return None


# ---------------------------------------------------------------------------
# Event-level validation
# ---------------------------------------------------------------------------

def _make_validation(
    event_id: str,
    event_title: str,
    is_valid: bool,
    reason: str,
    unit: str = "unknown",
    bucket_count: int = 0,
    min_exact: Optional[Decimal] = None,
    max_exact: Optional[Decimal] = None,
    buckets: Optional[List[TemperatureBucket]] = None,
) -> TemperatureBucketValidation:
    return TemperatureBucketValidation(
        event_id=event_id,
        event_title=event_title,
        is_valid=is_valid,
        reason=reason,
        unit=unit,
        bucket_count=bucket_count if buckets is None else len(buckets),
        min_exact=min_exact,
        max_exact=max_exact,
        buckets=buckets or [],
    )


def validate_temperature_bucket_event(
    event_id: str,
    event_title: str,
    neg_risk: bool,
    markets: List[Market],
) -> TemperatureBucketValidation:
    """Validate whether a NegRisk event is a complete, continuous temperature bucket event."""

    base = _make_validation(event_id, event_title, False, "not_validated")

    # 1. Must be temperature event
    title_low = event_title.lower()
    is_temp = any(kw in title_low for kw in [
        "highest temperature", "lowest temperature", "temperature",
        "high temp", "low temp"
    ])
    if not is_temp:
        return _make_validation(event_id, event_title, False, "not_temperature_event")

    # 2. Must be NegRisk
    if not neg_risk:
        return _make_validation(event_id, event_title, False, "event_not_negrisk")

    # 3. Parse all markets into buckets
    buckets: List[TemperatureBucket] = []
    for m in markets:
        b = parse_temperature_bucket(m)
        if b is None:
            return _make_validation(event_id, event_title, False, "bucket_not_parsed",
                                    buckets=buckets)
        buckets.append(b)

    if len(buckets) < 3:
        return _make_validation(event_id, event_title, False, "too_few_buckets",
                                buckets=buckets, bucket_count=len(buckets))

    # 4. Unit consistency
    units = {b.unit for b in buckets}
    if "unknown" in units and len(units) == 1:
        unit = "unknown"
    elif "unknown" in units:
        known = units - {"unknown"}
        if len(known) > 1:
            return _make_validation(event_id, event_title, False, "mixed_units",
                                    buckets=buckets)
        known_unit = next(iter(known))
        buckets = [_with_unit(b, known_unit) for b in buckets]
        unit = known_unit
    elif len(units) > 1:
        return _make_validation(event_id, event_title, False, "mixed_units",
                                buckets=buckets)
    else:
        unit = next(iter(units))

    # V5 fix: unit must be explicit C or F
    if unit not in ("C", "F"):
        return _make_validation(event_id, event_title, False, "unit_unknown",
                                unit=unit, buckets=buckets)

    # 5. Categorize by kind
    uppers = [b for b in buckets if b.kind == "upper"]
    lowers = [b for b in buckets if b.kind == "lower"]
    exacts = [b for b in buckets if b.kind == "exact"]

    if len(uppers) != 1:
        return _make_validation(event_id, event_title, False, "missing_upper_boundary",
                                unit=unit, buckets=buckets)
    if len(lowers) != 1:
        return _make_validation(event_id, event_title, False, "missing_lower_boundary",
                                unit=unit, buckets=buckets)

    upper_val = uppers[0].value
    lower_val = lowers[0].value

    if upper_val >= lower_val:
        return _make_validation(event_id, event_title, False, "invalid_boundary_order",
                                unit=unit, buckets=buckets)

    # 6. Non-integer guard
    if not is_integral_decimal(upper_val) or not is_integral_decimal(lower_val):
        return _make_validation(event_id, event_title, False, "non_integer_temperature_bucket",
                                unit=unit, buckets=buckets)
    for b in exacts:
        if not is_integral_decimal(b.value):
            return _make_validation(event_id, event_title, False, "non_integer_temperature_bucket",
                                    unit=unit, buckets=buckets)

    # 7. No duplicates among exacts
    exact_values = [b.value for b in exacts]
    if len(exact_values) != len(set(exact_values)):
        return _make_validation(event_id, event_title, False, "duplicate_bucket",
                                unit=unit, buckets=buckets)

    exact_sorted = sorted(exact_values)

    # 8. Exact must be continuous from upper+1 to lower-1
    expected = [Decimal(str(i)) for i in range(int(upper_val) + 1, int(lower_val))]
    if exact_sorted != expected:
        missing = set(expected) - set(exact_sorted)
        if missing:
            return _make_validation(event_id, event_title, False, "missing_exact_bucket",
                                    unit=unit, buckets=buckets)
        return _make_validation(event_id, event_title, False, "non_contiguous_exact_buckets",
                                unit=unit, buckets=buckets)

    # 9. Minimum bucket count
    if len(buckets) < 5:
        return _make_validation(event_id, event_title, False, "too_few_buckets",
                                unit=unit, buckets=buckets)

    # Valid!
    return _make_validation(
        event_id=event_id,
        event_title=event_title,
        is_valid=True,
        reason="valid_temperature_bucket_event",
        unit=unit,
        bucket_count=len(buckets),
        min_exact=min(exact_values),
        max_exact=max(exact_values),
        buckets=buckets,
    )


def _with_unit(b: TemperatureBucket, unit: str) -> TemperatureBucket:
    return TemperatureBucket(
        market_id=b.market_id,
        token_yes=b.token_yes,
        token_no=b.token_no,
        question=b.question,
        kind=b.kind,
        value=b.value,
        unit=unit,
    )


# ---------------------------------------------------------------------------
# Grouped validation helper (for use in main.py run_paper)
# ---------------------------------------------------------------------------

def validate_all_temperature_events(
    markets: List[Market],
) -> Dict[str, TemperatureBucketValidation]:
    """Group all markets by event_id, validate NegRisk temperature events.

    Uses event-level neg_risk from Market objects. The gamma.py parser already
    inherits event.negRisk into each sub-market via:
      neg_risk = market_neg_risk or event_neg_risk
    """
    from collections import defaultdict

    by_event: Dict[str, List[Market]] = defaultdict(list)
    event_info: Dict[str, Tuple[str, bool]] = {}  # event_id -> (title, neg_risk)

    for m in markets:
        # Include all markets; validate_temperature_bucket_event checks neg_risk
        by_event[m.event_id].append(m)
        if m.event_id not in event_info:
            event_info[m.event_id] = (m.event_title, m.neg_risk)

    validations: Dict[str, TemperatureBucketValidation] = {}
    for event_id, group in by_event.items():
        if event_id not in event_info:
            continue
        title, neg_risk = event_info[event_id]
        validation = validate_temperature_bucket_event(event_id, title, neg_risk, group)
        validations[event_id] = validation

    return validations
