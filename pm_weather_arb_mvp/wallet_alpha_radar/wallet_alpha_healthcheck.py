#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Healthcheck for Wallet Alpha Radar data recorders.

No wallet, no signing, no orders.

Checks:
- orderbook_snapshots.sqlite row growth and recency
- wallet_live_trades.sqlite row growth and recency
- category distribution of live wallet trades
- active watch wallets and markets

This is a monitoring tool only.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


def safe_query(conn: sqlite3.Connection, sql: str, default: Any = None) -> Any:
    try:
        return conn.execute(sql).fetchone()[0]
    except Exception:
        return default


def safe_fetchall(conn: sqlite3.Connection, sql: str) -> list:
    try:
        return conn.execute(sql).fetchall()
    except Exception:
        return []


def fmt_age(ts_ms: Optional[int]) -> str:
    if not ts_ms:
        return "na"
    age = int(time.time() - ts_ms / 1000)
    return f"{age}s"


def check_orderbook(path: Path, stale_sec: int) -> dict:
    if not path.exists():
        return {"exists": False, "status": "MISSING"}
    conn = sqlite3.connect(path)
    try:
        rows = safe_query(conn, "select count(*) from orderbook_snapshots", 0)
        markets = safe_query(conn, "select count(distinct market_id) from orderbook_snapshots", 0)
        tokens = safe_query(conn, "select count(distinct token_id) from orderbook_snapshots", 0)
        min_ts = safe_query(conn, "select min(ts_ms) from orderbook_snapshots", None)
        max_ts = safe_query(conn, "select max(ts_ms) from orderbook_snapshots", None)
        last_hour = safe_query(conn, "select count(*) from orderbook_snapshots where ts_ms >= (select max(ts_ms)-3600000 from orderbook_snapshots)", 0)
    finally:
        conn.close()
    age = int(time.time() - max_ts / 1000) if max_ts else 10**9
    status = "OK" if rows > 0 and age <= stale_sec else "STALE"
    return {
        "exists": True,
        "status": status,
        "size": path.stat().st_size,
        "rows": rows,
        "markets": markets,
        "tokens": tokens,
        "min_ts": min_ts,
        "max_ts": max_ts,
        "age": age,
        "age_text": fmt_age(max_ts),
        "last_hour_rows": last_hour,
    }


def check_live_trades(path: Path, stale_sec: int) -> dict:
    if not path.exists():
        return {"exists": False, "status": "MISSING"}
    conn = sqlite3.connect(path)
    try:
        rows = safe_query(conn, "select count(*) from wallet_live_trades", 0)
        wallets = safe_query(conn, "select count(distinct wallet) from wallet_live_trades", 0)
        markets = safe_query(conn, "select count(distinct market_key) from wallet_live_trades", 0)
        min_ts = safe_query(conn, "select min(seen_ts_ms) from wallet_live_trades", None)
        max_ts = safe_query(conn, "select max(seen_ts_ms) from wallet_live_trades", None)
        last_hour = safe_query(conn, "select count(*) from wallet_live_trades where seen_ts_ms >= (select max(seen_ts_ms)-3600000 from wallet_live_trades)", 0)
        cats = safe_fetchall(conn, "select category_enriched, count(*) from wallet_live_trades group by 1 order by 2 desc limit 10")
        gates = safe_fetchall(conn, "select wallet_gate, count(*) from wallet_live_trades group by 1 order by 2 desc")
    finally:
        conn.close()
    age = int(time.time() - max_ts / 1000) if max_ts else 10**9
    status = "OK" if rows > 0 and age <= stale_sec else "STALE"
    return {
        "exists": True,
        "status": status,
        "size": path.stat().st_size,
        "rows": rows,
        "wallets": wallets,
        "markets": markets,
        "min_ts": min_ts,
        "max_ts": max_ts,
        "age": age,
        "age_text": fmt_age(max_ts),
        "last_hour_rows": last_hour,
        "cats": cats,
        "gates": gates,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--orderbook-db", default="paper_logs/wallet_alpha/orderbook_snapshots.sqlite")
    p.add_argument("--live-db", default="paper_logs/wallet_alpha/wallet_live_trades.sqlite")
    p.add_argument("--stale-sec", type=int, default=300)
    args = p.parse_args()

    ob = check_orderbook(Path(args.orderbook_db), args.stale_sec)
    lt = check_live_trades(Path(args.live_db), args.stale_sec)

    overall = "GREEN" if ob.get("status") == "OK" and lt.get("status") == "OK" else "ORANGE"
    if ob.get("status") == "MISSING" or lt.get("status") == "MISSING":
        overall = "RED"

    print(f"WALLET_ALPHA_HEALTH overall={overall}")
    print(
        "ORDERBOOK_HEALTH "
        f"status={ob.get('status')} rows={ob.get('rows',0)} markets={ob.get('markets',0)} tokens={ob.get('tokens',0)} "
        f"last_age={ob.get('age_text','na')} last_hour_rows={ob.get('last_hour_rows',0)} size={ob.get('size',0)}"
    )
    print(
        "LIVE_TRADE_HEALTH "
        f"status={lt.get('status')} rows={lt.get('rows',0)} wallets={lt.get('wallets',0)} markets={lt.get('markets',0)} "
        f"last_age={lt.get('age_text','na')} last_hour_rows={lt.get('last_hour_rows',0)} size={lt.get('size',0)}"
    )
    print("LIVE_TRADE_CATS " + ",".join(f"{k}:{v}" for k, v in lt.get("cats", [])))
    print("LIVE_TRADE_GATES " + ",".join(f"{k}:{v}" for k, v in lt.get("gates", [])))
    return 0 if overall in {"GREEN", "ORANGE"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
