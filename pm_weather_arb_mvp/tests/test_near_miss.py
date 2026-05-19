from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from pm_weather_arb.near_miss import diagnose_near_misses, write_near_miss_csv
from pm_weather_arb.types import BookLevel, Market, OrderBook, Token


def market(event_id: str, market_id: str, question: str, yes: str, no: str, neg: bool = False) -> Market:
    return Market(
        event_id=event_id,
        event_slug=f"event-{event_id}",
        event_title="NYC temperature threshold event",
        market_id=market_id,
        market_slug=market_id,
        question=question,
        description="",
        condition_id=f"cond-{market_id}",
        neg_risk=neg,
        enable_order_book=True,
        active=True,
        closed=False,
        outcomes=["Yes", "No"],
        tokens=[Token("Yes", yes), Token("No", no)],
    )


def book(token_id: str, bids=None, asks=None) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        bids=[BookLevel(Decimal(str(p)), Decimal(str(s))) for p, s in (bids or [])],
        asks=[BookLevel(Decimal(str(p)), Decimal(str(s))) for p, s in (asks or [])],
    )


class NearMissTests(unittest.TestCase):
    def test_near_miss_reports_closest_even_when_not_profitable(self):
        m = market("e1", "m1", "Will NYC high temp be at least 80F?", "Y1", "N1")
        books = {
            "Y1": book("Y1", bids=[("0.47", "50")], asks=[("0.51", "50")]),
            "N1": book("N1", bids=[("0.47", "50")], asks=[("0.50", "50")]),
        }
        diag, misses = diagnose_near_misses([m], books, Decimal("0"), Decimal("5"), Decimal("20"), top_n=10)
        self.assertEqual(diag.binary_checked, 1)
        self.assertGreaterEqual(len(misses), 1)
        self.assertEqual(misses[0].kind, "YES_NO_BUY_BOTH")
        self.assertEqual(misses[0].edge_per_share, Decimal("-0.01"))

    def test_write_csv(self):
        m = market("e1", "m1", "Will NYC high temp be at least 80F?", "Y1", "N1")
        books = {
            "Y1": book("Y1", asks=[("0.51", "50")]),
            "N1": book("N1", asks=[("0.50", "50")]),
        }
        _, misses = diagnose_near_misses([m], books, Decimal("0"), Decimal("5"), Decimal("20"), top_n=10)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "near.csv"
            write_near_miss_csv(misses, path)
            self.assertIn("YES_NO_BUY_BOTH", path.read_text())


if __name__ == "__main__":
    unittest.main()
