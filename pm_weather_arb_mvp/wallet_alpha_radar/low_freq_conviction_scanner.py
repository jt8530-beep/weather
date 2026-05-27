#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Low-frequency conviction scanner for Wallet Alpha Radar.

No wallet, no signing, no orders.

Why this exists:
- The normal today_wallet_selector is statistical: it needs many copyable samples.
- Low-frequency wallets can be valuable but do not have enough intraday samples.
- This scanner finds low-frequency, high-conviction *manual review* candidates.

It does NOT approve automatic following. It only flags trades where:
- wallet is low-frequency today
- trade notional is meaningful
- entry price is not a 0.90+ obvious/settlement entry
- token order book at T+60/T+300 is still not fully gone
- spread/depth/chase price remain reasonable

States:
- LOW_FREQ_MANUAL_REVIEW: strongest, still manual/paper only
- LOW_FREQ_CONFIRMING: promising but incomplete/too early
- LOW_FREQ_WATCH: base candidate, waiting for confirmation
- LOW_FREQ_REJECT: not shown unless --include-rejects
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable, List, Optional


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


def write_csv(path: Path, rows: List[dict], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def decide_state(row: dict, args) -> tuple[str, str, int]:
    price = fnum(row["wallet_price"])
    notional = fnum(row["notional"])
    t60_mid_move = fnum(row["t60_mid_minus_entry"])
    t300_ask_chase = fnum(row["t300_ask_minus_entry"])
    t300_mid_move = fnum(row["t300_mid_minus_entry"])
    t300_spread = fnum(row["t300_spread"])
    t300_ask_size = fnum(row["t300_ask_size"])
    top_market_share = fnum(row["wallet_top_market_share"])
    high90_ratio = fnum(row["wallet_high90_ratio_today"])

    score = 0
    if notional >= args.manual_min_notional:
        score += 25
    elif notional >= args.min_notional:
        score += 15
    if args.min_entry_price <= price <= args.max_entry_price:
        score += 15
    if t60_mid_move >= 0:
        score += 15
    if t300_mid_move >= 0:
        score += 15
    if t300_ask_chase <= args.max_chase:
        score += 15
    if 0 < t300_spread <= args.max_spread:
        score += 10
    if t300_ask_size >= args.min_ask_size:
        score += 10
    if top_market_share <= args.max_top_market_share:
        score += 5
    if high90_ratio > args.max_high90_ratio:
        score -= 30

    # Hard rejects.
    if price < args.min_entry_price or price > args.max_entry_price:
        return "LOW_FREQ_REJECT", "entry_price_out_of_range", score
    if notional < args.min_notional:
        return "LOW_FREQ_REJECT", "notional_below_min", score
    if high90_ratio > args.max_high90_ratio:
        return "LOW_FREQ_REJECT", "wallet_today_high90_too_high", score
    if top_market_share > args.reject_top_market_share:
        return "LOW_FREQ_REJECT", "wallet_single_market_too_concentrated", score

    # Waiting for enough orderbook path.
    if row["t60_mid"] == "" or row["t300_ask"] == "":
        return "LOW_FREQ_WATCH", "waiting_for_t60_t300_orderbook", score

    # Strong manual review only.
    if (
        notional >= args.manual_min_notional
        and t60_mid_move >= 0
        and t300_mid_move >= 0
        and t300_ask_chase <= args.max_chase
        and 0 < t300_spread <= args.max_spread
        and t300_ask_size >= args.min_ask_size
    ):
        return "LOW_FREQ_MANUAL_REVIEW", "large_mid_entry_still_copyable_at_t300", score

    # Promising but not yet strong.
    if (
        t60_mid_move >= 0
        and t300_ask_chase <= args.max_chase
        and 0 < t300_spread <= args.max_spread
        and t300_ask_size >= args.min_ask_size
    ):
        return "LOW_FREQ_CONFIRMING", "positive_path_but_not_manual_grade", score

    return "LOW_FREQ_WATCH", "base_candidate_no_confirmation", score


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live-db", default="paper_logs/wallet_alpha/full_market_live_trades.sqlite")
    p.add_argument("--orderbook-db", default="paper_logs/wallet_alpha/orderbook_snapshots.sqlite")
    p.add_argument("--output", default="paper_logs/wallet_alpha/today_low_freq_conviction.csv")
    p.add_argument("--today-lookback-hours", type=float, default=24.0)
    p.add_argument("--tolerance-sec", type=int, default=45)
    p.add_argument("--min-wallet-trades", type=int, default=1)
    p.add_argument("--max-wallet-trades", type=int, default=10)
    p.add_argument("--min-notional", type=float, default=100.0)
    p.add_argument("--manual-min-notional", type=float, default=300.0)
    p.add_argument("--min-entry-price", type=float, default=0.25)
    p.add_argument("--max-entry-price", type=float, default=0.75)
    p.add_argument("--max-high90-ratio", type=float, default=0.40)
    p.add_argument("--max-top-market-share", type=float, default=0.80)
    p.add_argument("--reject-top-market-share", type=float, default=1.00)
    p.add_argument("--max-chase", type=float, default=0.03)
    p.add_argument("--max-spread", type=float, default=0.08)
    p.add_argument("--min-ask-size", type=float, default=2.0)
    p.add_argument("--include-rejects", action="store_true")
    p.add_argument("--limit", type=int, default=1000000)
    args = p.parse_args()

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(args.today_lookback_hours * 3600 * 1000)
    tol_ms = args.tolerance_sec * 1000

    live = sqlite3.connect(args.live_db)
    live.row_factory = sqlite3.Row
    rows = list(live.execute(
        """
        SELECT unique_id, seen_ts_ms, wallet, wallet_gate, token_id, market_key,
               side, price, size, notional, category_enriched, event_title, question
        FROM wallet_live_trades
        WHERE seen_ts_ms >= ? AND token_id IS NOT NULL AND token_id != ''
        ORDER BY seen_ts_ms ASC
        LIMIT ?
        """,
        (start_ms, args.limit),
    ))
    live.close()

    by_wallet = defaultdict(list)
    for r in rows:
        by_wallet[str(r["wallet"]).lower()].append(r)

    ob = sqlite3.connect(args.orderbook_db)
    out = []
    for wallet, wrs in by_wallet.items():
        n = len(wrs)
        if n < args.min_wallet_trades or n > args.max_wallet_trades:
            continue
        prices = [fnum(r["price"]) for r in wrs]
        high90 = sum(1 for x in prices if x >= 0.90) / len(prices) if prices else 1.0
        market_counts = Counter(str(r["market_key"] or "") for r in wrs)
        top_share = market_counts.most_common(1)[0][1] / n if market_counts and n else 1.0
        total_notional = sum(fnum(r["notional"]) for r in wrs)
        max_notional = max([fnum(r["notional"]) for r in wrs] or [0.0])

        for r in wrs:
            price = fnum(r["price"])
            notional = fnum(r["notional"])
            if notional < args.min_notional and not args.include_rejects:
                continue
            ts = int(r["seen_ts_ms"])
            token_id = str(r["token_id"] or "")
            t60 = get_snap(ob, token_id, ts + 60_000, tol_ms)
            t300 = get_snap(ob, token_id, ts + 300_000, tol_ms)
            t60_mid = fnum(t60.get("mid")) if t60 and t60.get("mid") is not None else None
            t300_mid = fnum(t300.get("mid")) if t300 and t300.get("mid") is not None else None
            t300_ask = fnum(t300.get("ask")) if t300 and t300.get("ask") is not None else None
            t300_spread = fnum(t300.get("spread")) if t300 and t300.get("spread") is not None else None
            t300_ask_size = fnum(t300.get("ask_size")) if t300 and t300.get("ask_size") is not None else None
            rec = {
                "wallet": wallet,
                "unique_id": r["unique_id"],
                "seen_ts_ms": ts,
                "wallet_trades_today": n,
                "wallet_total_notional_today": f"{total_notional:.6f}",
                "wallet_max_trade_notional_today": f"{max_notional:.6f}",
                "wallet_high90_ratio_today": f"{high90:.6f}",
                "wallet_top_market_share": f"{top_share:.6f}",
                "token_id": token_id,
                "market_key": r["market_key"],
                "category_enriched": r["category_enriched"],
                "wallet_price": f"{price:.6f}",
                "size": f"{fnum(r['size']):.6f}",
                "notional": f"{notional:.6f}",
                "t60_mid": f"{t60_mid:.6f}" if t60_mid is not None else "",
                "t60_mid_minus_entry": f"{(t60_mid - price):.6f}" if t60_mid is not None else "0.000000",
                "t300_mid": f"{t300_mid:.6f}" if t300_mid is not None else "",
                "t300_mid_minus_entry": f"{(t300_mid - price):.6f}" if t300_mid is not None else "0.000000",
                "t300_ask": f"{t300_ask:.6f}" if t300_ask is not None else "",
                "t300_ask_minus_entry": f"{(t300_ask - price):.6f}" if t300_ask is not None else "0.000000",
                "t300_spread": f"{t300_spread:.6f}" if t300_spread is not None else "",
                "t300_ask_size": f"{t300_ask_size:.6f}" if t300_ask_size is not None else "",
                "event_title": r["event_title"] or "",
                "question": r["question"] or "",
            }
            state, reason, score = decide_state(rec, args)
            rec["state"] = state
            rec["reason"] = reason
            rec["score"] = score
            if state != "LOW_FREQ_REJECT" or args.include_rejects:
                out.append(rec)
    ob.close()

    rank = {"LOW_FREQ_MANUAL_REVIEW": 4, "LOW_FREQ_CONFIRMING": 3, "LOW_FREQ_WATCH": 2, "LOW_FREQ_REJECT": 1}
    out.sort(key=lambda r: (rank.get(r["state"], 0), int(r["score"]), fnum(r["notional"])), reverse=True)
    fields = [
        "state", "reason", "score", "wallet", "wallet_trades_today", "wallet_total_notional_today", "wallet_max_trade_notional_today",
        "wallet_high90_ratio_today", "wallet_top_market_share", "unique_id", "seen_ts_ms", "token_id", "market_key",
        "category_enriched", "wallet_price", "size", "notional", "t60_mid", "t60_mid_minus_entry", "t300_mid",
        "t300_mid_minus_entry", "t300_ask", "t300_ask_minus_entry", "t300_spread", "t300_ask_size", "event_title", "question"
    ]
    write_csv(Path(args.output), out, fields)
    counts = Counter(r["state"] for r in out)
    print(
        f"LOW_FREQ_CONVICTION_SUMMARY live_rows={len(rows)} wallets_seen={len(by_wallet)} candidates={len(out)} "
        + " ".join(f"{k}={v}" for k, v in counts.items())
        + f" output={args.output}"
    )
    for r in out[:80]:
        print(
            f"LOW_FREQ_CANDIDATE state={r['state']} wallet={r['wallet'][:10]} score={r['score']} trades={r['wallet_trades_today']} "
            f"notional={r['notional']} price={r['wallet_price']} t300_ask_chase={r['t300_ask_minus_entry']} "
            f"spread={r['t300_spread']} reason={r['reason']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
