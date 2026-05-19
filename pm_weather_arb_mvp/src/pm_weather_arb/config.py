from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Config:
    gamma_host: str = os.getenv("PM_GAMMA_HOST", "https://gamma-api.polymarket.com")
    clob_host: str = os.getenv("PM_CLOB_HOST", "https://clob.polymarket.com")
    fee_rate: Decimal = Decimal(os.getenv("PM_FEE_RATE", "0.05"))
    min_edge: Decimal = Decimal(os.getenv("PM_MIN_EDGE", "0.005"))
    min_shares: Decimal = Decimal(os.getenv("PM_MIN_SHARES", "5"))
    max_shares: Decimal = Decimal(os.getenv("PM_MAX_SHARES", "100"))
    http_timeout: float = float(os.getenv("PM_HTTP_TIMEOUT", "10"))


DEFAULT_WEATHER_KEYWORDS = (
    "weather",
    "temperature",
    "temp",
    "high temp",
    "low temp",
    "degrees",
    "fahrenheit",
    "°f",
    "rain",
    "rainfall",
    "precip",
    "precipitation",
    "snow",
    "snowfall",
    "storm",
    "hurricane",
    "tropical",
    "wind",
    "nws",
    "noaa",
    "climate",
)
