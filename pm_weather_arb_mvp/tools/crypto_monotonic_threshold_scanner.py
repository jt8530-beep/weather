#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Crypto monotonic threshold scanner for Polymarket.

Paper-only. No wallet, no signing, no orders.

Idea:
For path-dependent threshold events such as "Will BTC hit/reach/be above $X by DATE?",
a later deadline should be a superset of an earlier deadline for the same asset,
threshold, and direction.

If A = earlier event and B = later event, then A => B.
A safe long-only coverage candidate is:
    BUY B YES + BUY A NO
If cost < 1, the pair is structurally attractive.

This scanner only accepts strict path-dependent wording. It rejects generic
"on DATE close above" style markets because those are not monotonic.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv

from pm_weather_arb.clob import ClobPublicClient
from pm_weather_arb.config import Config
from pm_weather_arb.gamma import GammaClient, parse_markets_from_events
from pm_weather_arb.types import Market, OrderBook
from pm_weather_arb.util import first_present


ASSET_TERMS = {
    "BTC": ["bitcoin", "btc"],
    "ETH": ["ethereum", "ether", "eth"],
    "SOL": ["solana", "sol"],
}

PATH_TERMS = ["hit", "reach", "touch", "trade above", "trade below", "go above", "go below", "break above", "break below"]
BAD_TERMS = ["close", "closing", "end", "on ", "at ", "average", "market cap", "etf", "election", "reserve", "company"]


@dataclass
class ParsedThreshold:
    asset: str
    direction: str  # above / below
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
    kind: str
    asset: str
    direction: str
    threshold: float
    earlier_expiry_ts: int
    later_expiry_ts: int
    earlier_event: str
    later_event: str
    earlier_question: str
    later_question: str
    buy_later_yes_ask: float
    buy_later_yes_size: float
    buy_earlier_no_ask: float
    buy_earlier_no_size: float
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


def parse_threshold(text: str) -> Optional[float]:
    t = str(text).replace(",", "")
    m = re.search(r"\$?\s*(\d+(?:\.\d+)?)\s*k\b", t, flags=re.I)
    if m:
        return float(m.group(1)) * 1000.0
    vals = []
    for m in re.finditer(r"\$?\s*(\d{2,7}(?:\.\d+)?)", t):
        v = float(m.group(1))
        if v > 10:
            vals.append(v)
    if not vals:
        return None
    # pick largest plausible price-looking number, not years.
    vals = [v for v in vals if v not in range(2020, 2035)]
    if not vals:
        return None
    return max(vals)


def parse_direction(text: str) -> Optional[str]:
    t = norm(text)
    if any(x in t for x in ["above", "over", "higher than", "greater than"]):
        return "above"
    if any(x in t for x in ["below", "under", "lower than", "less than"]):
        return "below"
    return None


def is_strict_path_dependent(text: str) -> bool:
    t = norm(text)
    if not any(x in t for x in PATH_TERMS):
        return False
    # Avoid close/on-date markets. They are not subset/superset monotonic.
    for bad in BAD_TERMS:
        if bad in t and not ("by " in t or "before" in t):
            return False
    if not any(x in t for x in [" by ", "before", "during", "in 202", "this year", "next year"]):
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
            if not asset or not direction or not threshold or not expiry:
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
    # de-dupe by market_id
    by_id = {x.market_id: x for x in out}
    return list(by_id.values())


def best_ask(book: Optional[OrderBook]) -> Tuple[Optional[float], float]:
    if not book or not book.asks:
        return None, 0.0
    return float(book.asks[0].price), float(book.asks[0].size)


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
    config = Config()
    parsed = parse_events(config, args.pages, args.limit, args.order, args.max_days)
    clob = ClobPublicClient(config)
    token_ids = sorted({x.yes_token_id for x in parsed} | {x.no_token_id for x in parsed})
    books = clob.get_books(token_ids, batch_size=250) if token_ids else {}

    groups: Dict[Tuple[str, str, float], List[ParsedThreshold]] = {}
    for x in parsed:
        groups.setdefault((x.asset, x.direction, x.threshold), []).append(x)

    out: List[MonoCandidate] = []
    for (asset, direction, threshold), items in groups.items():
        items = sorted(items, key=lambda x: x.expiry_ts)
        if len(items) < 2:
            continue
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                earlier = items[i]
                later = items[j]
                later_yes_ask, later_yes_size = best_ask(books.get(later.yes_token_id))
                earlier_no_ask, earlier_no_size = best_ask(books.get(earlier.no_token_id))
                if later_yes_ask is None or earlier_no_ask is None:
                    continue
                if later_yes_size < args.min_depth_shares or earlier_no_size < args.min_depth_shares:
                    continue
                cost = later_yes_ask + earlier_no_ask
                edge = 1.0 - cost
                if edge < args.min_edge:
                    continue
                out.append(MonoCandidate(
                    ts_ms=int(time.time() * 1000),
                    kind="MONOTONIC_BUY_LATER_YES_BUY_EARLIER_NO",
                    asset=asset,
                    direction=direction,
                    threshold=threshold,
                    earlier_expiry_ts=earlier.expiry_ts,
                    later_expiry_ts=later.expiry_ts,
                    earlier_event=earlier.event_title,
                    later_event=later.event_title,
                    earlier_question=earlier.question,
                    later_question=later.question,
                    buy_later_yes_ask=later_yes_ask,
                    buy_later_yes_size=later_yes_size,
                    buy_earlier_no_ask=earlier_no_ask,
                    buy_earlier_no_size=earlier_no_size,
                    cost=cost,
                    gross_edge=edge,
                    max_size=min(later_yes_size, earlier_no_size),
                    reason="same_asset_threshold_direction_later_deadline_superset",
                ))

    out.sort(key=lambda x: (x.gross_edge, x.max_size), reverse=True)
    if out:
        write_csv(args.output, [asdict(x) for x in out])
    best = out[0] if out else None
    print(
        f"MONO_THRESHOLD_SUMMARY parsed={len(parsed)} groups={len(groups)} candidates={len(out)} "
        f"best_edge={(best.gross_edge if best else float('-inf')):.4f} best_asset={(best.asset if best else '')} "
        f"best_threshold={(best.threshold if best else '')}"
    )
    for c in out[: args.top]:
        print(
            f"MONO_THRESHOLD_CANDIDATE asset={c.asset} dir={c.direction} threshold={c.threshold} "
            f"edge={c.gross_edge:.4f} cost={c.cost:.4f} max_size={c.max_size:.2f} "
            f"earlier=\"{c.earlier_event[:70]}\" later=\"{c.later_event[:70]}\""
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
