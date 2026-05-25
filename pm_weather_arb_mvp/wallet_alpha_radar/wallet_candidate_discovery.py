#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Discover candidate Polymarket wallets from recent public trades.

Phase 1 data tool. No wallet, no signing, no orders.

The public Polymarket API field names can drift. This script is deliberately
permissive and writes diagnostics so we can quickly see whether wallet fields are
available in the current API payload.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests


CLOB_TRADES_ENDPOINTS = [
    "https://clob.polymarket.com/trades",
    "https://data-api.polymarket.com/trades",
]

WALLET_KEYS = ["proxyWallet", "proxy_wallet", "wallet", "maker", "taker", "user", "address", "trader"]
MARKET_KEYS = ["conditionId", "condition_id", "market", "marketId", "market_id", "slug"]
PRICE_KEYS = ["price", "avgPrice", "avg_price", "outcomePrice"]
SIZE_KEYS = ["size", "amount", "shares", "matchedAmount"]
SIDE_KEYS = ["side", "outcome", "outcomeName", "asset"]
TIME_KEYS = ["timestamp", "createdAt", "created_at", "time"]


def first_present(obj: Dict[str, Any], keys: Iterable[str], default: Any = "") -> Any:
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    return default


def norm_wallet(value: Any) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    if s.startswith("0x") and len(s) >= 10:
        return s.lower()
    # Some APIs return user objects.
    try:
        if isinstance(value, dict):
            for k in WALLET_KEYS:
                if value.get(k):
                    return norm_wallet(value.get(k))
    except Exception:
        pass
    return s.lower()


def fetch_json(url: str, params: Dict[str, Any], timeout: float = 15.0) -> Any:
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def normalize_rows(payload: Any) -> List[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ["data", "trades", "results", "items"]:
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    return []


def fetch_recent_trades(limit: int, offset: int = 0) -> tuple[List[dict], str, Optional[str]]:
    errors = []
    for url in CLOB_TRADES_ENDPOINTS:
        for params in [
            {"limit": limit, "offset": offset},
            {"limit": limit},
        ]:
            try:
                payload = fetch_json(url, params)
                rows = normalize_rows(payload)
                if rows:
                    return rows, url, None
            except Exception as e:
                errors.append(f"{url} {params}: {e}")
    return [], "", "; ".join(errors[-4:])


def to_float(x: Any, default: float = 0.0) -> float:
    try:
        if x in (None, ""):
            return default
        return float(x)
    except Exception:
        return default


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--output", default="paper_logs/wallet_alpha/candidate_wallets.csv")
    p.add_argument("--raw-output", default="paper_logs/wallet_alpha/recent_trades_raw_sample.json")
    p.add_argument("--min-trades", type=int, default=2)
    args = p.parse_args()

    rows, source_url, err = fetch_recent_trades(args.limit)
    Path(args.raw_output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.raw_output).write_text(json.dumps(rows[:20], ensure_ascii=False, indent=2), encoding="utf-8")

    if not rows:
        print(f"WALLET_DISCOVERY_SUMMARY rows=0 wallets=0 source=none error={err!r}")
        return 0

    wallets: Dict[str, dict] = {}
    field_counter = Counter()
    missing_wallet = 0
    for r in rows:
        field_counter.update(r.keys())
        wallet = norm_wallet(first_present(r, WALLET_KEYS))
        if not wallet:
            # try nested values.
            for v in r.values():
                wallet = norm_wallet(v)
                if wallet.startswith("0x"):
                    break
        if not wallet or not wallet.startswith("0x"):
            missing_wallet += 1
            continue
        rec = wallets.setdefault(wallet, {
            "wallet": wallet,
            "source": source_url,
            "recent_trade_count": 0,
            "notional_hint": 0.0,
            "markets_hint": set(),
            "first_seen_ts": "",
            "last_seen_ts": "",
        })
        rec["recent_trade_count"] += 1
        price = to_float(first_present(r, PRICE_KEYS))
        size = to_float(first_present(r, SIZE_KEYS))
        rec["notional_hint"] += price * size
        market = str(first_present(r, MARKET_KEYS, ""))
        if market:
            rec["markets_hint"].add(market)
        ts = str(first_present(r, TIME_KEYS, ""))
        if ts:
            if not rec["first_seen_ts"]:
                rec["first_seen_ts"] = ts
            rec["last_seen_ts"] = ts

    out = []
    for rec in wallets.values():
        if rec["recent_trade_count"] < args.min_trades:
            continue
        out.append({
            "wallet": rec["wallet"],
            "source": rec["source"],
            "recent_trade_count": rec["recent_trade_count"],
            "notional_hint": f"{rec['notional_hint']:.4f}",
            "market_count_hint": len(rec["markets_hint"]),
            "first_seen_ts": rec["first_seen_ts"],
            "last_seen_ts": rec["last_seen_ts"],
            "tier_seed": "UNSCORED",
        })
    out.sort(key=lambda x: (int(x["recent_trade_count"]), float(x["notional_hint"])), reverse=True)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        fields = ["wallet", "source", "recent_trade_count", "notional_hint", "market_count_hint", "first_seen_ts", "last_seen_ts", "tier_seed"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out)

    top_fields = ",".join([f"{k}:{v}" for k, v in field_counter.most_common(12)])
    print(
        f"WALLET_DISCOVERY_SUMMARY rows={len(rows)} wallets_raw={len(wallets)} wallets_output={len(out)} "
        f"missing_wallet={missing_wallet} source={source_url} top_fields={top_fields} output={args.output}"
    )
    for x in out[:20]:
        print(
            f"WALLET_CANDIDATE wallet={x['wallet']} trades={x['recent_trade_count']} "
            f"notional_hint={x['notional_hint']} markets={x['market_count_hint']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
