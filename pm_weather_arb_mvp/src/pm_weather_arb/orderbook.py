from __future__ import annotations

from decimal import Decimal
from typing import Iterable, List, Tuple

from .fees import taker_fee
from .types import BookLevel, FillQuote, OrderBook


def cumulative_candidate_sizes(*level_lists: Iterable[BookLevel], max_size: Decimal) -> List[Decimal]:
    """Candidate sizes where at least one leg's marginal price changes."""
    candidates = {max_size}
    for levels in level_lists:
        running = Decimal("0")
        for level in levels:
            running += level.size
            if running > 0:
                candidates.add(min(running, max_size))
            if running >= max_size:
                break
    return sorted(c for c in candidates if c > 0)


def _walk_levels(levels: List[BookLevel], size: Decimal) -> Tuple[Decimal, Decimal, bool]:
    remaining = size
    gross = Decimal("0")
    filled = Decimal("0")
    for level in levels:
        if remaining <= 0:
            break
        take = min(level.size, remaining)
        gross += take * level.price
        filled += take
        remaining -= take
    return filled, gross, remaining <= 0


def quote_buy(book: OrderBook, size: Decimal, fee_rate: Decimal) -> FillQuote:
    filled, gross, complete = _walk_levels(book.asks, size)
    avg = gross / filled if filled > 0 else None
    fee = taker_fee(filled, avg, fee_rate) if avg is not None else Decimal("0")
    return FillQuote(
        side="BUY",
        token_id=book.token_id,
        requested_size=size,
        filled_size=filled,
        gross_cash=gross,
        avg_price=avg,
        fee=fee,
        complete=complete,
    )


def quote_sell(book: OrderBook, size: Decimal, fee_rate: Decimal) -> FillQuote:
    filled, gross, complete = _walk_levels(book.bids, size)
    avg = gross / filled if filled > 0 else None
    fee = taker_fee(filled, avg, fee_rate) if avg is not None else Decimal("0")
    return FillQuote(
        side="SELL",
        token_id=book.token_id,
        requested_size=size,
        filled_size=filled,
        gross_cash=gross,
        avg_price=avg,
        fee=fee,
        complete=complete,
    )
