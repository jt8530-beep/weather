from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .orderbook import quote_buy, quote_sell
from .temperature_buckets import TemperatureBucketValidation
from .types import Leg, Opportunity, OrderBook


DEFAULT_ALLOWED_PAPER_KINDS = ",".join(
    [
        "YES_NO_BUY_BOTH",
        "YES_NO_SPLIT_SELL_BOTH",
    ]
)
NEGRISK_KINDS = {"NEGRISK_BUY_ALL_YES", "NEGRISK_BUY_ALL_NO"}
KEY_PRICE_PRECISION = Decimal("0.0001")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_kind_decimal_map(spec: str | None) -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    if not spec:
        return out
    for item in spec.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        out[key] = Decimal(value)
    return out


def _q_decimal(value: Decimal | None, places: str = "0.0001") -> str:
    if value is None:
        return ""
    return str(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def opportunity_key(opportunity: Opportunity) -> str:
    leg_bits = []
    for leg in sorted(opportunity.legs, key=lambda x: (x.action, x.market_id, x.outcome, x.token_id)):
        leg_bits.append(
            ":".join(
                [
                    leg.action,
                    leg.outcome,
                    leg.market_id,
                    leg.token_id,
                    _q_decimal(leg.size, "0.0001"),
                    _q_decimal(leg.avg_price, "0.0001"),
                ]
            )
        )
    payload = "|".join(
        [
            opportunity.kind,
            opportunity.event_id,
            _q_decimal(opportunity.size, "0.0001"),
            _q_decimal(opportunity.edge_per_share, "0.0001"),
            ";".join(leg_bits),
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{opportunity.kind}:{opportunity.event_id}:{digest}"


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
    opportunity_key: str
    duplicate: bool
    verification_status: str
    verification_reason: str
    verification_bucket_count: str
    verification_unit: str
    legs: List[PaperLegResult]


class PaperExecutor:
    """Paper execution harness for opportunity objects.

    V4 guardrails:
    - NegRisk paper acceptance is disabled by default, even if PM_ALLOW_KINDS includes it.
    - Per-kind paper edge thresholds are supported through PM_PAPER_MIN_EDGE_BY_KIND.
    - Each result carries a stable opportunity_key for persistent de-duplication.

    V5: verified temperature NegRisk events may be accepted.
    """

    def __init__(
        self,
        allowed_kinds: Optional[Iterable[str]] = None,
        max_notional_per_trade: Decimal = Decimal("10"),
        max_book_age_ms: int = 500,
        min_edge: Decimal = Decimal("0.02"),
        min_edge_by_kind: Optional[Dict[str, Decimal]] = None,
        enable_negrisk_paper: bool | None = None,
        temperature_validations: Optional[Dict[str, TemperatureBucketValidation]] = None,
    ):
        env_allowed = os.getenv("PM_ALLOW_KINDS", "YES_NO_BUY_BOTH,YES_NO_SPLIT_SELL_BOTH")
        if allowed_kinds is None:
            allowed_kinds = [item.strip() for item in env_allowed.split(",") if item.strip()]
        self.allowed_kinds = set(allowed_kinds)
        self.max_notional_per_trade = max_notional_per_trade
        self.max_book_age_ms = max_book_age_ms
        self.min_edge = min_edge
        self.min_edge_by_kind = dict(min_edge_by_kind or {})
        self.min_edge_by_kind.update(parse_kind_decimal_map(os.getenv("PM_PAPER_MIN_EDGE_BY_KIND")))
        if enable_negrisk_paper is None:
            enable_negrisk_paper = _env_bool("PM_ENABLE_NEGRISK_PAPER", False)
        self.enable_negrisk_paper = enable_negrisk_paper
        self.temperature_validations = temperature_validations or {}

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

        v = self.temperature_validations.get(opportunity.event_id)
        v_status = "verified" if (v and v.is_valid) else "unverified"
        v_reason = v.reason if v else ""
        v_bucket_count = str(v.bucket_count) if v else "0"
        v_unit = v.unit if v else "unknown"
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
            opportunity_key=opportunity_key(opportunity),
            duplicate=False,
            verification_status=v_status,
            verification_reason=v_reason,
            verification_bucket_count=v_bucket_count,
            verification_unit=v_unit,
            legs=leg_results,
        )

    def _required_edge(self, kind: str) -> Decimal:
        return self.min_edge_by_kind.get(kind, self.min_edge)

    def _precheck(
        self,
        opportunity: Opportunity,
        books: Dict[str, OrderBook],
        book_ages_ms: Optional[Dict[str, int]],
    ) -> Optional[str]:
        # V4/V5: NegRisk gate — only verified temperature bucket events pass this gate
        negrisk_verified = False
        if opportunity.kind in NEGRISK_KINDS:
            validation = self.temperature_validations.get(opportunity.event_id)
            if not (validation and validation.is_valid):
                return "negrisk_disabled_requires_manual_verification"
            negrisk_verified = True

        if opportunity.kind not in self.allowed_kinds and not negrisk_verified:
            return f"kind_not_allowed:{opportunity.kind}"

        required_edge = self._required_edge(opportunity.kind)
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
        v = self.temperature_validations.get(opportunity.event_id)
        v_status = "verified" if (v and v.is_valid) else "unverified"
        v_reason = v.reason if v else reason
        v_bucket_count = str(v.bucket_count) if v else "0"
        v_unit = v.unit if v else "unknown"
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
            opportunity_key=opportunity_key(opportunity),
            duplicate=False,
            verification_status=v_status,
            verification_reason=v_reason,
            verification_bucket_count=v_bucket_count,
            verification_unit=v_unit,
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


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------

class SeenOpportunityStore:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.seen: set[str] = set()
        if self.path and self.path.exists():
            self.seen = {line.strip() for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()}

    def is_seen(self, key: str) -> bool:
        return key in self.seen

    def mark_seen(self, key: str) -> None:
        if key in self.seen:
            return
        self.seen.add(key)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(key + "\n")


def apply_dedup(
    results: Iterable[PaperExecutionResult],
    store: SeenOpportunityStore,
    accepted_only: bool = True,
) -> Tuple[List[PaperExecutionResult], int]:
    out: List[PaperExecutionResult] = []
    duplicates = 0
    for result in results:
        if accepted_only and not result.accepted:
            out.append(result)
            continue
        if store.is_seen(result.opportunity_key):
            duplicates += 1
            out.append(_replace_duplicate(result))
            continue
        store.mark_seen(result.opportunity_key)
        out.append(result)
    return out, duplicates


def _replace_duplicate(result: PaperExecutionResult) -> PaperExecutionResult:
    return PaperExecutionResult(
        ts_ms=result.ts_ms,
        kind=result.kind,
        event_id=result.event_id,
        event_title=result.event_title,
        accepted=False,
        reason="duplicate_observation_not_counted",
        requested_size=result.requested_size,
        estimated_profit=result.estimated_profit,
        estimated_edge_per_share=result.estimated_edge_per_share,
        total_cost=result.total_cost,
        total_proceeds=result.total_proceeds,
        residual_tokens=result.residual_tokens,
        opportunity_key=result.opportunity_key,
        duplicate=True,
        verification_status=result.verification_status,
        verification_reason=result.verification_reason,
        verification_bucket_count=result.verification_bucket_count,
        verification_unit=result.verification_unit,
        legs=result.legs,
    )


# ---------------------------------------------------------------------------
# CSV / JSONL / Seen Keys
# ---------------------------------------------------------------------------

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
        "opportunity_key",
        "duplicate",
        "verification_status",
        "verification_reason",
        "verification_bucket_count",
        "verification_unit",
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
