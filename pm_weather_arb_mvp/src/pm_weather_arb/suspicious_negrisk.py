from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from .classifier import classify_market
from .paper_executor import NEGRISK_KINDS, opportunity_key
from .types import Opportunity


def is_negrisk_opportunity(opportunity: Opportunity) -> bool:
    return opportunity.kind in NEGRISK_KINDS


def write_suspicious_negrisk_csv(opportunities: Iterable[Opportunity], path: str | Path) -> int:
    rows = []
    for opp in opportunities:
        if not is_negrisk_opportunity(opp):
            continue
        market_class = "other"
        if opp.legs:
            # Build a lightweight class based on questions/event text. The classifier
            # expects a Market object, so avoid importing the full parser here.
            joined = " ".join([opp.event_title] + [leg.question for leg in opp.legs])
            market_class = _class_text(joined)
        rows.append(
            {
                "opportunity_key": opportunity_key(opp),
                "kind": opp.kind,
                "event_id": opp.event_id,
                "event_title": opp.event_title,
                "market_class": market_class,
                "size": str(opp.size),
                "edge_per_share": str(opp.edge_per_share),
                "expected_profit": str(opp.expected_profit),
                "total_cost": str(opp.total_cost),
                "min_payout": str(opp.min_payout),
                "legs_count": str(len(opp.legs)),
                "reason": "suspicious_negrisk_requires_manual_complete_outcome_verification",
                "questions": " | ".join(leg.question[:140] for leg in opp.legs),
                "token_ids": ",".join(leg.token_id for leg in opp.legs if leg.token_id),
            }
        )

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "opportunity_key",
        "kind",
        "event_id",
        "event_title",
        "market_class",
        "size",
        "edge_per_share",
        "expected_profit",
        "total_cost",
        "min_payout",
        "legs_count",
        "reason",
        "questions",
        "token_ids",
    ]
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def _class_text(text: str) -> str:
    # Minimal dependency-free classifier aligned with classifier.py. This keeps the
    # suspicious writer robust even if no original Market object is available.
    low = text.lower()
    if any(w in low for w in ["weather", "temperature", "rain", "snow", "hurricane", "storm", "wind", "fahrenheit"]):
        return "weather"
    if any(w in low for w in ["trump", "putin", "election", "president", "senate", "governor", "minister"]):
        return "politics"
    if any(w in low for w in ["nba", "nfl", "mlb", "nhl", "ufc", "lck", "championship", "season winner"]):
        return "sports"
    if any(w in low for w in ["bitcoin", "btc", "ethereum", "crypto"]):
        return "crypto"
    if any(w in low for w in ["fed", "rate cut", "inflation", "cpi", "gdp"]):
        return "macro"
    if any(w in low for w in ["nobel", "movie", "oscar", "grammy"]):
        return "entertainment"
    return "other"
