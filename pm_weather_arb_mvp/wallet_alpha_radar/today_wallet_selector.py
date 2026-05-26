#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Today dynamic wallet selector for Wallet Alpha Radar.

No wallet, no signing, no orders.

Purpose:
- Do NOT follow fixed wallets for weeks.
- Re-rank all live wallets by today's rolling copyability.
- Identify wallets that are working today, and mark them as expired/stop once
  their recent edge disappears.

Core question:
If a watched wallet trades at time T, can we enter at T+delay using the then-current
ask and still have positive mark-to-mid / mark-to-bid after a short hold?

Outputs:
- paper_logs/wallet_alpha/today_wallet_targets.csv
- Optional detailed rows for auditing.

States:
- IGNORE
- WATCH
- HOT_RESEARCH
- TODAY_PAPER_FOLLOW
- TODAY_READY_REVIEW
- STOP_TODAY
"""

from __future__ import annotations

import argparse
import csv
import math
import sqlite3
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


def fnum(x, default=0.0) -> float:
    try:
        if x in (None, ""):
            return default
        return float(x)
    except Exception:
        return default


def mid(bid, ask) -> Optional[float]:
    if bid is None or ask is None:
        return None
    try:
        return (float(bid) + float(ask)) / 2.0
    except Exception:
        return None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def get_snap(conn: sqlite3.Connection, token_id: str, ts_ms: int, tolerance_ms: int) -> Optional[dict]:
    if not token_id:
        return None
    rows = conn.execute(
        """
        SELECT ts_ms, token_id, outcome, best_bid, best_ask, bid_size, ask_size, spread
        FROM orderbook_snapshots
        WHERE token_id=? AND ts_ms >= ? AND ts_ms <= ?
        ORDER BY ABS(ts_ms - ?) ASC
        LIMIT 1
        """,
        (token_id, ts_ms - tolerance_ms, ts_ms + tolerance_ms, ts_ms),
    ).fetchall()
    if not rows:
        return None
    r = rows[0]
    return {
        "ts_ms": r[0],
        "token_id": r[1],
        "outcome": r[2],
        "bid": r[3],
        "ask": r[4],
        "mid": mid(r[3], r[4]),
        "bid_size": r[5],
        "ask_size": r[6],
        "spread": r[7],
    }


def write_csv(path: Path, rows: List[dict], fields: Optional[List[str]] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = list(rows[0].keys()) if rows else ["empty"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        if rows:
            w.writerows(rows)


def avg(vals: Iterable[float]) -> float:
    xs = list(vals)
    return sum(xs) / len(xs) if xs else 0.0


def ratio(n: int, d: int) -> float:
    return n / d if d else 0.0


def bucket_age(ts_ms: int, now_ms: int) -> str:
    age = now_ms - ts_ms
    if age <= 60 * 60 * 1000:
        return "60m"
    if age <= 180 * 60 * 1000:
        return "180m"
    if age <= 360 * 60 * 1000:
        return "360m"
    return "today"


def in_window(ts_ms: int, now_ms: int, window_sec: int) -> bool:
    return ts_ms >= now_ms - window_sec * 1000


def compute_target_state(m: dict, args) -> Tuple[str, str, int]:
    """Return state, reason, score."""
    live_180 = int(m.get("live_trades_180m", 0))
    unique_180 = int(m.get("unique_markets_180m", 0))
    usable = int(m.get("usable_t300", 0))
    copyable = int(m.get("copyable_t300_h900", 0))
    copyable_1800 = int(m.get("copyable_t300_h1800", 0))
    pos = fnum(m.get("positive_rate_t300_h900"))
    pos_1800 = fnum(m.get("positive_rate_t300_h1800"))
    pnl = fnum(m.get("avg_follow_pnl_mid_t300_h900"))
    pnl_bid = fnum(m.get("avg_follow_pnl_bid_t300_h900"))
    pnl_1800 = fnum(m.get("avg_follow_pnl_mid_t300_h1800"))
    worse = fnum(m.get("avg_delay_ask_minus_wallet_price_t300"))
    spread = fnum(m.get("avg_spread_t300"))
    not_copy = fnum(m.get("not_copyable_ratio_t300_h900"))
    last60_pnl = fnum(m.get("avg_follow_pnl_mid_60m_t300_h900"))
    last60_copy = int(m.get("copyable_60m_t300_h900", 0))
    last10_pnl = fnum(m.get("last10_pnl_mid_t300_h900"))
    high90 = fnum(m.get("high90_ratio_today"))
    top_share = fnum(m.get("top_market_share_today"))
    broad = str(m.get("broad_high_freq", "0")) == "1"

    score = 0
    score += min(25, usable // 4)
    score += min(25, copyable)
    score += int(30 * max(0.0, min(1.0, pos)))
    if pnl > 0:
        score += min(20, int(pnl * 1000))
    if pnl_bid >= 0:
        score += 8
    if last60_copy >= 5 and last60_pnl > 0:
        score += 10
    if worse <= args.max_price_worse:
        score += 6
    if spread <= args.max_spread:
        score += 6
    if top_share <= 0.40:
        score += 6
    if high90 > 0.40:
        score -= 25
    if broad:
        score -= 15
    if top_share > 0.60:
        score -= 15

    # Stop first: if recent edge is negative, today's wallet is stale.
    if last60_copy >= args.stop_min_recent_copyable and last60_pnl <= 0:
        return "STOP_TODAY", "last_60m_follow_pnl_non_positive", score
    if last10_pnl < 0 and copyable >= 10:
        return "STOP_TODAY", "last10_follow_pnl_negative", score
    if high90 > 0.50:
        return "STOP_TODAY", "today_high90_too_high", score

    # Ready review: not trade approval, but strongest manual review state.
    if (
        copyable >= args.ready_min_copyable
        and pos >= args.ready_min_positive_rate
        and pnl >= args.ready_min_avg_pnl
        and pnl_bid >= args.ready_min_avg_bid_pnl
        and last60_pnl >= 0
        and top_share <= args.max_top_market_share_ready
        and worse <= args.max_price_worse
        and spread <= args.max_spread
    ):
        return "TODAY_READY_REVIEW", "copyable_positive_bid_safe", score

    # Paper follow candidate.
    if (
        copyable >= args.paper_min_copyable
        and pos >= args.paper_min_positive_rate
        and pnl >= args.paper_min_avg_pnl
        and worse <= args.max_price_worse
        and spread <= args.max_spread
        and not_copy <= args.max_not_copyable_ratio
        and last60_pnl >= -0.005
    ):
        return "TODAY_PAPER_FOLLOW", "copyable_mid_positive", score

    # Hot research: price movement / delayed pnl maybe promising but not enough copyability.
    if usable >= args.research_min_usable and live_180 >= args.research_min_live and unique_180 >= 3:
        return "HOT_RESEARCH", "enough_usable_needs_copyability", score

    if live_180 >= 5:
        return "WATCH", "active_but_insufficient_usable", score
    return "IGNORE", "inactive_or_insufficient_data", score


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live-db", default="paper_logs/wallet_alpha/wallet_live_trades.sqlite")
    p.add_argument("--orderbook-db", default="paper_logs/wallet_alpha/orderbook_snapshots.sqlite")
    p.add_argument("--output", default="paper_logs/wallet_alpha/today_wallet_targets.csv")
    p.add_argument("--details-output", default="paper_logs/wallet_alpha/today_wallet_follow_rows.csv")
    p.add_argument("--delay-sec", type=int, default=300)
    p.add_argument("--holds", default="900,1800")
    p.add_argument("--tolerance-sec", type=int, default=45)
    p.add_argument("--today-lookback-hours", type=float, default=24.0)
    p.add_argument("--max-price-worse", type=float, default=0.03)
    p.add_argument("--max-spread", type=float, default=0.08)
    p.add_argument("--min-ask-size", type=float, default=2.0)
    p.add_argument("--max-not-copyable-ratio", type=float, default=0.50)
    p.add_argument("--paper-min-copyable", type=int, default=20)
    p.add_argument("--paper-min-positive-rate", type=float, default=0.58)
    p.add_argument("--paper-min-avg-pnl", type=float, default=0.01)
    p.add_argument("--ready-min-copyable", type=int, default=50)
    p.add_argument("--ready-min-positive-rate", type=float, default=0.55)
    p.add_argument("--ready-min-avg-pnl", type=float, default=0.01)
    p.add_argument("--ready-min-avg-bid-pnl", type=float, default=0.0)
    p.add_argument("--max-top-market-share-ready", type=float, default=0.40)
    p.add_argument("--research-min-usable", type=int, default=20)
    p.add_argument("--research-min-live", type=int, default=20)
    p.add_argument("--stop-min-recent-copyable", type=int, default=5)
    p.add_argument("--limit", type=int, default=1000000)
    args = p.parse_args()

    holds = [int(x) for x in args.holds.split(",") if x.strip()]
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(args.today_lookback_hours * 3600 * 1000)
    tolerance_ms = args.tolerance_sec * 1000

    live = sqlite3.connect(args.live_db)
    live.row_factory = sqlite3.Row
    cols = table_columns(live, "wallet_live_trades")
    if "token_id" not in cols:
        print("TODAY_WALLET_SELECTOR_SUMMARY wallets=0 reason=no_token_id")
        return 0
    live_rows = list(live.execute(
        """
        SELECT unique_id, seen_ts_ms, wallet, wallet_gate, wallet_score, token_id, market_key,
               side, price, size, notional, category_enriched, event_title, question
        FROM wallet_live_trades
        WHERE seen_ts_ms >= ? AND token_id IS NOT NULL AND token_id != ''
        ORDER BY seen_ts_ms ASC
        LIMIT ?
        """,
        (start_ms, args.limit),
    ))
    live.close()

    ob = sqlite3.connect(args.orderbook_db)

    wallet_base = defaultdict(lambda: {
        "live_trades_today": 0,
        "live_trades_60m": 0,
        "live_trades_180m": 0,
        "live_trades_360m": 0,
        "markets_today": Counter(),
        "markets_180m": Counter(),
        "prices_today": [],
        "notional_today": 0.0,
        "gate": "",
    })
    detail_rows = []

    for tr in live_rows:
        wallet = str(tr["wallet"]).lower()
        token_id = str(tr["token_id"] or "")
        if not token_id:
            continue
        ts = int(tr["seen_ts_ms"])
        price = fnum(tr["price"])
        market_key = str(tr["market_key"] or "")
        wb = wallet_base[wallet]
        wb["gate"] = tr["wallet_gate"] or wb["gate"]
        wb["live_trades_today"] += 1
        wb["notional_today"] += fnum(tr["notional"])
        wb["markets_today"].update([market_key])
        wb["prices_today"].append(price)
        if in_window(ts, now_ms, 60 * 60):
            wb["live_trades_60m"] += 1
        if in_window(ts, now_ms, 180 * 60):
            wb["live_trades_180m"] += 1
            wb["markets_180m"].update([market_key])
        if in_window(ts, now_ms, 360 * 60):
            wb["live_trades_360m"] += 1

        delay_ts = ts + args.delay_sec * 1000
        entry = get_snap(ob, token_id, delay_ts, tolerance_ms)
        for hold in holds:
            exit_ts = delay_ts + hold * 1000
            exit_snap = get_snap(ob, token_id, exit_ts, tolerance_ms)
            usable = entry is not None and exit_snap is not None and entry.get("ask") is not None and exit_snap.get("mid") is not None
            delay_ask = fnum(entry.get("ask")) if entry else 0.0
            delay_bid = fnum(entry.get("bid")) if entry else 0.0
            delay_spread = fnum(entry.get("spread")) if entry else 0.0
            ask_size = fnum(entry.get("ask_size")) if entry else 0.0
            exit_mid = fnum(exit_snap.get("mid")) if exit_snap else 0.0
            exit_bid = fnum(exit_snap.get("bid")) if exit_snap else 0.0
            pnl_mid = exit_mid - delay_ask if usable else 0.0
            pnl_bid = exit_bid - delay_ask if usable else 0.0
            worse = delay_ask - price if usable else 0.0
            copyable = usable and worse <= args.max_price_worse and delay_spread <= args.max_spread and ask_size >= args.min_ask_size
            detail_rows.append({
                "wallet": wallet,
                "unique_id": tr["unique_id"],
                "seen_ts_ms": ts,
                "age_bucket": bucket_age(ts, now_ms),
                "token_id": token_id,
                "market_key": market_key,
                "delay_sec": args.delay_sec,
                "hold_sec": hold,
                "wallet_price": f"{price:.6f}",
                "delay_ask": f"{delay_ask:.6f}" if usable else "",
                "delay_bid": f"{delay_bid:.6f}" if usable else "",
                "exit_mid": f"{exit_mid:.6f}" if usable else "",
                "exit_bid": f"{exit_bid:.6f}" if usable else "",
                "follow_pnl_mid": f"{pnl_mid:.6f}",
                "follow_pnl_bid": f"{pnl_bid:.6f}",
                "delay_ask_minus_wallet_price": f"{worse:.6f}",
                "delay_spread": f"{delay_spread:.6f}" if entry else "",
                "delay_ask_size": f"{ask_size:.6f}" if entry else "",
                "usable": "1" if usable else "0",
                "copyable": "1" if copyable else "0",
                "category_enriched": tr["category_enriched"],
            })
    ob.close()

    # Aggregate details.
    by_wallet_hold = defaultdict(list)
    for r in detail_rows:
        by_wallet_hold[(r["wallet"], int(r["hold_sec"]))].append(r)

    target_rows = []
    for wallet, wb in wallet_base.items():
        prices = wb["prices_today"]
        high90 = ratio(sum(1 for p in prices if p >= 0.90), len(prices))
        mid_ratio = ratio(sum(1 for p in prices if 0.35 <= p <= 0.65), len(prices))
        low25 = ratio(sum(1 for p in prices if 0 < p < 0.25), len(prices))
        markets_today = wb["markets_today"]
        top_market_share = ratio(markets_today.most_common(1)[0][1], wb["live_trades_today"]) if markets_today else 1.0
        unique_markets_today = len(markets_today)
        unique_markets_180m = len(wb["markets_180m"])
        broad = 1 if wb["live_trades_today"] >= 300 and unique_markets_today >= 75 else 0

        # Primary decision uses delay=300 hold=900. Hold=1800 is secondary.
        rows_h900 = by_wallet_hold.get((wallet, 900), [])
        rows_h1800 = by_wallet_hold.get((wallet, 1800), [])

        def metrics(arr: List[dict], only_60m: bool = False):
            if only_60m:
                arr = [r for r in arr if r["age_bucket"] == "60m"]
            usable = [r for r in arr if r["usable"] == "1"]
            copyable = [r for r in usable if r["copyable"] == "1"]
            pos_mid = sum(1 for r in copyable if fnum(r["follow_pnl_mid"]) > 0)
            return {
                "rows": len(arr),
                "usable": len(usable),
                "copyable": len(copyable),
                "positive_rate": ratio(pos_mid, len(copyable)),
                "avg_pnl_mid": avg(fnum(r["follow_pnl_mid"]) for r in copyable),
                "avg_pnl_bid": avg(fnum(r["follow_pnl_bid"]) for r in copyable),
                "avg_worse": avg(fnum(r["delay_ask_minus_wallet_price"]) for r in usable),
                "avg_spread": avg(fnum(r["delay_spread"]) for r in usable),
                "not_copyable_ratio": 1.0 - ratio(len(copyable), len(usable)) if usable else 1.0,
                "last10_pnl": sum(fnum(r["follow_pnl_mid"]) for r in copyable[-10:]) if copyable else 0.0,
            }

        m900 = metrics(rows_h900)
        m1800 = metrics(rows_h1800)
        m900_60 = metrics(rows_h900, only_60m=True)

        row = {
            "wallet": wallet,
            "gate": wb["gate"],
            "live_trades_today": wb["live_trades_today"],
            "live_trades_60m": wb["live_trades_60m"],
            "live_trades_180m": wb["live_trades_180m"],
            "live_trades_360m": wb["live_trades_360m"],
            "unique_markets_today": unique_markets_today,
            "unique_markets_180m": unique_markets_180m,
            "notional_today": f"{wb['notional_today']:.6f}",
            "high90_ratio_today": f"{high90:.6f}",
            "mid_entry_ratio_today": f"{mid_ratio:.6f}",
            "low25_ratio_today": f"{low25:.6f}",
            "top_market_share_today": f"{top_market_share:.6f}",
            "broad_high_freq": str(broad),
            "usable_t300": m900["usable"],
            "copyable_t300_h900": m900["copyable"],
            "positive_rate_t300_h900": f"{m900['positive_rate']:.6f}",
            "avg_follow_pnl_mid_t300_h900": f"{m900['avg_pnl_mid']:.6f}",
            "avg_follow_pnl_bid_t300_h900": f"{m900['avg_pnl_bid']:.6f}",
            "avg_delay_ask_minus_wallet_price_t300": f"{m900['avg_worse']:.6f}",
            "avg_spread_t300": f"{m900['avg_spread']:.6f}",
            "not_copyable_ratio_t300_h900": f"{m900['not_copyable_ratio']:.6f}",
            "copyable_t300_h1800": m1800["copyable"],
            "positive_rate_t300_h1800": f"{m1800['positive_rate']:.6f}",
            "avg_follow_pnl_mid_t300_h1800": f"{m1800['avg_pnl_mid']:.6f}",
            "copyable_60m_t300_h900": m900_60["copyable"],
            "avg_follow_pnl_mid_60m_t300_h900": f"{m900_60['avg_pnl_mid']:.6f}",
            "last10_pnl_mid_t300_h900": f"{m900['last10_pnl']:.6f}",
        }
        state, reason, score = compute_target_state(row, args)
        row["state"] = state
        row["reason"] = reason
        row["target_score"] = score
        if state == "TODAY_READY_REVIEW":
            row["action"] = "MANUAL_REVIEW_ONLY"
        elif state == "TODAY_PAPER_FOLLOW":
            row["action"] = "PAPER_FOLLOW_ONLY"
        elif state == "STOP_TODAY":
            row["action"] = "STOP"
        else:
            row["action"] = "NO_TRADE"
        target_rows.append(row)

    state_rank = {
        "TODAY_READY_REVIEW": 5,
        "TODAY_PAPER_FOLLOW": 4,
        "HOT_RESEARCH": 3,
        "WATCH": 2,
        "STOP_TODAY": 1,
        "IGNORE": 0,
    }
    target_rows.sort(key=lambda r: (state_rank.get(r["state"], 0), int(r["target_score"]), int(r["copyable_t300_h900"])), reverse=True)

    fields = [
        "wallet", "state", "action", "reason", "target_score", "gate",
        "live_trades_today", "live_trades_60m", "live_trades_180m", "live_trades_360m",
        "unique_markets_today", "unique_markets_180m", "notional_today",
        "high90_ratio_today", "mid_entry_ratio_today", "low25_ratio_today", "top_market_share_today", "broad_high_freq",
        "usable_t300", "copyable_t300_h900", "positive_rate_t300_h900",
        "avg_follow_pnl_mid_t300_h900", "avg_follow_pnl_bid_t300_h900",
        "avg_delay_ask_minus_wallet_price_t300", "avg_spread_t300", "not_copyable_ratio_t300_h900",
        "copyable_t300_h1800", "positive_rate_t300_h1800", "avg_follow_pnl_mid_t300_h1800",
        "copyable_60m_t300_h900", "avg_follow_pnl_mid_60m_t300_h900", "last10_pnl_mid_t300_h900",
    ]
    write_csv(Path(args.output), target_rows, fields)
    # Details can be large; still useful for debugging.
    detail_fields = list(detail_rows[0].keys()) if detail_rows else ["empty"]
    write_csv(Path(args.details_output), detail_rows, detail_fields)

    states = Counter(r["state"] for r in target_rows)
    print(
        f"TODAY_WALLET_SELECTOR_SUMMARY wallets={len(target_rows)} live_rows={len(live_rows)} details={len(detail_rows)} "
        + " ".join(f"{k}={v}" for k, v in states.items())
        + f" output={args.output}"
    )
    for r in target_rows[:80]:
        print(
            f"TODAY_WALLET_TARGET wallet={r['wallet']} state={r['state']} action={r['action']} score={r['target_score']} "
            f"copyable={r['copyable_t300_h900']} pos={r['positive_rate_t300_h900']} pnl={r['avg_follow_pnl_mid_t300_h900']} "
            f"bidpnl={r['avg_follow_pnl_bid_t300_h900']} live180={r['live_trades_180m']} reason={r['reason']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
