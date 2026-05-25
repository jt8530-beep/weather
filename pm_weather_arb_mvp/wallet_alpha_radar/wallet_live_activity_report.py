#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live activity profiler for Wallet Alpha Radar.

No wallet, no signing, no orders.

This script summarizes future live trades captured by wallet_live_trade_recorder.
It is used to separate genuinely active signal wallets from high-frequency
noise / market-making / broad-market wallets.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple


def fnum(x, default=0.0) -> float:
    try:
        if x in (None, ""):
            return default
        return float(x)
    except Exception:
        return default


def bucket_price(p: float) -> str:
    if p <= 0:
        return "bad"
    if p < 0.10:
        return "p_lt_010"
    if p < 0.25:
        return "p_010_025"
    if p < 0.35:
        return "p_025_035"
    if p <= 0.65:
        return "p_035_065"
    if p <= 0.80:
        return "p_065_080"
    if p < 0.90:
        return "p_080_090"
    return "p_ge_090"


def write_csv(path: Path, rows: List[dict], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="paper_logs/wallet_alpha/wallet_live_trades.sqlite")
    p.add_argument("--output", default="paper_logs/wallet_alpha/wallet_live_activity_report.csv")
    p.add_argument("--top", type=int, default=50)
    args = p.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = list(con.execute("select * from wallet_live_trades"))
    con.close()

    by_wallet: Dict[str, list] = defaultdict(list)
    for r in rows:
        by_wallet[str(r["wallet"]).lower()].append(r)

    out: List[dict] = []
    for wallet, wrs in by_wallet.items():
        prices = [fnum(r["price"]) for r in wrs]
        notionals = [fnum(r["notional"]) for r in wrs]
        markets = [str(r["market_key"] or "") for r in wrs]
        cats = [str(r["category_enriched"] or "unknown") for r in wrs]
        gates = [str(r["wallet_gate"] or "") for r in wrs]
        p_buckets = Counter(bucket_price(x) for x in prices)
        m_counts = Counter(markets)
        cat_counts = Counter(cats)

        trades = len(wrs)
        uniq_markets = len(set(markets))
        total_notional = sum(notionals)
        avg_notional = total_notional / trades if trades else 0.0
        trades_per_market = trades / uniq_markets if uniq_markets else 0.0
        top_market_share = m_counts.most_common(1)[0][1] / trades if m_counts and trades else 1.0
        top_cat = cat_counts.most_common(1)[0][0] if cat_counts else "unknown"
        top_cat_share = cat_counts.most_common(1)[0][1] / trades if cat_counts and trades else 1.0
        mid_ratio = p_buckets["p_035_065"] / trades if trades else 0.0
        high90_ratio = p_buckets["p_ge_090"] / trades if trades else 0.0
        low25_ratio = (p_buckets["p_lt_010"] + p_buckets["p_010_025"]) / trades if trades else 0.0

        # Heuristic flags. These are not rejections; they tell us how to inspect.
        flags = []
        if trades >= 300 and uniq_markets >= 75:
            flags.append("broad_high_freq")
        if trades_per_market >= 5 and top_market_share < 0.20:
            flags.append("repeat_many_markets")
        if high90_ratio >= 0.40:
            flags.append("many_high90_entries")
        if low25_ratio >= 0.50:
            flags.append("many_longshot_entries")
        if top_cat == "unknown" and top_cat_share >= 0.80:
            flags.append("category_unknown")

        # Live priority is about future data value, not copy-trade approval.
        priority = 0
        if trades >= 50:
            priority += 20
        if uniq_markets >= 20:
            priority += 15
        if mid_ratio >= 0.25:
            priority += 15
        if high90_ratio < 0.30:
            priority += 10
        if top_market_share < 0.35:
            priority += 10
        if top_cat != "unknown":
            priority += 15
        if "broad_high_freq" in flags:
            priority -= 10
        if priority >= 60:
            tier = "LIVE_A_RESEARCH"
        elif priority >= 40:
            tier = "LIVE_B_RESEARCH"
        else:
            tier = "LIVE_C_LOW_PRIORITY"

        out.append({
            "wallet": wallet,
            "wallet_gate": Counter(gates).most_common(1)[0][0] if gates else "",
            "live_trade_count": trades,
            "unique_markets": uniq_markets,
            "total_notional": f"{total_notional:.6f}",
            "avg_notional": f"{avg_notional:.6f}",
            "trades_per_market": f"{trades_per_market:.6f}",
            "top_market_share": f"{top_market_share:.6f}",
            "top_category": top_cat,
            "top_category_share": f"{top_cat_share:.6f}",
            "mid_entry_35_65_ratio": f"{mid_ratio:.6f}",
            "high_entry_90_ratio": f"{high90_ratio:.6f}",
            "low_entry_lt25_ratio": f"{low25_ratio:.6f}",
            "live_priority_score": priority,
            "live_priority_tier": tier,
            "flags": "|".join(flags),
        })

    out.sort(key=lambda r: (int(r["live_priority_score"]), int(r["live_trade_count"]), float(r["total_notional"])), reverse=True)
    fields = [
        "wallet", "wallet_gate", "live_trade_count", "unique_markets", "total_notional", "avg_notional",
        "trades_per_market", "top_market_share", "top_category", "top_category_share",
        "mid_entry_35_65_ratio", "high_entry_90_ratio", "low_entry_lt25_ratio",
        "live_priority_score", "live_priority_tier", "flags",
    ]
    write_csv(Path(args.output), out, fields)

    tier_counts = Counter(r["live_priority_tier"] for r in out)
    gate_counts = Counter(r["wallet_gate"] for r in out)
    print(
        f"WALLET_LIVE_ACTIVITY_SUMMARY rows={len(rows)} wallets={len(out)} "
        f"live_A={tier_counts.get('LIVE_A_RESEARCH',0)} live_B={tier_counts.get('LIVE_B_RESEARCH',0)} "
        f"live_C={tier_counts.get('LIVE_C_LOW_PRIORITY',0)} gates=" + ",".join(f"{k}:{v}" for k, v in gate_counts.items())
    )
    for r in out[: args.top]:
        print(
            f"WALLET_LIVE_ACTIVITY wallet={r['wallet']} gate={r['wallet_gate']} tier={r['live_priority_tier']} "
            f"score={r['live_priority_score']} trades={r['live_trade_count']} markets={r['unique_markets']} "
            f"notional={r['total_notional']} mid={r['mid_entry_35_65_ratio']} high90={r['high_entry_90_ratio']} "
            f"top_cat={r['top_category']} flags={r['flags']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
