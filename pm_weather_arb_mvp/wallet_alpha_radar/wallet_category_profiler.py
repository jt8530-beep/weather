#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build wallet x category profiles from wallet history and enriched market metadata.

Phase 1 analysis. No wallet, no signing, no orders.

Purpose:
- The first scoring pass produced PROVISIONAL_A/B wallets but categories were
  UNKNOWN.
- market_metadata_enricher partially maps market ids to categories.
- This script joins wallet history to the enriched market watchlist and reports
  category specialization per wallet.

This is not copy-trading proof. It only tells us which wallet/category pairs are
worth recording and later paper-following.
"""

from __future__ import annotations

import argparse
import csv
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


def build_market_index(rows: List[dict]) -> Dict[str, dict]:
    idx: Dict[str, dict] = {}
    for r in rows:
        for k in ["market_id", "condition_id", "market_slug"]:
            v = str(r.get(k) or "").strip().lower()
            if v:
                idx[v] = r
    return idx


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="paper_logs/wallet_alpha/wallet_trade_history.csv")
    p.add_argument("--watchlist", default="paper_logs/wallet_alpha/wallet_watchlist.csv")
    p.add_argument("--markets", default="paper_logs/wallet_alpha/market_watchlist_enriched.csv")
    p.add_argument("--output", default="paper_logs/wallet_alpha/wallet_category_profiles.csv")
    p.add_argument("--wallet-output", default="paper_logs/wallet_alpha/wallet_category_summary.csv")
    p.add_argument("--min-wallet-gate", default="PROVISIONAL", help="PROVISIONAL means A/B only; ALL means all wallets")
    p.add_argument("--min-category-trades", type=int, default=20)
    args = p.parse_args()

    hist = read_csv(Path(args.history))
    watch = read_csv(Path(args.watchlist))
    markets = read_csv(Path(args.markets))
    market_idx = build_market_index(markets)

    allowed_wallets = set()
    wallet_gate = {}
    wallet_score = {}
    for r in watch:
        w = str(r.get("wallet") or "").lower()
        gate = str(r.get("gate") or "")
        if not w:
            continue
        if args.min_wallet_gate.upper() == "ALL" or gate.startswith("PROVISIONAL"):
            allowed_wallets.add(w)
            wallet_gate[w] = gate
            wallet_score[w] = r.get("score", "")

    stats = defaultdict(lambda: {
        "trade_count": 0,
        "notional": 0.0,
        "prices": [],
        "markets": Counter(),
        "matched": 0,
        "unmatched": 0,
    })
    wallet_totals = Counter()
    wallet_matched = Counter()

    for r in hist:
        w = str(r.get("wallet") or "").lower()
        if allowed_wallets and w not in allowed_wallets:
            continue
        m = str(r.get("market_id") or "").lower()
        meta = market_idx.get(m)
        if meta:
            cat = str(meta.get("category_enriched") or "unknown").lower()
        else:
            cat = "unknown"
        key = (w, cat)
        st = stats[key]
        st["trade_count"] += 1
        st["notional"] += fnum(r.get("notional"))
        price = fnum(r.get("price"))
        if price > 0:
            st["prices"].append(price)
        if m:
            st["markets"].update([m])
        if meta:
            st["matched"] += 1
            wallet_matched[w] += 1
        else:
            st["unmatched"] += 1
        wallet_totals[w] += 1

    rows = []
    for (w, cat), st in stats.items():
        prices = st["prices"]
        high90 = sum(1 for p in prices if p >= 0.90) / len(prices) if prices else 1.0
        mid = sum(1 for p in prices if 0.35 <= p <= 0.65) / len(prices) if prices else 0.0
        market_conc = st["markets"].most_common(1)[0][1] / st["trade_count"] if st["markets"] and st["trade_count"] else 1.0
        wallet_trade_total = wallet_totals[w] or 1
        category_share = st["trade_count"] / wallet_trade_total
        matched_ratio = st["matched"] / st["trade_count"] if st["trade_count"] else 0.0

        cat_score = 0
        if st["trade_count"] >= 100:
            cat_score += 20
        elif st["trade_count"] >= 50:
            cat_score += 14
        elif st["trade_count"] >= args.min_category_trades:
            cat_score += 8

        if category_share >= 0.50:
            cat_score += 20
        elif category_share >= 0.30:
            cat_score += 12
        elif category_share >= 0.15:
            cat_score += 5

        if high90 < 0.20:
            cat_score += 15
        elif high90 < 0.40:
            cat_score += 8
        else:
            cat_score -= 10

        if mid >= 0.35:
            cat_score += 10
        elif mid >= 0.20:
            cat_score += 4

        if market_conc < 0.25:
            cat_score += 10
        elif market_conc < 0.40:
            cat_score += 4
        else:
            cat_score -= 8

        if matched_ratio >= 0.60:
            cat_score += 10
        elif matched_ratio >= 0.30:
            cat_score += 4

        if cat_score >= 60:
            cat_tier = "CAT_A_WATCH"
        elif cat_score >= 40:
            cat_tier = "CAT_B_WATCH"
        else:
            cat_tier = "CAT_C_REJECT"

        rows.append({
            "wallet": w,
            "wallet_gate": wallet_gate.get(w, ""),
            "wallet_score": wallet_score.get(w, ""),
            "category": cat,
            "category_trade_count": st["trade_count"],
            "wallet_trade_total": wallet_trade_total,
            "category_share": f"{category_share:.6f}",
            "notional": f"{st['notional']:.6f}",
            "matched_ratio": f"{matched_ratio:.6f}",
            "high_entry_90_ratio": f"{high90:.6f}",
            "mid_entry_35_65_ratio": f"{mid:.6f}",
            "market_concentration": f"{market_conc:.6f}",
            "category_score": cat_score,
            "category_tier": cat_tier,
        })

    rows.sort(key=lambda r: (inum(r["category_score"]), inum(r["category_trade_count"]), fnum(r["notional"])), reverse=True)

    summary_rows = []
    best_by_wallet: Dict[str, dict] = {}
    for r in rows:
        w = r["wallet"]
        if w not in best_by_wallet:
            best_by_wallet[w] = r
    for w, r in best_by_wallet.items():
        summary_rows.append({
            "wallet": w,
            "wallet_gate": r["wallet_gate"],
            "wallet_score": r["wallet_score"],
            "best_category": r["category"],
            "best_category_score": r["category_score"],
            "best_category_tier": r["category_tier"],
            "best_category_trades": r["category_trade_count"],
            "best_category_share": r["category_share"],
            "history_rows": wallet_totals[w],
            "matched_history_rows": wallet_matched[w],
            "matched_ratio_total": f"{(wallet_matched[w] / wallet_totals[w]) if wallet_totals[w] else 0.0:.6f}",
        })
    summary_rows.sort(key=lambda r: (inum(r["best_category_score"]), inum(r["best_category_trades"])), reverse=True)

    fields = ["wallet", "wallet_gate", "wallet_score", "category", "category_trade_count", "wallet_trade_total", "category_share", "notional", "matched_ratio", "high_entry_90_ratio", "mid_entry_35_65_ratio", "market_concentration", "category_score", "category_tier"]
    summary_fields = ["wallet", "wallet_gate", "wallet_score", "best_category", "best_category_score", "best_category_tier", "best_category_trades", "best_category_share", "history_rows", "matched_history_rows", "matched_ratio_total"]
    write_csv(Path(args.output), rows, fields)
    write_csv(Path(args.wallet_output), summary_rows, summary_fields)

    tier_counts = Counter([r["category_tier"] for r in rows])
    cat_counts = Counter([r["category"] for r in rows])
    print(
        f"WALLET_CATEGORY_PROFILE_SUMMARY history_rows={len(hist)} wallets={len(best_by_wallet)} profiles={len(rows)} "
        f"cat_A={tier_counts.get('CAT_A_WATCH',0)} cat_B={tier_counts.get('CAT_B_WATCH',0)} cat_C={tier_counts.get('CAT_C_REJECT',0)} "
        f"cats=" + ",".join(f"{k}:{v}" for k, v in cat_counts.most_common(10))
    )
    for r in summary_rows[:30]:
        print(
            f"WALLET_CATEGORY_BEST wallet={r['wallet']} gate={r['wallet_gate']} best_cat={r['best_category']} "
            f"cat_score={r['best_category_score']} tier={r['best_category_tier']} trades={r['best_category_trades']} "
            f"share={r['best_category_share']} matched_total={r['matched_ratio_total']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
