from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path
from typing import Iterable, List

from .types import Opportunity


FIELDNAMES = [
    "kind",
    "event_id",
    "event_title",
    "size",
    "min_payout",
    "total_cost",
    "total_proceeds",
    "expected_profit",
    "edge_per_share",
    "notes",
    "legs",
]


class DecimalJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        return super().default(obj)


def write_csv(opportunities: Iterable[Opportunity], path: str | Path) -> None:
    rows = [opp.as_row() for opp in opportunities]
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(opportunities: List[Opportunity], top_n: int = 20) -> None:
    print(f"found_opportunities={len(opportunities)}")
    for idx, opp in enumerate(opportunities[:top_n], start=1):
        print(
            f"#{idx} {opp.kind} profit={opp.expected_profit} edge/share={opp.edge_per_share} "
            f"size={opp.size} event={opp.event_title[:80]}"
        )
        for leg in opp.legs:
            print(
                f"  - {leg.action:<5} {leg.outcome:<12} size={leg.size} avg={leg.avg_price} "
                f"fee={leg.fee} token={leg.token_id[:18]} question={leg.question[:100]}"
            )
        if opp.notes:
            print(f"    notes: {opp.notes}")
