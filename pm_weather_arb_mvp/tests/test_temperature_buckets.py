from __future__ import annotations

import unittest
from decimal import Decimal

from pm_weather_arb.temperature_buckets import (
    TemperatureBucket,
    TemperatureBucketValidation,
    detect_unit,
    is_integral_decimal,
    parse_temperature_bucket,
    validate_temperature_bucket_event,
)
from pm_weather_arb.types import Market, Token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _market(
    event_id: str,
    event_title: str,
    market_id: str,
    question: str,
    neg_risk: bool = True,
    group_item_title: str = "",
) -> Market:
    tokens = [Token("Yes", "Y" + market_id), Token("No", "N" + market_id)]
    return Market(
        event_id=event_id,
        event_slug="event-" + event_id,
        event_title=event_title,
        market_id=market_id,
        market_slug=market_id,
        question=question,
        description="",
        condition_id="cond-" + market_id,
        neg_risk=neg_risk,
        enable_order_book=True,
        active=True,
        closed=False,
        outcomes=["Yes", "No"],
        tokens=tokens,
        raw={"groupItemTitle": group_item_title},
    )


# ---------------------------------------------------------------------------
# Unit detection
# ---------------------------------------------------------------------------

class TestDetectUnit(unittest.TestCase):
    def test_celsius_from_degree(self):
        self.assertEqual(detect_unit("12\u00b0C or below"), "C")

    def test_fahrenheit_from_degree(self):
        self.assertEqual(detect_unit("80\u00b0F or above"), "F")

    def test_celsius_word(self):
        self.assertEqual(detect_unit("12 degrees Celsius or below"), "C")

    def test_fahrenheit_word(self):
        self.assertEqual(detect_unit("80 degrees Fahrenheit"), "F")

    def test_unknown(self):
        self.assertEqual(detect_unit("12 or below"), "unknown")


# ---------------------------------------------------------------------------
# Upper parse
# ---------------------------------------------------------------------------

class TestUpperParse(unittest.TestCase):
    def test_or_below_celsius(self):
        m = _market("e1", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 12\u00b0C or below on May 21?")
        b = parse_temperature_bucket(m)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.kind, "upper")
        self.assertEqual(b.value, Decimal("12"))
        self.assertEqual(b.unit, "C")

    def test_below_first(self):
        m = _market("e1", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be below 12\u00b0C on May 21?")
        b = parse_temperature_bucket(m)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.kind, "upper")

    def test_under(self):
        m = _market("e1", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be under 12\u00b0C?")
        b = parse_temperature_bucket(m)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.kind, "upper")

    def test_or_lower(self):
        m = _market("e1", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 12\u00b0C or lower on May 21?")
        b = parse_temperature_bucket(m)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.kind, "upper")
        self.assertEqual(b.value, Decimal("12"))

    def test_or_less(self):
        m = _market("e1", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 12\u00b0C or less on May 21?")
        b = parse_temperature_bucket(m)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.kind, "upper")

    def test_and_below(self):
        m = _market("e1", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 12\u00b0C and below on May 21?")
        b = parse_temperature_bucket(m)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.kind, "upper")


# ---------------------------------------------------------------------------
# Lower parse
# ---------------------------------------------------------------------------

class TestLowerParse(unittest.TestCase):
    def test_or_above_celsius(self):
        m = _market("e1", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 22\u00b0C or above on May 21?")
        b = parse_temperature_bucket(m)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.kind, "lower")
        self.assertEqual(b.value, Decimal("22"))
        self.assertEqual(b.unit, "C")

    def test_above_first(self):
        m = _market("e1", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be above 22\u00b0C?")
        b = parse_temperature_bucket(m)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.kind, "lower")

    def test_or_higher(self):
        m = _market("e1", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 22\u00b0C or higher on May 21?")
        b = parse_temperature_bucket(m)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.kind, "lower")
        self.assertEqual(b.value, Decimal("22"))

    def test_or_more(self):
        m = _market("e1", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 22\u00b0C or more on May 21?")
        b = parse_temperature_bucket(m)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.kind, "lower")


# ---------------------------------------------------------------------------
# Exact parse
# ---------------------------------------------------------------------------

class TestExactParse(unittest.TestCase):
    def test_exact_celsius(self):
        m = _market("e1", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 13\u00b0C on May 21?")
        b = parse_temperature_bucket(m)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.kind, "exact")
        self.assertEqual(b.value, Decimal("13"))
        self.assertEqual(b.unit, "C")

    def test_not_mistake_may_date(self):
        m = _market("e1", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be on May 21?")
        b = parse_temperature_bucket(m)
        self.assertIsNone(b)

    def test_or_lower_not_parsed_as_exact(self):
        """Ensure '12°C or lower' is upper, not exact."""
        m = _market("e1", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 12\u00b0C or lower on May 21?")
        b = parse_temperature_bucket(m)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.kind, "upper", "or lower must parse as upper, not exact")

    def test_or_higher_not_parsed_as_exact(self):
        """Ensure '22°C or higher' is lower, not exact."""
        m = _market("e1", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 22\u00b0C or higher on May 21?")
        b = parse_temperature_bucket(m)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.kind, "lower", "or higher must parse as lower, not exact")


# ---------------------------------------------------------------------------
# Fahrenheit parse
# ---------------------------------------------------------------------------

class TestFahrenheitParse(unittest.TestCase):
    def test_fahrenheit_lower(self):
        m = _market("e2", "Highest temperature in Miami?", "m2",
                     "Will the highest temperature in Miami be 80\u00b0F or above?")
        b = parse_temperature_bucket(m)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.kind, "lower")
        self.assertEqual(b.value, Decimal("80"))
        self.assertEqual(b.unit, "F")

    def test_fahrenheit_exact(self):
        m = _market("e2", "Highest temperature in Miami?", "m2",
                     "Will the highest temperature in Miami be 75\u00b0F?")
        b = parse_temperature_bucket(m)
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.kind, "exact")
        self.assertEqual(b.value, Decimal("75"))
        self.assertEqual(b.unit, "F")


# ---------------------------------------------------------------------------
# Event validation
# ---------------------------------------------------------------------------

class TestContinuousEventValid(unittest.TestCase):
    def test_valid_continuous(self):
        markets = [
            _market("e10", "Highest temperature in Seoul on May 21?", "m0",
                     "Will the highest temperature in Seoul be 12\u00b0C or below on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 13\u00b0C on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m2",
                     "Will the highest temperature in Seoul be 14\u00b0C on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m3",
                     "Will the highest temperature in Seoul be 15\u00b0C on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m4",
                     "Will the highest temperature in Seoul be 16\u00b0C or above on May 21?"),
        ]
        v = validate_temperature_bucket_event("e10", "Highest temperature in Seoul on May 21?", True, markets)
        self.assertTrue(v.is_valid, f"reason={v.reason}")
        self.assertEqual(v.reason, "valid_temperature_bucket_event")
        self.assertEqual(v.unit, "C")
        self.assertEqual(v.bucket_count, 5)


class TestMissingExactInvalid(unittest.TestCase):
    def test_missing_exact(self):
        markets = [
            _market("e10", "Highest temperature in Seoul on May 21?", "m0",
                     "Will the highest temperature in Seoul be 12\u00b0C or below on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 13\u00b0C on May 21?"),
            # missing 14
            _market("e10", "Highest temperature in Seoul on May 21?", "m3",
                     "Will the highest temperature in Seoul be 15\u00b0C on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m4",
                     "Will the highest temperature in Seoul be 16\u00b0C or above on May 21?"),
        ]
        v = validate_temperature_bucket_event("e10", "Highest temperature in Seoul on May 21?", True, markets)
        self.assertFalse(v.is_valid)
        self.assertEqual(v.reason, "missing_exact_bucket")


class TestMissingUpperInvalid(unittest.TestCase):
    def test_missing_upper(self):
        markets = [
            _market("e10", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 13\u00b0C on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m2",
                     "Will the highest temperature in Seoul be 14\u00b0C on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m4",
                     "Will the highest temperature in Seoul be 15\u00b0C or above on May 21?"),
        ]
        v = validate_temperature_bucket_event("e10", "Highest temperature in Seoul on May 21?", True, markets)
        self.assertFalse(v.is_valid)
        self.assertEqual(v.reason, "missing_upper_boundary")


class TestMixedUnitsInvalid(unittest.TestCase):
    def test_mixed_units(self):
        markets = [
            _market("e10", "Highest temperature in Seoul on May 21?", "m0",
                     "Will the highest temperature in Seoul be 12\u00b0C or below on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 13\u00b0F on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m2",
                     "Will the highest temperature in Seoul be 14\u00b0C or above on May 21?"),
        ]
        v = validate_temperature_bucket_event("e10", "Highest temperature in Seoul on May 21?", True, markets)
        self.assertFalse(v.is_valid)
        self.assertEqual(v.reason, "mixed_units")


class TestNonTemperatureInvalid(unittest.TestCase):
    def test_non_temperature_event(self):
        markets = [
            _market("e99", "Where will Trump and Putin meet next?", "m1",
                     "Will Trump and Putin meet in Helsinki?", neg_risk=True),
            _market("e99", "Where will Trump and Putin meet next?", "m2",
                     "Will Trump and Putin meet in Geneva?", neg_risk=True),
        ]
        v = validate_temperature_bucket_event("e99", "Where will Trump and Putin meet next?", True, markets)
        self.assertFalse(v.is_valid)
        self.assertEqual(v.reason, "not_temperature_event")


class TestDuplicateBucketInvalid(unittest.TestCase):
    def test_duplicate_bucket(self):
        markets = [
            _market("e10", "Highest temperature in Seoul on May 21?", "m0",
                     "Will the highest temperature in Seoul be 12\u00b0C or below on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 13\u00b0C on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m2",
                     "Will the highest temperature in Seoul be 13\u00b0C on May 21?"),  # duplicate
            _market("e10", "Highest temperature in Seoul on May 21?", "m3",
                     "Will the highest temperature in Seoul be 14\u00b0C on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m4",
                     "Will the highest temperature in Seoul be 15\u00b0C or above on May 21?"),
        ]
        v = validate_temperature_bucket_event("e10", "Highest temperature in Seoul on May 21?", True, markets)
        self.assertFalse(v.is_valid)
        self.assertEqual(v.reason, "duplicate_bucket")


# ---------------------------------------------------------------------------
# V5 new tests
# ---------------------------------------------------------------------------

class TestUnitUnknownInvalid(unittest.TestCase):
    def test_all_unknown_unit_rejected(self):
        """All buckets with no unit must be rejected as unit_unknown."""
        markets = [
            _market("e10", "Highest temperature in Seoul on May 21?", "m0",
                     "Will the highest temperature in Seoul be 12 or below on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 13 on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m2",
                     "Will the highest temperature in Seoul be 14 on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m3",
                     "Will the highest temperature in Seoul be 15 on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m4",
                     "Will the highest temperature in Seoul be 16 or above on May 21?"),
        ]
        v = validate_temperature_bucket_event("e10", "Highest temperature in Seoul on May 21?", True, markets)
        self.assertFalse(v.is_valid)
        self.assertEqual(v.reason, "unit_unknown")


class TestNonIntegerBucketRejected(unittest.TestCase):
    def test_non_integer_upper(self):
        markets = [
            _market("e10", "Highest temperature in Seoul on May 21?", "m0",
                     "Will the highest temperature in Seoul be 12.5\u00b0C or below on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 13\u00b0C on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m2",
                     "Will the highest temperature in Seoul be 14\u00b0C or above on May 21?"),
        ]
        v = validate_temperature_bucket_event("e10", "Highest temperature in Seoul on May 21?", True, markets)
        self.assertFalse(v.is_valid)
        self.assertEqual(v.reason, "non_integer_temperature_bucket")

    def test_non_integer_exact(self):
        markets = [
            _market("e10", "Highest temperature in Seoul on May 21?", "m0",
                     "Will the highest temperature in Seoul be 12\u00b0C or below on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 13.5\u00b0C on May 21?"),
            _market("e10", "Highest temperature in Seoul on May 21?", "m2",
                     "Will the highest temperature in Seoul be 14\u00b0C or above on May 21?"),
        ]
        v = validate_temperature_bucket_event("e10", "Highest temperature in Seoul on May 21?", True, markets)
        self.assertFalse(v.is_valid)
        self.assertEqual(v.reason, "non_integer_temperature_bucket")


class TestEventNegRiskInherited(unittest.TestCase):
    """Verify that event-level negRisk is used even when individual markets
    don't have neg_risk set, as long as the market was constructed with it."""

    def test_market_neg_risk_false_but_event_true(self):
        markets = [
            _market("e10", "Highest temperature in Seoul on May 21?", "m0",
                     "Will the highest temperature in Seoul be 12\u00b0C or below on May 21?",
                     neg_risk=True),  # event-level inherited
            _market("e10", "Highest temperature in Seoul on May 21?", "m1",
                     "Will the highest temperature in Seoul be 13\u00b0C on May 21?",
                     neg_risk=True),
            _market("e10", "Highest temperature in Seoul on May 21?", "m2",
                     "Will the highest temperature in Seoul be 14\u00b0C on May 21?",
                     neg_risk=True),
            _market("e10", "Highest temperature in Seoul on May 21?", "m3",
                     "Will the highest temperature in Seoul be 15\u00b0C on May 21?",
                     neg_risk=True),
            _market("e10", "Highest temperature in Seoul on May 21?", "m4",
                     "Will the highest temperature in Seoul be 16\u00b0C or above on May 21?",
                     neg_risk=True),
        ]
        v = validate_temperature_bucket_event("e10", "Highest temperature in Seoul on May 21?", True, markets)
        self.assertTrue(v.is_valid, f"Should be valid even if individual market neg_risk=False but event neg_risk=True. Got: {v.reason}")


class TestIsIntegralDecimal(unittest.TestCase):
    def test_integer(self):
        self.assertTrue(is_integral_decimal(Decimal("12")))

    def test_float(self):
        self.assertFalse(is_integral_decimal(Decimal("12.5")))

    def test_zero(self):
        self.assertTrue(is_integral_decimal(Decimal("0")))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# V5 blocker: verified temperature NegRisk must still pass all downstream checks
# ---------------------------------------------------------------------------

from pm_weather_arb.paper_executor import PaperExecutor
from pm_weather_arb.types import BookLevel, Leg, Opportunity, OrderBook


def _make_opp(kind="NEGRISK_BUY_ALL_YES", event_id="e10", edge="0.03", notional="8"):
    return Opportunity(
        kind=kind,
        event_id=event_id,
        event_title="Highest temperature in Seoul on May 21?",
        legs=[
            Leg("BUY", "YES", "m0", "12C or below", "Y0", "ask", Decimal("5"), Decimal("0.45"), Decimal("0")),
            Leg("BUY", "YES", "m1", "13C", "Y1", "ask", Decimal("5"), Decimal("0.50"), Decimal("0")),
        ],
        size=Decimal("5"),
        min_payout=Decimal("5"),
        total_cost=Decimal(notional),
        total_proceeds=Decimal("0"),
        expected_profit=Decimal("0.25"),
        edge_per_share=Decimal(edge),
        notes="test",
    )


def _make_books(*token_pairs):
    books = {}
    for tid, ask_price, ask_size in token_pairs:
        books[tid] = OrderBook(
            token_id=tid,
            bids=[],
            asks=[BookLevel(Decimal(str(ask_price)), Decimal(str(ask_size)))],
        )
    return books


class TestVerifiedNegRiskStillChecksEdge(unittest.TestCase):
    def test_edge_below_min_rejected(self):
        opp = _make_opp(edge="0.001")
        books = _make_books(("Y0", "0.45", "10"), ("Y1", "0.50", "10"))
        temp_validations = {
            "e10": TemperatureBucketValidation("e10", "Highest temperature in Seoul on May 21?", True, "valid_temperature_bucket_event", "C", 5, Decimal("13"), Decimal("14"), [])
        }
        executor = PaperExecutor(
            allowed_kinds=["NEGRISK_BUY_ALL_YES"],
            min_edge=Decimal("0.02"),
            temperature_validations=temp_validations,
            max_notional_per_trade=Decimal("100"),
        )
        result = executor.simulate(opp, books, Decimal("0"))
        self.assertFalse(result.accepted)
        self.assertIn("edge_below_paper_min", result.reason)


class TestVerifiedNegRiskStillChecksNotional(unittest.TestCase):
    def test_notional_above_limit_rejected(self):
        opp = _make_opp(notional="50")
        books = _make_books(("Y0", "0.45", "10"), ("Y1", "0.50", "10"))
        temp_validations = {
            "e10": TemperatureBucketValidation("e10", "Highest temperature in Seoul on May 21?", True, "valid_temperature_bucket_event", "C", 5, Decimal("13"), Decimal("14"), [])
        }
        executor = PaperExecutor(
            allowed_kinds=["NEGRISK_BUY_ALL_YES"],
            min_edge=Decimal("0.01"),
            temperature_validations=temp_validations,
            max_notional_per_trade=Decimal("10"),
        )
        result = executor.simulate(opp, books, Decimal("0"))
        self.assertFalse(result.accepted)
        self.assertIn("notional_above_limit", result.reason)


class TestVerifiedNegRiskStillChecksStaleBook(unittest.TestCase):
    def test_stale_book_rejected(self):
        opp = _make_opp(edge="0.05")
        books = _make_books(("Y0", "0.45", "10"), ("Y1", "0.50", "10"))
        temp_validations = {
            "e10": TemperatureBucketValidation("e10", "Highest temperature in Seoul on May 21?", True, "valid_temperature_bucket_event", "C", 5, Decimal("13"), Decimal("14"), [])
        }
        executor = PaperExecutor(
            allowed_kinds=["NEGRISK_BUY_ALL_YES"],
            min_edge=Decimal("0.01"),
            temperature_validations=temp_validations,
            max_notional_per_trade=Decimal("100"),
            max_book_age_ms=100,
        )
        result = executor.simulate(opp, books, Decimal("0"), book_ages_ms={"Y0": 600, "Y1": 600})
        self.assertFalse(result.accepted)
        self.assertIn("stale_book", result.reason)
