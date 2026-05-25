#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill normalized Polymarket identifiers for wallet live trades.

Data-only utility. No wallet connection, no signing, no orders.

Problem fixed:
- wallet_live_trades.market_key may be a 0x condition id.
- orderbook_snapshots.market_id is a numeric Gamma market id.
- orderbook_snapshots.token_id is the outcome token id.
- Without a bridge, alpha-decay joins return usable=0.

This script adds/fills:
- condition_id
- market_id_numeric
- token_id

It uses raw_json plus active Gamma metadata. Existing rows are migrated in place.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from dotenv import load_dotenv

from pm_weather_arb.config import Config
from pm_weather_arb.gamma import GammaClient, parse_markets_from_events
from pm_weather_arb.util import first_present


CONDITION_KEYS = ["conditionId", "condition_id", "conditionID", "condition"]
MARKET_NUMERIC_KEYS = ["marketId", "market_id", "marketID", "market", "id"]
TOKEN_KEYS = ["asset", "tokenId", "token_id", "tokenID", "outcomeTokenId", "outcome_token_id", "clobTokenId", "clob_token_id"]
SIDE_KEYS = ["side", "outcome", "outcomeName"]


def ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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


def find_token(obj: Dict[str, Any]) -> str:
    for k in TOKEN_KEYS:
        v = str(obj.get(k) or "").strip()
        if v.isdigit() and len(v) >= 10:
            return v
    for v in obj.values():
        if isinstance(v, dict):
            t = find_token(v)
            if t:
                return t
    return ""


def norm_side(*vals: object) -> str:
    s = " ".join(str(v or "") for v in vals).upper()
    if "YES" in s:
        return "YES"
    if "NO" in s:
        return "NO"
    return ""


def build_gamma_index(pages: int, limit: int, order: str) -> dict:
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
    markets = parse_markets_from_events(events, only_weatherish=False)
    idx = {}
    for m in markets:
        if not m.yes_token or not m.no_token:
            continue
        rec = {
            "market_id_numeric": str(m.market_id or ""),
            "condition_id": str(m.condition_id or "").lower(),
            "market_slug": str(m.market_slug or ""),
            "yes_token_id": str(m.yes_token.token_id or ""),
            "no_token_id": str(m.no_token.token_id or ""),
        }
        for key in [rec["market_id_numeric"], rec["condition_id"], rec["market_slug"], rec["yes_token_id"], rec["no_token_id"]]:
            if key:
                idx[str(key).lower()] = rec
    return idx


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="paper_logs/wallet_alpha/wallet_live_trades.sqlite")
    p.add_argument("--gamma-pages", type=int, default=80)
    p.add_argument("--gamma-limit", type=int, default=100)
    p.add_argument("--gamma-order", default="volume_24hr")
    p.add_argument("--limit", type=int, default=200000)
    args = p.parse_args()

    load_dotenv()
    idx = build_gamma_index(args.gamma_pages, args.gamma_limit, args.gamma_order)
    conn = sqlite3.connect(args.db)
    try:
        ensure_column(conn, "wallet_live_trades", "condition_id", "TEXT")
        ensure_column(conn, "wallet_live_trades", "market_id_numeric", "TEXT")
        ensure_column(conn, "wallet_live_trades", "token_id", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wlt_condition_ts ON wallet_live_trades(condition_id, seen_ts_ms)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wlt_numeric_market_ts ON wallet_live_trades(market_id_numeric, seen_ts_ms)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wlt_token_ts ON wallet_live_trades(token_id, seen_ts_ms)")
        rows = conn.execute(
            """
            SELECT unique_id, market_key, side, raw_json
            FROM wallet_live_trades
            ORDER BY seen_ts_ms DESC
            LIMIT ?
            """,
            (args.limit,),
        ).fetchall()
        updated = 0
        token_filled = 0
        numeric_filled = 0
        condition_filled = 0
        for uid, market_key, side, raw_json in rows:
            try:
                raw = json.loads(raw_json or "{}")
                if not isinstance(raw, dict):
                    raw = {}
            except Exception:
                raw = {}
            mk = str(market_key or "").lower()
            condition_id = find_hex(raw, CONDITION_KEYS) or (mk if mk.startswith("0x") else "")
            numeric_id = find_numeric(raw, MARKET_NUMERIC_KEYS)
            token_id = find_token(raw)
            meta = idx.get(condition_id.lower()) or idx.get(numeric_id.lower()) or idx.get(token_id.lower()) or idx.get(mk)
            if meta:
                condition_id = condition_id or meta.get("condition_id", "")
                numeric_id = numeric_id or meta.get("market_id_numeric", "")
                if not token_id:
                    s = norm_side(side, *(raw.get(k) for k in SIDE_KEYS))
                    if s == "YES":
                        token_id = meta.get("yes_token_id", "")
                    elif s == "NO":
                        token_id = meta.get("no_token_id", "")
            conn.execute(
                """
                UPDATE wallet_live_trades
                SET condition_id=?, market_id_numeric=?, token_id=?
                WHERE unique_id=?
                """,
                (condition_id, numeric_id, token_id, uid),
            )
            updated += 1
            if token_id:
                token_filled += 1
            if numeric_id:
                numeric_filled += 1
            if condition_id:
                condition_filled += 1
        conn.commit()
    finally:
        conn.close()

    print(
        f"WALLET_KEY_BACKFILL_SUMMARY rows={len(rows)} updated={updated} gamma_index={len(idx)} "
        f"condition_filled={condition_filled} numeric_filled={numeric_filled} token_filled={token_filled} db={args.db}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
