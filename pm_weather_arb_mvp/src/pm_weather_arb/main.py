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
from .paper_executor import PaperExecutor, append_csv, append_jsonl
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
    markets = parse_markets_from_events(events, only_weatherish=not args.all_markets)
    token_ids = _token_ids(markets)
    print(f"events={len(events)} weather_markets={len(markets)} tokens={len(token_ids)}")
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


def run_scan(args: argparse.Namespace) -> int:
    _, _, opportunities = _scan_from_args(args)
    print_summary(opportunities, top_n=args.top)
    if args.output:
        write_csv(opportunities, args.output)
        print(f"wrote={args.output}")
    return 0


def run_paper(args: argparse.Namespace) -> int:
    _, books, opportunities = _scan_from_args(args)
    print_summary(opportunities, top_n=args.top)
    executor = PaperExecutor(
        max_notional_per_trade=Decimal(str(args.max_notional)),
        max_book_age_ms=int(args.max_book_age_ms),
        min_edge=Decimal(str(args.paper_min_edge)),
    )
    results = [executor.simulate(opp, books, Decimal(str(args.fee_rate))) for opp in opportunities]
    accepted = [r for r in results if r.accepted]
    rejected = [r for r in results if not r.accepted]
    print(f"paper_results={len(results)} accepted={len(accepted)} rejected={len(rejected)}")
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
    scan.add_argument("--all-markets", action="store_true", help="disable weather keyword filter")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pm_weather_arb")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="scan Polymarket weather markets for arbitrage candidates")
    _add_scan_args(scan)
    scan.set_defaults(func=run_scan)

    paper = sub.add_parser("paper", help="scan and simulate FOK-style paper execution")
    _add_scan_args(paper)
    paper.add_argument("--paper-min-edge", default="0.02")
    paper.add_argument("--max-notional", default="10")
    paper.add_argument("--max-book-age-ms", type=int, default=500)
    paper.add_argument("--paper-csv", default="paper_logs/paper_executions.csv")
    paper.add_argument("--paper-jsonl", default="paper_logs/paper_executions.jsonl")
    paper.set_defaults(func=run_paper)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)
