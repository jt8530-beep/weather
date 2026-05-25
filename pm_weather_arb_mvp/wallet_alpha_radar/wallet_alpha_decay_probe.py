#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Probe post-trade price movement for selected wallet live trades.

No wallet, no signing, no orders.

This is an early alpha-decay probe. It joins captured wallet live trades with
recorded orderbook snapshots and estimates whether price moved in the wallet's
favor after T+delay.

Important limitations:
- This is only as good as the orderbook recorder coverage.
- Trade side normalization can be imperfect because public API fields vary.
- It is a research probe, not a copy-trading signal.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


def fnum(x, default=0.0) -> float:
    try:
        if x in (None, ""):
            return default
        return float(x)
    except Exception:
        return default


def norm_side(side: str) -> str:
    s = str(side or "").upper()
    if "YES" in s or s in {"BUY", "LONG"}:
        return "YES"
    if "NO" in s:
        return "NO"
    return s[:16] or "UNKNOWN"


def mid_from_book(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None:
        return None
    try:
        return (float(bid) + float(ask)) / 2.0
    except Exception:
        return None


def get_snapshot(conn: sqlite3.Connection, market_key: str, outcome: str, target_ts_ms: int, tolerance_ms: int) -> Optional[dict]:
    # Try exact market_id first. Historical recorder stores market_id, while live trades may contain condition_id.
    rows = conn.execute(
        """
        SELECT ts_ms, market_id, token_id, outcome, best_bid, best_ask, bid_size, ask_size, spread
        FROM orderbook_snapshots
        WHERE lower(market_id)=lower(?) AND outcome=? AND ts_ms >= ? AND ts_ms <= ?
        ORDER BY ABS(ts_ms - ?) ASC
        LIMIT 1
        """,
        (market_key, outcome, target_ts_ms - tolerance_ms, target_ts_ms + tolerance_ms, target_ts_ms),
    ).fetchall()
    if not rows:
        return None
    r = rows[0]
    bid = r[4]
    ask = r[5]
    return {
        "ts_ms": r[0],
        "market_id": r[1],
        "token_id": r[2],
        "outcome": r[3],
        "best_bid": bid,
        "best_ask": ask,
        "mid": mid_from_book(bid, ask),
        "bid_size": r[6],
        "ask_size": r[7],
        "spread": r[8],
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
        groups[(r["wallet"], r["delay_sec"])].append(r)
    out = []
    for (wallet, delay), rs in groups.items():
        usable = [r for r in rs if r.get("usable") == "1"]
        if not usable:
            out.append({
                "wallet": wallet,
                "delay_sec": delay,
                "trades": len(rs),
                "usable": 0,
                "same_direction_rate": "0.000000",
                "avg_mid_move": "0.000000",
                "avg_edge_to_entry": "0.000000",
            })
            continue
        same = sum(1 for r in usable if fnum(r.get("signed_mid_move")) > 0)
        avg_move = sum(fnum(r.get("signed_mid_move")) for r in usable) / len(usable)
        avg_edge_to_entry = sum(fnum(r.get("follow_mid_minus_entry_signed")) for r in usable) / len(usable)
        out.append({
            "wallet": wallet,
            "delay_sec": delay,
            "trades": len(rs),
            "usable": len(usable),
            "same_direction_rate": f"{same / len(usable):.6f}",
            "avg_mid_move": f"{avg_move:.6f}",
            "avg_edge_to_entry": f"{avg_edge_to_entry:.6f}",
        })
    out.sort(key=lambda r: (r["wallet"], int(r["delay_sec"])))
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live-db", default="paper_logs/wallet_alpha/wallet_live_trades.sqlite")
    p.add_argument("--orderbook-db", default="paper_logs/wallet_alpha/orderbook_snapshots.sqlite")
    p.add_argument("--wallets", default="", help="comma-separated wallet prefixes or full addresses; empty means all")
    p.add_argument("--delays", default="60,300,900")
    p.add_argument("--tolerance-sec", type=int, default=45)
    p.add_argument("--output", default="paper_logs/wallet_alpha/wallet_alpha_decay_rows.csv")
    p.add_argument("--summary-output", default="paper_logs/wallet_alpha/wallet_alpha_decay_summary.csv")
    p.add_argument("--limit", type=int, default=5000)
    args = p.parse_args()

    wallets_filter = [x.strip().lower() for x in args.wallets.split(",") if x.strip()]
    delays = [int(x.strip()) for x in args.delays.split(",") if x.strip()]
    tolerance_ms = args.tolerance_sec * 1000

    live = sqlite3.connect(args.live_db)
    live.row_factory = sqlite3.Row
    ob = sqlite3.connect(args.orderbook_db)

    live_rows = list(live.execute(
        """
        SELECT unique_id, seen_ts_ms, wallet, wallet_gate, wallet_score, trade_ts, market_key,
               side, price, size, notional, category_enriched, event_title, market_slug, question
        FROM wallet_live_trades
        ORDER BY seen_ts_ms ASC
        LIMIT ?
        """,
        (args.limit,),
    ))
    live.close()

    detail_rows: List[dict] = []
    for tr in live_rows:
        wallet = str(tr["wallet"]).lower()
        if wallets_filter and not any(wallet.startswith(w) for w in wallets_filter):
            continue
        market_key = str(tr["market_key"] or "").lower()
        if not market_key:
            continue
        outcome = norm_side(tr["side"])
        if outcome not in {"YES", "NO"}:
            continue
        entry_ts = int(tr["seen_ts_ms"])
        entry_price = fnum(tr["price"])
        entry_snap = get_snapshot(ob, market_key, outcome, entry_ts, tolerance_ms)
        entry_mid = entry_snap["mid"] if entry_snap else None
        for delay in delays:
            follow_ts = entry_ts + delay * 1000
            follow_snap = get_snapshot(ob, market_key, outcome, follow_ts, tolerance_ms)
            usable = entry_mid is not None and follow_snap is not None and follow_snap.get("mid") is not None
            signed_move = 0.0
            edge_to_entry = 0.0
            if usable:
                # For a long YES/NO token, favorable movement is token mid going up.
                signed_move = fnum(follow_snap["mid"]) - fnum(entry_mid)
                edge_to_entry = fnum(follow_snap["mid"]) - entry_price
            detail_rows.append({
                "wallet": wallet,
                "wallet_gate": tr["wallet_gate"],
                "wallet_score": tr["wallet_score"],
                "unique_id": tr["unique_id"],
                "market_key": market_key,
                "category_enriched": tr["category_enriched"],
                "outcome": outcome,
                "entry_seen_ts_ms": entry_ts,
                "delay_sec": delay,
                "entry_price": f"{entry_price:.6f}",
                "entry_mid": f"{fnum(entry_mid):.6f}" if entry_mid is not None else "",
                "follow_mid": f"{fnum(follow_snap.get('mid')):.6f}" if follow_snap and follow_snap.get("mid") is not None else "",
                "signed_mid_move": f"{signed_move:.6f}",
                "follow_mid_minus_entry_signed": f"{edge_to_entry:.6f}",
                "usable": "1" if usable else "0",
                "event_title": tr["event_title"] or "",
                "question": tr["question"] or "",
            })

    ob.close()
    write_csv(Path(args.output), detail_rows)
    summary = summarize(detail_rows)
    write_csv(Path(args.summary_output), summary)

    total = len(detail_rows)
    usable = sum(1 for r in detail_rows if r.get("usable") == "1")
    print(f"WALLET_ALPHA_DECAY_SUMMARY rows={total} usable={usable} wallets={len(set(r['wallet'] for r in detail_rows))} delays={args.delays}")
    for r in summary[:50]:
        print(
            f"WALLET_ALPHA_DECAY wallet={r['wallet']} delay={r['delay_sec']} trades={r['trades']} usable={r['usable']} "
            f"same_dir={r['same_direction_rate']} avg_move={r['avg_mid_move']} avg_edge_entry={r['avg_edge_to_entry']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
