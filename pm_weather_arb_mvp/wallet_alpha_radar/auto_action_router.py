#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Auto action router for Wallet Alpha Radar.

No wallet, no signing, no real orders.

This merges three automatic signal sources into one final action file:
1) today_wallet_targets_full_market_refined.csv   -> statistical copyability pool
2) today_low_freq_conviction.csv                  -> 1-10 trade conviction pool
3) today_mid_freq_conviction.csv                  -> 10-30 trade conviction pool

Actions:
- AUTO_PAPER_FOLLOW: automatically enter paper-follow queue, no manual review
- AUTO_WATCH: track only, no paper entry yet
- AUTO_STOP: stop/blacklist for today
- IGNORE: no action

This is intentionally paper-only. Real-money execution must be a separate module
with explicit wallet/risk controls; this router never signs or submits orders.
"""

from __future__ import annotations

import argparse
import csv
import time
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


def route_refined(rows: List[dict], args) -> List[dict]:
    out = []
    for r in rows:
        state = r.get("refined_state") or r.get("state") or ""
        wallet = r.get("wallet", "")
        copyable = inum(r.get("copyable_t300_h900"))
        pos = fnum(r.get("positive_rate_t300_h900"))
        pnl_mid = fnum(r.get("avg_follow_pnl_mid_t300_h900"))
        pnl_bid = fnum(r.get("avg_follow_pnl_bid_t300_h900"))
        last10 = fnum(r.get("last10_pnl_mid_t300_h900"))
        last60 = fnum(r.get("avg_follow_pnl_mid_60m_t300_h900"))
        action = "IGNORE"
        reason = state or "no_state"
        priority = 0
        if state in {"TODAY_PAPER_FOLLOW", "TODAY_READY_REVIEW"}:
            action = "AUTO_PAPER_FOLLOW"
            reason = "strict_statistical_copyability_passed"
            priority = 100
        elif state in {"ACTIVE_NEGATIVE", "STOP_TODAY"}:
            action = "AUTO_STOP"
            reason = "negative_or_stopped_today"
            priority = 10
        elif copyable >= args.watch_copyable and pos >= args.watch_pos and pnl_mid > 0 and last10 >= 0:
            action = "AUTO_WATCH"
            reason = "near_miss_positive_watch"
            priority = 50
        out.append({
            "source": "statistical",
            "wallet": wallet,
            "action": action,
            "priority": priority,
            "reason": reason,
            "state": state,
            "copyable": copyable,
            "positive_rate": f"{pos:.6f}",
            "pnl_mid": f"{pnl_mid:.6f}",
            "pnl_bid": f"{pnl_bid:.6f}",
            "last10_pnl": f"{last10:.6f}",
            "last60_pnl": f"{last60:.6f}",
            "token_id": "",
            "notional": "",
            "price": "",
            "extra": r.get("refined_reason") or r.get("reason") or "",
        })
    return out


def route_conviction(rows: List[dict], label: str, args) -> List[dict]:
    out = []
    for r in rows:
        state = r.get("state", "")
        wallet = r.get("wallet", "")
        score = inum(r.get("score"))
        notional = fnum(r.get("notional"))
        chase = fnum(r.get("t300_ask_minus_entry"))
        spread = fnum(r.get("t300_spread"))
        action = "IGNORE"
        reason = state or "no_state"
        priority = 0
        if state == "LOW_FREQ_MANUAL_REVIEW":
            # User requested no manual review. In this system this means automatic paper-follow only.
            action = "AUTO_PAPER_FOLLOW"
            reason = f"{label}_conviction_manual_grade_routed_to_paper"
            priority = 80 if label == "mid_freq" else 70
        elif state == "LOW_FREQ_CONFIRMING":
            action = "AUTO_WATCH"
            reason = f"{label}_conviction_confirming"
            priority = 45
        elif state == "LOW_FREQ_WATCH":
            action = "AUTO_WATCH"
            reason = f"{label}_conviction_watch"
            priority = 30
        elif state == "LOW_FREQ_REJECT":
            action = "AUTO_STOP"
            reason = f"{label}_conviction_reject"
            priority = 5
        out.append({
            "source": label,
            "wallet": wallet,
            "action": action,
            "priority": priority + min(20, score // 5),
            "reason": reason,
            "state": state,
            "copyable": "",
            "positive_rate": "",
            "pnl_mid": r.get("t300_mid_minus_entry", ""),
            "pnl_bid": "",
            "last10_pnl": "",
            "last60_pnl": "",
            "token_id": r.get("token_id", ""),
            "notional": f"{notional:.6f}",
            "price": r.get("wallet_price", ""),
            "extra": f"score={score};chase={chase:.6f};spread={spread:.6f};{r.get('reason','')}",
        })
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--refined", default="paper_logs/wallet_alpha/today_wallet_targets_full_market_refined.csv")
    p.add_argument("--low", default="paper_logs/wallet_alpha/today_low_freq_conviction.csv")
    p.add_argument("--mid", default="paper_logs/wallet_alpha/today_mid_freq_conviction.csv")
    p.add_argument("--output", default="paper_logs/wallet_alpha/auto_actions.csv")
    p.add_argument("--watch-copyable", type=int, default=10)
    p.add_argument("--watch-pos", type=float, default=0.50)
    args = p.parse_args()

    actions = []
    actions.extend(route_refined(read_csv(args.refined), args))
    actions.extend(route_conviction(read_csv(args.low), "low_freq", args))
    actions.extend(route_conviction(read_csv(args.mid), "mid_freq", args))

    # Deduplicate by source+wallet+token+action+reason, and keep highest priority first.
    seen = set()
    dedup = []
    for r in sorted(actions, key=lambda x: inum(x.get("priority")), reverse=True):
        key = (r.get("source"), r.get("wallet"), r.get("token_id"), r.get("action"), r.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        if r["action"] == "IGNORE":
            continue
        r["ts_ms"] = int(time.time() * 1000)
        dedup.append(r)

    fields = ["ts_ms", "source", "wallet", "action", "priority", "reason", "state", "copyable", "positive_rate", "pnl_mid", "pnl_bid", "last10_pnl", "last60_pnl", "token_id", "notional", "price", "extra"]
    write_csv(args.output, dedup, fields)
    counts = Counter(r["action"] for r in dedup)
    src_counts = Counter(r["source"] for r in dedup)
    print("AUTO_ACTION_ROUTER_SUMMARY actions=%d " % len(dedup) + " ".join(f"{k}={v}" for k, v in counts.items()) + " sources=" + ",".join(f"{k}:{v}" for k,v in src_counts.items()))
    for r in dedup[:100]:
        print(
            f"AUTO_ACTION source={r['source']} wallet={r['wallet'][:10]} action={r['action']} priority={r['priority']} "
            f"reason={r['reason']} pnl={r['pnl_mid']} token={r['token_id'][:12]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
