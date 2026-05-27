#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Profit-first wallet ranker for Wallet Alpha Radar.

No wallet, no signing, no orders.

This module is the FIRST gate for copy trading.
A wallet is NOT a follow target just because one trade is large or copyable.
A wallet must first prove it can make money.

Important design rule:
- If public data does not contain enough information to prove realized/settled PnL,
  the wallet is classified as UNKNOWN_PROFIT, not profitable.
- UNKNOWN_PROFIT can be explored separately, but must not be routed as verified follow.

Inputs:
- full_market_live_trades.sqlite: find active wallets today, if no wallet file supplied.
- auto_actions.csv: prioritize wallets currently generating signals.
- optional wallet_trade_history.csv: reuse existing history when API fetch is unavailable.

Outputs:
- wallet_profit_ranker.csv: wallet-level profit certification
- wallet_profit_history_raw.csv: normalized raw history for audit
- wallet_profit_errors.csv: API/parse errors
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

DATA_API = "https://data-api.polymarket.com/trades"
CLOB_API = "https://clob.polymarket.com/trades"

WALLET_KEYS = ["proxyWallet", "proxy_wallet", "wallet", "maker", "taker", "user", "address", "trader"]
TIME_KEYS = ["timestamp", "createdAt", "created_at", "time"]
MARKET_KEYS = ["conditionId", "condition_id", "market", "marketId", "market_id", "slug"]
TOKEN_KEYS = ["asset", "tokenId", "token_id", "tokenID", "outcomeTokenId", "outcome_token_id", "clobTokenId", "clob_token_id"]
PRICE_KEYS = ["price", "avgPrice", "avg_price", "outcomePrice"]
SIZE_KEYS = ["size", "amount", "shares", "matchedAmount"]
OUTCOME_KEYS = ["outcome", "outcomeName", "name", "title"]
TRADE_SIDE_KEYS = ["side", "tradeSide", "trade_side", "type", "action"]
CATEGORY_KEYS = ["category", "tag", "eventCategory"]
TX_KEYS = ["transactionHash", "transaction_hash", "txHash", "hash", "id"]

# Direct profit fields that may appear in some public/indexer payloads.
PNL_KEYS = [
    "realizedPnl", "realized_pnl", "realizedProfit", "realized_profit",
    "pnl", "profit", "profitLoss", "profit_loss", "netPnl", "net_pnl",
]
ROI_KEYS = ["roi", "return", "profitRate", "profit_rate"]


def first_val(obj: Dict[str, Any], keys: Iterable[str], default: Any = "") -> Any:
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    return default


def fnum(x: Any, default: float = 0.0) -> float:
    try:
        if x in (None, ""):
            return default
        return float(x)
    except Exception:
        return default


def norm_wallet(value: Any) -> str:
    if isinstance(value, dict):
        for k in WALLET_KEYS:
            if value.get(k):
                return norm_wallet(value.get(k))
    s = str(value or "").strip().lower()
    return s if s.startswith("0x") else ""


def find_wallet(obj: Dict[str, Any], fallback: str = "") -> str:
    w = norm_wallet(first_val(obj, WALLET_KEYS))
    if w:
        return w
    for v in obj.values():
        w = norm_wallet(v)
        if w:
            return w
    return fallback.lower()


def find_token_id(obj: Dict[str, Any]) -> str:
    for k in TOKEN_KEYS:
        v = str(obj.get(k) or "").strip()
        if v.isdigit() and len(v) >= 10:
            return v
    return ""


def parse_day(ts: Any) -> str:
    s = str(ts or "").strip()
    if not s:
        return ""
    try:
        if s.isdigit():
            val = int(s)
            if val > 10_000_000_000:
                val //= 1000
            return datetime.fromtimestamp(val, tz=timezone.utc).date().isoformat()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).date().isoformat()
    except Exception:
        return ""


def normalize_rows(payload: Any) -> List[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ["data", "trades", "results", "items"]:
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    return []


def fetch_wallet_trades(wallet: str, limit: int) -> tuple[List[dict], str, str]:
    param_sets = [
        {"user": wallet, "limit": limit},
        {"wallet": wallet, "limit": limit},
        {"proxyWallet": wallet, "limit": limit},
        {"address": wallet, "limit": limit},
    ]
    errors = []
    for url in [DATA_API, CLOB_API]:
        for params in param_sets:
            try:
                r = requests.get(url, params=params, timeout=20)
                r.raise_for_status()
                rows = normalize_rows(r.json())
                if rows:
                    return rows, f"{url}?{list(params.keys())[0]}", ""
            except Exception as e:
                errors.append(f"{url} {params}: {e}")
    return [], "", "; ".join(errors[-4:])


def read_csv(path: str) -> List[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: str, rows: List[dict], fields: List[str]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def wallet_candidates_from_actions(path: str) -> List[str]:
    rows = read_csv(path)
    out = []
    for r in rows:
        w = str(r.get("wallet") or "").lower().strip()
        if w.startswith("0x"):
            out.append(w)
    return out


def wallet_candidates_from_live_db(path: str, lookback_hours: float, limit: int) -> List[str]:
    p = Path(path)
    if not p.exists():
        return []
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(lookback_hours * 3600 * 1000)
    conn = sqlite3.connect(p)
    rows = conn.execute(
        """
        SELECT wallet, COUNT(*) AS n, SUM(COALESCE(notional,0)) AS total_notional
        FROM wallet_live_trades
        WHERE seen_ts_ms >= ? AND wallet IS NOT NULL AND wallet != ''
        GROUP BY wallet
        ORDER BY total_notional DESC, n DESC
        LIMIT ?
        """,
        (start_ms, limit),
    ).fetchall()
    conn.close()
    return [str(r[0]).lower() for r in rows if str(r[0]).startswith("0x")]


def normalize_trade(raw: dict, wallet: str, source: str) -> dict:
    price = fnum(first_val(raw, PRICE_KEYS))
    size = fnum(first_val(raw, SIZE_KEYS))
    pnl_val = None
    pnl_key = ""
    for k in PNL_KEYS:
        if k in raw and raw[k] not in (None, ""):
            pnl_val = fnum(raw[k])
            pnl_key = k
            break
    roi_val = None
    for k in ROI_KEYS:
        if k in raw and raw[k] not in (None, ""):
            roi_val = fnum(raw[k])
            break
    trade_side = str(first_val(raw, TRADE_SIDE_KEYS)).upper()
    # If the side field is actually YES/NO outcome, it is not a buy/sell side.
    if trade_side in {"YES", "NO"} or "YES" in trade_side or "NO" in trade_side:
        trade_side_norm = "UNKNOWN"
    elif "BUY" in trade_side:
        trade_side_norm = "BUY"
    elif "SELL" in trade_side:
        trade_side_norm = "SELL"
    else:
        trade_side_norm = "UNKNOWN"
    return {
        "wallet": find_wallet(raw, wallet),
        "timestamp": str(first_val(raw, TIME_KEYS)),
        "day": parse_day(first_val(raw, TIME_KEYS)),
        "market_id": str(first_val(raw, MARKET_KEYS)),
        "token_id": find_token_id(raw),
        "category": str(first_val(raw, CATEGORY_KEYS, "UNKNOWN")),
        "outcome": str(first_val(raw, OUTCOME_KEYS)),
        "trade_side": trade_side_norm,
        "raw_side": str(first_val(raw, TRADE_SIDE_KEYS)),
        "price": f"{price:.8f}",
        "size": f"{size:.8f}",
        "notional": f"{price * size:.8f}",
        "direct_pnl": "" if pnl_val is None else f"{pnl_val:.8f}",
        "direct_pnl_key": pnl_key,
        "direct_roi": "" if roi_val is None else f"{roi_val:.8f}",
        "source": source,
        "raw_keys": ",".join(sorted(raw.keys())[:80]),
    }


def calc_drawdown(daily_pnl: Dict[str, float]) -> float:
    if not daily_pnl:
        return 0.0
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for d in sorted(daily_pnl):
        equity += daily_pnl[d]
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return max_dd


def rank_wallet(wallet: str, rows: List[dict], args) -> dict:
    n = len(rows)
    notionals = [fnum(r.get("notional")) for r in rows]
    prices = [fnum(r.get("price")) for r in rows if fnum(r.get("price")) > 0]
    days = {r.get("day") for r in rows if r.get("day")}
    markets = [str(r.get("market_id") or "") for r in rows if r.get("market_id")]
    market_counts = Counter(markets)
    cats = [str(r.get("category") or "UNKNOWN") for r in rows]
    cat_counts = Counter(cats)
    pnl_rows = [r for r in rows if r.get("direct_pnl") not in (None, "")]
    pnl_values = [fnum(r.get("direct_pnl")) for r in pnl_rows]

    high90 = sum(1 for p in prices if p >= 0.90) / len(prices) if prices else 1.0
    mid_entry = sum(1 for p in prices if 0.35 <= p <= 0.65) / len(prices) if prices else 0.0
    low25 = sum(1 for p in prices if 0 < p < 0.25) / len(prices) if prices else 0.0
    top_market_share = market_counts.most_common(1)[0][1] / n if n and market_counts else 1.0
    top_cat = cat_counts.most_common(1)[0][0] if cat_counts else "UNKNOWN"
    cat_share = cat_counts.most_common(1)[0][1] / n if n and cat_counts else 0.0

    # Hedge approximation from both YES/NO outcomes inside same market.
    by_market_outcomes: Dict[str, set] = defaultdict(set)
    for r in rows:
        m = str(r.get("market_id") or "")
        o = str(r.get("outcome") or r.get("raw_side") or "").upper()
        if m and o:
            if "YES" in o:
                by_market_outcomes[m].add("YES")
            elif "NO" in o:
                by_market_outcomes[m].add("NO")
    hedge_markets = sum(1 for ss in by_market_outcomes.values() if {"YES", "NO"}.issubset(ss))
    hedge_ratio = hedge_markets / len(by_market_outcomes) if by_market_outcomes else 0.0

    total_pnl = sum(pnl_values)
    win_rate = sum(1 for x in pnl_values if x > 0) / len(pnl_values) if pnl_values else 0.0
    gross_win = sum(x for x in pnl_values if x > 0)
    gross_loss = -sum(x for x in pnl_values if x < 0)
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
    total_notional = sum(notionals)
    roi = total_pnl / total_notional if total_notional > 0 and pnl_values else 0.0
    daily_pnl = defaultdict(float)
    for r in pnl_rows:
        d = r.get("day") or ""
        if d:
            daily_pnl[d] += fnum(r.get("direct_pnl"))
    max_dd = calc_drawdown(daily_pnl)
    pos_days = sum(1 for v in daily_pnl.values() if v > 0)
    pos_day_ratio = pos_days / len(daily_pnl) if daily_pnl else 0.0
    largest_pnl = max([x for x in pnl_values if x > 0] or [0.0])
    profit_concentration = largest_pnl / gross_win if gross_win > 0 else 1.0

    # Certification is intentionally strict. No direct/settled PnL => unknown, not profitable.
    if len(pnl_values) < args.min_pnl_rows:
        tier = "UNKNOWN_PROFIT"
        reason = "insufficient_direct_pnl_rows"
    elif total_pnl <= 0:
        tier = "REJECT_NOT_PROFITABLE"
        reason = "total_pnl_non_positive"
    elif roi <= args.min_roi:
        tier = "REJECT_LOW_ROI"
        reason = "roi_below_min"
    elif win_rate < args.min_win_rate and profit_factor < args.min_profit_factor:
        tier = "REJECT_WEAK_EDGE"
        reason = "win_rate_and_profit_factor_weak"
    elif high90 > args.max_high90:
        tier = "REJECT_HIGH90"
        reason = "too_many_high90_entries"
    elif hedge_ratio > args.max_hedge:
        tier = "REJECT_HEDGE"
        reason = "hedge_ratio_too_high"
    elif profit_concentration > args.max_profit_concentration:
        tier = "REJECT_CONCENTRATED_PROFIT"
        reason = "profit_concentration_too_high"
    elif len(pnl_values) >= args.verified_min_pnl_rows and len(days) >= args.verified_min_days and profit_factor >= args.verified_min_profit_factor and total_pnl > 0:
        tier = "VERIFIED_PROFIT_WALLET"
        reason = "profit_gate_passed"
    else:
        tier = "RECENT_HOT_PROFIT_WALLET"
        reason = "profit_positive_but_less_history"

    return {
        "wallet": wallet,
        "profit_tier": tier,
        "profit_reason": reason,
        "history_rows": n,
        "pnl_rows": len(pnl_values),
        "active_days": len(days),
        "markets": len(set(markets)),
        "total_notional": f"{total_notional:.8f}",
        "direct_pnl_total": f"{total_pnl:.8f}",
        "roi": f"{roi:.8f}",
        "win_rate": f"{win_rate:.8f}",
        "profit_factor": f"{profit_factor:.8f}",
        "max_drawdown_pnl": f"{max_dd:.8f}",
        "positive_day_ratio": f"{pos_day_ratio:.8f}",
        "profit_concentration": f"{profit_concentration:.8f}",
        "high90_ratio": f"{high90:.8f}",
        "mid_entry_ratio": f"{mid_entry:.8f}",
        "low25_ratio": f"{low25:.8f}",
        "hedge_ratio": f"{hedge_ratio:.8f}",
        "top_market_share": f"{top_market_share:.8f}",
        "top_category": top_cat,
        "top_category_share": f"{cat_share:.8f}",
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--actions", default="paper_logs/wallet_alpha/auto_actions.csv")
    p.add_argument("--live-db", default="paper_logs/wallet_alpha/full_market_live_trades.sqlite")
    p.add_argument("--existing-history", default="paper_logs/wallet_alpha/wallet_trade_history.csv")
    p.add_argument("--output", default="paper_logs/wallet_alpha/wallet_profit_ranker.csv")
    p.add_argument("--raw-output", default="paper_logs/wallet_alpha/wallet_profit_history_raw.csv")
    p.add_argument("--errors-output", default="paper_logs/wallet_alpha/wallet_profit_errors.csv")
    p.add_argument("--max-wallets", type=int, default=500)
    p.add_argument("--limit-per-wallet", type=int, default=500)
    p.add_argument("--live-lookback-hours", type=float, default=24.0)
    p.add_argument("--min-pnl-rows", type=int, default=20)
    p.add_argument("--verified-min-pnl-rows", type=int, default=100)
    p.add_argument("--verified-min-days", type=int, default=20)
    p.add_argument("--min-roi", type=float, default=0.02)
    p.add_argument("--min-win-rate", type=float, default=0.52)
    p.add_argument("--min-profit-factor", type=float, default=1.10)
    p.add_argument("--verified-min-profit-factor", type=float, default=1.20)
    p.add_argument("--max-high90", type=float, default=0.40)
    p.add_argument("--max-hedge", type=float, default=0.35)
    p.add_argument("--max-profit-concentration", type=float, default=0.40)
    args = p.parse_args()

    action_wallets = wallet_candidates_from_actions(args.actions)
    live_wallets = wallet_candidates_from_live_db(args.live_db, args.live_lookback_hours, args.max_wallets)
    wallets = []
    seen = set()
    for w in action_wallets + live_wallets:
        if w and w not in seen:
            wallets.append(w)
            seen.add(w)
        if len(wallets) >= args.max_wallets:
            break

    raw_out: List[dict] = []
    errors: List[dict] = []
    by_wallet: Dict[str, List[dict]] = defaultdict(list)

    for i, wallet in enumerate(wallets, start=1):
        rows, source, err = fetch_wallet_trades(wallet, args.limit_per_wallet)
        if not rows:
            errors.append({"wallet": wallet, "error": err or "no_rows"})
            continue
        for raw in rows:
            rec = normalize_trade(raw, wallet, source)
            raw_out.append(rec)
            by_wallet[wallet].append(rec)
        if i % 25 == 0:
            print(f"WALLET_PROFIT_FETCH_PROGRESS wallets_done={i} raw_rows={len(raw_out)} errors={len(errors)}")
        time.sleep(0.03)

    ranks = [rank_wallet(w, rows, args) for w, rows in by_wallet.items()]
    ranks.sort(key=lambda r: (
        1 if r["profit_tier"] == "VERIFIED_PROFIT_WALLET" else 0,
        1 if r["profit_tier"] == "RECENT_HOT_PROFIT_WALLET" else 0,
        fnum(r.get("direct_pnl_total")),
        fnum(r.get("profit_factor")),
        fnum(r.get("win_rate")),
    ), reverse=True)

    rank_fields = [
        "wallet", "profit_tier", "profit_reason", "history_rows", "pnl_rows", "active_days", "markets",
        "total_notional", "direct_pnl_total", "roi", "win_rate", "profit_factor", "max_drawdown_pnl",
        "positive_day_ratio", "profit_concentration", "high90_ratio", "mid_entry_ratio", "low25_ratio",
        "hedge_ratio", "top_market_share", "top_category", "top_category_share"
    ]
    raw_fields = [
        "wallet", "timestamp", "day", "market_id", "token_id", "category", "outcome", "trade_side",
        "raw_side", "price", "size", "notional", "direct_pnl", "direct_pnl_key", "direct_roi", "source", "raw_keys"
    ]
    write_csv(args.output, ranks, rank_fields)
    write_csv(args.raw_output, raw_out, raw_fields)
    write_csv(args.errors_output, errors, ["wallet", "error"])

    counts = Counter(r["profit_tier"] for r in ranks)
    print(
        f"WALLET_PROFIT_RANKER_SUMMARY wallets_requested={len(wallets)} wallets_ranked={len(ranks)} raw_rows={len(raw_out)} errors={len(errors)} "
        + " ".join(f"{k}={v}" for k, v in counts.items())
        + f" output={args.output}"
    )
    for r in ranks[:80]:
        print(
            f"WALLET_PROFIT wallet={r['wallet'][:10]} tier={r['profit_tier']} pnl_rows={r['pnl_rows']} pnl={r['direct_pnl_total']} "
            f"roi={r['roi']} wr={r['win_rate']} pf={r['profit_factor']} high90={r['high90_ratio']} reason={r['profit_reason']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
