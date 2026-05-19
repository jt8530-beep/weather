#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paper settlement tracker for weather scanner CSV output.

This tool joins scanner candidates with a manually supplied final temperature CSV.
It is deliberately offline/manual because each market's official station and
resolution rule must be verified before using a result as ground truth.

Candidates CSV expected columns include:
city,target_date,temp_type,lower_f,upper_f,side,price,edge

Final results CSV columns:
city,target_date,temp_type,final_temp_f,source,notes
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def fnum(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def key(row: Dict[str, str]) -> Tuple[str, str, str]:
    return (row.get("city", "").strip().lower(), row.get("target_date", "").strip(), row.get("temp_type", "high").strip().lower())


def in_range(temp: float, lower: Optional[float], upper: Optional[float]) -> bool:
    if lower is not None and temp < lower:
        return False
    if upper is not None and temp >= upper:
        return False
    return True


def read_csv(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: str, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys())
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def settle(candidates_path: str, results_path: str, out_path: str) -> None:
    candidates = read_csv(candidates_path)
    finals = {key(row): row for row in read_csv(results_path)}
    settled: List[Dict[str, Any]] = []
    total_pnl = 0.0
    wins = 0
    losses = 0
    pending = 0
    for row in candidates:
        result = finals.get(key(row))
        out = dict(row)
        if not result:
            out.update({"settled": "false", "final_temp_f": "", "win": "", "paper_pnl_per_1usd": "", "result_source": "", "result_notes": "missing final result"})
            pending += 1
            settled.append(out)
            continue
        final_temp = float(result["final_temp_f"])
        lower = fnum(row.get("lower_f"))
        upper = fnum(row.get("upper_f"))
        yes_wins = in_range(final_temp, lower, upper)
        side = row.get("side", row.get("side_label", "YES")).strip().upper()
        if side in ("YES", "YES_RANGE"):
            win = yes_wins
        else:
            win = not yes_wins
        price = float(row.get("price") or row.get("public_price") or 0.0)
        pnl = (1.0 - price) if win else -price
        total_pnl += pnl
        wins += 1 if win else 0
        losses += 0 if win else 1
        out.update({
            "settled": "true",
            "final_temp_f": final_temp,
            "win": "true" if win else "false",
            "paper_pnl_per_1usd": round(pnl, 4),
            "result_source": result.get("source", "manual"),
            "result_notes": result.get("notes", ""),
        })
        settled.append(out)
    write_csv(out_path, settled)
    done = wins + losses
    win_rate = wins / done if done else 0.0
    print(f"settled={done} pending={pending} wins={wins} losses={losses} win_rate={win_rate:.3f} pnl_per_1usd_sum={total_pnl:.4f} saved={out_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", default="paper_logs/live_candidates.csv")
    parser.add_argument("--results", default="final_results_example.csv")
    parser.add_argument("--out", default="paper_logs/settled_candidates.csv")
    args = parser.parse_args()
    settle(args.candidates, args.results, args.out)


if __name__ == "__main__":
    main()
