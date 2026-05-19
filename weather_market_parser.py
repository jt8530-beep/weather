#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Weather market parser and station mapping helpers.

Robustness goals:
- Preserve original slugs so temperature ranges such as 61-67F are not destroyed.
- Do not treat date fragments such as may-6-61 as a temperature bucket.
- Prefer explicit Fahrenheit ranges, then the last plausible narrow range.
- Accept 61-67F, 61-67°F, 61–67°F, 61 to 67 F, between 61 and 67.
- Support basic station map overrides by slug or market id.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dateutil import parser as date_parser

from polymarket_public_client import extract_yes_no_tokens


@dataclass
class ParsedWeatherMarket:
    market_id: str
    question: str
    slug: str
    city: str
    target_date: date
    temp_type: str
    lower_f: Optional[float]
    upper_f: Optional[float]
    yes_token_id: str
    no_token_id: str
    rules_text: str
    station_id: str
    station_name: str
    latitude: float
    longitude: float
    timezone: str
    station_source: str
    rule_warning: str


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def load_station_map(path: str = "station_map.json") -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def market_text_parts(market: Dict[str, Any]) -> List[str]:
    fields = [
        "question",
        "title",
        "groupItemTitle",
        "groupItemThreshold",
        "outcome",
        "name",
        "slug",
        "subtitle",
        "description",
        "rules",
        "resolutionSource",
        "resolutionCriteria",
    ]
    parts: List[str] = []
    for key in fields:
        val = market.get(key)
        if val is None:
            continue
        if isinstance(val, (list, dict)):
            val = json.dumps(val, ensure_ascii=False)
        s = str(val)
        if s:
            parts.append(s)
            if key == "slug":
                parts.append(s.replace("-", " ").replace("_", " "))
    return parts


def compact_market_text(question: str, slug: str, rules: str) -> str:
    spaced_slug = slug.replace("-", " ").replace("_", " ")
    return " ".join([question or "", slug or "", spaced_slug, rules or ""])


def has_temperature_context(text: str) -> bool:
    t = text.lower()
    if any(k in t for k in ["temperature", "highest", "lowest", "high temp", "low temp", "weather"]):
        return True
    if re.search(r"\d{1,3}\s*(?:-|–|—|to)\s*\d{1,3}\s*(?:f|fahrenheit)\b", t):
        return True
    return False


def parse_city(text: str) -> Optional[str]:
    patterns = [
        r"\bin\s+([A-Za-z .,'-]+?)(?:\s+on\s+|\s+for\s+|\?|$)",
        r"\btemperature\s+in\s+([A-Za-z .,'-]+?)(?:\s+on\s+|\s+for\s+|\?|$)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        city = m.group(1).strip(" .,'-")
        city = re.sub(r"\bCity\b", "", city, flags=re.I).strip(" .,'-")
        city = re.sub(r",?\s+(TX|Texas|WA|Washington|NY|New York|CA|California|FL|Florida|IL|Illinois|CO|Colorado|AZ|Arizona|MA|Massachusetts|UK|England)$", "", city, flags=re.I).strip(" .,'-")
        if 2 <= len(city) <= 50:
            return city
    return None


def parse_target_date(text: str) -> Optional[date]:
    year = datetime.now(timezone.utc).year
    patterns = [
        r"(?:on|for)\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        r"(?:on|for)\s+([A-Za-z]+\s+\d{1,2})",
        r"(\d{4}-\d{2}-\d{2})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        try:
            return date_parser.parse(m.group(1), default=datetime(year, 1, 1)).date()
        except Exception:
            pass
    return None


def parse_temp_type(text: str) -> str:
    t = text.lower()
    if "lowest" in t or "minimum" in t or " low temperature" in t or " low temp" in t:
        return "low"
    return "high"


def _valid_bucket(a: float, b: float) -> bool:
    lo, hi = min(a, b), max(a, b)
    width = hi - lo
    return -80 <= lo <= 140 and -80 <= hi <= 140 and 0 < width <= 35


def parse_bucket_f(text: str) -> Tuple[Optional[float], Optional[float]]:
    """Parse a Fahrenheit temperature bucket.

    Important: slugs often look like `may-6-61-67f`. A naive first-match regex
    sees `6-61`, which is wrong. This function collects candidates and selects
    the best one by: explicit F unit > between phrase > plausible final range.
    """
    t = (text or "").replace("–", "-").replace("—", "-")
    candidates: List[Tuple[int, int, float, float]] = []

    # Highest confidence: explicit Fahrenheit unit after upper bound.
    explicit_pat = re.compile(
        r"(?<!\d)(-?\d{1,3}(?:\.\d+)?)\s*(?:-|to|through|and)\s*"
        r"(-?\d{1,3}(?:\.\d+)?)\s*(?:deg|degree|degrees|°)?\s*(?:[Ff]|fahrenheit)\b",
        re.I,
    )
    for m in explicit_pat.finditer(t):
        a, b = float(m.group(1)), float(m.group(2))
        if _valid_bucket(a, b):
            candidates.append((3000 + m.start(), m.start(), min(a, b), max(a, b)))

    # High confidence: between 61 and 67, optionally with units.
    between_pat = re.compile(
        r"between\s+(-?\d{1,3}(?:\.\d+)?)(?:\s*(?:[Ff]|fahrenheit))?\s+and\s+"
        r"(-?\d{1,3}(?:\.\d+)?)(?:\s*(?:[Ff]|fahrenheit))?",
        re.I,
    )
    for m in between_pat.finditer(t):
        a, b = float(m.group(1)), float(m.group(2))
        if _valid_bucket(a, b):
            candidates.append((2500 + m.start(), m.start(), min(a, b), max(a, b)))

    # Lower confidence fallback: narrow ranges without unit. Prefer later ranges.
    # This catches group titles like `61-67` but filters out `6-61` via width <= 35.
    no_unit_pat = re.compile(
        r"(?<!\d)(-?\d{1,3}(?:\.\d+)?)\s*(?:-|to|through)\s*"
        r"(-?\d{1,3}(?:\.\d+)?)(?!\d)",
        re.I,
    )
    for m in no_unit_pat.finditer(t):
        a, b = float(m.group(1)), float(m.group(2))
        if _valid_bucket(a, b):
            tail_bonus = 500 if m.end() >= max(0, len(t) - 20) else 0
            candidates.append((1000 + tail_bonus + m.start(), m.start(), min(a, b), max(a, b)))

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, _, lo, hi = candidates[0]
        return lo, hi

    above_pat = re.compile(r"(?:above|over|greater than|higher than|at least)\s*(-?\d{1,3}(?:\.\d+)?)(?:\s*(?:deg|degree|degrees|°)?\s*(?:[Ff]|fahrenheit))?\b", re.I)
    for m in above_pat.finditer(t):
        x = float(m.group(1))
        if -80 <= x <= 140:
            return x, None

    below_pat = re.compile(r"(?:below|under|less than|lower than|at most)\s*(-?\d{1,3}(?:\.\d+)?)(?:\s*(?:deg|degree|degrees|°)?\s*(?:[Ff]|fahrenheit))?\b", re.I)
    for m in below_pat.finditer(t):
        x = float(m.group(1))
        if -80 <= x <= 140:
            return None, x

    return None, None


def rules_text_from_market(market: Dict[str, Any]) -> str:
    fields = ["rules", "description", "resolutionSource", "resolutionCriteria", "question"]
    chunks = []
    for key in fields:
        val = market.get(key)
        if val:
            chunks.append(str(val))
    return "\n".join(chunks)


def clean_city_key(city: str) -> str:
    key = norm(city).replace(" city", "")
    key = re.sub(r",?\s+(tx|texas|wa|washington|ny|new york|ca|california|fl|florida|il|illinois|co|colorado|az|arizona|ma|massachusetts|uk|england)$", "", key)
    return key.strip()


def station_for_market(market_id: str, slug: str, city: str, station_map: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    overrides = station_map.get("_market_overrides", {}) if isinstance(station_map, dict) else {}
    for override_key in [market_id, slug]:
        if override_key and override_key in overrides:
            return overrides[override_key], "market_override"

    key = clean_city_key(city)
    if key in station_map:
        return station_map[key], "city_seed"
    for k, v in station_map.items():
        if k.startswith("_"):
            continue
        if key == clean_city_key(k):
            return v, "city_seed"
    return None, "missing_station_map"


def parse_weather_market(market: Dict[str, Any], station_map: Dict[str, Any]) -> Optional[ParsedWeatherMarket]:
    question = str(market.get("question") or market.get("title") or "")
    slug = str(market.get("slug") or "")
    market_id = str(market.get("id") or market.get("conditionId") or slug)
    rules = rules_text_from_market(market)
    text = " ".join(market_text_parts(market)) or compact_market_text(question, slug, rules)
    if not has_temperature_context(text):
        return None

    city = parse_city(text)
    target_date = parse_target_date(text)
    lower_f, upper_f = parse_bucket_f(text)
    yes_token, no_token = extract_yes_no_tokens(market)
    if not city or not target_date or not yes_token or not no_token or (lower_f is None and upper_f is None):
        return None

    station, station_source = station_for_market(market_id, slug, city, station_map)
    if not station:
        return None

    warning = ""
    rules_low = rules.lower()
    if station_source == "city_seed":
        warning = "station_from_seed_map_verify_resolution_rules"
    if rules and station.get("station_id", "").lower() not in rules_low and station.get("station_name", "").lower() not in rules_low:
        warning = "station_not_explicitly_confirmed_in_rules"

    return ParsedWeatherMarket(
        market_id=market_id,
        question=question,
        slug=slug,
        city=city,
        target_date=target_date,
        temp_type=parse_temp_type(text),
        lower_f=lower_f,
        upper_f=upper_f,
        yes_token_id=yes_token,
        no_token_id=no_token,
        rules_text=rules,
        station_id=str(station.get("station_id", "UNKNOWN")),
        station_name=str(station.get("station_name", city)),
        latitude=float(station["latitude"]),
        longitude=float(station["longitude"]),
        timezone=str(station.get("timezone", "UTC")),
        station_source=station_source,
        rule_warning=warning,
    )
