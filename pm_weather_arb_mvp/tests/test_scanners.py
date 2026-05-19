from __future__ import annotations

import unittest
from decimal import Decimal

from pm_weather_arb.scanners import scan_all
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


class ScannerTests(unittest.TestCase):
    def test_yes_no_buy_both(self):
        m = market("e1", "m1", "Will NYC high temp be at least 80F?", "Y1", "N1")
        books = {"Y1": book("Y1", asks=[("0.48", "50")]), "N1": book("N1", asks=[("0.49", "50")])}
        opps = scan_all([m], books, Decimal("0"), Decimal("0.005"), Decimal("5"), Decimal("50"))
        kinds = [o.kind for o in opps]
        self.assertIn("YES_NO_BUY_BOTH", kinds)
        opp = next(o for o in opps if o.kind == "YES_NO_BUY_BOTH")
        self.assertEqual(opp.expected_profit, Decimal("1.50"))

    def test_yes_no_split_sell_both(self):
        m = market("e1", "m1", "Will NYC high temp be at least 80F?", "Y1", "N1")
        books = {"Y1": book("Y1", bids=[("0.52", "50")]), "N1": book("N1", bids=[("0.51", "50")])}
        opps = scan_all([m], books, Decimal("0"), Decimal("0.005"), Decimal("5"), Decimal("50"))
        self.assertIn("YES_NO_SPLIT_SELL_BOTH", [o.kind for o in opps])

    def test_threshold_nested(self):
        low = market("e2", "low", "Will NYC high temperature be at least 80F?", "YLOW", "NLOW")
        high = market("e2", "high", "Will NYC high temperature be at least 85F?", "YHIGH", "NHIGH")
        books = {
            "YLOW": book("YLOW", asks=[("0.47", "25")]),
            "NLOW": book("NLOW"),
            "YHIGH": book("YHIGH"),
            "NHIGH": book("NHIGH", asks=[("0.48", "25")]),
        }
        opps = scan_all([low, high], books, Decimal("0"), Decimal("0.005"), Decimal("5"), Decimal("25"))
        self.assertIn("THRESHOLD_NESTED_BUY_SUPER_YES_SUB_NO", [o.kind for o in opps])

    def test_threshold_nested_ignores_non_weather_semantics(self):
        low = market("e2", "low", "Will Trump temperature be at least 80F?", "YLOW", "NLOW")
        high = market("e2", "high", "Will Trump temperature be at least 85F?", "YHIGH", "NHIGH")
        books = {
            "YLOW": book("YLOW", asks=[("0.47", "25")]),
            "NLOW": book("NLOW"),
            "YHIGH": book("YHIGH"),
            "NHIGH": book("NHIGH", asks=[("0.48", "25")]),
        }
        opps = scan_all([low, high], books, Decimal("0"), Decimal("0.005"), Decimal("5"), Decimal("25"))
        self.assertNotIn("THRESHOLD_NESTED_BUY_SUPER_YES_SUB_NO", [o.kind for o in opps])

    def test_negrisk_buy_all_yes(self):
        m1 = market("e3", "m1", "Outcome A", "YA", "NA", neg=True)
        m2 = market("e3", "m2", "Outcome B", "YB", "NB", neg=True)
        m3 = market("e3", "m3", "Outcome C", "YC", "NC", neg=True)
        books = {
            "YA": book("YA", asks=[("0.20", "10")]),
            "YB": book("YB", asks=[("0.30", "10")]),
            "YC": book("YC", asks=[("0.40", "10")]),
            "NA": book("NA"),
            "NB": book("NB"),
            "NC": book("NC"),
        }
        opps = scan_all([m1, m2, m3], books, Decimal("0"), Decimal("0.005"), Decimal("5"), Decimal("10"))
        self.assertIn("NEGRISK_BUY_ALL_YES", [o.kind for o in opps])


if __name__ == "__main__":
    unittest.main()
