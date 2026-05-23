#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strict live-readiness filter for auto_edge_scanner output.

This script is deliberately harsher than auto_edge_scanner.py.
The scanner finds candidates. This filter decides whether anything is close to
being worth real money.

Principles:
- No manual verification.
- No wallet, no signing, no orders.
- NegRisk is NOT live-ready unless it has strong machine-verifiable structure.
- Wide-spread maker candidates are NOT arbitrage; they are only paper-maker
  candidates unless repeated across snapshots.
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, List


LIVE_SAFE_HARD_KINDS = {
    "YES_NO_BUY_BOTH",
    "YES_NO_SPLIT_SELL_BOTH",
}
PAPER_ONLY_HARD_KINDS = {
    "THRESHOLD_NESTED_BUY_SUPER_YES_SUB_NO",
    "NEGRISK_BUY_ALL_YES",
    "NEGRISK_BUY_ALL_NO",
}


@dataclass
class Verdict:
    status: str
    reason: str
    live_ready: int
    paper_ready: int
    research_only: int


def read_csv(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fnum(x, default=0.0) -> float:
    try:
        if x in (None, "", "nan", "NaN"):
            return default
        return float(x)
    except Exception:
        return default


def bval(x) -> bool:
    return str(x).strip().lower() in {"true", "1", "yes", "y"}


def classify_hard_arbs(rows: List[dict], min_edge: float, max_notional: float) -> tuple[List[dict], List[dict], List[dict]]:
    live, paper, research = [], [], []
    for r in rows:
        kind = str(r.get("kind") or "")
        edge = fnum(r.get("edge_per_share"))
        cost = fnum(r.get("total_cost"))
        proceeds = fnum(r.get("total_proceeds"))
        notional = max(cost, proceeds)
        if edge < min_edge:
            continue
        if notional <= 0 or notional > max_notional:
            continue
        if kind in LIVE_SAFE_HARD_KINDS:
            rr = dict(r)
            rr["trade_ready_class"] = "LIVE_READY_HARD_ARB"
            live.append(rr)
        elif kind in PAPER_ONLY_HARD_KINDS:
            rr = dict(r)
            rr["trade_ready_class"] = "PAPER_ONLY_STRUCTURE_RISK"
            paper.append(rr)
        else:
            rr = dict(r)
            rr["trade_ready_class"] = "RESEARCH_UNKNOWN_KIND"
            research.append(rr)
    live.sort(key=lambda x: fnum(x.get("edge_per_share")), reverse=True)
    paper.sort(key=lambda x: fnum(x.get("edge_per_share")), reverse=True)
    return live, paper, research


def classify_negrisk_checks(rows: List[dict]) -> tuple[int, Counter]:
    reasons = Counter()
    passed = 0
    for r in rows:
        reason = str(r.get("reason") or "unknown")
        reasons[reason] += 1
        if bval(r.get("pass_auto")):
            passed += 1
    return passed, reasons


def classify_maker(rows: List[dict], min_repeats: int, min_edge: float, min_depth_hint: float, max_abs_parity_gap: float) -> tuple[List[dict], List[dict]]:
    # Group by market_id and require repeated observations. A single wide spread
    # snapshot is usually just stale/dust/liquidity trap, not a live signal.
    groups: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        groups[str(r.get("market_id") or "")].append(r)
    paper, research = [], []
    for market_id, items in groups.items():
        if not market_id:
            continue
        best = max(items, key=lambda x: fnum(x.get("best_maker_edge")))
        repeats = len(items)
        best_edge = fnum(best.get("best_maker_edge"))
        depth_hint = fnum(best.get("max_notional_hint"))
        parity_ask = abs(fnum(best.get("parity_gap_ask")))
        parity_bid = abs(fnum(best.get("parity_gap_bid")))
        mid = fnum(best.get("mid_yes"))
        bad_flags = str(best.get("risk_flags") or "")
        rr = dict(best)
        rr["repeat_count"] = str(repeats)
        if (
            repeats >= min_repeats
            and best_edge >= min_edge
            and depth_hint >= min_depth_hint
            and 0.10 <= mid <= 0.90
            and parity_ask <= max_abs_parity_gap
            and parity_bid <= max_abs_parity_gap
            and "bad_title" not in bad_flags
        ):
            rr["trade_ready_class"] = "PAPER_READY_MAKER_REPEAT"
            paper.append(rr)
        else:
            rr["trade_ready_class"] = "RESEARCH_MAKER_NOT_REPEAT_OR_PARITY_RISK"
            research.append(rr)
    paper.sort(key=lambda x: (fnum(x.get("best_maker_edge")), fnum(x.get("max_notional_hint"))), reverse=True)
    research.sort(key=lambda x: fnum(x.get("best_maker_edge")), reverse=True)
    return paper, research


def write_rows(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--paper-dir", default="paper_logs")
    p.add_argument("--min-hard-edge", type=float, default=0.01)
    p.add_argument("--max-hard-notional", type=float, default=10.0)
    p.add_argument("--maker-min-repeats", type=int, default=3)
    p.add_argument("--maker-min-edge", type=float, default=0.04)
    p.add_argument("--maker-min-depth-hint", type=float, default=20.0)
    p.add_argument("--maker-max-abs-parity-gap", type=float, default=0.08)
    p.add_argument("--top", type=int, default=10)
    args = p.parse_args()

    root = Path(args.paper_dir)
    hard_rows = read_csv(root / "auto_hard_arb_candidates.csv")
    maker_rows = read_csv(root / "auto_maker_candidates.csv")
    nr_rows = read_csv(root / "auto_negrisk_checks.csv")

    live_hard, paper_hard, research_hard = classify_hard_arbs(hard_rows, args.min_hard_edge, args.max_hard_notional)
    nr_passed, nr_reasons = classify_negrisk_checks(nr_rows)
    paper_maker, research_maker = classify_maker(
        maker_rows,
        args.maker_min_repeats,
        args.maker_min_edge,
        args.maker_min_depth_hint,
        args.maker_max_abs_parity_gap,
    )

    write_rows(root / "trade_ready_live_hard.csv", live_hard)
    write_rows(root / "trade_ready_paper_hard.csv", paper_hard)
    write_rows(root / "trade_ready_paper_maker.csv", paper_maker)

    if live_hard:
        status = "LIVE_CANDIDATE"
        reason = "has_live_safe_hard_arbs"
    elif paper_maker or paper_hard:
        status = "PAPER_ONLY"
        reason = "has_candidates_but_not_live_ready"
    else:
        status = "NO_TRADE"
        reason = "no_live_or_repeat_paper_candidates"

    best_live_edge = fnum(live_hard[0].get("edge_per_share")) if live_hard else float("-inf")
    best_paper_hard = fnum(paper_hard[0].get("edge_per_share")) if paper_hard else float("-inf")
    best_paper_maker = fnum(paper_maker[0].get("best_maker_edge")) if paper_maker else float("-inf")

    print(
        f"TRADE_READY_SUMMARY status={status} reason={reason} "
        f"live_hard={len(live_hard)} paper_hard={len(paper_hard)} paper_maker={len(paper_maker)} "
        f"research_hard={len(research_hard)} research_maker={len(research_maker)} "
        f"auto_negrisk_passed={nr_passed}/{len(nr_rows)} "
        f"best_live_hard_edge={best_live_edge:.4f} "
        f"best_paper_hard_edge={best_paper_hard:.4f} "
        f"best_paper_maker_edge={best_paper_maker:.4f}"
    )
    print("NEGRISK_REASONS " + ",".join(f"{k}:{v}" for k, v in nr_reasons.most_common(8)))
    for r in live_hard[: args.top]:
        print(
            f"LIVE_HARD kind={r.get('kind')} edge={r.get('edge_per_share')} "
            f"notional={max(fnum(r.get('total_cost')), fnum(r.get('total_proceeds'))):.2f} "
            f"event=\"{str(r.get('event_title') or '')[:90]}\""
        )
    for r in paper_maker[: args.top]:
        print(
            f"PAPER_MAKER edge={r.get('best_maker_edge')} repeats={r.get('repeat_count')} "
            f"depth_hint={r.get('max_notional_hint')} event=\"{str(r.get('event_title') or '')[:70]}\" "
            f"q=\"{str(r.get('question') or '')[:70]}\""
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
