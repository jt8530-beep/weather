#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live public-data weather scanner.

Read-only only:
- public metadata
- public depth snapshot
- Open-Meteo forecast
- optional OCR current-temperature bias
- CSV output

No keys, no wallet, no account access, no submission logic.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from forecast_tools import adjusted_forecast, sigma_value, station_forecast, temperature_range_probability
from polymarket_public_client import get_public_book_stats, iter_gamma_markets
from weather_market_parser import ParsedWeatherMarket, load_station_map, parse_weather_market

try:
    from alternative_data_ocr_ascii import OcrDataCollectorAscii as OcrCollector
except Exception:
    try:
        from alternative_data_ocr import OcrDataCollector as OcrCollector
    except Exception:
        OcrCollector = None  # type: ignore


@dataclass
class Row:
    timestamp_utc: str
    market_id: str
    slug: str
    question: str
    city: str
    station_id: str
    station_name: str
    target_date: str
    temp_type: str
    lower_f: Optional[float]
    upper_f: Optional[float]
    outcome: str
    price: float
    model_prob: float
    edge: float
    spread: float
    depth_usd: float
    forecast_raw_f: float
    forecast_adjusted_f: float
    sigma_f: float
    ocr_temp_f: Optional[float]
    public_current_f: Optional[float]
    ocr_bias_f: Optional[float]
    ocr_weight: float


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ocr_by_city(config: Dict[str, Any]) -> Dict[str, Any]:
    if not config.get("ocr_enabled") or OcrCollector is None:
        return {}
    data = {}
    for item in OcrCollector(config).collect_all():  # type: ignore[operator]
        if item.ok and item.temp_f is not None:
            data[item.city.strip().lower()] = item
    return data


def make_row(market: ParsedWeatherMarket, config: Dict[str, Any], outcome: str, token_id: str, probability: float, forecast_raw: float, forecast_adj: float, sigma_f: float, ocr_temp: Optional[float], public_current: Optional[float], ocr_bias: Optional[float], weight: float) -> Optional[Row]:
    stats = get_public_book_stats(token_id)
    price = stats.get("best_ask")
    spread = stats.get("spread")
    depth = float(stats.get("ask_depth_usd") or 0.0)
    if price is None or spread is None:
        return None
    edge = probability - float(price)
    if edge < float(config.get("min_edge", 0.10)):
        return None
    if depth < float(config.get("min_depth_usd", 25.0)):
        return None
    if float(spread) > float(config.get("max_spread", 0.15)):
        return None
    return Row(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        market_id=market.market_id,
        slug=market.slug,
        question=market.question,
        city=market.city,
        station_id=market.station_id,
        station_name=market.station_name,
        target_date=str(market.target_date),
        temp_type=market.temp_type,
        lower_f=market.lower_f,
        upper_f=market.upper_f,
        outcome=outcome,
        price=round(float(price), 4),
        model_prob=round(probability, 4),
        edge=round(edge, 4),
        spread=round(float(spread), 4),
        depth_usd=round(depth, 2),
        forecast_raw_f=round(forecast_raw, 2),
        forecast_adjusted_f=round(forecast_adj, 2),
        sigma_f=round(sigma_f, 2),
        ocr_temp_f=round(ocr_temp, 2) if ocr_temp is not None else None,
        public_current_f=round(public_current, 2) if public_current is not None else None,
        ocr_bias_f=round(ocr_bias, 2) if ocr_bias is not None else None,
        ocr_weight=round(weight, 3),
    )


def evaluate(market: ParsedWeatherMarket, config: Dict[str, Any], ocr_map: Dict[str, Any]) -> List[Row]:
    fc = station_forecast(market.latitude, market.longitude, market.timezone, market.target_date, market.temp_type)
    if not fc:
        return []
    forecast_raw, public_current = fc
    current_ocr = None
    found = ocr_map.get(market.city.strip().lower())
    if found is not None:
        current_ocr = found.temp_f
    forecast_adj, ocr_bias, weight = adjusted_forecast(config, market.temp_type, market.target_date, forecast_raw, public_current, current_ocr)
    sigma_f = sigma_value(config, market.target_date, market.temp_type)
    prob_in = temperature_range_probability(forecast_adj, sigma_f, market.lower_f, market.upper_f)
    rows = []
    a = make_row(market, config, "IN_RANGE", market.yes_token_id, prob_in, forecast_raw, forecast_adj, sigma_f, current_ocr, public_current, ocr_bias, weight)
    b = make_row(market, config, "OUT_RANGE", market.no_token_id, 1.0 - prob_in, forecast_raw, forecast_adj, sigma_f, current_ocr, public_current, ocr_bias, weight)
    if a:
        rows.append(a)
    if b:
        rows.append(b)
    return rows


def append_rows(path: Path, rows: List[Row]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def run_once(config: Dict[str, Any], top: int) -> List[Row]:
    station_map = load_station_map(config.get("station_map_path", "station_map.json"))
    ocr_map = ocr_by_city(config)
    rows: List[Row] = []
    parsed = 0
    for raw in iter_gamma_markets(limit=int(config.get("limit_markets", 500)), max_pages=int(config.get("max_pages", 5))):
        market = parse_weather_market(raw, station_map)
        if not market:
            continue
        parsed += 1
        try:
            rows.extend(evaluate(market, config, ocr_map))
        except Exception as exc:
            print(f"[skip] {market.slug}: {exc}")
    rows.sort(key=lambda x: x.edge, reverse=True)
    out_path = Path(config.get("live_candidates_file", "paper_logs/live_candidates.csv"))
    append_rows(out_path, rows)
    print(f"parsed={parsed} rows={len(rows)} saved={out_path}")
    for row in rows[:top]:
        print(f"{row.outcome:9s} edge={row.edge:.3f} price={row.price:.3f} model={row.model_prob:.3f} {row.city} {row.target_date} {row.lower_f}-{row.upper_f}F station={row.station_id}")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="weather_scanner_config_v2_ocr.json")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--sleep", type=int, default=None)
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()
    config = load_config(args.config)
    sleep_s = int(args.sleep or config.get("sleep_seconds", 300))
    if args.loop:
        while True:
            run_once(config, args.top)
            time.sleep(sleep_s)
    else:
        run_once(config, args.top)


if __name__ == "__main__":
    main()
