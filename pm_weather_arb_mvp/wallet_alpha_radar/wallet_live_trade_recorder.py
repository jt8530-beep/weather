#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Record future trades by PROVISIONAL wallet watchlist.

No wallet, no signing, no orders.

Why this exists:
Historical wallet trades are useful for first-pass scoring, but many historical
markets cannot be enriched because old Gamma metadata is hard to recover from
active-event endpoints. Future trades can be recorded with current market
metadata and order book snapshots, which is what we need for delayed follow
backtests.

This script polls recent public trades, filters wallets from wallet_watchlist.csv,
then writes matched trades to SQLite and CSV. It is a data recorder only.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

import requests
from dotenv import load_dotenv

from pm_weather_arb.config import Config
from pm_weather_arb.gamma import GammaClient
from pm_weather_arb.util import first_present


TRADE_ENDPOINTS = [
    "https://data-api.polymarket.com/trades",
    "https://clob.polymarket.com/trades",
]

WALLET_KEYS = ["proxyWallet", "proxy_wallet", "wallet", "maker", "taker", "user", "address", "trader"]
MARKET_KEYS = ["conditionId", "condition_id", "market", "marketId", "market_id", "slug"]
PRICE_KEYS = ["price", "avgPrice", "avg_price", "outcomePrice"]
SIZE_KEYS = ["size", "amount", "shares", "matchedAmount"]
SIDE_KEYS = ["side", "outcome", "outcomeName", "asset"]
TIME_KEYS = ["timestamp", "createdAt", "created_at", "time"]
TX_KEYS = ["transactionHash", "transaction_hash", "txHash", "hash", "id"]

CATEGORY_RULES = [
    ("weather", ["temperature", "weather", "rain", "snow", "hurricane", "tornado", "wind"]),
    ("crypto", ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "doge", "crypto", "binance", "coinbase"]),
    ("sports", ["nba", "nfl", "nhl", "mlb", "ufc", "soccer", "champions league", "premier league", "tennis", "golf", "f1"]),
    ("politics", ["election", "trump", "biden", "republican", "democrat", "senate", "house", "governor", "president", "minister"]),
    ("economics", ["fed", "inflation", "cpi", "rate", "recession", "gdp", "unemployment", "tariff"]),
    ("business", ["earnings", "ipo", "tesla", "nvidia", "apple", "microsoft", "stock", "spacex", "openai"]),
    ("culture", ["movie", "album", "song", "grammy", "oscar", "taylor", "weeknd", "sabrina", "box office"]),
]


def norm(s: object) -> str:
    return re.sub(r"\s+", " ", str(s or "").lower()).strip()


def infer_category(*parts: object) -> str:
    text = norm(" ".join(str(x or "") for x in parts))
    for cat, kws in CATEGORY_RULES:
        if any(k in text for k in kws):
            return cat
    return "unknown"


def first_val(obj: Dict[str, Any], keys: Iterable[str], default: Any = "") -> Any:
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    return default


def norm_wallet(value: Any) -> str:
    if isinstance(value, dict):
        for k in WALLET_KEYS:
            if value.get(k):
                return norm_wallet(value.get(k))
    s = str(value or "").strip().lower()
    return s if s.startswith("0x") else ""


def fnum(x: Any, default: float = 0.0) -> float:
    try:
        if x in (None, ""):
            return default
        return float(x)
    except Exception:
        return default


def normalize_rows(payload: Any) -> List[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ["data", "trades", "results", "items"]:
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    return []


def fetch_recent_trades(limit: int) -> tuple[List[dict], str, str]:
    errors = []
    for url in TRADE_ENDPOINTS:
        for params in [{"limit": limit}, {"limit": limit, "offset": 0}]:
            try:
                r = requests.get(url, params=params, timeout=20)
                r.raise_for_status()
                rows = normalize_rows(r.json())
                if rows:
                    return rows, url, ""
            except Exception as e:
                errors.append(f"{url} {params}: {e}")
    return [], "", "; ".join(errors[-4:])


def load_watch_wallets(path: Path, gates: Set[str]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            w = str(r.get("wallet") or "").lower()
            gate = str(r.get("gate") or "")
            if w and (not gates or gate in gates):
                out[w] = r
    return out


def build_active_market_index(pages: int, limit: int, order: str) -> Dict[str, dict]:
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
    idx: Dict[str, dict] = {}
    for e in events:
        event_title = str(first_present(e, "title", "question", default=""))
        event_slug = str(first_present(e, "slug", default=""))
        event_cat = str(first_present(e, "category", "subcategory", default=""))
        tags = e.get("tags") or []
        tag_text = " ".join(str(t.get("label") or t.get("slug") or t.get("name") or "") for t in tags if isinstance(t, dict))
        for raw in e.get("markets") or []:
            if not isinstance(raw, dict):
                continue
            market_id = str(first_present(raw, "id", "marketId", default=""))
            condition_id = str(first_present(raw, "conditionId", "condition_id", "questionID", default=""))
            market_slug = str(first_present(raw, "slug", default=""))
            q = str(first_present(raw, "question", "title", default=""))
            desc = str(first_present(raw, "description", "resolutionSource", default=""))
            cat = infer_category(event_cat, tag_text, event_title, event_slug, market_slug, q, desc)
            rec = {
                "event_title": event_title,
                "event_slug": event_slug,
                "market_id_gamma": market_id,
                "condition_id": condition_id,
                "market_slug": market_slug,
                "question": q,
                "category_enriched": cat,
            }
            for key in [market_id, condition_id, market_slug]:
                if key:
                    idx[key.lower()] = rec
    return idx


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_live_trades (
            unique_id TEXT PRIMARY KEY,
            seen_ts_ms INTEGER NOT NULL,
            wallet TEXT NOT NULL,
            wallet_gate TEXT,
            wallet_score TEXT,
            trade_ts TEXT,
            market_key TEXT,
            side TEXT,
            price REAL,
            size REAL,
            notional REAL,
            category_enriched TEXT,
            event_title TEXT,
            market_slug TEXT,
            question TEXT,
            source TEXT,
            raw_json TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wlt_wallet_ts ON wallet_live_trades(wallet, seen_ts_ms)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wlt_market_ts ON wallet_live_trades(market_key, seen_ts_ms)")
    conn.commit()


def make_unique_id(row: dict, wallet: str, market_key: str, price: float, size: float) -> str:
    tx = str(first_val(row, TX_KEYS, ""))
    ts = str(first_val(row, TIME_KEYS, ""))
    side = str(first_val(row, SIDE_KEYS, ""))
    if tx:
        return f"tx:{tx}:{wallet}:{market_key}:{side}:{price}:{size}"
    return f"row:{wallet}:{market_key}:{side}:{price}:{size}:{ts}"


def append_csv(path: Path, rows: List[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    fields = list(rows[0].keys())
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerows(rows)


def run_once(args) -> int:
    watches = load_watch_wallets(Path(args.wallet_watchlist), set(args.gates.split(",")) if args.gates else set())
    if not watches:
        print("WALLET_LIVE_RECORDER_SUMMARY trades=0 matched=0 reason=no_watch_wallets")
        return 0
    market_idx = build_active_market_index(args.gamma_pages, args.gamma_limit, args.gamma_order)
    rows, source, err = fetch_recent_trades(args.limit)
    seen_ts_ms = int(time.time() * 1000)
    matched_rows: List[dict] = []
    inserts = []
    for r in rows:
        wallet = norm_wallet(first_val(r, WALLET_KEYS))
        if not wallet:
            for v in r.values():
                wallet = norm_wallet(v)
                if wallet:
                    break
        if wallet not in watches:
            continue
        market_key = str(first_val(r, MARKET_KEYS, "")).strip().lower()
        price = fnum(first_val(r, PRICE_KEYS))
        size = fnum(first_val(r, SIZE_KEYS))
        side = str(first_val(r, SIDE_KEYS, ""))
        trade_ts = str(first_val(r, TIME_KEYS, ""))
        meta = market_idx.get(market_key, {})
        watch = watches.get(wallet, {})
        uid = make_unique_id(r, wallet, market_key, price, size)
        rec = {
            "unique_id": uid,
            "seen_ts_ms": seen_ts_ms,
            "wallet": wallet,
            "wallet_gate": watch.get("gate", ""),
            "wallet_score": watch.get("score", ""),
            "trade_ts": trade_ts,
            "market_key": market_key,
            "side": side,
            "price": price,
            "size": size,
            "notional": price * size,
            "category_enriched": meta.get("category_enriched", "unknown"),
            "event_title": meta.get("event_title", ""),
            "market_slug": meta.get("market_slug", ""),
            "question": meta.get("question", ""),
            "source": source,
        }
        matched_rows.append(rec)
        inserts.append((*rec.values(), json.dumps(r, ensure_ascii=False)))

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        init_db(conn)
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO wallet_live_trades
            (unique_id,seen_ts_ms,wallet,wallet_gate,wallet_score,trade_ts,market_key,side,price,size,notional,category_enriched,event_title,market_slug,question,source,raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            inserts,
        )
        conn.commit()
        inserted = conn.total_changes - before
    finally:
        conn.close()

    append_csv(Path(args.csv_output), matched_rows)
    print(
        f"WALLET_LIVE_RECORDER_SUMMARY recent_trades={len(rows)} watch_wallets={len(watches)} matched={len(matched_rows)} "
        f"inserted={inserted} active_market_index={len(market_idx)} source={source or 'none'} error={err!r} db={args.db}"
    )
    for rec in matched_rows[:20]:
        print(
            f"WALLET_LIVE_TRADE wallet={rec['wallet']} gate={rec['wallet_gate']} cat={rec['category_enriched']} "
            f"price={rec['price']} size={rec['size']} market={rec['market_key']} event=\"{str(rec['event_title'])[:80]}\""
        )
    return inserted


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--wallet-watchlist", default="paper_logs/wallet_alpha/wallet_watchlist.csv")
    p.add_argument("--gates", default="PROVISIONAL_A,PROVISIONAL_B")
    p.add_argument("--db", default="paper_logs/wallet_alpha/wallet_live_trades.sqlite")
    p.add_argument("--csv-output", default="paper_logs/wallet_alpha/wallet_live_trades.csv")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--gamma-pages", type=int, default=20)
    p.add_argument("--gamma-limit", type=int, default=100)
    p.add_argument("--gamma-order", default="volume_24hr")
    p.add_argument("--once", action="store_true")
    p.add_argument("--sleep", type=int, default=30)
    args = p.parse_args()

    load_dotenv()
    if args.once:
        run_once(args)
        return 0
    while True:
        try:
            run_once(args)
        except Exception as e:
            print(f"WALLET_LIVE_RECORDER_ERROR {type(e).__name__}: {e}")
        time.sleep(args.sleep)


if __name__ == "__main__":
    raise SystemExit(main())
