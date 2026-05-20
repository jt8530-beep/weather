from __future__ import annotations

import unittest
from decimal import Decimal

from pm_weather_arb.paper_executor import PaperExecutor
from pm_weather_arb.scanners import scan_all
from pm_weather_arb.types import BookLevel, Market, OrderBook, Token
from pm_weather_arb.ws_market import MarketBookCache


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


class PaperAndWsTests(unittest.TestCase):
    def test_paper_accepts_buy_both(self):
        m = market("e1", "m1", "Will NYC high temp be at least 80F?", "Y1", "N1")
        books = {"Y1": book("Y1", asks=[("0.45", "50")]), "N1": book("N1", asks=[("0.48", "50")])}
        opps = scan_all([m], books, Decimal("0"), Decimal("0.005"), Decimal("5"), Decimal("20"))
        opp = next(o for o in opps if o.kind == "YES_NO_BUY_BOTH")
        result = PaperExecutor(max_notional_per_trade=Decimal("20"), min_edge=Decimal("0.02")).simulate(
            opp, books, Decimal("0")
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "paper_fok_all_legs_filled")

    def test_paper_accepts_split_sell_both(self):
        m = market("e1", "m1", "Will NYC high temp be at least 80F?", "Y1", "N1")
        books = {"Y1": book("Y1", bids=[("0.53", "50")]), "N1": book("N1", bids=[("0.52", "50")])}
        opps = scan_all([m], books, Decimal("0"), Decimal("0.005"), Decimal("5"), Decimal("20"))
        opp = next(o for o in opps if o.kind == "YES_NO_SPLIT_SELL_BOTH")
        result = PaperExecutor(max_notional_per_trade=Decimal("30"), min_edge=Decimal("0.02")).simulate(
            opp, books, Decimal("0")
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.legs[0].reason, "paper_split_collateral")

    def test_paper_rejects_negrisk_by_default(self):
        m1 = market("e2", "m1", "Outcome A", "YA", "NA", neg=True)
        m2 = market("e2", "m2", "Outcome B", "YB", "NB", neg=True)
        books = {
            "YA": book("YA", asks=[("0.20", "50")]),
            "YB": book("YB", asks=[("0.30", "50")]),
            "NA": book("NA"),
            "NB": book("NB"),
        }
        opps = scan_all([m1, m2], books, Decimal("0"), Decimal("0.005"), Decimal("5"), Decimal("20"))
        opp = next(o for o in opps if o.kind == "NEGRISK_BUY_ALL_YES")
        result = PaperExecutor(max_notional_per_trade=Decimal("20"), min_edge=Decimal("0.02")).simulate(
            opp, books, Decimal("0")
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "negrisk_disabled_requires_manual_verification")

    def test_ws_price_change_updates_book(self):
        cache = MarketBookCache()
        cache.apply_message(
            {
                "event_type": "book",
                "asset_id": "Y1",
                "market": "M1",
                "bids": [{"price": "0.40", "size": "10"}],
                "asks": [{"price": "0.50", "size": "10"}],
                "timestamp": "1000",
            }
        )
        cache.apply_message(
            {
                "event_type": "price_change",
                "market": "M1",
                "price_changes": [
                    {"asset_id": "Y1", "side": "BUY", "price": "0.41", "size": "7", "hash": "h"},
                    {"asset_id": "Y1", "side": "SELL", "price": "0.50", "size": "0", "hash": "h"},
                ],
                "timestamp": "1001",
            }
        )
        cached = cache.get("Y1")
        assert cached is not None
        self.assertEqual(cached.best_bid(), Decimal("0.41"))
        self.assertIsNone(cached.best_ask())


if __name__ == "__main__":
    unittest.main()
