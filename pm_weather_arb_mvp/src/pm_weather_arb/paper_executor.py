from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .orderbook import quote_buy, quote_sell
from .types import Leg, Opportunity, OrderBook


DEFAULT_ALLOWED_PAPER_KINDS = ",".join(
    [
        "YES_NO_BUY_BOTH",
        "YES_NO_SPLIT_SELL_BOTH",
    ]
)
NEGRISK_KINDS = {"NEGRISK_BUY_ALL_YES", "NEGRISK_BUY_ALL_NO"}
KEY_PRICE_PRECISION = Decimal("0.0001")


@dataclass(frozen=True)
class PaperLegResult:
    action: str
    outcome: str
    token_id: str
    requested_size: str
    filled_size: str
    avg_price: str
    fee: str
    success: bool
    reason: str


@dataclass(frozen=True)
class PaperExecutionResult:
    ts_ms: int
    kind: str
    event_id: str
    event_title: str
    accepted: bool
    reason: str
    requested_size: str
    estimated_profit: str
    estimated_edge_per_share: str
    total_cost: str
    total_proceeds: str
    residual_tokens: str
    legs: List[PaperLegResult]


class PaperExecutor:
    """Paper execution harness for opportunity objects.

    This class intentionally does not sign orders. It simulates whether the current
    local book can satisfy the opportunity legs under FOK-style rules and records
    residual exposure if a later leg fails.
    """

    def __init__(
        self,
        allowed_kinds: Optional[Iterable[str]] = None,
        max_notional_per_trade: Decimal = Decimal("10"),
        max_book_age_ms: int = 500,
        min_edge: Decimal = Decimal("0.02"),
        per_kind_min_edge: Optional[Dict[str, Decimal]] = None,
    ):
        env_allowed = os.getenv("PM_ALLOW_KINDS", DEFAULT_ALLOWED_PAPER_KINDS)
        if allowed_kinds is None:
            allowed_kinds = [item.strip() for item in env_allowed.split(",") if item.strip()]
        self.allowed_kinds = set(allowed_kinds)
        self.max_notional_per_trade = max_notional_per_trade
        self.max_book_age_ms = max_book_age_ms
        self.min_edge = min_edge
        self.per_kind_min_edge = per_kind_min_edge or parse_kind_min_edges(os.getenv("PM_KIND_MIN_EDGE", ""))

    def simulate(
        self,
        opportunity: Opportunity,
        books: Dict[str, OrderBook],
        fee_rate: Decimal,
        book_ages_ms: Optional[Dict[str, int]] = None,
    ) -> PaperExecutionResult:
        ts_ms = int(time.time() * 1000)
        leg_results: List[PaperLegResult] = []
        residual: List[str] = []

        rejection = self._precheck(opportunity, books, book_ages_ms)
        if rejection:
            return self._result(ts_ms, opportunity, False, rejection, leg_results, residual)

        total_cost = Decimal("0")
        total_proceeds = Decimal("0")
        for leg in opportunity.legs:
            if leg.action == "SPLIT":
                total_cost += leg.size
                residual.append(f"SPLIT:{leg.market_id}:{leg.size}")
                leg_results.append(
                    PaperLegResult(
                        action=leg.action,
                        outcome=leg.outcome,
                        token_id=leg.token_id,
                        requested_size=str(leg.size),
                        filled_size=str(leg.size),
                        avg_price=str(leg.avg_price or ""),
                        fee=str(leg.fee),
                        success=True,
                        reason="paper_split_collateral",
                    )
                )
                continue

            book = books.get(leg.token_id)
            if book is None:
                leg_results.append(_failed_leg(leg, "missing_book"))
                return self._result(ts_ms, opportunity, False, "missing_book", leg_results, residual)

            if leg.action == "BUY":
                quote = quote_buy(book, leg.size, fee_rate)
                if not quote.complete:
                    leg_results.append(_failed_leg(leg, "insufficient_ask_depth"))
                    return self._result(ts_ms, opportunity, False, "insufficient_ask_depth", leg_results, residual)
                total_cost += quote.total_cost
                residual.append(f"BUY:{leg.token_id}:{quote.filled_size}")
                leg_results.append(
                    PaperLegResult(
                        action=leg.action,
                        outcome=leg.outcome,
                        token_id=leg.token_id,
                        requested_size=str(leg.size),
                        filled_size=str(quote.filled_size),
                        avg_price=str(quote.avg_price or ""),
                        fee=str(quote.fee),
                        success=True,
                        reason="filled_paper_fok",
                    )
                )
            elif leg.action == "SELL":
                quote = quote_sell(book, leg.size, fee_rate)
                if not quote.complete:
                    leg_results.append(_failed_leg(leg, "insufficient_bid_depth"))
                    return self._result(ts_ms, opportunity, False, "insufficient_bid_depth", leg_results, residual)
                total_proceeds += quote.net_proceeds
                residual.append(f"SELL:{leg.token_id}:{quote.filled_size}")
                leg_results.append(
                    PaperLegResult(
                        action=leg.action,
                        outcome=leg.outcome,
                        token_id=leg.token_id,
                        requested_size=str(leg.size),
                        filled_size=str(quote.filled_size),
                        avg_price=str(quote.avg_price or ""),
                        fee=str(quote.fee),
                        success=True,
                        reason="filled_paper_fok",
                    )
                )
            else:
                leg_results.append(_failed_leg(leg, "unsupported_leg_action"))
                return self._result(ts_ms, opportunity, False, "unsupported_leg_action", leg_results, residual)

        # For accepted opportunities, residual is expected until the paired set is merged or settled.
        # It is still logged explicitly so a future live executor can enforce residual limits.
        return PaperExecutionResult(
            ts_ms=ts_ms,
            kind=opportunity.kind,
            event_id=opportunity.event_id,
            event_title=opportunity.event_title,
            accepted=True,
            reason="paper_fok_all_legs_filled",
            requested_size=str(opportunity.size),
            estimated_profit=str(opportunity.expected_profit),
            estimated_edge_per_share=str(opportunity.edge_per_share),
            total_cost=str(total_cost),
            total_proceeds=str(total_proceeds),
            residual_tokens=" | ".join(residual),
            legs=leg_results,
        )

    def _precheck(
        self,
        opportunity: Opportunity,
        books: Dict[str, OrderBook],
        book_ages_ms: Optional[Dict[str, int]],
    ) -> Optional[str]:
        if opportunity.kind not in self.allowed_kinds:
            if opportunity.kind in NEGRISK_KINDS:
                return "negrisk_disabled_requires_manual_verification"
            return f"kind_not_allowed:{opportunity.kind}"
        required_edge = self.per_kind_min_edge.get(opportunity.kind, self.min_edge)
        if opportunity.edge_per_share < required_edge:
            return "edge_below_paper_min"
        notional = opportunity.total_cost if opportunity.total_cost > 0 else opportunity.total_proceeds
        if notional > self.max_notional_per_trade:
            return "notional_above_limit"
        for leg in opportunity.legs:
            if leg.action == "SPLIT":
                continue
            if leg.token_id not in books:
                return "missing_book"
            if book_ages_ms is not None:
                age = book_ages_ms.get(leg.token_id)
                if age is None:
                    return "missing_book_age"
                if age > self.max_book_age_ms:
                    return "stale_book"
        return None

    def _result(
        self,
        ts_ms: int,
        opportunity: Opportunity,
        accepted: bool,
        reason: str,
        leg_results: List[PaperLegResult],
        residual: List[str],
    ) -> PaperExecutionResult:
        return PaperExecutionResult(
            ts_ms=ts_ms,
            kind=opportunity.kind,
            event_id=opportunity.event_id,
            event_title=opportunity.event_title,
            accepted=accepted,
            reason=reason,
            requested_size=str(opportunity.size),
            estimated_profit=str(opportunity.expected_profit),
            estimated_edge_per_share=str(opportunity.edge_per_share),
            total_cost=str(opportunity.total_cost),
            total_proceeds=str(opportunity.total_proceeds),
            residual_tokens=" | ".join(residual),
            legs=leg_results,
        )


def _failed_leg(leg: Leg, reason: str) -> PaperLegResult:
    return PaperLegResult(
        action=leg.action,
        outcome=leg.outcome,
        token_id=leg.token_id,
        requested_size=str(leg.size),
        filled_size="0",
        avg_price="",
        fee="0",
        success=False,
        reason=reason,
    )


def parse_kind_min_edges(raw: str) -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        kind, value = item.split("=", 1)
        kind = kind.strip()
        value = value.strip()
        if not kind or not value:
            continue
        out[kind] = Decimal(value)
    return out


def _rounded_price(value: str | Decimal | None) -> str:
    if value in (None, ""):
        return ""
    return str(Decimal(str(value)).quantize(KEY_PRICE_PRECISION))


def paper_opportunity_key(opportunity: Opportunity) -> str:
    leg_parts = []
    for leg in opportunity.legs:
        if leg.action == "SPLIT":
            continue
        leg_parts.append(
            "|".join(
                [
                    leg.action,
                    leg.token_id,
                    str(leg.size),
                    _rounded_price(leg.avg_price),
                ]
            )
        )
    return "||".join([opportunity.kind, opportunity.event_id, *sorted(leg_parts)])


def paper_row_key(row: dict[str, str]) -> str:
    try:
        legs = json.loads(row.get("legs") or "[]")
    except json.JSONDecodeError:
        legs = []
    leg_parts = []
    for leg in legs:
        if not isinstance(leg, dict) or leg.get("action") == "SPLIT":
            continue
        leg_parts.append(
            "|".join(
                [
                    str(leg.get("action") or ""),
                    str(leg.get("token_id") or ""),
                    str(leg.get("requested_size") or ""),
                    _rounded_price(leg.get("avg_price") or ""),
                ]
            )
        )
    return "||".join([row.get("kind", ""), row.get("event_id", ""), *sorted(leg_parts)])


def load_seen_paper_keys(path: str | Path, existing_csv: str | Path | None = None) -> set[str]:
    seen_path = Path(path)
    seen: set[str] = set()
    if seen_path.exists():
        seen.update(line.strip() for line in seen_path.read_text(encoding="utf-8").splitlines() if line.strip())
    if existing_csv:
        csv_path = Path(existing_csv)
        if csv_path.exists():
            with csv_path.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if str(row.get("accepted", "")).lower() == "true":
                        key = paper_row_key(row)
                        if key:
                            seen.add(key)
    return seen


def append_seen_paper_keys(keys: Iterable[str], path: str | Path) -> None:
    key_list = [key for key in keys if key]
    if not key_list:
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        for key in key_list:
            f.write(key + "\n")


def append_jsonl(results: Iterable[PaperExecutionResult], path: str | Path) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        for result in results:
            f.write(json.dumps(asdict(result), ensure_ascii=True) + "\n")


def append_csv(results: Iterable[PaperExecutionResult], path: str | Path) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    exists = out.exists()
    fields = [
        "ts_ms",
        "kind",
        "event_id",
        "event_title",
        "accepted",
        "reason",
        "requested_size",
        "estimated_profit",
        "estimated_edge_per_share",
        "total_cost",
        "total_proceeds",
        "residual_tokens",
        "legs",
    ]
    with out.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        for result in results:
            row = asdict(result)
            row["legs"] = json.dumps(row["legs"], ensure_ascii=True)
            writer.writerow(row)
