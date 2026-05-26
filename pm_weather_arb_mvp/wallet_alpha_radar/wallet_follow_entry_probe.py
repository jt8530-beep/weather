#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Probe whether delayed copy entries are still profitable on mark-to-mid.

No wallet, no signing, no orders.

Difference from wallet_alpha_decay_probe.py:
- decay_probe asks: after a watched wallet trade, did token mid move in the wallet's favor?
- follow_entry_probe asks: if WE enter at T+delay at the available ask, is there still positive mark-to-mid after a holding period?

This is still not settlement PnL. It is an early copyability test.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import List, Optional


def fnum(x, default=0.0) -> float:
    try:
        if x in (None, ""):
            return default
        return float(x)
    except Exception:
        return default


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


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


def write_csv(path: Path, rows: List[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def summarize(rows: List[dict]) -> List[dict]:
    groups = defaultdict(list)
    for r in rows:
        groups[(r["wallet"], r["delay_sec"], r["hold_sec"])].append(r)
    out = []
    for (wallet, delay, hold), rs in groups.items():
        usable = [r for r in rs if r.get("usable") == "1"]
        copyable = [r for r in usable if r.get("copyable") == "1"]
        def avg(key, arr):
            return sum(fnum(r.get(key)) for r in arr) / len(arr) if arr else 0.0
        pos = sum(1 for r in copyable if fnum(r.get("follow_pnl_mid")) > 0)
        out.append({
            "wallet": wallet,
            "delay_sec": delay,
            "hold_sec": hold,
            "rows": len(rs),
            "usable": len(usable),
            "copyable": len(copyable),
            "positive_rate": f"{(pos / len(copyable)) if copyable else 0.0:.6f}",
            "avg_follow_pnl_mid": f"{avg('follow_pnl_mid', copyable):.6f}",
            "avg_delay_ask_minus_wallet_price": f"{avg('delay_ask_minus_wallet_price', usable):.6f}",
            "avg_delay_spread": f"{avg('delay_spread', usable):.6f}",
        })
    out.sort(key=lambda r: (r["wallet"], int(r["delay_sec"]), int(r["hold_sec"])))
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live-db", default="paper_logs/wallet_alpha/wallet_live_trades.sqlite")
    p.add_argument("--orderbook-db", default="paper_logs/wallet_alpha/orderbook_snapshots.sqlite")
    p.add_argument("--wallets", default="")
    p.add_argument("--delays", default="60,300,900")
    p.add_argument("--holds", default="300,900,1800")
    p.add_argument("--tolerance-sec", type=int, default=45)
    p.add_argument("--max-price-worse", type=float, default=0.03)
    p.add_argument("--max-spread", type=float, default=0.08)
    p.add_argument("--min-ask-size", type=float, default=2.0)
    p.add_argument("--limit", type=int, default=500000)
    p.add_argument("--output", default="paper_logs/wallet_alpha/wallet_follow_entry_rows_top_clean.csv")
    p.add_argument("--summary-output", default="paper_logs/wallet_alpha/wallet_follow_entry_summary_top_clean.csv")
    args = p.parse_args()

    wallets_filter = [x.strip().lower() for x in args.wallets.split(",") if x.strip()]
    delays = [int(x) for x in args.delays.split(",") if x.strip()]
    holds = [int(x) for x in args.holds.split(",") if x.strip()]
    tolerance_ms = args.tolerance_sec * 1000

    live = sqlite3.connect(args.live_db)
    live.row_factory = sqlite3.Row
    cols = table_columns(live, "wallet_live_trades")
    if "token_id" not in cols:
        print("WALLET_FOLLOW_ENTRY_SUMMARY rows=0 usable=0 reason=no_token_id_column")
        return 0
    live_rows = list(live.execute(
        """
        SELECT unique_id, seen_ts_ms, wallet, wallet_gate, wallet_score, token_id,
               market_key, side, price, size, notional, category_enriched, event_title, question
        FROM wallet_live_trades
        WHERE token_id IS NOT NULL AND token_id != ''
        ORDER BY seen_ts_ms ASC
        LIMIT ?
        """,
        (args.limit,),
    ))
    live.close()

    ob = sqlite3.connect(args.orderbook_db)
    rows = []
    for tr in live_rows:
        wallet = str(tr["wallet"]).lower()
        if wallets_filter and not any(wallet.startswith(w) for w in wallets_filter):
            continue
        token_id = str(tr["token_id"] or "")
        if not token_id:
            continue
        wallet_price = fnum(tr["price"])
        base_ts = int(tr["seen_ts_ms"])
        for delay in delays:
            delay_ts = base_ts + delay * 1000
            entry = get_snap(ob, token_id, delay_ts, tolerance_ms)
            for hold in holds:
                exit_ts = delay_ts + hold * 1000
                exit_snap = get_snap(ob, token_id, exit_ts, tolerance_ms)
                usable = entry is not None and exit_snap is not None and entry.get("ask") is not None and exit_snap.get("mid") is not None
                delay_ask = fnum(entry.get("ask")) if entry else 0.0
                delay_spread = fnum(entry.get("spread")) if entry else 0.0
                delay_ask_size = fnum(entry.get("ask_size")) if entry else 0.0
                pnl_mid = fnum(exit_snap.get("mid")) - delay_ask if usable else 0.0
                worse = delay_ask - wallet_price if usable else 0.0
                copyable = usable and worse <= args.max_price_worse and delay_spread <= args.max_spread and delay_ask_size >= args.min_ask_size
                rows.append({
                    "wallet": wallet,
                    "wallet_gate": tr["wallet_gate"],
                    "unique_id": tr["unique_id"],
                    "token_id": token_id,
                    "delay_sec": delay,
                    "hold_sec": hold,
                    "wallet_price": f"{wallet_price:.6f}",
                    "delay_ask": f"{delay_ask:.6f}" if usable else "",
                    "exit_mid": f"{fnum(exit_snap.get('mid')):.6f}" if exit_snap and exit_snap.get("mid") is not None else "",
                    "follow_pnl_mid": f"{pnl_mid:.6f}",
                    "delay_ask_minus_wallet_price": f"{worse:.6f}",
                    "delay_spread": f"{delay_spread:.6f}" if entry else "",
                    "delay_ask_size": f"{delay_ask_size:.6f}" if entry else "",
                    "usable": "1" if usable else "0",
                    "copyable": "1" if copyable else "0",
                    "category_enriched": tr["category_enriched"],
                    "event_title": tr["event_title"] or "",
                    "question": tr["question"] or "",
                })
    ob.close()

    write_csv(Path(args.output), rows)
    summary = summarize(rows)
    write_csv(Path(args.summary_output), summary)
    usable = sum(1 for r in rows if r["usable"] == "1")
    copyable = sum(1 for r in rows if r["copyable"] == "1")
    print(f"WALLET_FOLLOW_ENTRY_SUMMARY rows={len(rows)} usable={usable} copyable={copyable} wallets={len(set(r['wallet'] for r in rows))}")
    for r in summary[:80]:
        print(
            f"WALLET_FOLLOW_ENTRY wallet={r['wallet']} delay={r['delay_sec']} hold={r['hold_sec']} rows={r['rows']} usable={r['usable']} copyable={r['copyable']} "
            f"pos={r['positive_rate']} avg_pnl={r['avg_follow_pnl_mid']} avg_worse={r['avg_delay_ask_minus_wallet_price']} spread={r['avg_delay_spread']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
