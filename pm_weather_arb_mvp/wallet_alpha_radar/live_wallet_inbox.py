#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Full-market live wallet inbox.

No wallet, no signing, no orders.

This records every wallet seen in public recent trades into the same schema used
by today_wallet_selector.py. It is the correct input for intraday dynamic wallet
discovery. Fixed watchlists are only references, not the source of truth.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List

import requests
from dotenv import load_dotenv

TRADE_ENDPOINTS = [
    "https://data-api.polymarket.com/trades",
    "https://clob.polymarket.com/trades",
]

WALLET_KEYS = ["proxyWallet", "proxy_wallet", "wallet", "maker", "taker", "user", "address", "trader"]
CONDITION_KEYS = ["conditionId", "condition_id", "conditionID", "condition"]
MARKET_NUMERIC_KEYS = ["marketId", "market_id", "marketID", "market", "id"]
TOKEN_KEYS = ["asset", "tokenId", "token_id", "tokenID", "outcomeTokenId", "outcome_token_id", "clobTokenId", "clob_token_id"]
MARKET_KEYS = CONDITION_KEYS + MARKET_NUMERIC_KEYS + ["slug", "marketSlug", "market_slug"]
PRICE_KEYS = ["price", "avgPrice", "avg_price", "outcomePrice"]
SIZE_KEYS = ["size", "amount", "shares", "matchedAmount"]
SIDE_KEYS = ["side", "outcome", "outcomeName", "asset"]
TIME_KEYS = ["timestamp", "createdAt", "created_at", "time"]
TX_KEYS = ["transactionHash", "transaction_hash", "txHash", "hash", "id"]


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


def find_wallet(obj: Dict[str, Any]) -> str:
    w = norm_wallet(first_val(obj, WALLET_KEYS))
    if w:
        return w
    for v in obj.values():
        w = norm_wallet(v)
        if w:
            return w
    return ""


def find_hex(obj: Dict[str, Any], keys: Iterable[str]) -> str:
    for k in keys:
        v = str(obj.get(k) or "").strip().lower()
        if v.startswith("0x") and len(v) >= 20:
            return v
    return ""


def find_numeric(obj: Dict[str, Any], keys: Iterable[str], min_len: int = 1) -> str:
    for k in keys:
        v = str(obj.get(k) or "").strip()
        if v.isdigit() and len(v) >= min_len:
            return v
    return ""


def find_token_id(obj: Dict[str, Any]) -> str:
    for k in TOKEN_KEYS:
        v = str(obj.get(k) or "").strip()
        if v.isdigit() and len(v) >= 10:
            return v
    for v in obj.values():
        if isinstance(v, dict):
            t = find_token_id(v)
            if t:
                return t
    return ""


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


def ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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
            condition_id TEXT,
            market_id_numeric TEXT,
            token_id TEXT,
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
    for col in ["condition_id", "market_id_numeric", "token_id"]:
        ensure_column(conn, "wallet_live_trades", col, "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wlt_wallet_ts ON wallet_live_trades(wallet, seen_ts_ms)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wlt_market_ts ON wallet_live_trades(market_key, seen_ts_ms)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wlt_condition_ts ON wallet_live_trades(condition_id, seen_ts_ms)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wlt_numeric_market_ts ON wallet_live_trades(market_id_numeric, seen_ts_ms)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wlt_token_ts ON wallet_live_trades(token_id, seen_ts_ms)")
    conn.commit()


def make_unique_id(row: dict, wallet: str, market_key: str, token_id: str, price: float, size: float) -> str:
    tx = str(first_val(row, TX_KEYS, ""))
    ts = str(first_val(row, TIME_KEYS, ""))
    side = str(first_val(row, SIDE_KEYS, ""))
    if tx:
        return f"tx:{tx}:{wallet}:{market_key}:{token_id}:{side}:{price}:{size}"
    return f"row:{wallet}:{market_key}:{token_id}:{side}:{price}:{size}:{ts}"


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
    rows, source, err = fetch_recent_trades(args.limit)
    seen_ts_ms = int(time.time() * 1000)
    records: List[dict] = []
    inserts = []
    missing_wallet = 0
    missing_token = 0
    for r in rows:
        wallet = find_wallet(r)
        if not wallet:
            missing_wallet += 1
            continue
        condition_id = find_hex(r, CONDITION_KEYS)
        market_id_numeric = find_numeric(r, MARKET_NUMERIC_KEYS)
        token_id = find_token_id(r)
        if not token_id:
            missing_token += 1
        market_key = str(first_val(r, MARKET_KEYS, "")).strip().lower() or condition_id or market_id_numeric or token_id
        price = fnum(first_val(r, PRICE_KEYS))
        size = fnum(first_val(r, SIZE_KEYS))
        side = str(first_val(r, SIDE_KEYS, ""))
        trade_ts = str(first_val(r, TIME_KEYS, ""))
        uid = make_unique_id(r, wallet, market_key, token_id, price, size)
        rec = {
            "unique_id": uid,
            "seen_ts_ms": seen_ts_ms,
            "wallet": wallet,
            "wallet_gate": "FULL_MARKET",
            "wallet_score": "",
            "trade_ts": trade_ts,
            "market_key": market_key,
            "condition_id": condition_id,
            "market_id_numeric": market_id_numeric,
            "token_id": token_id,
            "side": side,
            "price": price,
            "size": size,
            "notional": price * size,
            "category_enriched": "unknown",
            "event_title": "",
            "market_slug": "",
            "question": "",
            "source": source,
        }
        records.append(rec)
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
            (unique_id,seen_ts_ms,wallet,wallet_gate,wallet_score,trade_ts,market_key,condition_id,market_id_numeric,token_id,side,price,size,notional,category_enriched,event_title,market_slug,question,source,raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            inserts,
        )
        conn.commit()
        inserted = conn.total_changes - before
    finally:
        conn.close()
    append_csv(Path(args.csv_output), records)

    cats = Counter(r["category_enriched"] for r in records)
    print(
        f"LIVE_WALLET_INBOX_SUMMARY recent_trades={len(rows)} records={len(records)} inserted={inserted} "
        f"wallets={len({r['wallet'] for r in records})} tokens={len({r['token_id'] for r in records if r['token_id']})} "
        f"missing_wallet={missing_wallet} missing_token={missing_token} source={source or 'none'} error={err!r} db={args.db} cats=" + ",".join(f"{k}:{v}" for k, v in cats.most_common(8))
    )
    for rec in records[:20]:
        print(f"LIVE_WALLET_INBOX_TRADE wallet={rec['wallet']} token={rec['token_id'][:12]} price={rec['price']} size={rec['size']}")
    return inserted


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="paper_logs/wallet_alpha/full_market_live_trades.sqlite")
    p.add_argument("--csv-output", default="paper_logs/wallet_alpha/full_market_live_trades.csv")
    p.add_argument("--limit", type=int, default=1000)
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
            print(f"LIVE_WALLET_INBOX_ERROR {type(e).__name__}: {e}")
        time.sleep(args.sleep)


if __name__ == "__main__":
    raise SystemExit(main())
