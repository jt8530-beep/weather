#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build best-effort trade history for candidate Polymarket wallets.

Phase 1 data tool. No wallet, no signing, no orders.

This uses public APIs only. Polymarket API shapes can drift; if wallet-specific
endpoints are unavailable, the script still emits diagnostics rather than
faking results.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests


DATA_API = "https://data-api.polymarket.com/trades"
CLOB_API = "https://clob.polymarket.com/trades"

PRICE_KEYS = ["price", "avgPrice", "avg_price", "outcomePrice"]
SIZE_KEYS = ["size", "amount", "shares", "matchedAmount"]
SIDE_KEYS = ["side", "outcome", "outcomeName", "asset"]
MARKET_KEYS = ["conditionId", "condition_id", "market", "marketId", "market_id", "slug"]
CATEGORY_KEYS = ["category", "tag", "eventCategory"]
TIME_KEYS = ["timestamp", "createdAt", "created_at", "time"]


def first_present(obj: Dict[str, Any], keys: Iterable[str], default: Any = "") -> Any:
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    return default


def read_candidates(path: Path, top_n: int) -> List[str]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    wallets = [str(r.get("wallet") or "").strip().lower() for r in rows if r.get("wallet")]
    return wallets[:top_n]


def normalize_rows(payload: Any) -> List[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ["data", "trades", "results", "items"]:
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    return []


def fetch_wallet_trades(wallet: str, limit: int) -> tuple[List[dict], str, Optional[str]]:
    attempts = []
    endpoints = [DATA_API, CLOB_API]
    param_sets = [
        {"user": wallet, "limit": limit},
        {"wallet": wallet, "limit": limit},
        {"proxyWallet": wallet, "limit": limit},
        {"address": wallet, "limit": limit},
    ]
    for url in endpoints:
        for params in param_sets:
            try:
                r = requests.get(url, params=params, timeout=20)
                r.raise_for_status()
                rows = normalize_rows(r.json())
                if rows:
                    return rows, f"{url}?{list(params.keys())[0]}", None
            except Exception as e:
                attempts.append(f"{url} {params}: {e}")
    return [], "", "; ".join(attempts[-4:])


def fnum(x: Any, default: float = 0.0) -> float:
    try:
        if x in (None, ""):
            return default
        return float(x)
    except Exception:
        return default


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--candidates", default="paper_logs/wallet_alpha/candidate_wallets.csv")
    p.add_argument("--top-wallets", type=int, default=100)
    p.add_argument("--limit-per-wallet", type=int, default=500)
    p.add_argument("--output", default="paper_logs/wallet_alpha/wallet_trade_history.csv")
    p.add_argument("--errors-output", default="paper_logs/wallet_alpha/wallet_history_errors.csv")
    args = p.parse_args()

    wallets = read_candidates(Path(args.candidates), args.top_wallets)
    out: List[dict] = []
    errors: List[dict] = []

    for idx, wallet in enumerate(wallets, start=1):
        rows, source, err = fetch_wallet_trades(wallet, args.limit_per_wallet)
        if not rows:
            errors.append({"wallet": wallet, "error": err or "no_rows"})
            continue
        for r in rows:
            price = fnum(first_present(r, PRICE_KEYS))
            size = fnum(first_present(r, SIZE_KEYS))
            out.append({
                "wallet": wallet,
                "timestamp": str(first_present(r, TIME_KEYS)),
                "market_id": str(first_present(r, MARKET_KEYS)),
                "category": str(first_present(r, CATEGORY_KEYS)),
                "side": str(first_present(r, SIDE_KEYS)),
                "price": price,
                "size": size,
                "notional": price * size,
                "source": source,
                "raw_keys": ",".join(sorted(r.keys())[:30]),
            })
        if idx % 20 == 0:
            print(f"WALLET_HISTORY_PROGRESS wallets_done={idx} rows={len(out)} errors={len(errors)}")
        time.sleep(0.05)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    fields = ["wallet", "timestamp", "market_id", "category", "side", "price", "size", "notional", "source", "raw_keys"]
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out)

    with open(args.errors_output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["wallet", "error"])
        w.writeheader()
        w.writerows(errors)

    wallets_with_rows = len({r["wallet"] for r in out})
    print(
        f"WALLET_HISTORY_SUMMARY candidates={len(wallets)} wallets_with_rows={wallets_with_rows} "
        f"rows={len(out)} errors={len(errors)} output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
