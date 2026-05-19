#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local weather range paper scanner.

Purpose:
- Read a local CSV snapshot of weather range contracts.
- Pull Open-Meteo forecast for each city/date.
- Estimate probability for each temperature range.
- Compare model probability with the snapshot price.
- Write paper candidates to CSV.

This file is intentionally local-snapshot based. It contains no account access,
no credentials, no signing, and no order submission.

Input CSV columns:
city,target_date,temp_type,lower_f,upper_f,side,price,spread,depth_usd,question,slug

Example:
Seattle,2026-05-06,high,61,67,YES,0.24,0.04,120,"Highest temperature in Seattle on May 6","example-slug"
Seattle,2026-05-06,high,67,73,NO,0.31,0.05,90,"Highest temperature in Seattle on May 6","example-slug-2"
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from alternative_data_ocr import OcrDataCollector, OcrReading
except Exception:
    OcrDataCollector = None  # type: ignore
    OcrReading = Any  # type: ignore

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

CITY_COORDS: Dict[str, Tuple[float, float, str]] = {
    "seattle": (47.6062, -122.3321, "America/Los_Angeles"),
    "new york": (40.7128, -74.0060, "America/New_York"),
    "nyc": (40.7128, -74.0060, "America/New_York"),
    "london": (51.5072, -0.1276, "Europe/London"),
    "austin": (30.2672, -97.7431, "America/Chicago"),
    "chicago": (41.8781, -87.6298, "America/Chicago"),
    "miami": (25.7617, -80.1918, "America/New_York"),
    "los angeles": (34.0522, -118.2437, "America/Los_Angeles"),
    "san francisco": (37.7749, -122.4194, "America/Los_Angeles"),
    "denver": (39.7392, -104.9903, "America/Denver"),
    "phoenix": (33.4484, -112.0740, "America/Phoenix"),
    "boston": (42.3601, -71.0589, "America/New_York"),
}


@dataclass
class MarketSnapshot:
    city: str
    target_date: date
    temp_type: str
    lower_f: Optional[float]
    upper_f: Optional[float]
    side: str
    price: float
    spread: float
    depth_usd: float
    question: str
    slug: str


@dataclass
class Candidate:
    timestamp_utc: str
    city: str
    target_date: str
    temp_type: str
    lower_f: Optional[float]
    upper_f: Optional[float]
    side: str
    price: float
    model_prob_side: float
    edge: float
    spread: float
    depth_usd: float
    forecast_raw_f: float
    forecast_value_f: float
    sigma_f: float
    ocr_temp_f: Optional[float]
    public_current_f: Optional[float]
    ocr_bias_f: Optional[float]
    ocr_weight: float
    question: str
    slug: str


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def norm_city(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def to_float_or_none(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    return float(v)


def fetch_json(url: str, params: Dict[str, Any], timeout: int = 15) -> Any:
    r = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "weather-range-scanner/0.1"})
    r.raise_for_status()
    return r.json()


def get_city_coords(city: str) -> Optional[Tuple[float, float, str]]:
    key = norm_city(city).replace(" city", "")
    if key in CITY_COORDS:
        return CITY_COORDS[key]
    try:
        data = fetch_json(OPEN_METEO_GEOCODE_URL, {"name": city, "count": 1, "language": "en", "format": "json"})
        results = data.get("results") or []
        if results:
            r = results[0]
            return float(r["latitude"]), float(r["longitude"]), str(r.get("timezone") or "UTC")
    except Exception:
        return None
    return None


def open_meteo_forecast(city: str, target: date, temp_type: str) -> Optional[Tuple[float, Optional[float]]]:
    coords = get_city_coords(city)
    if not coords:
        return None
    lat, lon, tz = coords
    data = fetch_json(OPEN_METEO_FORECAST_URL, {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min",
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": tz,
        "forecast_days": 16,
    })
    daily = data.get("daily", {})
    times = daily.get("time", [])
    key = "temperature_2m_min" if temp_type.lower() == "low" else "temperature_2m_max"
    if str(target) not in times:
        return None
    idx = times.index(str(target))
    forecast = float(daily[key][idx])
    hourly_vals = data.get("hourly", {}).get("temperature_2m", [])
    current_temp = float(hourly_vals[0]) if hourly_vals else None
    return forecast, current_temp


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def range_probability(mu: float, sigma: float, lower: Optional[float], upper: Optional[float]) -> float:
    if lower is None and upper is None:
        return 0.0
    if lower is None:
        return max(0.0, min(1.0, normal_cdf(float(upper), mu, sigma)))
    if upper is None:
        return max(0.0, min(1.0, 1.0 - normal_cdf(float(lower), mu, sigma)))
    return max(0.0, min(1.0, normal_cdf(float(upper), mu, sigma) - normal_cdf(float(lower), mu, sigma)))


def read_snapshot(path: str) -> List[MarketSnapshot]:
    rows: List[MarketSnapshot] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(MarketSnapshot(
                city=r["city"].strip(),
                target_date=datetime.strptime(r["target_date"].strip(), "%Y-%m-%d").date(),
                temp_type=(r.get("temp_type") or "high").strip().lower(),
                lower_f=to_float_or_none(r.get("lower_f")),
                upper_f=to_float_or_none(r.get("upper_f")),
                side=(r.get("side") or "YES").strip().upper(),
                price=float(r["price"]),
                spread=float(r.get("spread") or 0.0),
                depth_usd=float(r.get("depth_usd") or 0.0),
                question=r.get("question") or "",
                slug=r.get("slug") or "",
            ))
    return rows


def collect_ocr_by_city(cfg: Dict[str, Any]) -> Dict[str, OcrReading]:
    if not cfg.get("ocr_enabled") or OcrDataCollector is None:
        return {}
    result: Dict[str, OcrReading] = {}
    for reading in OcrDataCollector(cfg).collect_all():  # type: ignore[operator]
        if getattr(reading, "ok", False) and getattr(reading, "temp_f", None) is not None:
            result[norm_city(reading.city)] = reading
    return result


def ocr_weight_for(cfg: Dict[str, Any], row: MarketSnapshot) -> float:
    if row.temp_type == "low" and not cfg.get("ocr_apply_to_low", False):
        return 0.0
    days = (row.target_date - datetime.now(timezone.utc).date()).days
    if days <= 0:
        return float(cfg.get("ocr_weight_today", 0.55))
    if days == 1:
        return float(cfg.get("ocr_weight_tomorrow", 0.30))
    return float(cfg.get("ocr_weight_later", 0.10))


def evaluate_row(row: MarketSnapshot, cfg: Dict[str, Any], ocr_map: Dict[str, OcrReading]) -> Optional[Candidate]:
    fc = open_meteo_forecast(row.city, row.target_date, row.temp_type)
    if not fc:
        return None
    forecast_raw, public_current = fc
    forecast_raw += float(cfg.get("city_bias_f", {}).get(norm_city(row.city), 0.0))

    forecast_value = forecast_raw
    ocr_temp = None
    ocr_bias = None
    weight = 0.0
    reading = ocr_map.get(norm_city(row.city))
    if reading and getattr(reading, "temp_f", None) is not None and public_current is not None:
        ocr_temp = float(reading.temp_f)
        ocr_bias = ocr_temp - float(public_current)
        if abs(ocr_bias) <= float(cfg.get("ocr_max_abs_bias_f", 8.0)):
            weight = ocr_weight_for(cfg, row)
            forecast_value = forecast_raw + ocr_bias * weight

    days = max(0, (row.target_date - datetime.now(timezone.utc).date()).days)
    base_sigma_key = "base_sigma_f_low" if row.temp_type == "low" else "base_sigma_f_high"
    sigma = float(cfg.get(base_sigma_key, 3.25)) + days * float(cfg.get("sigma_per_day_f", 0.35))
    prob_yes = range_probability(forecast_value, sigma, row.lower_f, row.upper_f)
    prob_side = prob_yes if row.side == "YES" else 1.0 - prob_yes
    edge = prob_side - row.price

    if edge < float(cfg.get("min_edge", 0.10)):
        return None
    if row.depth_usd < float(cfg.get("min_depth_usd", 25.0)):
        return None
    if row.spread > float(cfg.get("max_spread", 0.15)):
        return None

    return Candidate(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        city=row.city,
        target_date=str(row.target_date),
        temp_type=row.temp_type,
        lower_f=row.lower_f,
        upper_f=row.upper_f,
        side=row.side,
        price=round(row.price, 4),
        model_prob_side=round(prob_side, 4),
        edge=round(edge, 4),
        spread=round(row.spread, 4),
        depth_usd=round(row.depth_usd, 2),
        forecast_raw_f=round(forecast_raw, 2),
        forecast_value_f=round(forecast_value, 2),
        sigma_f=round(sigma, 2),
        ocr_temp_f=round(ocr_temp, 2) if ocr_temp is not None else None,
        public_current_f=round(public_current, 2) if public_current is not None else None,
        ocr_bias_f=round(ocr_bias, 2) if ocr_bias is not None else None,
        ocr_weight=round(weight, 3),
        question=row.question,
        slug=row.slug,
    )


def write_candidates(path: str, rows: List[Candidate]) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def run_once(cfg: Dict[str, Any], snapshot_path: str, out_path: str, top: int) -> List[Candidate]:
    snapshot = read_snapshot(snapshot_path)
    ocr_map = collect_ocr_by_city(cfg)
    candidates: List[Candidate] = []
    for row in snapshot:
        try:
            c = evaluate_row(row, cfg, ocr_map)
            if c:
                candidates.append(c)
        except Exception as exc:
            print(f"[skip] {row.city} {row.target_date} {row.lower_f}-{row.upper_f}: {exc}")
    candidates.sort(key=lambda x: x.edge, reverse=True)
    write_candidates(out_path, candidates)
    print(f"snapshot_rows={len(snapshot)} candidates={len(candidates)} saved={out_path}")
    for c in candidates[:top]:
        print(f"{c.side:3s} edge={c.edge:.3f} price={c.price:.3f} model={c.model_prob_side:.3f} {c.city} {c.target_date} {c.lower_f}-{c.upper_f}F forecast={c.forecast_value_f}")
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="weather_scanner_config_v2_ocr.json")
    parser.add_argument("--snapshot", default="market_snapshot_example.csv")
    parser.add_argument("--out", default="paper_logs/candidates.csv")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()
    cfg = load_config(args.config)
    run_once(cfg, args.snapshot, args.out, args.top)


if __name__ == "__main__":
    main()
