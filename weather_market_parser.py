#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Weather market parser and station mapping helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def load_station_map(path: str = "station_map.json") -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def is_weather_temperature_text(text: str) -> bool:
    t = text.lower()
    keys = ["temperature", "highest", "lowest", "high temp", "low temp", "weather"]
    units = ["°f", " fahrenheit", " f", " degrees"]
    return any(k in t for k in keys) and any(u in t for u in units)


def parse_city(text: str) -> Optional[str]:
    m = re.search(r"\bin\s+([A-Za-z .'-]+?)(?:\s+on\s+|\s+for\s+|\?|$)", text, re.I)
    if m:
        city = re.sub(r"\bCity\b", "", m.group(1), flags=re.I).strip(" .")
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
    if "lowest" in t or "minimum" in t or " low " in t:
        return "low"
    return "high"


def parse_bucket_f(text: str) -> Tuple[Optional[float], Optional[float]]:
    t = text.replace("–", "-").replace("—", "-")
    m = re.search(r"(-?\d{1,3}(?:\.\d+)?)\s*(?:-|to|through|and)\s*(-?\d{1,3}(?:\.\d+)?)\s*(?:deg|degrees|°)?\s*[Ff]", t)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        return min(a, b), max(a, b)
    m = re.search(r"(?:above|over|greater than|higher than|at least)\s*(-?\d{1,3}(?:\.\d+)?)\s*(?:deg|degrees|°)?\s*[Ff]", t, re.I)
    if m:
        return float(m.group(1)), None
    m = re.search(r"(?:below|under|less than|lower than|at most)\s*(-?\d{1,3}(?:\.\d+)?)\s*(?:deg|degrees|°)?\s*[Ff]", t, re.I)
    if m:
        return None, float(m.group(1))
    return None, None


def rules_text_from_market(market: Dict[str, Any]) -> str:
    fields = ["rules", "description", "resolutionSource", "resolutionCriteria", "question"]
    chunks = []
    for key in fields:
        val = market.get(key)
        if val:
            chunks.append(str(val))
    return "\n".join(chunks)


def station_for_city(city: str, station_map: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    key = norm(city).replace(" city", "")
    if key in station_map:
        return station_map[key]
    for k, v in station_map.items():
        if key == norm(k).replace(" city", ""):
            return v
    return None


def parse_weather_market(market: Dict[str, Any], station_map: Dict[str, Any]) -> Optional[ParsedWeatherMarket]:
    question = str(market.get("question") or market.get("title") or "")
    slug = str(market.get("slug") or "")
    rules = rules_text_from_market(market)
    text = " ".join([question, slug.replace("-", " "), rules])
    if not is_weather_temperature_text(text):
        return None
    city = parse_city(text)
    target_date = parse_target_date(text)
    lower_f, upper_f = parse_bucket_f(text)
    yes_token, no_token = extract_yes_no_tokens(market)
    if not city or not target_date or not yes_token or not no_token or (lower_f is None and upper_f is None):
        return None
    station = station_for_city(city, station_map)
    if not station:
        return None
    return ParsedWeatherMarket(
        market_id=str(market.get("id") or market.get("conditionId") or slug),
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
        station_source=str(station.get("source", "manual")),
    )
