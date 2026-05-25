#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Record Polymarket top-of-book snapshots for candidate markets.

Phase 1 data recorder. No wallet, no signing, no orders.

The purpose is to build our own historical order book snapshots for future
T+60s/T+300s delayed follow backtesting. Without this database, copy-trading
backtests are mostly fantasy.

If a market watchlist is provided, the recorder prioritizes active markets that
match the watchlist by market_id / condition_id / market_slug. It then fills the
remaining slots with high-volume active markets.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import time
from pathlib import Path
from typing import Iterable, List, Optional, Set

from dotenv import load_dotenv

from pm_weather_arb.clob import ClobPublicClient
from pm_weather_arb.config import Config
from pm_weather_arb.gamma import GammaClient, parse_markets_from_events
from pm_weather_arb.types import OrderBook


def read_candidate_markets(path: Path, top_markets: int) -> Set[str]:
    if not path.exists():
        return set()
    markets: List[str] = []
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for key in ["market_id", "condition_id", "conditionId", "market", "slug", "market_slug"]:
            val = str(r.get(key) or "").strip().lower()
            if val:
                markets.append(val)
                break
        if len(markets) >= top_markets:
            break
    return set(markets)


def discover_active_markets(limit: int, pages: int, order: str) -> list:
    gamma = GammaClient(Config())
    events = []
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
    return parse_markets_from_events(events, only_weatherish=False)


def market_keys(m) -> Set[str]:
    vals = [m.market_id, m.condition_id, m.market_slug, m.event_slug]
    return {str(x).strip().lower() for x in vals if x}


def prioritize_markets(markets: list, watch_ids: Set[str], top_markets: int) -> tuple[list, int]:
    if not watch_ids:
        return markets[:top_markets], 0
    watched = []
    other = []
    for m in markets:
        if market_keys(m) & watch_ids:
            watched.append(m)
        else:
            other.append(m)
    selected = (watched + other)[:top_markets]
    return selected, len(watched)


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            ts_ms INTEGER NOT NULL,
            event_id TEXT,
            event_title TEXT,
            market_id TEXT NOT NULL,
            market_slug TEXT,
            token_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            best_bid REAL,
            best_ask REAL,
            bid_size REAL,
            ask_size REAL,
            spread REAL,
            PRIMARY KEY (ts_ms, token_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_market_ts ON orderbook_snapshots(market_id, ts_ms)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_token_ts ON orderbook_snapshots(token_id, ts_ms)")
    conn.commit()


def best_values(book: Optional[OrderBook]) -> tuple[Optional[float], Optional[float], float, float, Optional[float]]:
    if not book:
        return None, None, 0.0, 0.0, None
    bid = book.best_bid()
    ask = book.best_ask()
    bid_f = float(bid) if bid is not None else None
    ask_f = float(ask) if ask is not None else None
    bid_size = float(book.bids[0].size) if book.bids else 0.0
    ask_size = float(book.asks[0].size) if book.asks else 0.0
    spread = (ask_f - bid_f) if bid_f is not None and ask_f is not None else None
    return bid_f, ask_f, bid_size, ask_size, spread


def record_once(db_path: Path, top_markets: int, pages: int, limit: int, order: str, market_watchlist: Optional[Path] = None) -> int:
    load_dotenv()
    watch_ids = read_candidate_markets(market_watchlist, top_markets * 5) if market_watchlist else set()
    markets_all = discover_active_markets(limit=limit, pages=pages, order=order)
    markets_all = [m for m in markets_all if m.yes_token and m.no_token]
    markets, watched_available = prioritize_markets(markets_all, watch_ids, top_markets)
    token_ids = []
    for m in markets:
        token_ids.append(m.yes_token.token_id)
        token_ids.append(m.no_token.token_id)
    clob = ClobPublicClient(Config())
    books = clob.get_books(sorted(set(token_ids)), batch_size=250) if token_ids else {}
    ts_ms = int(time.time() * 1000)
    rows = []
    for m in markets:
        for outcome, token in [("YES", m.yes_token), ("NO", m.no_token)]:
            b = books.get(token.token_id)
            bid, ask, bid_size, ask_size, spread = best_values(b)
            rows.append((
                ts_ms,
                m.event_id,
                m.event_title,
                m.market_id,
                m.market_slug,
                token.token_id,
                outcome,
                bid,
                ask,
                bid_size,
                ask_size,
                spread,
            ))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        conn.executemany(
            """
            INSERT OR REPLACE INTO orderbook_snapshots
            (ts_ms,event_id,event_title,market_id,market_slug,token_id,outcome,best_bid,best_ask,bid_size,ask_size,spread)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    print(
        f"ORDERBOOK_RECORDER_SUMMARY ts_ms={ts_ms} markets={len(markets)} tokens={len(token_ids)} "
        f"books={len(books)} rows={len(rows)} watched_ids={len(watch_ids)} watched_available={watched_available} db={db_path}"
    )
    return len(rows)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--candidates", default="paper_logs/wallet_alpha/candidate_wallets.csv", help="reserved legacy alias")
    p.add_argument("--market-watchlist", default="", help="CSV with market_id / condition_id / market_slug columns")
    p.add_argument("--db", default="paper_logs/wallet_alpha/orderbook_snapshots.sqlite")
    p.add_argument("--top-markets", type=int, default=200)
    p.add_argument("--pages", type=int, default=5)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--order", default="volume_24hr")
    p.add_argument("--once", action="store_true")
    p.add_argument("--sleep", type=int, default=30)
    args = p.parse_args()

    watch_path = Path(args.market_watchlist) if args.market_watchlist else None

    if args.once:
        record_once(Path(args.db), args.top_markets, args.pages, args.limit, args.order, watch_path)
        return 0

    while True:
        try:
            record_once(Path(args.db), args.top_markets, args.pages, args.limit, args.order, watch_path)
        except Exception as e:
            print(f"ORDERBOOK_RECORDER_ERROR {type(e).__name__}: {e}")
        time.sleep(args.sleep)


if __name__ == "__main__":
    raise SystemExit(main())
