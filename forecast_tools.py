#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Forecast helpers for temperature range research."""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def fetch_json(url: str, params: Dict[str, Any], timeout: int = 15) -> Any:
    response = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "weather-research/0.1"})
    response.raise_for_status()
    return response.json()


def normal_cdf(x: float, mean: float, sigma: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mean) / (sigma * math.sqrt(2.0))))


def temperature_range_probability(mean_f: float, sigma_f: float, lower_f: Optional[float], upper_f: Optional[float]) -> float:
    if lower_f is None and upper_f is None:
        return 0.0
    if lower_f is None:
        return max(0.0, min(1.0, normal_cdf(float(upper_f), mean_f, sigma_f)))
    if upper_f is None:
        return max(0.0, min(1.0, 1.0 - normal_cdf(float(lower_f), mean_f, sigma_f)))
    return max(0.0, min(1.0, normal_cdf(float(upper_f), mean_f, sigma_f) - normal_cdf(float(lower_f), mean_f, sigma_f)))


def station_forecast(latitude: float, longitude: float, timezone_name: str, target_date: date, temp_type: str) -> Optional[Tuple[float, Optional[float]]]:
    data = fetch_json(OPEN_METEO_FORECAST_URL, {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "temperature_2m_max,temperature_2m_min",
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": timezone_name,
        "forecast_days": 16,
    })
    daily = data.get("daily", {})
    times = daily.get("time", [])
    key = "temperature_2m_min" if temp_type == "low" else "temperature_2m_max"
    if str(target_date) not in times:
        return None
    idx = times.index(str(target_date))
    forecast_f = float(daily[key][idx])
    hourly_vals = data.get("hourly", {}).get("temperature_2m", [])
    current_f = float(hourly_vals[0]) if hourly_vals else None
    return forecast_f, current_f


def sigma_value(config: Dict[str, Any], target_date: date, temp_type: str) -> float:
    days = max(0, (target_date - datetime.now(timezone.utc).date()).days)
    key = "base_sigma_f_low" if temp_type == "low" else "base_sigma_f_high"
    return float(config.get(key, 3.25)) + days * float(config.get("sigma_per_day_f", 0.35))


def adjusted_forecast(config: Dict[str, Any], temp_type: str, target_date: date, forecast_f: float, current_public_f: Optional[float], current_ocr_f: Optional[float]) -> Tuple[float, Optional[float], float]:
    if current_ocr_f is None or current_public_f is None:
        return forecast_f, None, 0.0
    bias_f = current_ocr_f - current_public_f
    if temp_type == "low" and not config.get("ocr_apply_to_low", False):
        return forecast_f, bias_f, 0.0
    if abs(bias_f) > float(config.get("ocr_max_abs_bias_f", 8.0)):
        return forecast_f, bias_f, 0.0
    days = (target_date - datetime.now(timezone.utc).date()).days
    if days <= 0:
        weight = float(config.get("ocr_weight_today", 0.55))
    elif days == 1:
        weight = float(config.get("ocr_weight_tomorrow", 0.30))
    else:
        weight = float(config.get("ocr_weight_later", 0.10))
    return forecast_f + bias_f * weight, bias_f, weight
