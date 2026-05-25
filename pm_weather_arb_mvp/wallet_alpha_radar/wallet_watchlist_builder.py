#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build provisional wallet and market watchlists from Phase 1 scores/history.

No wallet, no signing, no orders.

This script is intentionally conservative: A_WATCH from wallet_score.py is only
PROVISIONAL until category enrichment and delayed follow testing exist.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Set


def fnum(x, default=0.0) -> float:
    try:
        if x in (None, ""):
            return default
        return float(x)
    except Exception:
        return default


def inum(x, default=0) -> int:
    try:
        if x in (None, ""):
            return default
        return int(float(x))
    except Exception:
        return default


def read_csv(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: List[dict], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--scores", default="paper_logs/wallet_alpha/wallet_scores.csv")
    p.add_argument("--history", default="paper_logs/wallet_alpha/wallet_trade_history.csv")
    p.add_argument("--wallet-output", default="paper_logs/wallet_alpha/wallet_watchlist.csv")
    p.add_argument("--market-output", default="paper_logs/wallet_alpha/market_watchlist.csv")
    p.add_argument("--top-wallets", type=int, default=50)
    p.add_argument("--max-high90", type=float, default=0.40)
    p.add_argument("--max-hedge", type=float, default=0.35)
    p.add_argument("--min-trades", type=int, default=30)
    args = p.parse_args()

    scores = read_csv(Path(args.scores))
    hist = read_csv(Path(args.history))
    hist_by_wallet: Dict[str, List[dict]] = defaultdict(list)
    for r in hist:
        w = str(r.get("wallet") or "").lower()
        if w:
            hist_by_wallet[w].append(r)

    wallet_rows: List[dict] = []
    for s in sorted(scores, key=lambda r: (inum(r.get("score")), inum(r.get("trade_count"))), reverse=True):
        w = str(s.get("wallet") or "").lower()
        if not w:
            continue
        trades = inum(s.get("trade_count"))
        high90 = fnum(s.get("high_entry_90_ratio"), 1.0)
        hedge = fnum(s.get("hedge_ratio_approx"), 1.0)
        score = inum(s.get("score"))
        if trades < args.min_trades:
            gate = "REJECT_LOW_TRADES"
        elif high90 > args.max_high90:
            gate = "REJECT_HIGH_90_ENTRY"
        elif hedge > args.max_hedge:
            gate = "REJECT_HIGH_HEDGE"
        elif score >= 75:
            gate = "PROVISIONAL_A"
        elif score >= 55:
            gate = "PROVISIONAL_B"
        else:
            gate = "REJECT_LOW_SCORE"
        wallet_rows.append({
            "wallet": w,
            "score": score,
            "tier_raw": s.get("tier", ""),
            "gate": gate,
            "trade_count": trades,
            "active_days": s.get("active_days", ""),
            "total_notional": s.get("total_notional", ""),
            "median_entry_price": s.get("median_entry_price", ""),
            "high_entry_90_ratio": s.get("high_entry_90_ratio", ""),
            "mid_entry_35_65_ratio": s.get("mid_entry_35_65_ratio", ""),
            "hedge_ratio_approx": s.get("hedge_ratio_approx", ""),
            "top_category": s.get("top_category", "UNKNOWN"),
        })
        if len([x for x in wallet_rows if x["gate"] in {"PROVISIONAL_A", "PROVISIONAL_B"}]) >= args.top_wallets:
            # Keep rejected rows around the top too for diagnostics, but avoid huge output.
            pass

    # Build market watchlist from PROVISIONAL A/B wallets only.
    good_wallets = {r["wallet"] for r in wallet_rows if r["gate"] in {"PROVISIONAL_A", "PROVISIONAL_B"}}
    market_stats: Dict[str, dict] = {}
    for r in hist:
        w = str(r.get("wallet") or "").lower()
        if w not in good_wallets:
            continue
        m = str(r.get("market_id") or "").strip()
        if not m:
            continue
        st = market_stats.setdefault(m, {
            "market_id": m,
            "wallet_count": set(),
            "trade_count": 0,
            "notional": 0.0,
            "sides": Counter(),
            "categories": Counter(),
        })
        st["wallet_count"].add(w)
        st["trade_count"] += 1
        st["notional"] += fnum(r.get("notional"))
        st["sides"].update([str(r.get("side") or "")[:32]])
        st["categories"].update([str(r.get("category") or "UNKNOWN")])

    market_rows = []
    for m, st in market_stats.items():
        market_rows.append({
            "market_id": m,
            "wallet_count": len(st["wallet_count"]),
            "trade_count": st["trade_count"],
            "notional": f"{st['notional']:.6f}",
            "top_side": st["sides"].most_common(1)[0][0] if st["sides"] else "",
            "top_category": st["categories"].most_common(1)[0][0] if st["categories"] else "UNKNOWN",
        })
    market_rows.sort(key=lambda x: (int(x["wallet_count"]), int(x["trade_count"]), float(x["notional"])), reverse=True)

    wallet_fields = ["wallet", "score", "tier_raw", "gate", "trade_count", "active_days", "total_notional", "median_entry_price", "high_entry_90_ratio", "mid_entry_35_65_ratio", "hedge_ratio_approx", "top_category"]
    market_fields = ["market_id", "wallet_count", "trade_count", "notional", "top_side", "top_category"]
    write_csv(Path(args.wallet_output), wallet_rows, wallet_fields)
    write_csv(Path(args.market_output), market_rows, market_fields)

    gate_counts = Counter([r["gate"] for r in wallet_rows])
    print(
        "WALLET_WATCHLIST_SUMMARY "
        f"wallets={len(wallet_rows)} provisional_A={gate_counts.get('PROVISIONAL_A',0)} "
        f"provisional_B={gate_counts.get('PROVISIONAL_B',0)} reject_high90={gate_counts.get('REJECT_HIGH_90_ENTRY',0)} "
        f"reject_hedge={gate_counts.get('REJECT_HIGH_HEDGE',0)} market_watchlist={len(market_rows)}"
    )
    for r in wallet_rows[:30]:
        print(
            f"WALLET_WATCH wallet={r['wallet']} gate={r['gate']} score={r['score']} "
            f"trades={r['trade_count']} high90={r['high_entry_90_ratio']} hedge={r['hedge_ratio_approx']}"
        )
    for r in market_rows[:20]:
        print(
            f"MARKET_WATCH market={r['market_id']} wallets={r['wallet_count']} trades={r['trade_count']} "
            f"notional={r['notional']} cat={r['top_category']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
