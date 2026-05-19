from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence

from .market_classifier import filter_threshold_markets
from .normalize import ThresholdSpec, extract_threshold_spec, implies
from .orderbook import cumulative_candidate_sizes, quote_buy, quote_sell
from .types import FillQuote, Leg, Market, Opportunity, OrderBook


def _best_profitable_size_buy(
    books: Sequence[OrderBook],
    fee_rate: Decimal,
    min_edge: Decimal,
    min_shares: Decimal,
    max_shares: Decimal,
    payout_multiplier: Decimal,
) -> Optional[tuple[Decimal, List[FillQuote], Decimal, Decimal]]:
    if any(not b.asks for b in books):
        return None
    candidates = cumulative_candidate_sizes(*(b.asks for b in books), max_size=max_shares)
    best = None
    for size in candidates:
        if size < min_shares:
            continue
        quotes = [quote_buy(book, size, fee_rate) for book in books]
        if not all(q.complete for q in quotes):
            continue
        total_cost = sum((q.total_cost for q in quotes), Decimal("0"))
        min_payout = payout_multiplier * size
        profit = min_payout - total_cost
        edge = profit / size if size > 0 else Decimal("-999")
        if edge >= min_edge:
            if best is None or profit > best[3]:
                best = (size, quotes, min_payout, profit)
    return best


def _best_profitable_size_sell_pair(
    books: Sequence[OrderBook],
    fee_rate: Decimal,
    min_edge: Decimal,
    min_shares: Decimal,
    max_shares: Decimal,
    split_cost_per_pair: Decimal = Decimal("1"),
) -> Optional[tuple[Decimal, List[FillQuote], Decimal, Decimal]]:
    if any(not b.bids for b in books):
        return None
    candidates = cumulative_candidate_sizes(*(b.bids for b in books), max_size=max_shares)
    best = None
    for size in candidates:
        if size < min_shares:
            continue
        quotes = [quote_sell(book, size, fee_rate) for book in books]
        if not all(q.complete for q in quotes):
            continue
        proceeds = sum((q.net_proceeds for q in quotes), Decimal("0"))
        split_cost = split_cost_per_pair * size
        profit = proceeds - split_cost
        edge = profit / size if size > 0 else Decimal("-999")
        if edge >= min_edge:
            if best is None or profit > best[3]:
                best = (size, quotes, proceeds, profit)
    return best


def scan_yes_no_complement(
    markets: Iterable[Market],
    books: Dict[str, OrderBook],
    fee_rate: Decimal,
    min_edge: Decimal,
    min_shares: Decimal,
    max_shares: Decimal,
) -> List[Opportunity]:
    opps: List[Opportunity] = []
    for market in markets:
        yes = market.yes_token
        no = market.no_token
        if not yes or not no or yes.token_id not in books or no.token_id not in books:
            continue
        yes_book = books[yes.token_id]
        no_book = books[no.token_id]

        buy_best = _best_profitable_size_buy(
            [yes_book, no_book], fee_rate, min_edge, min_shares, max_shares, Decimal("1")
        )
        if buy_best:
            size, quotes, min_payout, profit = buy_best
            total_cost = sum((q.total_cost for q in quotes), Decimal("0"))
            opps.append(
                Opportunity(
                    kind="YES_NO_BUY_BOTH",
                    event_id=market.event_id,
                    event_title=market.event_title,
                    legs=[
                        Leg("BUY", "YES", market.market_id, market.question, yes.token_id, "ask", size, quotes[0].avg_price, quotes[0].fee),
                        Leg("BUY", "NO", market.market_id, market.question, no.token_id, "ask", size, quotes[1].avg_price, quotes[1].fee),
                    ],
                    size=size,
                    min_payout=min_payout,
                    total_cost=total_cost,
                    total_proceeds=Decimal("0"),
                    expected_profit=profit,
                    edge_per_share=profit / size,
                    notes="Buy YES+NO below 1. Hold to resolution or merge if available.",
                )
            )

        sell_best = _best_profitable_size_sell_pair(
            [yes_book, no_book], fee_rate, min_edge, min_shares, max_shares
        )
        if sell_best:
            size, quotes, proceeds, profit = sell_best
            opps.append(
                Opportunity(
                    kind="YES_NO_SPLIT_SELL_BOTH",
                    event_id=market.event_id,
                    event_title=market.event_title,
                    legs=[
                        Leg("SPLIT", "PAIR", market.market_id, market.question, "", "collateral", size, Decimal("1"), Decimal("0")),
                        Leg("SELL", "YES", market.market_id, market.question, yes.token_id, "bid", size, quotes[0].avg_price, quotes[0].fee),
                        Leg("SELL", "NO", market.market_id, market.question, no.token_id, "bid", size, quotes[1].avg_price, quotes[1].fee),
                    ],
                    size=size,
                    min_payout=Decimal("0"),
                    total_cost=size,
                    total_proceeds=proceeds,
                    expected_profit=profit,
                    edge_per_share=profit / size,
                    notes="Split collateral into YES+NO, then sell both above 1 net of fees.",
                )
            )
    return opps


def scan_neg_risk_full_sets(
    markets: Iterable[Market],
    books: Dict[str, OrderBook],
    fee_rate: Decimal,
    min_edge: Decimal,
    min_shares: Decimal,
    max_shares: Decimal,
) -> List[Opportunity]:
    by_event: Dict[str, List[Market]] = defaultdict(list)
    for market in markets:
        if market.neg_risk and market.yes_token and market.yes_token.token_id in books:
            by_event[market.event_id].append(market)

    opps: List[Opportunity] = []
    for event_id, group in by_event.items():
        if len(group) < 2:
            continue
        yes_books = [books[m.yes_token.token_id] for m in group if m.yes_token]  # type: ignore[union-attr]
        buy_best = _best_profitable_size_buy(
            yes_books, fee_rate, min_edge, min_shares, max_shares, Decimal("1")
        )
        if buy_best:
            size, quotes, min_payout, profit = buy_best
            total_cost = sum((q.total_cost for q in quotes), Decimal("0"))
            legs = []
            for market, quote in zip(group, quotes):
                token = market.yes_token
                assert token is not None
                legs.append(Leg("BUY", "YES", market.market_id, market.question, token.token_id, "ask", size, quote.avg_price, quote.fee))
            opps.append(
                Opportunity(
                    kind="NEGRISK_BUY_ALL_YES",
                    event_id=event_id,
                    event_title=group[0].event_title,
                    legs=legs,
                    size=size,
                    min_payout=min_payout,
                    total_cost=total_cost,
                    total_proceeds=Decimal("0"),
                    expected_profit=profit,
                    edge_per_share=profit / size,
                    notes="NegRisk event: buy every outcome YES below 1 net of fees. Verify no placeholder/augmented outcome before live trading.",
                )
            )

        no_markets = [m for m in group if m.no_token and m.no_token.token_id in books]
        if len(no_markets) >= 2:
            no_books = [books[m.no_token.token_id] for m in no_markets]  # type: ignore[union-attr]
            # If exactly one outcome can win, buying all K NO pays at least K-1.
            buy_no = _best_profitable_size_buy(
                no_books,
                fee_rate,
                min_edge,
                min_shares,
                max_shares,
                Decimal(len(no_markets) - 1),
            )
            if buy_no:
                size, quotes, min_payout, profit = buy_no
                total_cost = sum((q.total_cost for q in quotes), Decimal("0"))
                legs = []
                for market, quote in zip(no_markets, quotes):
                    token = market.no_token
                    assert token is not None
                    legs.append(Leg("BUY", "NO", market.market_id, market.question, token.token_id, "ask", size, quote.avg_price, quote.fee))
                opps.append(
                    Opportunity(
                        kind="NEGRISK_BUY_ALL_NO",
                        event_id=event_id,
                        event_title=no_markets[0].event_title,
                        legs=legs,
                        size=size,
                        min_payout=min_payout,
                        total_cost=total_cost,
                        total_proceeds=Decimal("0"),
                        expected_profit=profit,
                        edge_per_share=profit / size,
                        notes="NegRisk event: at most one YES can win, so all NO pays at least K-1. Verify event semantics before live trading.",
                    )
                )
    return opps


def scan_threshold_nested(
    markets: Iterable[Market],
    books: Dict[str, OrderBook],
    fee_rate: Decimal,
    min_edge: Decimal,
    min_shares: Decimal,
    max_shares: Decimal,
) -> List[Opportunity]:
    market_list = list(markets)
    market_by_id = {m.market_id: m for m in market_list}
    specs: List[ThresholdSpec] = []
    for market in market_list:
        spec = extract_threshold_spec(market)
        if spec:
            specs.append(spec)

    opps: List[Opportunity] = []
    for subset in specs:
        for superset in specs:
            if not implies(subset, superset):
                continue
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
            buy_best = _best_profitable_size_buy(
                [books[yes_super.token_id], books[no_subset.token_id]],
                fee_rate,
                min_edge,
                min_shares,
                max_shares,
                Decimal("1"),
            )
            if not buy_best:
                continue
            size, quotes, min_payout, profit = buy_best
            total_cost = sum((q.total_cost for q in quotes), Decimal("0"))
            opps.append(
                Opportunity(
                    kind="THRESHOLD_NESTED_BUY_SUPER_YES_SUB_NO",
                    event_id=m_subset.event_id,
                    event_title=m_subset.event_title,
                    legs=[
                        Leg("BUY", "YES_SUPERSET", m_superset.market_id, m_superset.question, yes_super.token_id, "ask", size, quotes[0].avg_price, quotes[0].fee),
                        Leg("BUY", "NO_SUBSET", m_subset.market_id, m_subset.question, no_subset.token_id, "ask", size, quotes[1].avg_price, quotes[1].fee),
                    ],
                    size=size,
                    min_payout=min_payout,
                    total_cost=total_cost,
                    total_proceeds=Decimal("0"),
                    expected_profit=profit,
                    edge_per_share=profit / size,
                    notes=f"Logical implication: '{m_subset.question[:80]}' implies '{m_superset.question[:80]}'.",
                )
            )
    return opps


def scan_all(
    markets: Iterable[Market],
    books: Dict[str, OrderBook],
    fee_rate: Decimal,
    min_edge: Decimal,
    min_shares: Decimal,
    max_shares: Decimal,
) -> List[Opportunity]:
    market_list = list(markets)
    opps: List[Opportunity] = []
    opps.extend(scan_yes_no_complement(market_list, books, fee_rate, min_edge, min_shares, max_shares))
    opps.extend(scan_neg_risk_full_sets(market_list, books, fee_rate, min_edge, min_shares, max_shares))
    opps.extend(scan_threshold_nested(filter_threshold_markets(market_list), books, fee_rate, min_edge, min_shares, max_shares))
    opps.sort(key=lambda x: (x.edge_per_share, x.expected_profit), reverse=True)
    return opps
