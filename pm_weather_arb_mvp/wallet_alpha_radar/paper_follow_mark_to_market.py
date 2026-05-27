#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mark paper-follow entries to market.

No wallet, no signing, no real orders.

Reads paper_follow_entries.csv and orderbook snapshots, then writes:
- paper_follow_marks.csv: per-entry T+900 / T+1800 mark rows
- paper_follow_summary.csv: aggregate PnL summary

This is the proof layer for AUTO_PAPER_FOLLOW. Signals are useless unless their
paper entries produce positive mark-to-mid / mark-to-bid after realistic holds.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import time
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


def get_snap(conn: sqlite3.Connection, token_id: str, target_ts_ms: int, tolerance_ms: int) -> Optional[dict]:
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
        (token_id, target_ts_ms - tolerance_ms, target_ts_ms + tolerance_ms, target_ts_ms),
    ).fetchall()
    if not rows:
        return None
    r = rows[0]
    bid = fnum(r[3])
    ask = fnum(r[4])
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
    return {
        "snap_ts_ms": r[0],
        "token_id": r[1],
        "outcome": r[2],
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "bid_size": fnum(r[5]),
        "ask_size": fnum(r[6]),
        "spread": fnum(r[7]),
    }


def summarize(marks: List[dict]) -> List[dict]:
    groups = defaultdict(list)
    for r in marks:
        if r.get("mark_available") != "1":
            continue
        groups[(r.get("source", ""), r.get("hold_sec", ""))].append(r)
        groups[("ALL", r.get("hold_sec", ""))].append(r)
    out = []
    for (source, hold), rs in sorted(groups.items()):
        mid_pnls = [fnum(r["pnl_mid"] ) for r in rs]
        bid_pnls = [fnum(r["pnl_bid"] ) for r in rs]
        wins_mid = sum(1 for x in mid_pnls if x > 0)
        wins_bid = sum(1 for x in bid_pnls if x > 0)
        out.append({
            "source": source,
            "hold_sec": hold,
            "marks": len(rs),
            "mid_win_rate": f"{wins_mid / len(rs):.6f}" if rs else "0.000000",
            "bid_win_rate": f"{wins_bid / len(rs):.6f}" if rs else "0.000000",
            "pnl_mid_total": f"{sum(mid_pnls):.6f}",
            "pnl_bid_total": f"{sum(bid_pnls):.6f}",
            "avg_pnl_mid": f"{sum(mid_pnls) / len(rs):.6f}" if rs else "0.000000",
            "avg_pnl_bid": f"{sum(bid_pnls) / len(rs):.6f}" if rs else "0.000000",
        })
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--entries", default="paper_logs/wallet_alpha/paper_follow_entries.csv")
    p.add_argument("--orderbook-db", default="paper_logs/wallet_alpha/orderbook_snapshots.sqlite")
    p.add_argument("--marks", default="paper_logs/wallet_alpha/paper_follow_marks.csv")
    p.add_argument("--summary", default="paper_logs/wallet_alpha/paper_follow_summary.csv")
    p.add_argument("--holds", default="900,1800")
    p.add_argument("--tolerance-sec", type=int, default=90)
    p.add_argument("--only-open", action="store_true")
    args = p.parse_args()

    entries = read_csv(args.entries)
    holds = [int(x) for x in args.holds.split(",") if x.strip()]
    tolerance_ms = args.tolerance_sec * 1000
    now_ms = int(time.time() * 1000)

    conn = sqlite3.connect(args.orderbook_db)
    marks = []
    pending = 0
    unavailable = 0
    for e in entries:
        if args.only_open and e.get("status") not in {"OPEN", ""}:
            continue
        entry_ts = inum(e.get("entry_ts_ms"))
        token_id = e.get("token_id", "")
        entry_ask = fnum(e.get("entry_ask"))
        shares = fnum(e.get("paper_shares"))
        for hold in holds:
            target_ts = entry_ts + hold * 1000
            if now_ms < target_ts - tolerance_ms:
                pending += 1
                continue
            snap = get_snap(conn, token_id, target_ts, tolerance_ms)
            if not snap:
                unavailable += 1
                marks.append({
                    "trade_day": e.get("trade_day", ""),
                    "wallet": e.get("wallet", ""),
                    "source": e.get("source", ""),
                    "token_id": token_id,
                    "entry_ts_ms": entry_ts,
                    "hold_sec": hold,
                    "target_ts_ms": target_ts,
                    "mark_available": "0",
                    "mark_ts_ms": "",
                    "entry_ask": f"{entry_ask:.6f}",
                    "mark_mid": "",
                    "mark_bid": "",
                    "mark_ask": "",
                    "pnl_mid": "0.000000",
                    "pnl_bid": "0.000000",
                    "pnl_mid_dollars": "0.000000",
                    "pnl_bid_dollars": "0.000000",
                    "spread": "",
                    "reason": "no_snapshot",
                })
                continue
            pnl_mid = snap["mid"] - entry_ask
            pnl_bid = snap["bid"] - entry_ask
            marks.append({
                "trade_day": e.get("trade_day", ""),
                "wallet": e.get("wallet", ""),
                "source": e.get("source", ""),
                "token_id": token_id,
                "entry_ts_ms": entry_ts,
                "hold_sec": hold,
                "target_ts_ms": target_ts,
                "mark_available": "1",
                "mark_ts_ms": snap["snap_ts_ms"],
                "entry_ask": f"{entry_ask:.6f}",
                "mark_mid": f"{snap['mid']:.6f}",
                "mark_bid": f"{snap['bid']:.6f}",
                "mark_ask": f"{snap['ask']:.6f}",
                "pnl_mid": f"{pnl_mid:.6f}",
                "pnl_bid": f"{pnl_bid:.6f}",
                "pnl_mid_dollars": f"{pnl_mid * shares:.6f}",
                "pnl_bid_dollars": f"{pnl_bid * shares:.6f}",
                "spread": f"{snap['spread']:.6f}",
                "reason": "marked",
            })
    conn.close()

    mark_fields = [
        "trade_day", "wallet", "source", "token_id", "entry_ts_ms", "hold_sec", "target_ts_ms",
        "mark_available", "mark_ts_ms", "entry_ask", "mark_mid", "mark_bid", "mark_ask",
        "pnl_mid", "pnl_bid", "pnl_mid_dollars", "pnl_bid_dollars", "spread", "reason"
    ]
    write_csv(args.marks, marks, mark_fields)
    summary = summarize(marks)
    summary_fields = ["source", "hold_sec", "marks", "mid_win_rate", "bid_win_rate", "pnl_mid_total", "pnl_bid_total", "avg_pnl_mid", "avg_pnl_bid"]
    write_csv(args.summary, summary, summary_fields)

    print(
        f"PAPER_FOLLOW_MARK_SUMMARY entries={len(entries)} marks={len(marks)} pending={pending} unavailable={unavailable} "
        f"available={sum(1 for r in marks if r.get('mark_available')=='1')} summary={args.summary}"
    )
    for r in summary:
        print(
            f"PAPER_FOLLOW_SUMMARY source={r['source']} hold={r['hold_sec']} marks={r['marks']} "
            f"mid_wr={r['mid_win_rate']} bid_wr={r['bid_win_rate']} mid_pnl={r['pnl_mid_total']} bid_pnl={r['pnl_bid_total']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
