#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only Polymarket public data client.

Only public GET endpoints are used. This module has no wallet/account logic.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
BOOK_URL = "https://clob.polymarket.com/book"


def get_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 15) -> Any:
    response = requests.get(url, params=params, timeout=timeout, headers={"User-Agent": "weather-live-scanner/0.1"})
    response.raise_for_status()
    return response.json()


def parse_jsonish(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def normalize_market_page(rows: Any) -> List[Dict[str, Any]]:
    if isinstance(rows, list):
        return [item for item in rows if isinstance(item, dict)]
    if isinstance(rows, dict):
        for key in ("markets", "data", "value", "results"):
            value = rows.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def iter_gamma_markets(limit: int = 500, max_pages: int = 5) -> Iterable[Dict[str, Any]]:
    """Yield active Gamma markets without skipping pages when Gamma caps page size.

    Gamma may return fewer rows than the requested limit. Using page * limit can
    skip markets when that happens, so advance offset by the actual number of
    returned rows.
    """
    offset = 0
    for _ in range(max_pages):
        rows = get_json(GAMMA_MARKETS_URL, {"closed": "false", "active": "true", "limit": limit, "offset": offset})
        markets = normalize_market_page(rows)
        if not markets:
            break
        for market in markets:
            yield market
        offset += len(markets)
        if len(markets) < min(limit, 100):
            break


def extract_yes_no_tokens(market: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    outcomes = parse_jsonish(market.get("outcomes")) or []
    tokens = parse_jsonish(market.get("clobTokenIds") or market.get("clobTokenIDs")) or []
    yes_token = None
    no_token = None
    if isinstance(outcomes, list) and isinstance(tokens, list):
        for outcome, token in zip(outcomes, tokens):
            name = str(outcome).strip().lower()
            if name == "yes":
                yes_token = str(token)
            elif name == "no":
                no_token = str(token)
    if yes_token is None and isinstance(tokens, list) and len(tokens) >= 1:
        yes_token = str(tokens[0])
    if no_token is None and isinstance(tokens, list) and len(tokens) >= 2:
        no_token = str(tokens[1])
    return yes_token, no_token


def _level_to_pair(level: Any) -> Tuple[Optional[float], Optional[float]]:
    try:
        if isinstance(level, dict):
            return float(level.get("price")), float(level.get("size"))
        if isinstance(level, list) and len(level) >= 2:
            return float(level[0]), float(level[1])
    except Exception:
        pass
    return None, None


def get_public_book_stats(token_id: str, levels: int = 3) -> Dict[str, Any]:
    book = get_json(BOOK_URL, {"token_id": token_id}, timeout=10)
    asks = []
    bids = []
    for item in book.get("asks") or []:
        p, s = _level_to_pair(item)
        if p is not None and s is not None:
            asks.append((p, s))
    for item in book.get("bids") or []:
        p, s = _level_to_pair(item)
        if p is not None and s is not None:
            bids.append((p, s))
    asks.sort(key=lambda x: x[0])
    bids.sort(key=lambda x: x[0], reverse=True)
    best_ask = asks[0][0] if asks else None
    best_bid = bids[0][0] if bids else None
    ask_depth = sum(p * s for p, s in asks[:levels]) if asks else 0.0
    spread = None
    if best_ask is not None and best_bid is not None:
        spread = best_ask - best_bid
    return {"best_ask": best_ask, "best_bid": best_bid, "spread": spread, "ask_depth_usd": ask_depth}
