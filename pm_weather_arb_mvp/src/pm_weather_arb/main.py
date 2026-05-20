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
    SeenOpportunityStore,
    append_csv,
    append_jsonl,
    append_seen_paper_keys,
    apply_dedup,
    load_seen_paper_keys,
    paper_opportunity_key,
    parse_kind_min_edges,
)
from .report import print_summary, write_csv
from .scanners import scan_all
from .suspicious_negrisk import write_suspicious_negrisk_csv
from .temperature_buckets import TemperatureBucketValidation, validate_all_temperature_events
from .types import Market, OrderBook
from .util import first_present


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

    # V6: volume scan + targeted temperature discovery + slug fallback
    volume_events = gamma.list_active_events(pages=args.pages, limit=args.limit, order=args.order)
    volume_count = len(volume_events)

    targeted_temperature_enabled = bool(getattr(args, "target_temperature_events", False))
    targeted_events: list = []
    targeted_temp_count = 0

    if targeted_temperature_enabled:
        # 1. Search-term based discovery
        search_terms_raw = getattr(args, "temperature_search_terms", "") or ""
        search_terms = [t.strip() for t in search_terms_raw.split(",") if t.strip()]
        search_limit = int(getattr(args, "temperature_search_limit", 100))
        if search_terms:
            targeted_events = gamma.list_temperature_events(terms=search_terms, limit=search_limit)
        else:
            # No explicit terms provided: use default temperature search terms from gamma.py
            targeted_events = gamma.list_temperature_events(terms=None, limit=search_limit)

        # 2. Explicit slug discovery
        slugs_raw = getattr(args, "target_event_slugs", "") or ""
        slugs = [s.strip() for s in slugs_raw.split(",") if s.strip()]
        if slugs:
            slug_events = gamma.list_events_by_slugs(slugs)
            targeted_events.extend(slug_events)

        targeted_temp_count = len(targeted_events)

    # Merge by event_id, dedupe
    events_by_id: dict[str, dict] = {}
    for event in volume_events + targeted_events:
        event_id = str(first_present(event, "id", "eventId", default=""))
        if event_id:
            events_by_id[event_id] = event
    events = list(events_by_id.values())

    weather_only = bool(getattr(args, "weather_only", False)) and not bool(getattr(args, "all_markets", False))
    markets = parse_markets_from_events(events, only_weatherish=weather_only)
    token_ids = _token_ids(markets)
    binary_markets = sum(1 for market in markets if market.yes_token and market.no_token)
    scope = "weather" if weather_only else "all"
    print(
        f"events_volume={volume_count} "
        f"events_targeted_temperature={targeted_temp_count} "
        f"events_merged={len(events)} "
        f"targeted_temperature_enabled={targeted_temperature_enabled} "
        f"raw_markets={len(markets)} binary_markets={binary_markets} "
        f"tokens={len(token_ids)} market_scope={scope}"
    )
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
    count = write_suspicious_negrisk_csv(suspicious, output)
    print(f"suspicious_negrisk={count} wrote={output}")


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

    # V5: Validate temperature bucket events
    temp_validations = validate_all_temperature_events(markets)
    temp_checked = len(temp_validations)
    temp_valid = sum(1 for v in temp_validations.values() if v.is_valid)
    temp_invalid = temp_checked - temp_valid
    invalid_reasons: dict[str, int] = {}
    for v in temp_validations.values():
        if not v.is_valid:
            invalid_reasons[v.reason] = invalid_reasons.get(v.reason, 0) + 1
    if temp_checked > 0:
        reasons_str = ",".join(f"{k}:{v}" for k, v in sorted(invalid_reasons.items()))
        print(f"temperature_bucket_events_checked={temp_checked} valid={temp_valid} invalid={temp_invalid} invalid_reasons={reasons_str}")

    # V6: diagnostic for valid temperature bucket events → four-way arb evaluation
    for event_id, v in temp_validations.items():
        if not v.is_valid:
            continue

        # Per-bucket book snapshot + detailed missing counts
        sum_yes_ask = 0.0
        sum_yes_bid = 0.0
        sum_no_ask = 0.0
        sum_no_bid = 0.0
        yes_ask_count = 0
        yes_bid_count = 0
        no_ask_count = 0
        no_bid_count = 0
        missing_yes_ask = 0
        missing_yes_bid = 0
        missing_no_ask = 0
        missing_no_bid = 0
        for b in v.buckets:
            yb = books.get(b.token_yes)
            nb = books.get(b.token_no)
            ya = float(yb.best_ask()) if yb and yb.best_ask() is not None else None
            yb_p = float(yb.best_bid()) if yb and yb.best_bid() is not None else None
            na = float(nb.best_ask()) if nb and nb.best_ask() is not None else None
            nb_p = float(nb.best_bid()) if nb and nb.best_bid() is not None else None
            if ya is not None:
                yes_ask_count += 1
                sum_yes_ask += ya
            else:
                missing_yes_ask += 1
            if yb_p is not None:
                yes_bid_count += 1
                sum_yes_bid += yb_p
            else:
                missing_yes_bid += 1
            if na is not None:
                no_ask_count += 1
                sum_no_ask += na
            else:
                missing_no_ask += 1
            if nb_p is not None:
                no_bid_count += 1
                sum_no_bid += nb_p
            else:
                missing_no_bid += 1
            ya_s = f"{ya:.4f}" if ya is not None else "none"
            yb_s = f"{yb_p:.4f}" if yb_p is not None else "none"
            na_s = f"{na:.4f}" if na is not None else "none"
            nb_s = f"{nb_p:.4f}" if nb_p is not None else "none"
            print(f"TEMP_BUCKET_BOOK bucket={b.kind}={b.value}{v.unit} yes_ask={ya_s} yes_bid={yb_s} no_ask={na_s} no_bid={nb_s}")

        k = v.bucket_count
        print(
            f"TEMP_BUCKET_COUNTS "
            f"yes_ask={yes_ask_count}/{k} yes_bid={yes_bid_count}/{k} no_ask={no_ask_count}/{k} no_bid={no_bid_count}/{k} "
            f"miss_ya={missing_yes_ask} miss_yb={missing_yes_bid} miss_na={missing_no_ask} miss_nb={missing_no_bid}"
        )

        # BUY_ALL_YES: buy every YES, payout = 1
        buy_yes_edge = 1.0 - sum_yes_ask - float(args.fee_rate)
        buy_yes_full = (yes_ask_count == k)
        if buy_yes_full and buy_yes_edge > 0:
            buy_yes_status = f"profitable edge={buy_yes_edge:.4f}"
        elif buy_yes_full:
            buy_yes_status = f"rejected sum_yes_ask={sum_yes_ask:.4f} > 1"
        else:
            buy_yes_status = f"rejected missing_yes_ask={missing_yes_ask}"
        print(f"TEMP_BUCKET_ARB direction=BUY_ALL_YES sum_yes_ask={sum_yes_ask:.4f} gross_edge={buy_yes_edge:.4f} full={buy_yes_full} status={buy_yes_status}")

        # BUY_ALL_NO: buy every NO, payout = k-1 (exactly one YES wins)
        buy_no_edge = float(k - 1) - sum_no_ask - float(args.fee_rate)
        buy_no_full = (no_ask_count == k)
        if buy_no_full and buy_no_edge > 0:
            buy_no_status = f"profitable edge={buy_no_edge:.4f}"
        elif buy_no_full:
            buy_no_status = f"rejected sum_no_ask={sum_no_ask:.4f} > {k-1}"
        else:
            buy_no_status = f"rejected missing_no_ask={missing_no_ask} req={k} avail={no_ask_count}"
        print(f"TEMP_BUCKET_ARB direction=BUY_ALL_NO sum_no_ask={sum_no_ask:.4f} gross_edge={buy_no_edge:.4f} full={buy_no_full} status={buy_no_status}")

        # SELL_ALL_YES: sell every YES (mint/redeem complete set), gross = sum_yes_bid
        sell_yes_edge = sum_yes_bid - 1.0 - float(args.fee_rate)
        sell_yes_full = (yes_bid_count == k)
        if sell_yes_full and sell_yes_edge > 0:
            sell_yes_status = f"theoretical_gross edge={sell_yes_edge:.4f} requires_mint_negrisk_verified"
        elif sell_yes_full:
            sell_yes_status = f"rejected sum_yes_bid={sum_yes_bid:.4f} < 1"
        else:
            sell_yes_status = f"rejected missing_yes_bid={missing_yes_bid}"
        print(f"TEMP_BUCKET_ARB direction=SELL_ALL_YES sum_yes_bid={sum_yes_bid:.4f} gross_edge={sell_yes_edge:.4f} full={sell_yes_full} status={sell_yes_status}")

        # SELL_ALL_NO: sell every NO, theoretical only
        sell_no_full = (no_bid_count == k)
        sell_no_status = f"bid_count={no_bid_count}/{k} theoretical_only" if sell_no_full else f"rejected missing_no_bid={missing_no_bid}"
        print(f"TEMP_BUCKET_ARB direction=SELL_ALL_NO sum_no_bid={sum_no_bid:.4f} full={sell_no_full} status={sell_no_status}")

    min_edge_by_kind = parse_kind_min_edges(getattr(args, "paper_min_edge_by_kind", ""))
    executor = PaperExecutor(
        max_notional_per_trade=Decimal(str(args.max_notional)),
        max_book_age_ms=int(args.max_book_age_ms),
        min_edge=Decimal(str(args.paper_min_edge)),
        min_edge_by_kind=min_edge_by_kind,
        enable_negrisk_paper=bool(getattr(args, "enable_negrisk_paper", False)),
        temperature_validations=temp_validations,
    )
    results_raw = [executor.simulate(opp, books, Decimal(str(args.fee_rate))) for opp in opportunities]
    seen_store = SeenOpportunityStore(getattr(args, "paper_seen_keys", None))
    results, duplicate_observations = apply_dedup(results_raw, seen_store, accepted_only=True)

    suspicious_path = getattr(args, "suspicious_negrisk_output", None)
    if suspicious_path:
        suspicious_count = write_suspicious_negrisk_csv(opportunities, suspicious_path)
        print(f"suspicious_negrisk={suspicious_count} wrote={suspicious_path}")

    accepted = [r for r in results if r.accepted]
    rejected = [r for r in results if not r.accepted]
    negrisk_rejected = [r for r in results if r.reason == "negrisk_disabled_requires_manual_verification"]
    negrisk_accepted = [r for r in accepted if r.kind in ("NEGRISK_BUY_ALL_YES", "NEGRISK_BUY_ALL_NO")]
    bad_verified = sum(1 for r in negrisk_accepted if r.verification_status != "verified")
    print(
        f"paper_results={len(opportunities)} accepted={len(accepted)} rejected={len(rejected)} "
        f"duplicates={duplicate_observations} negrisk_guarded={len(negrisk_rejected)} "
        f"verified_negrisk={len(negrisk_accepted)} bad_accepted_negrisk={bad_verified}"
    )
    for result in accepted[: args.top]:
        print(
            f"PAPER_ACCEPT kind={result.kind} size={result.requested_size} "
            f"edge={result.estimated_edge_per_share} profit={result.estimated_profit} "
            f"event={result.event_title[:80]}"
        )
    for result in negrisk_rejected[: min(args.top, 5)]:
        print(
            f"PAPER_GUARD kind={result.kind} reason={result.reason} "
            f"edge={result.estimated_edge_per_share} profit={result.estimated_profit} "
            f"event={result.event_title[:80]}"
        )
    if args.paper_csv:
        append_csv(results, args.paper_csv)
        print(f"wrote={args.paper_csv}")
    if args.paper_jsonl:
        append_jsonl(results, args.paper_jsonl)
        print(f"wrote={args.paper_jsonl}")
    if seen_store.path:
        new_keys = [r.opportunity_key for r in results if r.accepted and not r.duplicate]
        append_seen_paper_keys(new_keys, seen_store.path)
        print(f"paper_seen_new={len(new_keys)} wrote={seen_store.path}")
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
    scan.add_argument("--all-markets", action="store_true", help="deprecated compatibility flag")
    scan.add_argument("--weather-only", action="store_true", help="restrict discovery to weather-like events")
    scan.add_argument("--diagnostics", action="store_true", help="print scanner diagnostics even if near-miss output is disabled")
    scan.add_argument("--near-miss-output", default="near_misses.csv")
    scan.add_argument("--near-miss-top", type=int, default=50)
    scan.add_argument("--suspicious-negrisk-output", default="paper_logs/suspicious_negrisk.csv")
    scan.add_argument("--target-temperature-events", action="store_true", help="enable targeted temperature event discovery via search terms and slugs")
    scan.add_argument("--temperature-search-terms", default="", help="comma-separated search terms, e.g. 'highest temperature,lowest temperature'")
    scan.add_argument("--temperature-search-limit", type=int, default=100, help="limit per search term")
    scan.add_argument("--target-event-slugs", default="", help="comma-separated event slugs to explicitly fetch")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pm_weather_arb")
    sub = parser.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan", help="scan Polymarket weather markets for arbitrage candidates")
    _add_scan_args(scan)
    scan.set_defaults(func=run_scan)

    paper = sub.add_parser("paper", help="scan and simulate FOK-style paper execution")
    _add_scan_args(paper)
    paper.add_argument("--paper-min-edge", default="0.02")
    paper.add_argument("--paper-min-edge-by-kind", default="", help="comma map such as YES_NO_BUY_BOTH=0.005,YES_NO_SPLIT_SELL_BOTH=0.005")
    paper.add_argument("--enable-negrisk-paper", action="store_true", help="allow NegRisk opportunities to be accepted in paper after manual verification")
    paper.add_argument("--max-notional", default="10")
    paper.add_argument("--max-book-age-ms", type=int, default=500)
    paper.add_argument("--paper-seen-keys", default="paper_logs/paper_seen_keys.txt")
    paper.add_argument("--paper-csv", default="paper_logs/paper_executions.csv")
    paper.add_argument("--paper-jsonl", default="paper_logs/paper_executions.jsonl")
    paper.set_defaults(func=run_paper)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)
