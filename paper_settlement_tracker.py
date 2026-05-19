#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paper settlement tracker for weather scanner CSV output.

Two modes:
1. Manual final results CSV.
2. Optional Open-Meteo Archive fallback using station_id/station columns from live candidates.

This remains paper-only. Official Polymarket settlement rules still control final
truth, so archive fallback should be treated as research data until verified.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


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
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8-sig", newline="") as f:
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


def archive_final_temp(row: Dict[str, str]) -> Optional[Dict[str, str]]:
    lat = row.get("latitude") or row.get("station_latitude")
    lon = row.get("longitude") or row.get("station_longitude")
    target_date = row.get("target_date", "")
    temp_type = row.get("temp_type", "high").strip().lower()
    timezone = row.get("timezone") or "UTC"
    if not lat or not lon or not target_date:
        return None
    try:
        params = {
            "latitude": float(lat),
            "longitude": float(lon),
            "start_date": target_date,
            "end_date": target_date,
            "daily": "temperature_2m_max,temperature_2m_min",
            "temperature_unit": "fahrenheit",
            "timezone": timezone,
        }
        response = requests.get("https://archive-api.open-meteo.com/v1/archive", params=params, timeout=15, headers={"User-Agent": "weather-paper-settlement/0.1"})
        response.raise_for_status()
        data = response.json()
        daily = data.get("daily", {})
        values = daily.get("temperature_2m_min" if temp_type == "low" else "temperature_2m_max", [])
        if not values:
            return None
        return {
            "city": row.get("city", ""),
            "target_date": target_date,
            "temp_type": temp_type,
            "final_temp_f": str(float(values[0])),
            "source": "open_meteo_archive_fallback",
            "notes": "research fallback; verify against official resolution source",
        }
    except Exception:
        return None


def result_for_row(row: Dict[str, str], finals: Dict[Tuple[str, str, str], Dict[str, str]], use_archive: bool) -> Optional[Dict[str, str]]:
    result = finals.get(key(row))
    if result:
        return result
    if use_archive:
        return archive_final_temp(row)
    return None


def settle(candidates_path: str, results_path: str, out_path: str, use_archive: bool = False) -> None:
    candidates = read_csv(candidates_path)
    finals = {key(row): row for row in read_csv(results_path)}
    settled: List[Dict[str, Any]] = []
    total_pnl = 0.0
    wins = 0
    losses = 0
    pending = 0
    for row in candidates:
        result = result_for_row(row, finals, use_archive)
        out = dict(row)
        if not result:
            out.update({"settled": "false", "final_temp_f": "", "win": "", "paper_pnl_per_1usd": "", "result_source": "", "result_notes": "missing final result"})
            pending += 1
            settled.append(out)
            continue
        final_temp = float(result["final_temp_f"])
        lower = fnum(row.get("lower_f"))
        upper = fnum(row.get("upper_f"))
        in_bucket = in_range(final_temp, lower, upper)
        side = row.get("side", row.get("side_label", row.get("outcome", "IN_RANGE"))).strip().upper()
        if side in ("YES", "YES_RANGE", "IN_RANGE"):
            win = in_bucket
        else:
            win = not in_bucket
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
    parser.add_argument("--use-archive", action="store_true", help="Use Open-Meteo Archive fallback when manual final result is missing.")
    args = parser.parse_args()
    settle(args.candidates, args.results, args.out, args.use_archive)


if __name__ == "__main__":
    main()
