#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Settle crypto_15m_prob_scanner paper signals with Binance proxy data.

Paper-only. No wallet, no signing, no orders.

Reads paper_logs/crypto_15m_signals.csv and marks each signal as win/loss once
its 15m window is complete. This uses Binance 1m klines as a proxy settlement
source. Polymarket's official resolution source may differ; this script is for
model validation, not final accounting.

Two aggregation modes are reported:
1. all_signals: every row in the CSV.
2. first_per_market: first signal per asset + market_window_ts + action.
   This avoids counting repeated every-minute observations as separate trades.
"""

from __future__ import annotations

import argparse
import csv
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests


ASSET_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
}


@dataclass
class SettledSignal:
    ts_ms: int
    asset: str
    symbol: str
    action: str
    side: str
    market_slug: str
    market_window_ts: int
    entry_ask: float
    model_prob: float
    edge: float
    start_price_signal: float
    proxy_open: float
    proxy_close: float
    proxy_return: float
    outcome_up: bool
    win: bool
    pnl_per_share: float
    pnl_usd: float
    stake_usd: float
    elapsed_sec: int
    remaining_sec: int
    reason: str


def fnum(x, default=0.0) -> float:
    try:
        if x in (None, "", "nan", "NaN"):
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


def write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def binance_klines(symbol: str, start_ms: int, end_ms: int, timeout: float = 10.0) -> List[dict]:
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": "1m",
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 1000,
    }
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    rows = r.json()
    out = []
    for x in rows:
        out.append({
            "open_time": int(x[0]),
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4]),
            "close_time": int(x[6]),
        })
    return out


def proxy_window_prices(symbol: str, window_ts: int, cache: Dict[Tuple[str, int], Tuple[float, float]]) -> Optional[Tuple[float, float]]:
    key = (symbol, window_ts)
    if key in cache:
        return cache[key]
    start_ms = window_ts * 1000
    end_ms = (window_ts + 15 * 60) * 1000
    rows = binance_klines(symbol, start_ms, end_ms)
    if not rows:
        return None
    # Prefer exact 15 candles. First candle open and last available close.
    open_px = float(rows[0]["open"])
    close_px = float(rows[-1]["close"])
    cache[key] = (open_px, close_px)
    return cache[key]


def settle_row(row: dict, stake_usd: float, now_ms: int, cache: Dict[Tuple[str, int], Tuple[float, float]]) -> Optional[SettledSignal]:
    asset = str(row.get("asset") or "").upper()
    symbol = str(row.get("symbol") or ASSET_SYMBOLS.get(asset, ""))
    if not asset or symbol not in ASSET_SYMBOLS.values():
        symbol = ASSET_SYMBOLS.get(asset, symbol)
    if not symbol:
        return None
    window_ts = inum(row.get("market_window_ts"))
    if not window_ts:
        # Backward compatibility for old rows with only slug.
        slug = str(row.get("market_slug") or row.get("slug") or "")
        try:
            window_ts = int(slug.rsplit("-", 1)[-1])
        except Exception:
            return None
    # Only settle after the 15m window is complete plus a small buffer.
    if now_ms < (window_ts + 15 * 60 + 20) * 1000:
        return None
    prices = proxy_window_prices(symbol, window_ts, cache)
    if not prices:
        return None
    open_px, close_px = prices
    outcome_up = close_px > open_px
    action = str(row.get("action") or "").upper()
    side = str(row.get("side") or "").upper()
    ask = fnum(row.get("ask"))
    model_prob = fnum(row.get("model_prob"))
    edge = fnum(row.get("edge"))
    if ask <= 0:
        return None
    if action == "BUY_YES" or side == "YES":
        win = outcome_up
    elif action == "BUY_NO" or side == "NO":
        win = not outcome_up
    else:
        return None
    # If spending stake_usd at ask, shares = stake / ask. PnL: winning shares pay $1.
    shares = stake_usd / ask
    pnl = shares * (1.0 - ask) if win else -stake_usd
    pnl_per_share = (1.0 - ask) if win else -ask
    return SettledSignal(
        ts_ms=inum(row.get("ts_ms")),
        asset=asset,
        symbol=symbol,
        action=action,
        side=side,
        market_slug=str(row.get("market_slug") or row.get("slug") or ""),
        market_window_ts=window_ts,
        entry_ask=ask,
        model_prob=model_prob,
        edge=edge,
        start_price_signal=fnum(row.get("start_price")),
        proxy_open=open_px,
        proxy_close=close_px,
        proxy_return=(close_px / open_px - 1.0) if open_px > 0 else 0.0,
        outcome_up=outcome_up,
        win=win,
        pnl_per_share=pnl_per_share,
        pnl_usd=pnl,
        stake_usd=stake_usd,
        elapsed_sec=inum(row.get("elapsed_sec")),
        remaining_sec=inum(row.get("remaining_sec")),
        reason="binance_proxy_settlement",
    )


def dedupe_first_per_market(rows: List[SettledSignal]) -> List[SettledSignal]:
    out: Dict[Tuple[str, int, str], SettledSignal] = {}
    for r in sorted(rows, key=lambda x: x.ts_ms):
        key = (r.asset, r.market_window_ts, r.action)
        if key not in out:
            out[key] = r
    return list(out.values())


def summarize(label: str, rows: List[SettledSignal]) -> dict:
    n = len(rows)
    if n == 0:
        return {
            "label": label,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "pnl_usd": 0.0,
            "avg_pnl": 0.0,
            "avg_edge": 0.0,
            "max_drawdown": 0.0,
        }
    wins = sum(1 for r in rows if r.win)
    pnl = sum(r.pnl_usd for r in rows)
    avg_edge = sum(r.edge for r in rows) / n
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in sorted(rows, key=lambda x: x.ts_ms):
        eq += r.pnl_usd
        peak = max(peak, eq)
        max_dd = min(max_dd, eq - peak)
    return {
        "label": label,
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": wins / n,
        "pnl_usd": pnl,
        "avg_pnl": pnl / n,
        "avg_edge": avg_edge,
        "max_drawdown": max_dd,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--signals", default="paper_logs/crypto_15m_signals.csv")
    p.add_argument("--output", default="paper_logs/crypto_15m_settled.csv")
    p.add_argument("--stake-usd", type=float, default=2.0)
    p.add_argument("--min-edge", type=float, default=0.0)
    p.add_argument("--drop-old-bug-rows", action="store_true", help="drop rows without reason ending current_window or elapsed<60")
    args = p.parse_args()

    raw = read_csv(Path(args.signals))
    now_ms = int(time.time() * 1000)
    cache: Dict[Tuple[str, int], Tuple[float, float]] = {}
    settled: List[SettledSignal] = []
    skipped = {"unsettled_or_bad": 0, "edge_filter": 0, "old_bug_row": 0}
    for row in raw:
        if fnum(row.get("edge")) < args.min_edge:
            skipped["edge_filter"] += 1
            continue
        if args.drop_old_bug_rows:
            reason = str(row.get("reason") or "")
            elapsed = inum(row.get("elapsed_sec"))
            if not reason.endswith("current_window") or elapsed < 60:
                skipped["old_bug_row"] += 1
                continue
        s = settle_row(row, args.stake_usd, now_ms, cache)
        if not s:
            skipped["unsettled_or_bad"] += 1
            continue
        settled.append(s)
    settled.sort(key=lambda x: x.ts_ms)
    write_csv(Path(args.output), [asdict(x) for x in settled])
    first = dedupe_first_per_market(settled)
    s_all = summarize("all_signals", settled)
    s_first = summarize("first_per_asset_window_action", first)
    print(
        "CRYPTO15_SETTLEMENT_SUMMARY "
        f"raw={len(raw)} settled={len(settled)} first_dedup={len(first)} "
        f"skipped={skipped} "
        f"all_win_rate={s_all['win_rate']:.4f} all_pnl={s_all['pnl_usd']:.4f} all_avg_pnl={s_all['avg_pnl']:.4f} "
        f"first_win_rate={s_first['win_rate']:.4f} first_pnl={s_first['pnl_usd']:.4f} first_avg_pnl={s_first['avg_pnl']:.4f} "
        f"first_max_dd={s_first['max_drawdown']:.4f} avg_edge={s_first['avg_edge']:.4f}"
    )
    # Asset-level summary for first-dedup mode.
    for asset in sorted({r.asset for r in first}):
        rows = [r for r in first if r.asset == asset]
        ss = summarize(asset, rows)
        print(
            f"CRYPTO15_SETTLEMENT_ASSET asset={asset} trades={ss['trades']} win_rate={ss['win_rate']:.4f} "
            f"pnl={ss['pnl_usd']:.4f} avg_pnl={ss['avg_pnl']:.4f} avg_edge={ss['avg_edge']:.4f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
