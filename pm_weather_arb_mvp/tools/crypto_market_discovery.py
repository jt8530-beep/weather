#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discover actual tradeable Polymarket crypto markets before modeling.

This fixes the strategic mistake of hard-coding a horizon such as 15m before
confirming that such a market exists.

Paper/diagnostic only. No wallet, no signing, no orders.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from dataclasses import asdict, dataclass
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

SEARCH_TERMS = [
    "Bitcoin", "BTC", "Bitcoin price", "BTC price", "Bitcoin up or down", "BTC up or down",
    "Ethereum", "ETH", "Ethereum price", "ETH price", "Ethereum up or down", "ETH up or down",
    "Solana", "SOL", "Solana price", "SOL price", "Solana up or down", "SOL up or down",
    "crypto", "cryptocurrency",
]

BAD_CONTEXT = [
    "microstrategy", "mstr", "kraken", "ipo", "etf", "reserve", "treasury", "company",
    "president", "macron", "election", "candidate", "senate", "governor", "nobel",
]

PRICE_CONTEXT = [
    "up or down", "above", "below", "higher", "lower", "over", "under", "price", "reach", "hit",
    "close", "end", "ath", "all-time high",
]


@dataclass
class CryptoMarketRow:
    ts_ms: int
    asset: str
    event_id: str
    event_title: str
    event_slug: str
    market_id: str
    market_slug: str
    question: str
    active: bool
    closed: bool
    neg_risk: bool
    yes_bid: str
    yes_ask: str
    yes_bid_size: str
    yes_ask_size: str
    no_bid: str
    no_ask: str
    no_bid_size: str
    no_ask_size: str
    yes_spread: str
    no_spread: str
    classification: str
    tradeability: str
    reason: str


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").lower()).strip()


def infer_asset(m: Market) -> Optional[str]:
    text = norm(" ".join([m.event_title, m.question, m.event_slug, m.market_slug]))
    for asset, terms in ASSET_TERMS.items():
        if any(re.search(rf"\b{re.escape(t)}\b", text) for t in terms):
            return asset
    return None


def classify_market(m: Market) -> tuple[str, str, str]:
    text = norm(" ".join([m.event_title, m.question, m.event_slug, m.market_slug, m.description]))
    if any(x in text for x in BAD_CONTEXT):
        return "non_price_related", "REJECT", "bad_context"
    if not any(x in text for x in PRICE_CONTEXT):
        return "crypto_related_not_price", "REJECT", "no_price_context"
    if "up or down" in text or "15m" in text or "15-minute" in text or "15 minute" in text:
        return "up_down_or_short_horizon", "CANDIDATE", "short_horizon_or_updown"
    if any(x in text for x in ["above", "below", "over", "under", "higher", "lower"]):
        return "threshold_price", "CANDIDATE", "price_threshold"
    if any(x in text for x in ["reach", "hit", "ath", "all-time high"]):
        return "path_dependent_price", "RESEARCH", "path_dependent"
    return "other_price", "RESEARCH", "price_but_unclear"


def best_bid_ask(book: Optional[OrderBook]) -> Tuple[str, str, str, str, Optional[float], Optional[float]]:
    if not book:
        return "", "", "", "", None, None
    bid = book.best_bid()
    ask = book.best_ask()
    bid_size = book.bids[0].size if book.bids else ""
    ask_size = book.asks[0].size if book.asks else ""
    bid_f = float(bid) if bid is not None else None
    ask_f = float(ask) if ask is not None else None
    return (
        str(bid) if bid is not None else "",
        str(ask) if ask is not None else "",
        str(bid_size),
        str(ask_size),
        bid_f,
        ask_f,
    )


def raw_gamma_events(config: Config, terms: Iterable[str], limit: int) -> List[dict]:
    gamma = GammaClient(config)
    seen = set()
    out: List[dict] = []
    for term in terms:
        for key in ("search", "q", "query"):
            try:
                batch = gamma.list_events_raw({
                    "active": "true",
                    "closed": "false",
                    key: term,
                    "limit": limit,
                })
            except Exception:
                continue
            for e in batch:
                eid = str(first_present(e, "id", "eventId", default=""))
                if not eid or eid in seen:
                    continue
                seen.add(eid)
                out.append(e)
    return out


def token_ids(markets: Iterable[Market]) -> List[str]:
    out: List[str] = []
    for m in markets:
        if m.yes_token:
            out.append(m.yes_token.token_id)
        if m.no_token:
            out.append(m.no_token.token_id)
    return sorted(set(out))


def write_csv(path: str | Path, rows: List[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        p.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--assets", default="BTC,ETH,SOL")
    p.add_argument("--search-limit", type=int, default=100)
    p.add_argument("--book-batch-size", type=int, default=250)
    p.add_argument("--output", default="paper_logs/crypto_market_discovery.csv")
    p.add_argument("--top", type=int, default=50)
    args = p.parse_args()

    load_dotenv()
    assets = {x.strip().upper() for x in args.assets.split(",") if x.strip()}
    config = Config()
    events = raw_gamma_events(config, SEARCH_TERMS, limit=args.search_limit)
    markets_all = parse_markets_from_events(events, only_weatherish=False)
    markets: List[Market] = []
    seen = set()
    for m in markets_all:
        asset = infer_asset(m)
        if not asset or asset not in assets:
            continue
        if not m.yes_token or not m.no_token:
            continue
        key = f"{m.event_id}:{m.market_id}"
        if key in seen:
            continue
        seen.add(key)
        markets.append(m)

    clob = ClobPublicClient(config)
    books = clob.get_books(token_ids(markets), batch_size=args.book_batch_size) if markets else {}

    rows: List[CryptoMarketRow] = []
    now = int(time.time() * 1000)
    for m in markets:
        asset = infer_asset(m) or "UNKNOWN"
        classification, tradeability, reason = classify_market(m)
        ybook = books.get(m.yes_token.token_id if m.yes_token else "")
        nbook = books.get(m.no_token.token_id if m.no_token else "")
        ybid, yask, ybid_sz, yask_sz, ybid_f, yask_f = best_bid_ask(ybook)
        nbid, nask, nbid_sz, nask_sz, nbid_f, nask_f = best_bid_ask(nbook)
        yes_spread = ""
        no_spread = ""
        if ybid_f is not None and yask_f is not None:
            yes_spread = f"{(yask_f - ybid_f):.4f}"
        if nbid_f is not None and nask_f is not None:
            no_spread = f"{(nask_f - nbid_f):.4f}"
        rows.append(CryptoMarketRow(
            ts_ms=now,
            asset=asset,
            event_id=m.event_id,
            event_title=m.event_title,
            event_slug=m.event_slug,
            market_id=m.market_id,
            market_slug=m.market_slug,
            question=m.question,
            active=m.active,
            closed=m.closed,
            neg_risk=m.neg_risk,
            yes_bid=ybid,
            yes_ask=yask,
            yes_bid_size=ybid_sz,
            yes_ask_size=yask_sz,
            no_bid=nbid,
            no_ask=nask,
            no_bid_size=nbid_sz,
            no_ask_size=nask_sz,
            yes_spread=yes_spread,
            no_spread=no_spread,
            classification=classification,
            tradeability=tradeability,
            reason=reason,
        ))

    rows_sorted = sorted(rows, key=lambda r: (r.tradeability == "CANDIDATE", r.asset, r.event_title), reverse=True)
    write_csv(args.output, [asdict(r) for r in rows_sorted])

    counts: Dict[str, int] = {}
    for r in rows:
        k = f"{r.asset}:{r.tradeability}:{r.classification}"
        counts[k] = counts.get(k, 0) + 1
    candidate_count = sum(1 for r in rows if r.tradeability == "CANDIDATE")
    print(
        f"CRYPTO_DISCOVERY_SUMMARY events={len(events)} markets={len(markets)} books={len(books)} "
        f"candidates={candidate_count} counts=" + json.dumps(counts, sort_keys=True)
    )
    for r in [x for x in rows_sorted if x.tradeability == "CANDIDATE"][: args.top]:
        print(
            f"CRYPTO_MARKET_CANDIDATE asset={r.asset} class={r.classification} "
            f"yes={r.yes_bid}/{r.yes_ask} no={r.no_bid}/{r.no_ask} "
            f"event=\"{r.event_title[:90]}\" q=\"{r.question[:90]}\""
        )
    if candidate_count == 0:
        print("CRYPTO_DISCOVERY_NO_TRADEABLE_PRICE_MARKETS reason=no_active_btc_eth_sol_updown_or_threshold_candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
