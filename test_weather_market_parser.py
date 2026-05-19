#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import datetime

from weather_market_parser import parse_bucket_f, parse_weather_market


STATION_MAP = {
    "seattle": {
        "station_id": "KSEA",
        "station_name": "Seattle-Tacoma International Airport",
        "latitude": 47.4502,
        "longitude": -122.3088,
        "timezone": "America/Los_Angeles",
        "source": "test",
    }
}


def test_bucket_variants():
    cases = [
        ("61-67F", (61.0, 67.0)),
        ("61-67°F", (61.0, 67.0)),
        ("61–67°F", (61.0, 67.0)),
        ("61 to 67 F", (61.0, 67.0)),
        ("between 61 and 67", (61.0, 67.0)),
        ("above 73F", (73.0, None)),
        ("below 61F", (None, 61.0)),
    ]
    for text, expected in cases:
        assert parse_bucket_f(text) == expected, (text, parse_bucket_f(text), expected)


def test_parse_market_slug_range_preserved():
    current_year = datetime.utcnow().year
    market = {
        "id": "test-id",
        "question": f"Highest temperature in Seattle on May 6?",
        "slug": "highest-temperature-in-seattle-on-may-6-61-67f",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-token", "no-token"]',
        "description": "Temperature market. Verify official station before use.",
    }
    parsed = parse_weather_market(market, STATION_MAP)
    assert parsed is not None
    assert parsed.city.lower() == "seattle"
    assert parsed.lower_f == 61.0
    assert parsed.upper_f == 67.0
    assert parsed.yes_token_id == "yes-token"
    assert parsed.no_token_id == "no-token"


if __name__ == "__main__":
    test_bucket_variants()
    test_parse_market_slug_range_preserved()
    print("parser tests passed")
