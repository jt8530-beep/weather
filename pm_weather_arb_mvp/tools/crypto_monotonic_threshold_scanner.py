#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Crypto monotonic threshold scanner for Polymarket.

Paper-only. No wallet, no signing, no orders.

This version only accepts *coin spot-price* path-dependent threshold events. It
rejects NFT floor markets denominated in ETH/SOL, such as CryptoPunks/Pudgy
floor price markets. Those are not ETH/SOL coin price thresholds.

Two monotonic structures are checked:

1. Same threshold, different deadline:
   Earlier deadline event A implies later deadline event B.
   Safe long-only pair: BUY later YES + BUY earlier NO.

2. Same deadline, nested thresholds:
   For direction=below: deeper lower threshold is subset of shallower threshold.
     e.g. hit below $20 => hit below $40.
   For direction=above: higher threshold is subset of lower threshold.
     e.g. hit above $120k => hit above $100k.
   Safe long-only pair: BUY superset YES + BUY subset NO.
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

from pm_weather_arb.clob import ClobPublicClient
from pm_weather_arb.config import Config
from pm_weather_arb.gamma import GammaClient, parse_markets_from_events
from pm_weather_arb.types import OrderBook
from pm_weather_arb.util import first_present


SCANNER_VERSION = "mono_threshold_v3_spot_usd_only_20260524"

ASSET_TERMS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "ether", "eth"],
    "SOL": ["solana", "sol"],
}

# Hard sanity ranges for coin USD spot thresholds. These prevent NFT-floor and
# unit-denominated false positives such as CryptoPunks 20 ETH or Pudgy 2 ETH.
PRICE_RANGES = {
    "BTC": (1000.0, 1_000_000.0),
    "ETH": (100.0, 100_000.0),
    "SOL": (1.0, 10_000.0),
}

SPOT_PRICE_TERMS = ["price", "$", "usd", "usdc", "dollar", "dollars"]
PATH_TERMS = ["hit", "reach", "touch", "trade above", "trade below", "go above", "go below", "break above", "break below", "dip", "drop"]
NFT_FLOOR_TERMS = ["cryptopunk", "cryptopunks", "pudgy", "penguin", "penguins", "azuki", "bayc", "mayc", "floor", "floor price", "nft", "milady"]
BAD_TERMS = ["market cap", "etf", "election", "reserve", "company", "ipo", "stock", "mstr", "microstrategy"]


@dataclass
class ParsedThreshold:
    asset: str
    direction: str
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
class MonoCandidate:
    ts_ms: int
    scanner_version: str
    kind: str
    asset: str
    direction: str
    threshold: float
    superset_threshold: float
    subset_threshold: float
    earlier_expiry_ts: int
    later_expiry_ts: int
    superset_event: str
    subset_event: str
    superset_question: str
    subset_question: str
    buy_superset_yes_ask: float
    buy_superset_yes_size: float
    buy_subset_no_ask: float
    buy_subset_no_size: float
    cost: float
    gross_edge: float
    max_size: float
    reason: str


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").lower()).strip()


def parse_iso_ts(value: object) -> Optional[int]:
    if not value:
        return None
    s = str(value).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return None


def event_expiry_ts(event: dict, market: dict) -> Optional[int]:
    keys = ["endDate", "end_date", "endTime", "end_time", "closeTime", "close_time", "resolutionTime", "resolution_time"]
    for src in (market, event):
        for k in keys:
            if isinstance(src, dict):
                ts = parse_iso_ts(src.get(k))
                if ts:
                    return ts
    return None


def infer_asset(text: str) -> Optional[str]:
    t = norm(text)
    for asset, terms in ASSET_TERMS.items():
        if any(re.search(rf"\b{re.escape(x)}\b", t) for x in terms):
            return asset
    return None


def threshold_in_asset_range(asset: str, threshold: float) -> bool:
    lo, hi = PRICE_RANGES.get(asset, (0.0, float("inf")))
    return lo <= threshold <= hi


def has_usd_price_context(text: str) -> bool:
    t = norm(text)
    if any(x in t for x in NFT_FLOOR_TERMS):
        return False
    if any(x in t for x in BAD_TERMS):
        return False
    if not any(x in t for x in SPOT_PRICE_TERMS):
        return False
    # Reject plain "20 ETH" / "10 ETH" denominated thresholds unless there is a
    # clear USD/dollar threshold too.
    if re.search(r"\b\d+(?:\.\d+)?\s*(eth|sol|btc)\b", t) and "$" not in t and "usd" not in t and "dollar" not in t:
        return False
    return True


def parse_threshold(text: str) -> Optional[float]:
    t = str(text).replace(",", "")
    dollar_vals = []
    for m in re.finditer(r"\$\s*(\d+(?:\.\d+)?)\s*k\b", t, flags=re.I):
        dollar_vals.append(float(m.group(1)) * 1000.0)
    for m in re.finditer(r"\$\s*(\d{1,7}(?:\.\d+)?)", t):
        dollar_vals.append(float(m.group(1)))
    if dollar_vals:
        vals = [v for v in dollar_vals if int(v) not in range(2020, 2035)]
        return max(vals) if vals else None
    if "usd" in t.lower() or "dollar" in t.lower():
        m = re.search(r"(\d+(?:\.\d+)?)\s*k\b", t, flags=re.I)
        if m:
            return float(m.group(1)) * 1000.0
        vals = [float(m.group(1)) for m in re.finditer(r"\b(\d{1,7}(?:\.\d+)?)\b", t)]
        vals = [v for v in vals if int(v) not in range(2020, 2035)]
        return max(vals) if vals else None
    return None


def parse_direction(text: str) -> Optional[str]:
    t = norm(text)
    if any(x in t for x in ["below", "under", "lower than", "less than", "dip", "drop", "break below"]):
        return "below"
    if any(x in t for x in ["above", "over", "higher than", "greater than", "reach", "hit", "touch", "break above"]):
        return "above"
    return None


def is_strict_path_dependent(text: str) -> bool:
    t = norm(text)
    if not has_usd_price_context(t):
        return False
    if not any(x in t for x in PATH_TERMS):
        return False
    if not any(x in t for x in [" by ", "before", "during", "in 202", "this year", "next year"]):
        return False
    if any(x in t for x in ["close above", "close below", "closing price", "at end", "end of"]):
        return False
    return True


def parse_events(config: Config, pages: int, limit: int, order: str, max_days: float) -> List[ParsedThreshold]:
    gamma = GammaClient(config)
    events: List[dict] = []
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
    now = int(time.time())
    out: List[ParsedThreshold] = []
    for e in events:
        event_title = str(first_present(e, "title", "question", default=""))
        event_slug = str(first_present(e, "slug", default=""))
        event_id = str(first_present(e, "id", "eventId", default=""))
        for raw_m in e.get("markets") or []:
            if not isinstance(raw_m, dict):
                continue
            q = str(first_present(raw_m, "question", "title", default=""))
            desc = str(first_present(raw_m, "description", "resolutionSource", default=""))
            text = " ".join([event_title, event_slug, q, desc])
            if not is_strict_path_dependent(text):
                continue
            asset = infer_asset(text)
            direction = parse_direction(text)
            threshold = parse_threshold(text)
            expiry = event_expiry_ts(e, raw_m)
            if not asset or not direction or threshold is None or not expiry:
                continue
            if not threshold_in_asset_range(asset, threshold):
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
            out.append(ParsedThreshold(
                asset=asset,
                direction=direction,
                threshold=round(threshold, 4),
                expiry_ts=expiry,
                event_id=event_id,
                event_title=event_title,
                event_slug=event_slug,
                market_id=m.market_id,
                market_slug=m.market_slug,
                question=q,
                description=desc,
                yes_token_id=m.yes_token.token_id,
                no_token_id=m.no_token.token_id,
            ))
    by_id = {x.market_id: x for x in out}
    return list(by_id.values())


def best_ask(book: Optional[OrderBook]) -> Tuple[Optional[float], float]:
    if not book or not book.asks:
        return None, 0.0
    return float(book.asks[0].price), float(book.asks[0].size)


def add_candidate(
    out: List[MonoCandidate],
    kind: str,
    superset: ParsedThreshold,
    subset: ParsedThreshold,
    books: Dict[str, OrderBook],
    min_depth: float,
    min_edge: float,
    reason: str,
) -> None:
    sup_yes_ask, sup_yes_size = best_ask(books.get(superset.yes_token_id))
    sub_no_ask, sub_no_size = best_ask(books.get(subset.no_token_id))
    if sup_yes_ask is None or sub_no_ask is None:
        return
    if sup_yes_size < min_depth or sub_no_size < min_depth:
        return
    cost = sup_yes_ask + sub_no_ask
    edge = 1.0 - cost
    if edge < min_edge:
        return
    out.append(MonoCandidate(
        ts_ms=int(time.time() * 1000),
        scanner_version=SCANNER_VERSION,
        kind=kind,
        asset=superset.asset,
        direction=superset.direction,
        threshold=subset.threshold,
        superset_threshold=superset.threshold,
        subset_threshold=subset.threshold,
        earlier_expiry_ts=min(superset.expiry_ts, subset.expiry_ts),
        later_expiry_ts=max(superset.expiry_ts, subset.expiry_ts),
        superset_event=superset.event_title,
        subset_event=subset.event_title,
        superset_question=superset.question,
        subset_question=subset.question,
        buy_superset_yes_ask=sup_yes_ask,
        buy_superset_yes_size=sup_yes_size,
        buy_subset_no_ask=sub_no_ask,
        buy_subset_no_size=sub_no_size,
        cost=cost,
        gross_edge=edge,
        max_size=min(sup_yes_size, sub_no_size),
        reason=reason,
    ))


def write_csv(path: str | Path, rows: List[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    exists = p.exists()
    fields = list(rows[0].keys())
    with p.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerows(rows)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pages", type=int, default=30)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--order", default="volume_24hr")
    p.add_argument("--max-days", type=float, default=365)
    p.add_argument("--min-edge", type=float, default=0.02)
    p.add_argument("--min-depth-shares", type=float, default=10)
    p.add_argument("--output", default="paper_logs/crypto_monotonic_threshold_candidates.csv")
    p.add_argument("--top", type=int, default=30)
    args = p.parse_args()

    load_dotenv()
    print(f"MONO_THRESHOLD_VERSION {SCANNER_VERSION}")
    config = Config()
    parsed = parse_events(config, args.pages, args.limit, args.order, args.max_days)
    clob = ClobPublicClient(config)
    token_ids = sorted({x.yes_token_id for x in parsed} | {x.no_token_id for x in parsed})
    books = clob.get_books(token_ids, batch_size=250) if token_ids else {}

    out: List[MonoCandidate] = []

    by_same_threshold: Dict[Tuple[str, str, float], List[ParsedThreshold]] = {}
    for x in parsed:
        by_same_threshold.setdefault((x.asset, x.direction, x.threshold), []).append(x)
    for _, items in by_same_threshold.items():
        items = sorted(items, key=lambda x: x.expiry_ts)
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                add_candidate(
                    out,
                    "MONO_SAME_THRESHOLD_BUY_LATER_YES_BUY_EARLIER_NO",
                    superset=items[j],
                    subset=items[i],
                    books=books,
                    min_depth=args.min_depth_shares,
                    min_edge=args.min_edge,
                    reason="same_threshold_later_deadline_superset",
                )

    by_same_expiry: Dict[Tuple[str, str, int], List[ParsedThreshold]] = {}
    for x in parsed:
        by_same_expiry.setdefault((x.asset, x.direction, x.expiry_ts), []).append(x)
    for (_, direction, _), items in by_same_expiry.items():
        items = sorted(items, key=lambda x: x.threshold)
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                low = items[i]
                high = items[j]
                if direction == "below":
                    superset, subset = high, low
                else:
                    superset, subset = low, high
                add_candidate(
                    out,
                    "MONO_NESTED_THRESHOLD_BUY_SUPERSET_YES_BUY_SUBSET_NO",
                    superset=superset,
                    subset=subset,
                    books=books,
                    min_depth=args.min_depth_shares,
                    min_edge=args.min_edge,
                    reason="same_expiry_nested_threshold_superset_yes_subset_no",
                )

    out.sort(key=lambda x: (x.gross_edge, x.max_size), reverse=True)
    if out:
        write_csv(args.output, [asdict(x) for x in out])
    best = out[0] if out else None
    groups = len(by_same_threshold) + len(by_same_expiry)
    print(
        f"MONO_THRESHOLD_SUMMARY version={SCANNER_VERSION} parsed={len(parsed)} groups={groups} candidates={len(out)} "
        f"best_edge={(best.gross_edge if best else float('-inf')):.4f} best_asset={(best.asset if best else '')} "
        f"best_threshold={(best.threshold if best else '')}"
    )
    for c in out[: args.top]:
        print(
            f"MONO_THRESHOLD_CANDIDATE version={SCANNER_VERSION} kind={c.kind} asset={c.asset} dir={c.direction} "
            f"super={c.superset_threshold} sub={c.subset_threshold} edge={c.gross_edge:.4f} "
            f"cost={c.cost:.4f} max_size={c.max_size:.2f} "
            f"superset=\"{c.superset_event[:65]}\" subset=\"{c.subset_event[:65]}\""
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
