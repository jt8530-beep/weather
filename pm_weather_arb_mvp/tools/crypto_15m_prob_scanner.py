#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified BTC/ETH/SOL 15-minute Polymarket probability paper scanner.

Paper-only. No wallet, no signing, no order submission.

It scans BTC, ETH, and SOL in one strategy/risk pool:
- discover active Polymarket crypto up/down markets through Gamma
- pull CLOB YES/NO books
- pull Binance 1m spot data
- estimate probability of the current 15m window closing UP vs window open
- compare model probability with Polymarket ask prices
- output ranked paper signals across BTC/ETH/SOL

This is not arbitrage. It is short-horizon probability trading research.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, Iterable, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from pm_weather_arb.clob import ClobPublicClient
from pm_weather_arb.config import Config
from pm_weather_arb.gamma import GammaClient, parse_markets_from_events
from pm_weather_arb.types import Market, OrderBook
from pm_weather_arb.util import first_present


ASSET_SPECS = {
    "BTC": {
        "binance": "BTCUSDT",
        "terms": ["bitcoin", "btc"],
        "search": ["Bitcoin Up or Down", "BTC Up or Down", "Bitcoin 15 minute", "BTC 15m"],
    },
    "ETH": {
        "binance": "ETHUSDT",
        "terms": ["ethereum", "ether", "eth"],
        "search": ["Ethereum Up or Down", "ETH Up or Down", "Ethereum 15 minute", "ETH 15m"],
    },
    "SOL": {
        "binance": "SOLUSDT",
        "terms": ["solana", "sol"],
        "search": ["Solana Up or Down", "SOL Up or Down", "Solana 15 minute", "SOL 15m"],
    },
}


@dataclass
class AssetState:
    asset: str
    symbol: str
    now_ms: int
    interval_start_ms: int
    interval_end_ms: int
    elapsed_sec: int
    remaining_sec: int
    start_price: float
    last_price: float
    r_now: float
    sigma_min: float
    mu_min: float
    p_norm_up: float
    p_emp_up: float
    p_up: float
    samples: int


@dataclass
class CryptoSignal:
    ts_ms: int
    asset: str
    symbol: str
    event_id: str
    event_title: str
    market_id: str
    question: str
    side: str
    action: str
    ask: float
    bid: float
    spread: float
    ask_size: float
    bid_size: float
    model_prob: float
    edge: float
    start_price: float
    last_price: float
    r_now: float
    elapsed_sec: int
    remaining_sec: int
    p_up: float
    p_down: float
    reason: str


def utc_ms() -> int:
    return int(time.time() * 1000)


def iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def log_return(a: float, b: float) -> float:
    if a <= 0 or b <= 0:
        return 0.0
    return math.log(b / a)


def binance_klines(symbol: str, limit: int = 1000, timeout: float = 10.0) -> List[dict]:
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": "1m", "limit": int(limit)}
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
            "volume": float(x[5]),
            "close_time": int(x[6]),
        })
    return out


def current_15m_window(now_ms: int) -> Tuple[int, int]:
    interval = 15 * 60 * 1000
    start = (now_ms // interval) * interval
    return start, start + interval


def find_start_price(klines: List[dict], start_ms: int) -> Optional[float]:
    for k in klines:
        if int(k["open_time"]) == start_ms:
            return float(k["open"])
    after = [k for k in klines if int(k["open_time"]) >= start_ms]
    if after:
        return float(after[0]["open"])
    return None


def one_min_returns(klines: List[dict]) -> List[float]:
    out = []
    for i in range(1, len(klines)):
        out.append(log_return(float(klines[i - 1]["close"]), float(klines[i]["close"])))
    return out


def empirical_prob_up(klines: List[dict], elapsed_min: int, r_now: float, sigma_min: float) -> Tuple[float, int]:
    if elapsed_min <= 0:
        elapsed_min = 1
    if elapsed_min >= 15:
        elapsed_min = 14
    by_time = {int(k["open_time"]): k for k in klines}
    times = sorted(by_time)
    vals = []
    weights = []
    bandwidth = max(sigma_min * math.sqrt(max(1, elapsed_min)) * 2.0, 0.0008)
    for t in times:
        if (t // 60000) % 15 != 0:
            continue
        t_elapsed = t + elapsed_min * 60_000
        t_final = t + 14 * 60_000
        if t_elapsed not in by_time or t_final not in by_time:
            continue
        start = float(by_time[t]["open"])
        px_elapsed = float(by_time[t_elapsed]["close"])
        px_final = float(by_time[t_final]["close"])
        if start <= 0:
            continue
        r_elapsed = log_return(start, px_elapsed)
        r_final = log_return(start, px_final)
        diff = r_elapsed - r_now
        w = math.exp(-0.5 * (diff / bandwidth) ** 2)
        vals.append(1.0 if r_final > 0 else 0.0)
        weights.append(w)
    if not vals or sum(weights) <= 0:
        return 0.5, 0
    p = sum(v * w for v, w in zip(vals, weights)) / sum(weights)
    return clamp(float(p), 0.02, 0.98), len(vals)


def estimate_asset_state(asset: str, symbol: str, kline_limit: int, min_remaining_sec: int, max_elapsed_sec: int) -> Optional[AssetState]:
    klines = binance_klines(symbol, limit=kline_limit)
    if len(klines) < 120:
        return None
    now = utc_ms()
    start_ms, end_ms = current_15m_window(now)
    elapsed_sec = int((now - start_ms) / 1000)
    remaining_sec = int((end_ms - now) / 1000)
    if remaining_sec < min_remaining_sec:
        return None
    if elapsed_sec > max_elapsed_sec:
        return None
    start_price = find_start_price(klines, start_ms)
    if not start_price:
        return None
    last_price = float(klines[-1]["close"])
    rets = one_min_returns(klines[-360:])
    if len(rets) < 60:
        return None
    sigma_min = max(pstdev(rets[-240:]), 0.00015)
    mu_min = mean(rets[-8:]) if len(rets) >= 8 else 0.0
    mu_min = clamp(mu_min, -2.0 * sigma_min, 2.0 * sigma_min)
    r_now = log_return(start_price, last_price)
    rem_min = max(remaining_sec / 60.0, 0.25)
    sigma_rem = max(sigma_min * math.sqrt(rem_min), 0.0001)
    z = (r_now + mu_min * rem_min) / sigma_rem
    p_norm = clamp(normal_cdf(z), 0.02, 0.98)
    p_emp, samples = empirical_prob_up(klines, max(1, elapsed_sec // 60), r_now, sigma_min)
    p_up = clamp(0.65 * p_norm + 0.35 * p_emp, 0.02, 0.98)
    return AssetState(
        asset=asset,
        symbol=symbol,
        now_ms=now,
        interval_start_ms=start_ms,
        interval_end_ms=end_ms,
        elapsed_sec=elapsed_sec,
        remaining_sec=remaining_sec,
        start_price=start_price,
        last_price=last_price,
        r_now=r_now,
        sigma_min=sigma_min,
        mu_min=mu_min,
        p_norm_up=p_norm,
        p_emp_up=p_emp,
        p_up=p_up,
        samples=samples,
    )


def raw_gamma_events(config: Config, terms: Iterable[str], limit: int) -> List[dict]:
    gamma = GammaClient(config)
    seen = set()
    out: List[dict] = []
    for term in terms:
        for key in ("search", "q", "query"):
            try:
                batch = gamma.list_events_raw({
                    "active": "true",
                    "closed": "false",
                    key: term,
                    "limit": limit,
                })
            except Exception:
                continue
            for e in batch:
                eid = str(first_present(e, "id", "eventId", default=""))
                if not eid or eid in seen:
                    continue
                seen.add(eid)
                out.append(e)
    return out


def infer_asset(market: Market) -> Optional[str]:
    text = " ".join([market.event_title, market.question, market.event_slug, market.market_slug]).lower()
    for asset, spec in ASSET_SPECS.items():
        if any(t in text for t in spec["terms"]):
            return asset
    return None


def looks_like_updown_15m(market: Market) -> bool:
    text = " ".join([market.event_title, market.question, market.event_slug, market.market_slug]).lower()
    if not any(x in text for x in ["up or down", "higher", "above", "below", "increase", "decrease", "up/down"]):
        return False
    bad = ["2027", "2028", "annual", "election", "winner", "etf", "reserve", "market cap"]
    if any(x in text for x in bad):
        return False
    return any(x in text for x in ["15", "15m", "15-minute", "15 minute", "up or down"])


def discover_crypto_markets(config: Config, assets: set[str], search_limit: int) -> List[Market]:
    terms: List[str] = []
    for asset in assets:
        if asset in ASSET_SPECS:
            terms.extend(ASSET_SPECS[asset]["search"])
    events = raw_gamma_events(config, terms, limit=search_limit)
    markets = parse_markets_from_events(events, only_weatherish=False)
    out = []
    seen = set()
    for m in markets:
        if not m.yes_token or not m.no_token:
            continue
        asset = infer_asset(m)
        if not asset or asset not in assets:
            continue
        if not looks_like_updown_15m(m):
            continue
        key = f"{m.event_id}:{m.market_id}"
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def best_bid_ask(book: Optional[OrderBook]) -> Tuple[Optional[float], Optional[float], float, float]:
    if not book:
        return None, None, 0.0, 0.0
    bid = book.best_bid()
    ask = book.best_ask()
    bid_size = float(book.bids[0].size) if book.bids else 0.0
    ask_size = float(book.asks[0].size) if book.asks else 0.0
    return (float(bid) if bid is not None else None, float(ask) if ask is not None else None, bid_size, ask_size)


def write_csv(path: str | Path, rows: List[dict]) -> None:
    if not rows:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    exists = p.exists()
    fields = list(rows[0].keys())
    with p.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerows(rows)


def scan(args: argparse.Namespace) -> int:
    load_dotenv()
    config = Config(
        fee_rate=Decimal(str(args.fee_rate)),
        min_edge=Decimal(str(args.min_edge)),
        min_shares=Decimal(str(args.min_shares)),
        max_shares=Decimal(str(args.max_shares)),
    )

    states: Dict[str, AssetState] = {}
    for asset in sorted(args.assets):
        if asset not in ASSET_SPECS:
            continue
        st = estimate_asset_state(
            asset=asset,
            symbol=ASSET_SPECS[asset]["binance"],
            kline_limit=args.kline_limit,
            min_remaining_sec=args.min_remaining_sec,
            max_elapsed_sec=args.max_elapsed_sec,
        )
        if st:
            states[asset] = st
            print(
                f"CRYPTO15_MODEL asset={asset} symbol={st.symbol} "
                f"window={iso_ms(st.interval_start_ms)}->{iso_ms(st.interval_end_ms)} "
                f"elapsed={st.elapsed_sec}s remaining={st.remaining_sec}s "
                f"start={st.start_price:.4f} last={st.last_price:.4f} "
                f"r_now={st.r_now:.5f} sigma_min={st.sigma_min:.6f} "
                f"p_norm_up={st.p_norm_up:.4f} p_emp_up={st.p_emp_up:.4f} "
                f"p_up={st.p_up:.4f} samples={st.samples}"
            )
        else:
            print(f"CRYPTO15_MODEL asset={asset} status=skipped reason=timing_or_data_filter")

    if not states:
        print("CRYPTO15_SUMMARY markets=0 books=0 signals=0 reason=no_asset_state")
        return 0

    markets = discover_crypto_markets(config, args.assets, search_limit=args.search_limit)
    if args.debug:
        for m in markets[: args.top]:
            print(f"CRYPTO15_MARKET asset={infer_asset(m)} event=\"{m.event_title}\" q=\"{m.question}\" slug={m.market_slug}")

    token_ids = []
    for m in markets:
        if m.yes_token:
            token_ids.append(m.yes_token.token_id)
        if m.no_token:
            token_ids.append(m.no_token.token_id)
    clob = ClobPublicClient(config)
    books = clob.get_books(sorted(set(token_ids)), batch_size=args.book_batch_size) if token_ids else {}

    now = utc_ms()
    signals: List[CryptoSignal] = []
    rejects = {"asset_missing": 0, "no_book": 0, "wide_spread": 0, "low_depth": 0, "edge_below_min": 0}
    for m in markets:
        asset = infer_asset(m)
        if not asset or asset not in states:
            rejects["asset_missing"] += 1
            continue
        st = states[asset]
        yb = books.get(m.yes_token.token_id if m.yes_token else "")
        nb = books.get(m.no_token.token_id if m.no_token else "")
        yes_bid, yes_ask, yes_bid_sz, yes_ask_sz = best_bid_ask(yb)
        no_bid, no_ask, no_bid_sz, no_ask_sz = best_bid_ask(nb)
        if yes_ask is None or no_ask is None or yes_bid is None or no_bid is None:
            rejects["no_book"] += 1
            continue
        yes_spread = yes_ask - yes_bid
        no_spread = no_ask - no_bid
        p_yes = st.p_up
        p_no = 1.0 - st.p_up
        candidates = [
            ("YES", "BUY_YES", yes_ask, yes_bid, yes_spread, yes_ask_sz, yes_bid_sz, p_yes),
            ("NO", "BUY_NO", no_ask, no_bid, no_spread, no_ask_sz, no_bid_sz, p_no),
        ]
        for side, action, ask, bid, spread, ask_sz, bid_sz, model_prob in candidates:
            if spread > args.max_spread:
                rejects["wide_spread"] += 1
                continue
            if ask_sz < args.min_depth_shares:
                rejects["low_depth"] += 1
                continue
            edge = model_prob - ask
            if edge < args.min_edge:
                rejects["edge_below_min"] += 1
                continue
            signals.append(CryptoSignal(
                ts_ms=now,
                asset=asset,
                symbol=st.symbol,
                event_id=m.event_id,
                event_title=m.event_title,
                market_id=m.market_id,
                question=m.question,
                side=side,
                action=action,
                ask=ask,
                bid=bid,
                spread=spread,
                ask_size=ask_sz,
                bid_size=bid_sz,
                model_prob=model_prob,
                edge=edge,
                start_price=st.start_price,
                last_price=st.last_price,
                r_now=st.r_now,
                elapsed_sec=st.elapsed_sec,
                remaining_sec=st.remaining_sec,
                p_up=st.p_up,
                p_down=1.0 - st.p_up,
                reason="model_prob_minus_ask",
            ))

    signals.sort(key=lambda x: (x.edge, x.ask_size), reverse=True)
    if signals:
        write_csv(args.output, [asdict(x) for x in signals])

    by_asset = {asset: 0 for asset in sorted(args.assets)}
    for s in signals:
        by_asset[s.asset] = by_asset.get(s.asset, 0) + 1
    best = signals[0] if signals else None
    best_edge = best.edge if best else float("-inf")
    print(
        f"CRYPTO15_SUMMARY assets={','.join(sorted(args.assets))} markets={len(markets)} books={len(books)} "
        f"signals={len(signals)} by_asset=" + json.dumps(by_asset, sort_keys=True) + " "
        f"best_edge={best_edge:.4f} best_asset={best.asset if best else ''} best_action={best.action if best else ''} "
        f"rejects=" + json.dumps(rejects, sort_keys=True)
    )
    for s in signals[: args.top]:
        print(
            f"CRYPTO15_SIGNAL asset={s.asset} action={s.action} edge={s.edge:.4f} "
            f"prob={s.model_prob:.4f} ask={s.ask:.4f} bid={s.bid:.4f} spread={s.spread:.4f} "
            f"depth={s.ask_size:.2f} remaining={s.remaining_sec}s event=\"{s.event_title[:80]}\" q=\"{s.question[:80]}\""
        )
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--assets", default="BTC,ETH,SOL", help="comma list: BTC,ETH,SOL")
    p.add_argument("--search-limit", type=int, default=100)
    p.add_argument("--book-batch-size", type=int, default=250)
    p.add_argument("--kline-limit", type=int, default=1000)
    p.add_argument("--fee-rate", default="0.01")
    p.add_argument("--min-edge", type=float, default=0.06)
    p.add_argument("--max-spread", type=float, default=0.08)
    p.add_argument("--min-depth-shares", type=float, default=20.0)
    p.add_argument("--min-shares", default="5")
    p.add_argument("--max-shares", default="20")
    p.add_argument("--min-remaining-sec", type=int, default=180)
    p.add_argument("--max-elapsed-sec", type=int, default=720)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--output", default="paper_logs/crypto_15m_signals.csv")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    args.assets = {x.strip().upper() for x in str(args.assets).split(",") if x.strip()}
    return scan(args)


if __name__ == "__main__":
    raise SystemExit(main())
