from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .market_classifier import (
    classify_market,
    classify_markets,
    filter_threshold_markets,
    semantic_required_for_kind,
    strategy_scope_for_kind,
)
from .normalize import ThresholdSpec, extract_threshold_spec, implies
from .orderbook import cumulative_candidate_sizes, quote_buy, quote_sell
from .types import FillQuote, Market, OrderBook


@dataclass(frozen=True)
class NearMiss:
    kind: str
    event_id: str
    event_title: str
    market_ids: str
    size: Decimal
    edge_per_share: Decimal
    expected_profit: Decimal
    total_cost: Decimal
    total_proceeds: Decimal
    min_payout: Decimal
    reason: str
    questions: str
    token_ids: str
    market_class: str = "other"
    strategy_scope: str = "universal"
    semantic_required: bool = False

    def as_row(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "market_class": self.market_class,
            "strategy_scope": self.strategy_scope,
            "semantic_required": str(self.semantic_required).lower(),
            "event_id": self.event_id,
            "event_title": self.event_title,
            "market_ids": self.market_ids,
            "size": str(self.size),
            "edge_per_share": str(self.edge_per_share),
            "expected_profit": str(self.expected_profit),
            "total_cost": str(self.total_cost),
            "total_proceeds": str(self.total_proceeds),
            "min_payout": str(self.min_payout),
            "reason": self.reason,
            "questions": self.questions,
            "token_ids": self.token_ids,
        }


@dataclass(frozen=True)
class ScanDiagnostics:
    markets_total: int
    tokens_total: int
    books_total: int
    books_with_asks: int
    books_with_bids: int
    books_empty: int
    binary_markets: int
    binary_checked: int
    binary_missing_books: int
    binary_empty_asks: int
    binary_empty_bids: int
    numeric_threshold_markets: int
    weather_threshold_markets: int
    threshold_specs: int
    threshold_pairs: int
    negrisk_groups: int
    negrisk_markets: int
    class_weather: int
    class_politics: int
    class_sports: int
    class_crypto: int
    class_macro: int
    class_other: int
    class_mixed: int = 0

    def as_log_line(self) -> str:
        return (
            f"diagnostics raw_markets={self.markets_total} binary_markets={self.binary_markets} "
            f"tokens={self.tokens_total} books={self.books_total} "
            f"books_with_asks={self.books_with_asks} books_with_bids={self.books_with_bids} books_empty={self.books_empty} "
            f"universal_binary_checked={self.binary_checked} binary_missing_books={self.binary_missing_books} "
            f"binary_empty_asks={self.binary_empty_asks} binary_empty_bids={self.binary_empty_bids} "
            f"numeric_threshold_markets={self.numeric_threshold_markets} "
            f"weather_threshold_markets={self.weather_threshold_markets} "
            f"threshold_specs={self.threshold_specs} threshold_pairs={self.threshold_pairs} "
            f"negrisk_groups={self.negrisk_groups} negrisk_markets={self.negrisk_markets} "
            f"class_weather={self.class_weather} class_politics={self.class_politics} "
            f"class_sports={self.class_sports} class_crypto={self.class_crypto} "
            f"class_macro={self.class_macro} class_other={self.class_other} class_mixed={self.class_mixed}"
        )


NEAR_MISS_FIELDNAMES = [
    "kind",
    "market_class",
    "strategy_scope",
    "semantic_required",
    "event_id",
    "event_title",
    "market_ids",
    "size",
    "edge_per_share",
    "expected_profit",
    "total_cost",
    "total_proceeds",
    "min_payout",
    "reason",
    "questions",
    "token_ids",
]


def _fmt_questions(markets: Sequence[Market], limit: int = 4) -> str:
    values = [m.question.replace("\n", " ")[:160] for m in markets[:limit]]
    if len(markets) > limit:
        values.append(f"... +{len(markets) - limit} more")
    return " | ".join(values)


def _candidate_sizes(level_lists: Sequence[Sequence], min_shares: Decimal, max_shares: Decimal) -> list[Decimal]:
    if not level_lists:
        return []
    sizes = cumulative_candidate_sizes(*level_lists, max_size=max_shares)
    return [s for s in sizes if s >= min_shares]


def _best_buy_edge(
    books: Sequence[OrderBook],
    fee_rate: Decimal,
    min_shares: Decimal,
    max_shares: Decimal,
    payout_multiplier: Decimal,
) -> Tuple[Optional[Decimal], Optional[List[FillQuote]], Decimal, Decimal, Decimal, str]:
    """Return best edge even if it is negative.

    Returns: (size, quotes, min_payout, total_cost, profit, reason)
    """
    if any(not b.asks for b in books):
        return None, None, Decimal("0"), Decimal("0"), Decimal("-999"), "missing_asks"
    sizes = _candidate_sizes([b.asks for b in books], min_shares, max_shares)
    if not sizes:
        return None, None, Decimal("0"), Decimal("0"), Decimal("-999"), "insufficient_ask_depth"

    best = None
    for size in sizes:
        quotes = [quote_buy(book, size, fee_rate) for book in books]
        if not all(q.complete for q in quotes):
            continue
        total_cost = sum((q.total_cost for q in quotes), Decimal("0"))
        min_payout = payout_multiplier * size
        profit = min_payout - total_cost
        edge = profit / size if size > 0 else Decimal("-999")
        candidate = (edge, size, quotes, min_payout, total_cost, profit)
        if best is None or candidate[0] > best[0]:
            best = candidate

    if best is None:
        return None, None, Decimal("0"), Decimal("0"), Decimal("-999"), "incomplete_ask_fill"
    _, size, quotes, min_payout, total_cost, profit = best
    return size, quotes, min_payout, total_cost, profit, "below_edge_threshold"


def _best_sell_edge(
    books: Sequence[OrderBook],
    fee_rate: Decimal,
    min_shares: Decimal,
    max_shares: Decimal,
    split_cost_per_pair: Decimal = Decimal("1"),
) -> Tuple[Optional[Decimal], Optional[List[FillQuote]], Decimal, Decimal, Decimal, str]:
    """Return best split-sell edge even if it is negative.

    Returns: (size, quotes, split_cost, proceeds, profit, reason)
    """
    if any(not b.bids for b in books):
        return None, None, Decimal("0"), Decimal("0"), Decimal("-999"), "missing_bids"
    sizes = _candidate_sizes([b.bids for b in books], min_shares, max_shares)
    if not sizes:
        return None, None, Decimal("0"), Decimal("0"), Decimal("-999"), "insufficient_bid_depth"

    best = None
    for size in sizes:
        quotes = [quote_sell(book, size, fee_rate) for book in books]
        if not all(q.complete for q in quotes):
            continue
        proceeds = sum((q.net_proceeds for q in quotes), Decimal("0"))
        split_cost = split_cost_per_pair * size
        profit = proceeds - split_cost
        edge = profit / size if size > 0 else Decimal("-999")
        candidate = (edge, size, quotes, split_cost, proceeds, profit)
        if best is None or candidate[0] > best[0]:
            best = candidate

    if best is None:
        return None, None, Decimal("0"), Decimal("0"), Decimal("-999"), "incomplete_bid_fill"
    _, size, quotes, split_cost, proceeds, profit = best
    return size, quotes, split_cost, proceeds, profit, "below_edge_threshold"


def _near_yes_no(
    markets: Sequence[Market],
    books: Dict[str, OrderBook],
    fee_rate: Decimal,
    min_shares: Decimal,
    max_shares: Decimal,
) -> List[NearMiss]:
    misses: List[NearMiss] = []
    for market in markets:
        market_class = classify_market(market).market_class
        yes = market.yes_token
        no = market.no_token
        if not yes or not no or yes.token_id not in books or no.token_id not in books:
            continue
        yes_book = books[yes.token_id]
        no_book = books[no.token_id]

        size, quotes, min_payout, total_cost, profit, reason = _best_buy_edge(
            [yes_book, no_book], fee_rate, min_shares, max_shares, Decimal("1")
        )
        if size is not None:
            misses.append(
                NearMiss(
                    kind="YES_NO_BUY_BOTH",
                    event_id=market.event_id,
                    event_title=market.event_title,
                    market_ids=market.market_id,
                    size=size,
                    edge_per_share=profit / size,
                    expected_profit=profit,
                    total_cost=total_cost,
                    total_proceeds=Decimal("0"),
                    min_payout=min_payout,
                    reason=reason,
                    questions=market.question,
                    token_ids=f"{yes.token_id},{no.token_id}",
                    market_class=market_class,
                    strategy_scope=strategy_scope_for_kind("YES_NO_BUY_BOTH"),
                    semantic_required=semantic_required_for_kind("YES_NO_BUY_BOTH"),
                )
            )

        size, quotes, split_cost, proceeds, profit, reason = _best_sell_edge(
            [yes_book, no_book], fee_rate, min_shares, max_shares, Decimal("1")
        )
        if size is not None:
            misses.append(
                NearMiss(
                    kind="YES_NO_SPLIT_SELL_BOTH",
                    event_id=market.event_id,
                    event_title=market.event_title,
                    market_ids=market.market_id,
                    size=size,
                    edge_per_share=profit / size,
                    expected_profit=profit,
                    total_cost=split_cost,
                    total_proceeds=proceeds,
                    min_payout=Decimal("0"),
                    reason=reason,
                    questions=market.question,
                    token_ids=f"{yes.token_id},{no.token_id}",
                    market_class=market_class,
                    strategy_scope=strategy_scope_for_kind("YES_NO_SPLIT_SELL_BOTH"),
                    semantic_required=semantic_required_for_kind("YES_NO_SPLIT_SELL_BOTH"),
                )
            )
    return misses


def _near_negrisk(
    markets: Sequence[Market],
    books: Dict[str, OrderBook],
    fee_rate: Decimal,
    min_shares: Decimal,
    max_shares: Decimal,
) -> List[NearMiss]:
    by_event: Dict[str, List[Market]] = defaultdict(list)
    for market in markets:
        if market.neg_risk:
            by_event[market.event_id].append(market)

    misses: List[NearMiss] = []
    for event_id, group in by_event.items():
        if len(group) < 2:
            continue
        group_class = classify_markets(group)
        yes_markets = [m for m in group if m.yes_token and m.yes_token.token_id in books]
        if len(yes_markets) >= 2:
            yes_books = [books[m.yes_token.token_id] for m in yes_markets]  # type: ignore[union-attr]
            size, quotes, min_payout, total_cost, profit, reason = _best_buy_edge(
                yes_books, fee_rate, min_shares, max_shares, Decimal("1")
            )
            if size is not None:
                misses.append(
                    NearMiss(
                        kind="NEGRISK_BUY_ALL_YES",
                        event_id=event_id,
                        event_title=yes_markets[0].event_title,
                        market_ids=",".join(m.market_id for m in yes_markets),
                        size=size,
                        edge_per_share=profit / size,
                        expected_profit=profit,
                        total_cost=total_cost,
                        total_proceeds=Decimal("0"),
                        min_payout=min_payout,
                        reason=reason,
                        questions=_fmt_questions(yes_markets),
                        token_ids=",".join(m.yes_token.token_id for m in yes_markets if m.yes_token),
                        market_class=group_class,
                        strategy_scope=strategy_scope_for_kind("NEGRISK_BUY_ALL_YES"),
                        semantic_required=semantic_required_for_kind("NEGRISK_BUY_ALL_YES"),
                    )
                )

        no_markets = [m for m in group if m.no_token and m.no_token.token_id in books]
        if len(no_markets) >= 2:
            no_books = [books[m.no_token.token_id] for m in no_markets]  # type: ignore[union-attr]
            size, quotes, min_payout, total_cost, profit, reason = _best_buy_edge(
                no_books, fee_rate, min_shares, max_shares, Decimal(len(no_markets) - 1)
            )
            if size is not None:
                misses.append(
                    NearMiss(
                        kind="NEGRISK_BUY_ALL_NO",
                        event_id=event_id,
                        event_title=no_markets[0].event_title,
                        market_ids=",".join(m.market_id for m in no_markets),
                        size=size,
                        edge_per_share=profit / size,
                        expected_profit=profit,
                        total_cost=total_cost,
                        total_proceeds=Decimal("0"),
                        min_payout=min_payout,
                        reason=reason,
                        questions=_fmt_questions(no_markets),
                        token_ids=",".join(m.no_token.token_id for m in no_markets if m.no_token),
                        market_class=group_class,
                        strategy_scope=strategy_scope_for_kind("NEGRISK_BUY_ALL_NO"),
                        semantic_required=semantic_required_for_kind("NEGRISK_BUY_ALL_NO"),
                    )
                )
    return misses


def _threshold_pairs(markets: Sequence[Market]) -> tuple[list[ThresholdSpec], list[tuple[ThresholdSpec, ThresholdSpec]]]:
    specs: list[ThresholdSpec] = []
    for market in markets:
        spec = extract_threshold_spec(market)
        if spec:
            specs.append(spec)
    pairs = []
    for subset in specs:
        for superset in specs:
            if implies(subset, superset):
                pairs.append((subset, superset))
    return specs, pairs


def _near_thresholds(
    markets: Sequence[Market],
    books: Dict[str, OrderBook],
    fee_rate: Decimal,
    min_shares: Decimal,
    max_shares: Decimal,
) -> List[NearMiss]:
    market_by_id = {m.market_id: m for m in markets}
    _, pairs = _threshold_pairs(markets)
    misses: List[NearMiss] = []
    for subset, superset in pairs:
        m_subset = market_by_id.get(subset.market_id)
        m_superset = market_by_id.get(superset.market_id)
        if not m_subset or not m_superset:
            continue
        yes_super = m_superset.yes_token
        no_subset = m_subset.no_token
        if not yes_super or not no_subset:
            continue
        if yes_super.token_id not in books or no_subset.token_id not in books:
            continue
        size, quotes, min_payout, total_cost, profit, reason = _best_buy_edge(
            [books[yes_super.token_id], books[no_subset.token_id]], fee_rate, min_shares, max_shares, Decimal("1")
        )
        if size is None:
            continue
        misses.append(
            NearMiss(
                kind="THRESHOLD_NESTED_BUY_SUPER_YES_SUB_NO",
                event_id=m_subset.event_id,
                event_title=m_subset.event_title,
                market_ids=f"{m_superset.market_id},{m_subset.market_id}",
                size=size,
                edge_per_share=profit / size,
                expected_profit=profit,
                total_cost=total_cost,
                total_proceeds=Decimal("0"),
                min_payout=min_payout,
                reason=reason,
                questions=f"YES_SUPER: {m_superset.question} | NO_SUB: {m_subset.question}",
                token_ids=f"{yes_super.token_id},{no_subset.token_id}",
                market_class=classify_markets([m_subset, m_superset]),
                strategy_scope=strategy_scope_for_kind("THRESHOLD_NESTED_BUY_SUPER_YES_SUB_NO"),
                semantic_required=semantic_required_for_kind("THRESHOLD_NESTED_BUY_SUPER_YES_SUB_NO"),
            )
        )
    return misses


def build_diagnostics(markets: Iterable[Market], books: Dict[str, OrderBook]) -> ScanDiagnostics:
    market_list = list(markets)
    token_ids = [token.token_id for market in market_list for token in market.tokens]
    classifications = [classify_market(market) for market in market_list]
    class_counts = Counter(item.market_class for item in classifications)
    binary_checked = 0
    binary_markets = 0
    missing_books = 0
    empty_asks = 0
    empty_bids = 0
    for market in market_list:
        yes = market.yes_token
        no = market.no_token
        if not yes or not no:
            continue
        binary_markets += 1
        if yes.token_id not in books or no.token_id not in books:
            missing_books += 1
            continue
        binary_checked += 1
        if not books[yes.token_id].asks or not books[no.token_id].asks:
            empty_asks += 1
        if not books[yes.token_id].bids or not books[no.token_id].bids:
            empty_bids += 1

    threshold_markets = filter_threshold_markets(market_list)
    specs, pairs = _threshold_pairs(threshold_markets)
    negrisk_events: Dict[str, int] = defaultdict(int)
    negrisk_market_count = 0
    for market in market_list:
        if market.neg_risk:
            negrisk_events[market.event_id] += 1
            negrisk_market_count += 1

    return ScanDiagnostics(
        markets_total=len(market_list),
        tokens_total=len(set(token_ids)),
        books_total=len(books),
        books_with_asks=sum(1 for b in books.values() if b.asks),
        books_with_bids=sum(1 for b in books.values() if b.bids),
        books_empty=sum(1 for b in books.values() if not b.asks and not b.bids),
        binary_markets=binary_markets,
        binary_checked=binary_checked,
        binary_missing_books=missing_books,
        binary_empty_asks=empty_asks,
        binary_empty_bids=empty_bids,
        numeric_threshold_markets=sum(1 for item in classifications if item.has_numeric_threshold),
        weather_threshold_markets=sum(1 for item in classifications if item.threshold_strategy_allowed),
        threshold_specs=len(specs),
        threshold_pairs=len(pairs),
        negrisk_groups=sum(1 for count in negrisk_events.values() if count >= 2),
        negrisk_markets=negrisk_market_count,
        class_weather=class_counts["weather"],
        class_politics=class_counts["politics"],
        class_sports=class_counts["sports"],
        class_crypto=class_counts["crypto"],
        class_macro=class_counts["macro"],
        class_other=class_counts["other"],
        class_mixed=class_counts["mixed"],
    )


def diagnose_near_misses(
    markets: Iterable[Market],
    books: Dict[str, OrderBook],
    fee_rate: Decimal,
    min_shares: Decimal,
    max_shares: Decimal,
    top_n: int = 50,
) -> tuple[ScanDiagnostics, list[NearMiss]]:
    market_list = list(markets)
    diagnostics = build_diagnostics(market_list, books)
    misses: list[NearMiss] = []
    misses.extend(_near_yes_no(market_list, books, fee_rate, min_shares, max_shares))
    misses.extend(_near_negrisk(market_list, books, fee_rate, min_shares, max_shares))
    misses.extend(_near_thresholds(filter_threshold_markets(market_list), books, fee_rate, min_shares, max_shares))
    misses.sort(key=lambda m: (m.edge_per_share, m.expected_profit), reverse=True)
    return diagnostics, misses[:top_n]


def write_near_miss_csv(near_misses: Iterable[NearMiss], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True) if Path(path).parent != Path(".") else None
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=NEAR_MISS_FIELDNAMES)
        writer.writeheader()
        for miss in near_misses:
            writer.writerow(miss.as_row())


def print_diagnostics(diagnostics: ScanDiagnostics, near_misses: Sequence[NearMiss], top_n: int = 10) -> None:
    print(diagnostics.as_log_line())
    if near_misses:
        closest = near_misses[0]
        print(
            f"near_misses={len(near_misses)} closest_kind={closest.kind} "
            f"closest_edge={closest.edge_per_share} closest_profit={closest.expected_profit}"
        )
        _print_closest("closest_universal", near_misses, lambda miss: miss.strategy_scope == "universal")
        _print_closest("closest_semantic", near_misses, lambda miss: miss.strategy_scope == "semantic")
        _print_closest("closest_weather", near_misses, lambda miss: miss.market_class == "weather")
        for idx, miss in enumerate(near_misses[:top_n], start=1):
            print(
                f"NEAR_MISS #{idx} kind={miss.kind} edge={miss.edge_per_share} "
                f"profit={miss.expected_profit} size={miss.size} reason={miss.reason} "
                f"class={miss.market_class} scope={miss.strategy_scope} "
                f"event={miss.event_title[:90]}"
            )
    else:
        print("near_misses=0 closest_edge=NA")


def _print_closest(label: str, near_misses: Sequence[NearMiss], predicate) -> None:
    miss = next((item for item in near_misses if predicate(item)), None)
    if miss is None:
        print(f"{label}_edge=NA")
        return
    print(
        f"{label}_kind={miss.kind} {label}_edge={miss.edge_per_share} "
        f"{label}_profit={miss.expected_profit} {label}_event={miss.event_title[:90]}"
    )
