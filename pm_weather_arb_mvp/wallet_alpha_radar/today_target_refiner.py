#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Refine today_wallet_selector output into stricter human-facing states.

No wallet, no signing, no orders.

Why this exists:
The base selector may label an active wallet as HOT_RESEARCH if it has enough
usable/copyable rows, even when follow-entry positive rate and PnL are bad.
That is mathematically honest but operationally misleading.

This post-processor separates:
- ACTIVE_NEGATIVE: enough samples, but follow-entry is losing today
- ACTIVE_NEUTRAL: enough samples, but no clear edge
- HOT_RESEARCH: positive but not strong enough for paper-follow
- TODAY_PAPER_FOLLOW / TODAY_READY_REVIEW: unchanged, still no real money
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path
from typing import List


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


def refine(row: dict, args) -> tuple[str, str, str]:
    base_state = row.get("state", "")
    action = row.get("action", "NO_TRADE")
    copyable = inum(row.get("copyable_t300_h900"))
    pos = fnum(row.get("positive_rate_t300_h900"))
    pnl = fnum(row.get("avg_follow_pnl_mid_t300_h900"))
    bid_pnl = fnum(row.get("avg_follow_pnl_bid_t300_h900"))
    last60_copy = inum(row.get("copyable_60m_t300_h900"))
    last60_pnl = fnum(row.get("avg_follow_pnl_mid_60m_t300_h900"))
    last10_pnl = fnum(row.get("last10_pnl_mid_t300_h900"))
    spread = fnum(row.get("avg_spread_t300"))
    worse = fnum(row.get("avg_delay_ask_minus_wallet_price_t300"))
    top_share = fnum(row.get("top_market_share_today"))
    high90 = fnum(row.get("high90_ratio_today"))

    if base_state in {"TODAY_READY_REVIEW", "TODAY_PAPER_FOLLOW", "STOP_TODAY", "IGNORE"}:
        return base_state, action, row.get("reason", "")

    if copyable >= args.min_negative_samples:
        if pos < args.negative_pos_rate or pnl <= args.negative_avg_pnl:
            return "ACTIVE_NEGATIVE", "NO_TRADE", "enough_samples_but_follow_entry_negative"
        if last60_copy >= args.min_recent_samples and last60_pnl <= 0:
            return "ACTIVE_NEGATIVE", "NO_TRADE", "recent_follow_entry_negative"
        if last10_pnl < 0:
            return "ACTIVE_NEGATIVE", "NO_TRADE", "last10_follow_entry_negative"

    if copyable >= args.min_hot_samples:
        if pos >= args.hot_pos_rate and pnl > args.hot_avg_pnl and spread <= args.max_spread and worse <= args.max_worse:
            return "HOT_RESEARCH", "NO_TRADE", "positive_but_below_paper_threshold"
        return "ACTIVE_NEUTRAL", "NO_TRADE", "active_no_clear_copyable_edge"

    if inum(row.get("usable_t300")) >= args.min_usable_watch:
        return "WATCH", "NO_TRADE", "usable_but_not_enough_copyable"

    return "WATCH", "NO_TRADE", "insufficient_copyable_data"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="paper_logs/wallet_alpha/today_wallet_targets.csv")
    p.add_argument("--output", default="paper_logs/wallet_alpha/today_wallet_targets_refined.csv")
    p.add_argument("--min-negative-samples", type=int, default=20)
    p.add_argument("--negative-pos-rate", type=float, default=0.50)
    p.add_argument("--negative-avg-pnl", type=float, default=0.0)
    p.add_argument("--min-recent-samples", type=int, default=5)
    p.add_argument("--min-hot-samples", type=int, default=20)
    p.add_argument("--hot-pos-rate", type=float, default=0.52)
    p.add_argument("--hot-avg-pnl", type=float, default=0.0)
    p.add_argument("--max-spread", type=float, default=0.08)
    p.add_argument("--max-worse", type=float, default=0.03)
    p.add_argument("--min-usable-watch", type=int, default=20)
    args = p.parse_args()

    rows = read_csv(Path(args.input))
    out = []
    for r in rows:
        refined, action, reason = refine(r, args)
        rr = dict(r)
        rr["base_state"] = r.get("state", "")
        rr["base_reason"] = r.get("reason", "")
        rr["refined_state"] = refined
        rr["refined_action"] = action
        rr["refined_reason"] = reason
        out.append(rr)

    state_rank = {
        "TODAY_READY_REVIEW": 7,
        "TODAY_PAPER_FOLLOW": 6,
        "HOT_RESEARCH": 5,
        "ACTIVE_NEUTRAL": 4,
        "WATCH": 3,
        "ACTIVE_NEGATIVE": 2,
        "STOP_TODAY": 1,
        "IGNORE": 0,
    }
    out.sort(key=lambda r: (state_rank.get(r.get("refined_state", ""), 0), inum(r.get("target_score")), inum(r.get("copyable_t300_h900"))), reverse=True)

    fields = list(out[0].keys()) if out else ["empty"]
    write_csv(Path(args.output), out, fields)
    counts = Counter(r.get("refined_state") for r in out)
    print("TODAY_TARGET_REFINED_SUMMARY wallets=%d " % len(out) + " ".join(f"{k}={v}" for k, v in counts.items()))
    for r in out[:80]:
        print(
            f"TODAY_TARGET_REFINED wallet={r.get('wallet')} state={r.get('refined_state')} action={r.get('refined_action')} "
            f"copyable={r.get('copyable_t300_h900')} pos={r.get('positive_rate_t300_h900')} pnl={r.get('avg_follow_pnl_mid_t300_h900')} "
            f"base={r.get('base_state')} reason={r.get('refined_reason')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
