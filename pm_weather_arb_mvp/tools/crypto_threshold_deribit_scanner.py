#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BTC/ETH Polymarket threshold scanner using Deribit options-implied probability.

Paper-only. No wallet, no signing, no orders.

New direction after short-horizon crypto direction models failed:
- Do not predict 5m/15m direction ourselves.
- Use Deribit option market as external probability anchor.
- Scan Polymarket BTC/ETH price threshold markets.
- Compare Polymarket YES/NO ask to option-implied probability.

This is a relative-value scanner, not arbitrage and not investment advice.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from pm_weather_arb.clob import ClobPublicClient
from pm_weather_arb.config import Config
from pm_weather_arb.gamma import GammaClient, parse_markets_from_events
from pm_weather_arb.types import Market, OrderBook
from pm_weather_arb.util import first_present


ASSETS = {
    "BTC": {"terms": ["bitcoin", "btc"], "index": "btc_usd", "deribit_currency": "BTC"},
    "ETH": {"terms": ["ethereum", "ether", "eth"], "index": "eth_usd", "deribit_currency": "ETH"},
}

SEARCH_TERMS = ["Bitcoin price", "BTC price", "Bitcoin above", "Bitcoin below", "BTC above", "BTC below", "Ethereum price", "ETH price", "Ethereum above", "Ethereum below", "ETH above", "ETH below"]
MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


@dataclass
class ThresholdMarket:
    asset: str
    condition: str  # above / below
    threshold: float
    expiry_ts: int
    event_id: str
    event_title: str
    event_slug: str
    market_id: str
    market_slug: str
    question: str
    description: str
    yes_token_id: str
    no_token_id: str


@dataclass
class DeribitAnchor:
    asset: str
    spot: float
    expiry_ts: int
    expiry_name: str
    atm_strike: float
    atm_iv: float
    t_years: float
    source_count: int


@dataclass
class ThresholdSignal:
    ts_ms: int
    strategy: str
    asset: str
    event_title: str
    question: str
    condition: str
    threshold: float
    expiry_ts: int
    days_to_expiry: float
    spot: float
    atm_iv: float
    deribit_expiry_name: str
    p_yes: float
    p_no: float
    yes_bid: float
    yes_ask: float
    yes_spread: float
    yes_ask_size: float
    no_bid: float
    no_ask: float
    no_spread: float
    no_ask_size: float
    action: str
    ask: float
    model_prob: float
    edge: float
    reason: str


def now_ts() -> int:
    return int(time.time())


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def parse_iso_ts(value: object) -> Optional[int]:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return None


def event_expiry_ts(raw: dict, market_raw: dict) -> Optional[int]:
    keys = [
        "endDate", "end_date", "endTime", "end_time", "closeTime", "close_time",
        "resolutionTime", "resolution_time", "gameStartTime", "game_start_time",
    ]
    for src in (market_raw, raw):
        for k in keys:
            ts = parse_iso_ts(src.get(k)) if isinstance(src, dict) else None
            if ts:
                return ts
    return None


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


def infer_asset(text: str) -> Optional[str]:
    t = norm(text)
    for asset, spec in ASSETS.items():
        if any(re.search(rf"\b{re.escape(term)}\b", t) for term in spec["terms"]):
            return asset
    return None


def parse_threshold(text: str) -> Optional[float]:
    t = text.replace(",", "")
    # $100k / 100k / $100000 / 100000
    m = re.search(r"\$?\s*(\d+(?:\.\d+)?)\s*k\b", t, flags=re.I)
    if m:
        return float(m.group(1)) * 1000.0
    nums = []
    for m in re.finditer(r"\$?\s*(\d{4,7}(?:\.\d+)?)", t):
        nums.append(float(m.group(1)))
    if not nums:
        return None
    # Ignore years like 2026 if mixed with larger crypto prices.
    nums = [x for x in nums if x > 5000]
    if not nums:
        return None
    return nums[0]


def parse_condition(text: str) -> Optional[str]:
    t = norm(text)
    if any(x in t for x in ["above", "over", "higher than", "greater than", "at or above"]):
        return "above"
    if any(x in t for x in ["below", "under", "lower than", "less than", "at or below"]):
        return "below"
    return None


def discover_polymarket_thresholds(config: Config, pages: int, limit: int, order: str, max_days: float) -> List[ThresholdMarket]:
    gamma = GammaClient(config)
    events: List[dict] = []
    # Full pagination is more reliable than Gamma search for some categories.
    for page in range(pages):
        batch = gamma.list_events_raw({
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": page * limit,
            "order": order,
            "ascending": "false",
        })
        if not batch:
            break
        events.extend(batch)
        if len(batch) < limit:
            break
    # Add search probes, but do not depend on them.
    for term in SEARCH_TERMS:
        try:
            events.extend(gamma.list_events_raw({"active": "true", "closed": "false", "search": term, "limit": limit}))
        except Exception:
            pass

    seen_events = set()
    unique_events = []
    for e in events:
        eid = str(first_present(e, "id", "eventId", default=""))
        if not eid or eid in seen_events:
            continue
        seen_events.add(eid)
        unique_events.append(e)

    out: List[ThresholdMarket] = []
    now = now_ts()
    for e in unique_events:
        eid = str(first_present(e, "id", "eventId", default=""))
        event_title = str(first_present(e, "title", "question", default=""))
        event_slug = str(first_present(e, "slug", default=""))
        for raw_m in e.get("markets") or []:
            if not isinstance(raw_m, dict):
                continue
            q = str(first_present(raw_m, "question", "title", default=""))
            desc = str(first_present(raw_m, "description", "resolutionSource", default=""))
            text = " ".join([event_title, event_slug, q, desc])
            asset = infer_asset(text)
            if asset not in ASSETS:
                continue
            condition = parse_condition(text)
            threshold = parse_threshold(text)
            expiry = event_expiry_ts(e, raw_m)
            if not condition or not threshold or not expiry:
                continue
            days = (expiry - now) / 86400.0
            if days <= 0 or days > max_days:
                continue
            markets = parse_markets_from_events([dict(e, markets=[raw_m])], only_weatherish=False)
            if not markets:
                continue
            m = markets[0]
            if not m.yes_token or not m.no_token:
                continue
            out.append(ThresholdMarket(
                asset=asset,
                condition=condition,
                threshold=threshold,
                expiry_ts=expiry,
                event_id=eid,
                event_title=event_title,
                event_slug=event_slug,
                market_id=m.market_id,
                market_slug=m.market_slug,
                question=q,
                description=desc,
                yes_token_id=m.yes_token.token_id,
                no_token_id=m.no_token.token_id,
            ))
    # de-duplicate by market_id
    dedup: Dict[str, ThresholdMarket] = {}
    for x in out:
        dedup[x.market_id] = x
    return list(dedup.values())


def deribit_get(path: str, params: dict) -> dict:
    url = "https://www.deribit.com/api/v2/" + path.lstrip("/")
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    payload = r.json()
    if "result" not in payload:
        raise RuntimeError(f"bad deribit payload: {payload}")
    return payload["result"]


def deribit_index_price(asset: str) -> float:
    result = deribit_get("public/get_index_price", {"index_name": ASSETS[asset]["index"]})
    return float(result["index_price"])


def parse_deribit_instrument(name: str) -> Optional[Tuple[int, str, float, str]]:
    # BTC-27JUN25-100000-C
    parts = name.split("-")
    if len(parts) < 4:
        return None
    date_s = parts[1].upper()
    m = re.match(r"(\d{1,2})([A-Z]{3})(\d{2})", date_s)
    if not m:
        return None
    day = int(m.group(1))
    month = MONTHS.get(m.group(2))
    year = 2000 + int(m.group(3))
    if not month:
        return None
    expiry_dt = datetime(year, month, day, 8, 0, 0, tzinfo=timezone.utc)
    try:
        strike = float(parts[2])
    except Exception:
        return None
    opt_type = parts[3].upper()
    return int(expiry_dt.timestamp()), date_s, strike, opt_type


def deribit_option_anchor(asset: str, target_expiry_ts: int) -> Optional[DeribitAnchor]:
    currency = ASSETS[asset]["deribit_currency"]
    spot = deribit_index_price(asset)
    summaries = deribit_get("public/get_book_summary_by_currency", {"currency": currency, "kind": "option"})
    rows = []
    for item in summaries:
        name = str(item.get("instrument_name") or "")
        parsed = parse_deribit_instrument(name)
        if not parsed:
            continue
        expiry_ts, expiry_name, strike, opt_type = parsed
        iv = item.get("mark_iv")
        if iv is None or float(iv) <= 0:
            continue
        rows.append((expiry_ts, expiry_name, strike, opt_type, float(iv), name))
    if not rows:
        return None
    # choose nearest expiry to market expiry, then strikes nearest spot; average call/put IV around ATM
    expiries = sorted({r[0] for r in rows}, key=lambda x: abs(x - target_expiry_ts))
    expiry = expiries[0]
    e_rows = [r for r in rows if r[0] == expiry]
    e_rows.sort(key=lambda r: abs(r[2] - spot))
    near = e_rows[: max(2, min(8, len(e_rows)))]
    ivs = [r[4] / 100.0 for r in near if r[4] > 0]
    if not ivs:
        return None
    atm_iv = sum(ivs) / len(ivs)
    atm_strike = near[0][2]
    t_years = max((target_expiry_ts - now_ts()) / (365.25 * 86400.0), 1 / (365.25 * 24))
    return DeribitAnchor(
        asset=asset,
        spot=spot,
        expiry_ts=expiry,
        expiry_name=near[0][1],
        atm_strike=atm_strike,
        atm_iv=atm_iv,
        t_years=t_years,
        source_count=len(near),
    )


def prob_above_lognormal(spot: float, strike: float, sigma: float, t_years: float) -> float:
    if spot <= 0 or strike <= 0 or sigma <= 0 or t_years <= 0:
        return 0.5
    d2 = (math.log(spot / strike) - 0.5 * sigma * sigma * t_years) / (sigma * math.sqrt(t_years))
    return clamp(normal_cdf(d2), 0.001, 0.999)


def best_bid_ask(book: Optional[OrderBook]) -> Tuple[Optional[float], Optional[float], float, float]:
    if not book:
        return None, None, 0.0, 0.0
    bid = book.best_bid()
    ask = book.best_ask()
    bid_size = float(book.bids[0].size) if book.bids else 0.0
    ask_size = float(book.asks[0].size) if book.asks else 0.0
    return (float(bid) if bid is not None else None, float(ask) if ask is not None else None, bid_size, ask_size)


def write_csv(path: str | Path, rows: List[dict]) -> None:
    if not rows:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    exists = p.exists()
    fields = list(rows[0].keys())
    with p.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerows(rows)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pages", type=int, default=15)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--order", default="volume_24hr")
    p.add_argument("--max-days", type=float, default=90)
    p.add_argument("--min-edge", type=float, default=0.08)
    p.add_argument("--max-spread", type=float, default=0.12)
    p.add_argument("--min-depth-shares", type=float, default=20)
    p.add_argument("--output", default="paper_logs/crypto_threshold_deribit_signals.csv")
    p.add_argument("--top", type=int, default=20)
    args = p.parse_args()

    load_dotenv()
    config = Config()
    markets = discover_polymarket_thresholds(config, args.pages, args.limit, args.order, args.max_days)
    clob = ClobPublicClient(config)
    token_ids = sorted({x.yes_token_id for x in markets} | {x.no_token_id for x in markets})
    books = clob.get_books(token_ids, batch_size=250) if token_ids else {}
    anchors: Dict[Tuple[str, int], Optional[DeribitAnchor]] = {}
    signals: List[ThresholdSignal] = []
    rejects = {"no_book": 0, "wide_spread": 0, "low_depth": 0, "edge_below_min": 0, "no_deribit_anchor": 0}
    ts_ms = int(time.time() * 1000)

    for m in markets:
        anchor_key = (m.asset, m.expiry_ts)
        if anchor_key not in anchors:
            anchors[anchor_key] = deribit_option_anchor(m.asset, m.expiry_ts)
        anchor = anchors[anchor_key]
        if not anchor:
            rejects["no_deribit_anchor"] += 1
            continue
        p_above = prob_above_lognormal(anchor.spot, m.threshold, anchor.atm_iv, anchor.t_years)
        p_yes = p_above if m.condition == "above" else 1.0 - p_above
        p_no = 1.0 - p_yes
        yb = books.get(m.yes_token_id)
        nb = books.get(m.no_token_id)
        yes_bid, yes_ask, yes_bid_sz, yes_ask_sz = best_bid_ask(yb)
        no_bid, no_ask, no_bid_sz, no_ask_sz = best_bid_ask(nb)
        if None in (yes_bid, yes_ask, no_bid, no_ask):
            rejects["no_book"] += 1
            continue
        assert yes_bid is not None and yes_ask is not None and no_bid is not None and no_ask is not None
        yes_spread = yes_ask - yes_bid
        no_spread = no_ask - no_bid
        candidates = [
            ("BUY_YES", yes_ask, yes_bid, yes_spread, yes_ask_sz, p_yes),
            ("BUY_NO", no_ask, no_bid, no_spread, no_ask_sz, p_no),
        ]
        for action, ask, bid, spread, ask_sz, model_prob in candidates:
            if spread > args.max_spread:
                rejects["wide_spread"] += 1
                continue
            if ask_sz < args.min_depth_shares:
                rejects["low_depth"] += 1
                continue
            edge = model_prob - ask
            if edge < args.min_edge:
                rejects["edge_below_min"] += 1
                continue
            signals.append(ThresholdSignal(
                ts_ms=ts_ms,
                strategy="crypto_threshold_deribit_iv",
                asset=m.asset,
                event_title=m.event_title,
                question=m.question,
                condition=m.condition,
                threshold=m.threshold,
                expiry_ts=m.expiry_ts,
                days_to_expiry=(m.expiry_ts - now_ts()) / 86400.0,
                spot=anchor.spot,
                atm_iv=anchor.atm_iv,
                deribit_expiry_name=anchor.expiry_name,
                p_yes=p_yes,
                p_no=p_no,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                yes_spread=yes_spread,
                yes_ask_size=yes_ask_sz,
                no_bid=no_bid,
                no_ask=no_ask,
                no_spread=no_spread,
                no_ask_size=no_ask_sz,
                action=action,
                ask=ask,
                model_prob=model_prob,
                edge=edge,
                reason="deribit_lognormal_iv_minus_polymarket_ask",
            ))

    signals.sort(key=lambda x: (x.edge, x.yes_ask_size if x.action == "BUY_YES" else x.no_ask_size), reverse=True)
    if signals:
        write_csv(args.output, [asdict(x) for x in signals])
    best = signals[0] if signals else None
    print(
        f"DERIBIT_THRESHOLD_SUMMARY markets={len(markets)} books={len(books)} signals={len(signals)} "
        f"best_edge={(best.edge if best else float('-inf')):.4f} best_asset={(best.asset if best else '')} "
        f"best_action={(best.action if best else '')} rejects=" + json.dumps(rejects, sort_keys=True)
    )
    for s in signals[: args.top]:
        print(
            f"DERIBIT_THRESHOLD_SIGNAL asset={s.asset} action={s.action} edge={s.edge:.4f} "
            f"prob={s.model_prob:.4f} ask={s.ask:.4f} spot={s.spot:.2f} threshold={s.threshold:.2f} "
            f"days={s.days_to_expiry:.1f} iv={s.atm_iv:.3f} event=\"{s.event_title[:90]}\" q=\"{s.question[:90]}\""
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
