from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Iterable, List

from dotenv import load_dotenv

from .clob import ClobPublicClient, parse_book
from .config import Config
from .gamma import GammaClient, parse_markets_from_events
from .near_miss import diagnose_near_misses, print_diagnostics, write_near_miss_csv
from .paper_executor import (
    PaperExecutor,
    append_csv,
    append_jsonl,
    append_seen_paper_keys,
    load_seen_paper_keys,
    paper_opportunity_key,
    parse_kind_min_edges,
)
from .report import print_summary, write_csv
from .scanners import scan_all
from .types import Market, OrderBook


def _token_ids(markets: Iterable[Market]) -> List[str]:
    ids = []
    for market in markets:
        for token in market.tokens:
            ids.append(token.token_id)
    return sorted(set(ids))


def _load_fixture(path: str | Path) -> tuple[list[Market], dict[str, OrderBook]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    events = data.get("events", [])
    books_raw = data.get("books", {})
    markets = parse_markets_from_events(events, only_weatherish=False)
    books = {token_id: parse_book(raw, token_id) for token_id, raw in books_raw.items()}
    return markets, books


def _build_config(args: argparse.Namespace) -> Config:
    return Config(
        fee_rate=Decimal(str(args.fee_rate)),
        min_edge=Decimal(str(args.min_edge)),
        min_shares=Decimal(str(args.min_shares)),
        max_shares=Decimal(str(args.max_shares)),
    )


def _load_markets_books(args: argparse.Namespace, config: Config) -> tuple[list[Market], dict[str, OrderBook]]:
    if getattr(args, "fixture", None):
        return _load_fixture(args.fixture)
    gamma = GammaClient(config)
    clob = ClobPublicClient(config)
    events = gamma.list_active_events(pages=args.pages, limit=args.limit, order=args.order)
    weather_only = bool(getattr(args, "weather_only", False)) and not bool(getattr(args, "all_markets", False))
    markets = parse_markets_from_events(events, only_weatherish=weather_only)
    token_ids = _token_ids(markets)
    binary_markets = sum(1 for market in markets if market.yes_token and market.no_token)
    scope = "weather" if weather_only else "all"
    print(f"events={len(events)} raw_markets={len(markets)} binary_markets={binary_markets} tokens={len(token_ids)} market_scope={scope}")
    books = clob.get_books(token_ids, batch_size=args.book_batch_size) if token_ids else {}
    return markets, books


def _scan_from_args(args: argparse.Namespace) -> tuple[list[Market], dict[str, OrderBook], list]:
    load_dotenv()
    config = _build_config(args)
    markets, books = _load_markets_books(args, config)
    opportunities = scan_all(
        markets=markets,
        books=books,
        fee_rate=config.fee_rate,
        min_edge=config.min_edge,
        min_shares=config.min_shares,
        max_shares=config.max_shares,
    )
    return markets, books, opportunities


def _maybe_write_near_misses(args: argparse.Namespace, markets: list[Market], books: dict[str, OrderBook]) -> None:
    near_miss_output = getattr(args, "near_miss_output", None)
    diagnostics_only = bool(getattr(args, "diagnostics", False))
    if not near_miss_output and not diagnostics_only:
        return
    diagnostics, near_misses = diagnose_near_misses(
        markets=markets,
        books=books,
        fee_rate=Decimal(str(args.fee_rate)),
        min_shares=Decimal(str(args.min_shares)),
        max_shares=Decimal(str(args.max_shares)),
        top_n=int(getattr(args, "near_miss_top", 50)),
    )
    print_diagnostics(diagnostics, near_misses, top_n=min(int(args.top), 10))
    if near_miss_output:
        write_near_miss_csv(near_misses, near_miss_output)
        print(f"wrote={near_miss_output}")


def _maybe_write_suspicious_negrisk(args: argparse.Namespace, opportunities: list) -> None:
    output = getattr(args, "suspicious_negrisk_output", None)
    if not output:
        return
    suspicious = [opp for opp in opportunities if str(getattr(opp, "kind", "")).startswith("NEGRISK_")]
    write_csv(suspicious, output)
    print(f"suspicious_negrisk={len(suspicious)} wrote={output}")


def run_scan(args: argparse.Namespace) -> int:
    markets, books, opportunities = _scan_from_args(args)
    print_summary(opportunities, top_n=args.top)
    _maybe_write_near_misses(args, markets, books)
    _maybe_write_suspicious_negrisk(args, opportunities)
    if args.output:
        write_csv(opportunities, args.output)
        print(f"wrote={args.output}")
    return 0


def run_paper(args: argparse.Namespace) -> int:
    markets, books, opportunities = _scan_from_args(args)
    print_summary(opportunities, top_n=args.top)
    _maybe_write_near_misses(args, markets, books)
    executor = PaperExecutor(
        max_notional_per_trade=Decimal(str(args.max_notional)),
        max_book_age_ms=int(args.max_book_age_ms),
        min_edge=Decimal(str(args.paper_min_edge)),
        per_kind_min_edge=parse_kind_min_edges(str(args.paper_kind_min_edge)),
    )
    seen_keys = load_seen_paper_keys(args.paper_seen_keys, args.paper_csv)
    results = []
    raw_accepted = 0
    repeated_observations = 0
    new_seen_keys = []
    for opp in opportunities:
        result = executor.simulate(opp, books, Decimal(str(args.fee_rate)))
        if result.accepted:
            raw_accepted += 1
            key = paper_opportunity_key(opp)
            if key in seen_keys:
                repeated_observations += 1
                continue
            seen_keys.add(key)
            new_seen_keys.append(key)
        results.append(result)
    accepted = [r for r in results if r.accepted]
    rejected = [r for r in results if not r.accepted]
    print(
        f"paper_results={len(opportunities)} accepted={len(accepted)} rejected={len(rejected)} "
        f"raw_accepted={raw_accepted} repeated_observations={repeated_observations} "
        f"unique_opportunities={len(seen_keys)}"
    )
    for result in accepted[: args.top]:
        print(
            f"PAPER_ACCEPT kind={result.kind} size={result.requested_size} "
            f"edge={result.estimated_edge_per_share} profit={result.estimated_profit} "
            f"event={result.event_title[:80]}"
        )
    if args.paper_csv:
        append_csv(results, args.paper_csv)
        print(f"wrote={args.paper_csv}")
    if args.paper_jsonl:
        append_jsonl(results, args.paper_jsonl)
        print(f"wrote={args.paper_jsonl}")
    if args.paper_seen_keys:
        append_seen_paper_keys(new_seen_keys, args.paper_seen_keys)
        print(f"paper_seen_new={len(new_seen_keys)} wrote={args.paper_seen_keys}")
    _maybe_write_suspicious_negrisk(args, opportunities)
    if args.output:
        write_csv(opportunities, args.output)
        print(f"wrote={args.output}")
    return 0


def _add_scan_args(scan: argparse.ArgumentParser) -> None:
    scan.add_argument("--pages", type=int, default=5)
    scan.add_argument("--limit", type=int, default=100)
    scan.add_argument("--order", default="volume_24hr")
    scan.add_argument("--book-batch-size", type=int, default=250)
    scan.add_argument("--max-shares", default="100")
    scan.add_argument("--min-shares", default="5")
    scan.add_argument("--min-edge", default="0.005")
    scan.add_argument("--fee-rate", default="0.05")
    scan.add_argument("--output", default="opportunities.csv")
    scan.add_argument("--top", type=int, default=20)
    scan.add_argument("--fixture", help="load fixture JSON instead of live API")
    scan.add_argument("--all-markets", action="store_true", help="deprecated compatibility flag; all-market universal scanning is now the default")
    scan.add_argument("--weather-only", action="store_true", help="restrict discovery to weather-like events for debugging")
    scan.add_argument("--diagnostics", action="store_true", help="print scanner diagnostics even if near-miss output is disabled")
    scan.add_argument("--near-miss-output", default="near_misses.csv", help="write closest non-arb candidates for debugging; use empty string to disable")
    scan.add_argument("--near-miss-top", type=int, default=50, help="number of near-misses to keep in CSV")
    scan.add_argument("--suspicious-negrisk-output", default="suspicious_negrisk.csv", help="write profitable NegRisk candidates for manual completeness checks")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pm_weather_arb")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="scan Polymarket weather markets for arbitrage candidates")
    _add_scan_args(scan)
    scan.set_defaults(func=run_scan)

    paper = sub.add_parser("paper", help="scan and simulate FOK-style paper execution")
    _add_scan_args(paper)
    paper.add_argument("--paper-min-edge", default="0.02")
    paper.add_argument("--paper-kind-min-edge", default="", help="per-kind minimum edge overrides, e.g. YES_NO_BUY_BOTH=0.01")
    paper.add_argument("--max-notional", default="10")
    paper.add_argument("--max-book-age-ms", type=int, default=500)
    paper.add_argument("--paper-csv", default="paper_logs/paper_executions.csv")
    paper.add_argument("--paper-jsonl", default="paper_logs/paper_executions.jsonl")
    paper.add_argument("--paper-seen-keys", default="paper_logs/paper_seen_keys.txt")
    paper.set_defaults(func=run_paper)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)
