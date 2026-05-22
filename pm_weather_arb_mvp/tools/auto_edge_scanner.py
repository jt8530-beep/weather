#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Full-market automated edge scanner.

This is intentionally paper/diagnostic only:
- no wallet
- no signing
- no order submission
- no manual verification dependency

It scans all Polymarket events for two automatically checkable classes:
1. Hard arbitrage from the existing scanner: YES/NO complement, threshold nesting,
   and API-metadata-only NegRisk full-set candidates.
2. Maker candidates: two-sided markets with enough spread and depth to justify
   passive quoting experiments.

The goal is not to force trades. The goal is to stop wasting time on pure
accepted=0 hard-arb monitoring and produce a ranked, automatically generated
paper opportunity feed across all markets.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv

from pm_weather_arb.clob import ClobPublicClient
from pm_weather_arb.config import Config
from pm_weather_arb.gamma import GammaClient, parse_markets_from_events
from pm_weather_arb.scanners import scan_all
from pm_weather_arb.types import Market, OrderBook
from pm_weather_arb.util import first_present, truthy


@dataclass
class MakerCandidate:
    ts_ms: int
    kind: str
    event_id: str
    event_title: str
    market_id: str
    question: str
    market_slug: str
    yes_bid: float
    yes_ask: float
    yes_spread: float
    no_bid: float
    no_ask: float
    no_spread: float
    yes_bid_size: float
    yes_ask_size: float
    no_bid_size: float
    no_ask_size: float
    mid_yes: float
    parity_gap_ask: float
    parity_gap_bid: float
    maker_edge_yes: float
    maker_edge_no: float
    best_maker_edge: float
    max_notional_hint: float
    risk_flags: str


@dataclass
class AutoNegRiskCheck:
    event_id: str
    event_title: str
    market_count: int
    pass_auto: bool
    reason: str
    raw_event_neg_risk: bool
    all_market_neg_risk: bool
    all_binary: bool
    all_books_present: bool
    unique_conditions: bool
    has_forbidden_status: bool


FORBIDDEN_TITLE_TERMS = [
    "deprecated",
    "test market",
    "invalid",
    "cancelled",
    "canceled",
]


def token_ids(markets: Iterable[Market]) -> List[str]:
    out = []
    for market in markets:
        for token in market.tokens:
            if token.token_id:
                out.append(token.token_id)
    return sorted(set(out))


def best_bid_ask(book: Optional[OrderBook]) -> Tuple[Optional[float], Optional[float], float, float]:
    if not book:
        return None, None, 0.0, 0.0
    bid = book.best_bid()
    ask = book.best_ask()
    bid_size = float(book.bids[0].size) if book.bids else 0.0
    ask_size = float(book.asks[0].size) if book.asks else 0.0
    return (float(bid) if bid is not None else None, float(ask) if ask is not None else None, bid_size, ask_size)


def normalize_tick(value: Any) -> float:
    try:
        v = float(value or 0)
        if v > 0:
            return v
    except Exception:
        pass
    return 0.001


def is_bad_title(text: str) -> bool:
    low = text.lower()
    return any(t in low for t in FORBIDDEN_TITLE_TERMS)


def event_raw_neg_risk(group: List[Market]) -> bool:
    if not group:
        return False
    # Event-level flags are not always copied into every raw market. Use both raw
    # market flags and parsed market flags. This is still metadata-only, not manual.
    if all(m.neg_risk for m in group):
        return True
    for m in group:
        raw = m.raw or {}
        if truthy(first_present(raw, "negRisk", "neg_risk", "enableNegRisk", default=False)):
            return True
    return False


def auto_negrisk_check(group: List[Market], books: Dict[str, OrderBook]) -> AutoNegRiskCheck:
    event_id = group[0].event_id if group else ""
    event_title = group[0].event_title if group else ""
    all_market_neg = all(m.neg_risk for m in group)
    raw_neg = event_raw_neg_risk(group)
    all_binary = all(m.yes_token and m.no_token and len(m.tokens) >= 2 for m in group)
    unique_conditions = len({m.condition_id for m in group if m.condition_id}) == len(group)
    has_forbidden_status = any((not m.active) or m.closed or (not m.enable_order_book) for m in group) or is_bad_title(event_title)
    all_books_present = True
    for m in group:
        if not m.yes_token or not m.no_token:
            all_books_present = False
            break
        if m.yes_token.token_id not in books or m.no_token.token_id not in books:
            all_books_present = False
            break
    if len(group) < 2:
        reason = "too_few_markets"
        ok = False
    elif not raw_neg:
        reason = "event_not_metadata_negrisk"
        ok = False
    elif not all_market_neg:
        reason = "not_all_markets_metadata_negrisk"
        ok = False
    elif not all_binary:
        reason = "not_all_markets_binary"
        ok = False
    elif not unique_conditions:
        reason = "duplicate_or_missing_condition_id"
        ok = False
    elif has_forbidden_status:
        reason = "closed_inactive_disabled_or_bad_title"
        ok = False
    elif not all_books_present:
        reason = "missing_books"
        ok = False
    else:
        reason = "metadata_auto_verified"
        ok = True
    return AutoNegRiskCheck(
        event_id=event_id,
        event_title=event_title,
        market_count=len(group),
        pass_auto=ok,
        reason=reason,
        raw_event_neg_risk=raw_neg,
        all_market_neg_risk=all_market_neg,
        all_binary=all_binary,
        all_books_present=all_books_present,
        unique_conditions=unique_conditions,
        has_forbidden_status=has_forbidden_status,
    )


def scan_maker_candidates(
    markets: Iterable[Market],
    books: Dict[str, OrderBook],
    min_spread: float,
    min_edge: float,
    min_depth_shares: float,
    max_mid: float,
    min_mid: float,
    max_candidates: int,
) -> List[MakerCandidate]:
    now = int(time.time() * 1000)
    out: List[MakerCandidate] = []
    for m in markets:
        if not m.yes_token or not m.no_token:
            continue
        yb = books.get(m.yes_token.token_id)
        nb = books.get(m.no_token.token_id)
        yes_bid, yes_ask, yes_bid_sz, yes_ask_sz = best_bid_ask(yb)
        no_bid, no_ask, no_bid_sz, no_ask_sz = best_bid_ask(nb)
        if None in (yes_bid, yes_ask, no_bid, no_ask):
            continue
        assert yes_bid is not None and yes_ask is not None and no_bid is not None and no_ask is not None
        if yes_ask <= yes_bid or no_ask <= no_bid:
            continue
        yes_spread = yes_ask - yes_bid
        no_spread = no_ask - no_bid
        mid_yes = (yes_bid + yes_ask) / 2.0
        if mid_yes < min_mid or mid_yes > max_mid:
            continue
        if min(yes_bid_sz, yes_ask_sz, no_bid_sz, no_ask_sz) < min_depth_shares:
            continue
        if max(yes_spread, no_spread) < min_spread:
            continue
        tick = normalize_tick(m.minimum_tick_size or (yb.tick_size if yb else None) or (nb.tick_size if nb else None))
        parity_gap_ask = (yes_ask + no_ask) - 1.0
        parity_gap_bid = 1.0 - (yes_bid + no_bid)
        # Maker edge is estimated one-sided spread capture after moving one tick
        # inside the book twice. This is not a guaranteed profit; it is a paper
        # candidate score for passive market-making experiments.
        maker_edge_yes = max(0.0, yes_spread - 2.0 * tick)
        maker_edge_no = max(0.0, no_spread - 2.0 * tick)
        best_edge = max(maker_edge_yes, maker_edge_no)
        if best_edge < min_edge:
            continue
        flags = []
        if parity_gap_ask < -0.002:
            flags.append("yes_no_buy_cross_possible")
        if parity_gap_bid < -0.002:
            flags.append("yes_no_sell_cross_possible")
        if is_bad_title(m.event_title + " " + m.question):
            flags.append("bad_title")
        if flags and "bad_title" in flags:
            continue
        max_notional_hint = min(yes_bid_sz, yes_ask_sz, no_bid_sz, no_ask_sz) * mid_yes
        out.append(
            MakerCandidate(
                ts_ms=now,
                kind="MAKER_SPREAD_CANDIDATE",
                event_id=m.event_id,
                event_title=m.event_title,
                market_id=m.market_id,
                question=m.question,
                market_slug=m.market_slug,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                yes_spread=yes_spread,
                no_bid=no_bid,
                no_ask=no_ask,
                no_spread=no_spread,
                yes_bid_size=yes_bid_sz,
                yes_ask_size=yes_ask_sz,
                no_bid_size=no_bid_sz,
                no_ask_size=no_ask_sz,
                mid_yes=mid_yes,
                parity_gap_ask=parity_gap_ask,
                parity_gap_bid=parity_gap_bid,
                maker_edge_yes=maker_edge_yes,
                maker_edge_no=maker_edge_no,
                best_maker_edge=best_edge,
                max_notional_hint=max_notional_hint,
                risk_flags=",".join(flags),
            )
        )
    out.sort(key=lambda x: (x.best_maker_edge, x.max_notional_hint), reverse=True)
    return out[:max_candidates]


def write_csv_rows(path: str | Path, rows: List[dict]) -> None:
    if not rows:
        return
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    exists = p.exists()
    fields = list(rows[0].keys())
    with p.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def load_markets_books(args: argparse.Namespace) -> tuple[list[dict], list[Market], dict[str, OrderBook]]:
    config = Config(
        fee_rate=Decimal(str(args.fee_rate)),
        min_edge=Decimal(str(args.min_edge)),
        min_shares=Decimal(str(args.min_shares)),
        max_shares=Decimal(str(args.max_shares)),
    )
    gamma = GammaClient(config)
    clob = ClobPublicClient(config)
    raw_events: List[dict] = []
    for page in range(args.pages):
        batch = gamma.list_active_events(pages=1, limit=args.limit, order=args.order)
        if not batch:
            break
        # list_active_events pages from offset 0 internally, so use raw endpoint here for stable pagination.
        batch = gamma.list_events_raw({
            "active": "true",
            "closed": "false",
            "limit": args.limit,
            "offset": page * args.limit,
            "order": args.order,
            "ascending": "false",
        })
        raw_events.extend(batch)
        if len(batch) < args.limit:
            break
    by_id: Dict[str, dict] = {}
    for e in raw_events:
        eid = str(first_present(e, "id", "eventId", default=""))
        if eid:
            by_id[eid] = e
    events = list(by_id.values())
    markets = parse_markets_from_events(events, only_weatherish=False)
    ids = token_ids(markets)
    books = clob.get_books(ids, batch_size=args.book_batch_size) if ids else {}
    return events, markets, books


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=8)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--order", default="volume_24hr")
    parser.add_argument("--book-batch-size", type=int, default=250)
    parser.add_argument("--fee-rate", default="0.01")
    parser.add_argument("--min-edge", default="0.005")
    parser.add_argument("--min-shares", default="5")
    parser.add_argument("--max-shares", default="20")
    parser.add_argument("--maker-min-spread", type=float, default=0.035)
    parser.add_argument("--maker-min-edge", type=float, default=0.025)
    parser.add_argument("--maker-min-depth", type=float, default=20.0)
    parser.add_argument("--maker-min-mid", type=float, default=0.03)
    parser.add_argument("--maker-max-mid", type=float, default=0.97)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--maker-output", default="paper_logs/auto_maker_candidates.csv")
    parser.add_argument("--arb-output", default="paper_logs/auto_hard_arb_candidates.csv")
    parser.add_argument("--negrisk-check-output", default="paper_logs/auto_negrisk_checks.csv")
    args = parser.parse_args()

    load_dotenv()
    events, markets, books = load_markets_books(args)
    binary = sum(1 for m in markets if m.yes_token and m.no_token)

    hard_opps = scan_all(
        markets=markets,
        books=books,
        fee_rate=Decimal(str(args.fee_rate)),
        min_edge=Decimal(str(args.min_edge)),
        min_shares=Decimal(str(args.min_shares)),
        max_shares=Decimal(str(args.max_shares)),
    )
    hard_rows = []
    for opp in hard_opps:
        hard_rows.append({
            "ts_ms": int(time.time() * 1000),
            "kind": opp.kind,
            "event_id": opp.event_id,
            "event_title": opp.event_title,
            "size": str(opp.size),
            "edge_per_share": str(opp.edge_per_share),
            "expected_profit": str(opp.expected_profit),
            "total_cost": str(opp.total_cost),
            "total_proceeds": str(opp.total_proceeds),
            "notes": opp.notes,
        })

    by_event: Dict[str, List[Market]] = {}
    for m in markets:
        by_event.setdefault(m.event_id, []).append(m)
    checks = [auto_negrisk_check(group, books) for group in by_event.values() if any(m.neg_risk for m in group)]
    check_rows = [asdict(x) for x in checks]
    auto_verified_count = sum(1 for x in checks if x.pass_auto)

    maker = scan_maker_candidates(
        markets=markets,
        books=books,
        min_spread=args.maker_min_spread,
        min_edge=args.maker_min_edge,
        min_depth_shares=args.maker_min_depth,
        min_mid=args.maker_min_mid,
        max_mid=args.maker_max_mid,
        max_candidates=args.top,
    )

    write_csv_rows(args.arb_output, hard_rows)
    write_csv_rows(args.negrisk_check_output, check_rows)
    write_csv_rows(args.maker_output, [asdict(x) for x in maker])

    best_hard_edge = float(hard_opps[0].edge_per_share) if hard_opps else float("-inf")
    best_hard_kind = hard_opps[0].kind if hard_opps else ""
    best_hard_event = hard_opps[0].event_title[:80] if hard_opps else ""
    best_maker_edge = maker[0].best_maker_edge if maker else float("-inf")
    best_maker_event = maker[0].event_title[:80] if maker else ""
    print(
        f"AUTO_EDGE_SUMMARY events={len(events)} markets={len(markets)} binary={binary} "
        f"books={len(books)} hard_arbs={len(hard_opps)} "
        f"best_hard_kind={best_hard_kind} best_hard_edge={best_hard_edge:.4f} "
        f"best_hard_event=\"{best_hard_event}\" "
        f"auto_negrisk_verified={auto_verified_count}/{len(checks)} "
        f"maker_candidates={len(maker)} best_maker_edge={best_maker_edge:.4f} "
        f"best_maker_event=\"{best_maker_event}\""
    )
    for c in maker[: min(args.top, 10)]:
        print(
            f"MAKER_CANDIDATE edge={c.best_maker_edge:.4f} mid={c.mid_yes:.4f} "
            f"yes={c.yes_bid:.4f}/{c.yes_ask:.4f} no={c.no_bid:.4f}/{c.no_ask:.4f} "
            f"depth_hint={c.max_notional_hint:.2f} event=\"{c.event_title[:70]}\" q=\"{c.question[:70]}\""
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
