#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Score Polymarket candidate wallets using Phase 1 metrics.

No historical order book required. No wallet, no signing, no orders.

This is not a profitability proof. It only creates A/B/C watchlists for future
orderbook recording and delayed follow testing.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple


def fnum(x, default=0.0) -> float:
    try:
        if x in (None, ""):
            return default
        return float(x)
    except Exception:
        return default


def read_csv(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_day(ts: str) -> str:
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


def percentile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    idx = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * q))))
    return xs[idx]


def score_wallet(rows: List[dict]) -> dict:
    n = len(rows)
    prices = [fnum(r.get("price")) for r in rows if fnum(r.get("price")) > 0]
    notionals = [fnum(r.get("notional")) for r in rows]
    markets = [str(r.get("market_id") or "") for r in rows if r.get("market_id")]
    cats = [str(r.get("category") or "UNKNOWN") for r in rows]
    sides = [str(r.get("side") or "").upper() for r in rows]
    days = {parse_day(r.get("timestamp")) for r in rows if parse_day(r.get("timestamp"))}

    total_notional = sum(notionals)
    market_counts = Counter(markets)
    category_counts = Counter(cats)
    median_entry = percentile(prices, 0.5)
    high_entry_ratio = sum(1 for p in prices if p >= 0.90) / len(prices) if prices else 1.0
    mid_entry_ratio = sum(1 for p in prices if 0.35 <= p <= 0.65) / len(prices) if prices else 0.0
    market_concentration = (market_counts.most_common(1)[0][1] / n) if market_counts and n else 1.0
    category_concentration = (category_counts.most_common(1)[0][1] / n) if category_counts and n else 1.0

    # Hedge approximation: same wallet has both YES-like and NO-like sides in a market.
    by_market_sides: Dict[str, set] = defaultdict(set)
    for r in rows:
        m = str(r.get("market_id") or "")
        side = str(r.get("side") or "").upper()
        if m and side:
            if "YES" in side:
                by_market_sides[m].add("YES")
            elif "NO" in side:
                by_market_sides[m].add("NO")
            else:
                by_market_sides[m].add(side[:16])
    hedge_markets = sum(1 for ss in by_market_sides.values() if len(ss) >= 2 and {"YES", "NO"}.issubset(ss))
    hedge_ratio = hedge_markets / len(by_market_sides) if by_market_sides else 1.0

    score = 0
    if n >= 100:
        score += 18
    elif n >= 50:
        score += 14
    elif n >= 30:
        score += 10
    elif n >= 10:
        score += 5

    if len(days) >= 30:
        score += 14
    elif len(days) >= 14:
        score += 9
    elif len(days) >= 5:
        score += 4

    if total_notional >= 5000:
        score += 12
    elif total_notional >= 1000:
        score += 8
    elif total_notional >= 200:
        score += 4

    if high_entry_ratio < 0.20:
        score += 12
    elif high_entry_ratio < 0.40:
        score += 7
    elif high_entry_ratio < 0.60:
        score += 3

    if mid_entry_ratio >= 0.40:
        score += 10
    elif mid_entry_ratio >= 0.25:
        score += 6
    elif mid_entry_ratio >= 0.10:
        score += 2

    if hedge_ratio < 0.10:
        score += 12
    elif hedge_ratio < 0.20:
        score += 8
    elif hedge_ratio < 0.35:
        score += 3
    else:
        score -= 15

    if market_concentration < 0.20:
        score += 10
    elif market_concentration < 0.35:
        score += 6
    elif market_concentration < 0.50:
        score += 2
    else:
        score -= 10

    if category_concentration >= 0.35 and category_concentration <= 0.80:
        score += 8
    elif category_concentration > 0.80:
        score += 2

    if score >= 75:
        tier = "A_WATCH"
    elif score >= 55:
        tier = "B_WATCH"
    else:
        tier = "C_REJECT"

    top_cat = category_counts.most_common(1)[0][0] if category_counts else "UNKNOWN"
    return {
        "trade_count": n,
        "active_days": len(days),
        "total_notional": total_notional,
        "median_entry_price": median_entry,
        "high_entry_90_ratio": high_entry_ratio,
        "mid_entry_35_65_ratio": mid_entry_ratio,
        "hedge_ratio_approx": hedge_ratio,
        "market_concentration": market_concentration,
        "category_concentration": category_concentration,
        "top_category": top_cat,
        "score": score,
        "tier": tier,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--history", default="paper_logs/wallet_alpha/wallet_trade_history.csv")
    p.add_argument("--candidates", default="paper_logs/wallet_alpha/candidate_wallets.csv")
    p.add_argument("--output", default="paper_logs/wallet_alpha/wallet_scores.csv")
    args = p.parse_args()

    hist = read_csv(Path(args.history))
    cand = read_csv(Path(args.candidates))
    by_wallet: Dict[str, List[dict]] = defaultdict(list)
    for r in hist:
        w = str(r.get("wallet") or "").lower()
        if w:
            by_wallet[w].append(r)

    # If history is empty, fall back to candidate-level weak score.
    out: List[dict] = []
    if by_wallet:
        for w, rows in by_wallet.items():
            s = score_wallet(rows)
            out.append({"wallet": w, **{k: (f"{v:.6f}" if isinstance(v, float) else v) for k, v in s.items()}})
    else:
        for r in cand:
            trades = int(float(r.get("recent_trade_count") or 0))
            notional = fnum(r.get("notional_hint"))
            score = 0
            if trades >= 20:
                score += 20
            elif trades >= 10:
                score += 12
            elif trades >= 3:
                score += 5
            if notional >= 1000:
                score += 15
            elif notional >= 200:
                score += 8
            tier = "B_WATCH" if score >= 25 else "C_REJECT"
            out.append({
                "wallet": str(r.get("wallet") or "").lower(),
                "trade_count": trades,
                "active_days": "",
                "total_notional": f"{notional:.6f}",
                "median_entry_price": "",
                "high_entry_90_ratio": "",
                "mid_entry_35_65_ratio": "",
                "hedge_ratio_approx": "",
                "market_concentration": "",
                "category_concentration": "",
                "top_category": "UNKNOWN",
                "score": score,
                "tier": tier,
            })

    out.sort(key=lambda x: (int(float(x.get("score") or 0)), int(float(x.get("trade_count") or 0))), reverse=True)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    fields = ["wallet", "trade_count", "active_days", "total_notional", "median_entry_price", "high_entry_90_ratio", "mid_entry_35_65_ratio", "hedge_ratio_approx", "market_concentration", "category_concentration", "top_category", "score", "tier"]
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out)

    counts = Counter([x["tier"] for x in out])
    print(
        f"WALLET_SCORE_SUMMARY wallets={len(out)} A={counts.get('A_WATCH',0)} B={counts.get('B_WATCH',0)} "
        f"C={counts.get('C_REJECT',0)} history_rows={len(hist)} output={args.output}"
    )
    for x in out[:30]:
        print(
            f"WALLET_SCORE wallet={x['wallet']} score={x['score']} tier={x['tier']} "
            f"trades={x['trade_count']} high90={x['high_entry_90_ratio']} hedge={x['hedge_ratio_approx']} top_cat={x['top_category']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
