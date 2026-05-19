from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional


D = Decimal


@dataclass(frozen=True)
class Token:
    outcome: str
    token_id: str


@dataclass
class Market:
    event_id: str
    event_slug: str
    event_title: str
    market_id: str
    market_slug: str
    question: str
    description: str
    condition_id: str
    neg_risk: bool
    enable_order_book: bool
    active: bool
    closed: bool
    outcomes: List[str]
    tokens: List[Token]
    minimum_tick_size: Optional[str] = None
    fees_enabled: Optional[bool] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def token_for(self, outcome_name: str) -> Optional[Token]:
        target = outcome_name.strip().lower()
        for token in self.tokens:
            if token.outcome.strip().lower() == target:
                return token
        return None

    @property
    def yes_token(self) -> Optional[Token]:
        return self.token_for("yes") or (self.tokens[0] if self.tokens else None)

    @property
    def no_token(self) -> Optional[Token]:
        return self.token_for("no") or (self.tokens[1] if len(self.tokens) > 1 else None)


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass
class OrderBook:
    token_id: str
    bids: List[BookLevel]
    asks: List[BookLevel]
    market: Optional[str] = None
    tick_size: Optional[str] = None
    min_order_size: Optional[Decimal] = None
    neg_risk: Optional[bool] = None
    hash: Optional[str] = None

    def best_bid(self) -> Optional[Decimal]:
        return self.bids[0].price if self.bids else None

    def best_ask(self) -> Optional[Decimal]:
        return self.asks[0].price if self.asks else None


@dataclass(frozen=True)
class FillQuote:
    side: str
    token_id: str
    requested_size: Decimal
    filled_size: Decimal
    gross_cash: Decimal
    avg_price: Optional[Decimal]
    fee: Decimal
    complete: bool

    @property
    def total_cost(self) -> Decimal:
        if self.side.upper() == "BUY":
            return self.gross_cash + self.fee
        raise ValueError("total_cost only applies to BUY quotes")

    @property
    def net_proceeds(self) -> Decimal:
        if self.side.upper() == "SELL":
            return self.gross_cash - self.fee
        raise ValueError("net_proceeds only applies to SELL quotes")


@dataclass(frozen=True)
class Leg:
    action: str  # BUY / SELL / SPLIT / MERGE
    outcome: str
    market_id: str
    question: str
    token_id: str
    side_hint: str
    size: Decimal
    avg_price: Optional[Decimal]
    fee: Decimal = Decimal("0")


@dataclass
class Opportunity:
    kind: str
    event_id: str
    event_title: str
    legs: List[Leg]
    size: Decimal
    min_payout: Decimal
    total_cost: Decimal
    total_proceeds: Decimal
    expected_profit: Decimal
    edge_per_share: Decimal
    notes: str = ""

    def as_row(self) -> Dict[str, str]:
        return {
            "kind": self.kind,
            "event_id": self.event_id,
            "event_title": self.event_title,
            "size": str(self.size),
            "min_payout": str(self.min_payout),
            "total_cost": str(self.total_cost),
            "total_proceeds": str(self.total_proceeds),
            "expected_profit": str(self.expected_profit),
            "edge_per_share": str(self.edge_per_share),
            "notes": self.notes,
            "legs": " | ".join(
                f"{leg.action} {leg.outcome} size={leg.size} avg={leg.avg_price} token={leg.token_id[:12]}... q={leg.question[:80]}"
                for leg in self.legs
            ),
        }
