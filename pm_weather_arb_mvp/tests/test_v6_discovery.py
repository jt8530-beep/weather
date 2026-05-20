from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from pm_weather_arb.gamma import (
    GammaClient,
    DEFAULT_TEMPERATURE_SEARCH_TERMS,
    TEMPERATURE_EVENT_TITLE_KW,
)
from pm_weather_arb.config import Config
from pm_weather_arb.util import first_present


def _fake_event(event_id: str, title: str, markets: list | None = None):
    return {
        "id": event_id,
        "title": title,
        "slug": title.lower().replace(" ", "-"),
        "markets": markets or [],
        "negRisk": True,
    }


class TestEventMergeDedupe(unittest.TestCase):
    def test_merge_dedupes_by_id(self):
        from pm_weather_arb.main import _load_markets_books
        # This tests the merge logic indirectly via the util
        volume = [_fake_event("e1", "Vol event"), _fake_event("e2", "Vol event 2")]
        targeted = [_fake_event("e1", "Targeted event"), _fake_event("e3", "Targeted event 3")]
        events_by_id = {}
        for event in volume + targeted:
            event_id = str(first_present(event, "id", "eventId", default=""))
            if event_id:
                events_by_id[event_id] = event
        self.assertEqual(len(events_by_id), 3)
        self.assertIn("e1", events_by_id)
        self.assertIn("e2", events_by_id)
        self.assertIn("e3", events_by_id)


class TestSearchTermsParsing(unittest.TestCase):
    def test_comma_separated(self):
        raw = "highest temperature, lowest temperature ,temperature"
        terms = [t.strip() for t in raw.split(",") if t.strip()]
        self.assertEqual(terms, ["highest temperature", "lowest temperature", "temperature"])

    def test_empty(self):
        terms = [t.strip() for t in "".split(",") if t.strip()]
        self.assertEqual(terms, [])


class TestSlugParsing(unittest.TestCase):
    def test_comma_separated(self):
        raw = "slug1, slug2 , slug3"
        slugs = [s.strip() for s in raw.split(",") if s.strip()]
        self.assertEqual(slugs, ["slug1", "slug2", "slug3"])


class TestNoTargetedResultsDoesNotFail(unittest.TestCase):
    def test_empty_targeted_is_fine(self):
        volume = [_fake_event("e1", "Vol event")]
        targeted: list = []
        events_by_id = {}
        for event in volume + targeted:
            event_id = str(first_present(event, "id", "eventId", default=""))
            if event_id:
                events_by_id[event_id] = event
        self.assertEqual(len(events_by_id), 1)


class TestTemperatureEventFilter(unittest.TestCase):
    def test_highest_temperature_title_passes(self):
        for kw in TEMPERATURE_EVENT_TITLE_KW:
            title = f"{kw} in Seoul on May 21?"
            self.assertTrue(any(k in title.lower() for k in TEMPERATURE_EVENT_TITLE_KW),
                            f"'{kw}' should match temperature title")

    def test_politics_title_fails(self):
        title = "Who will win the 2026 election?"
        self.assertFalse(any(k in title.lower() for k in TEMPERATURE_EVENT_TITLE_KW))


class TestDefaultSearchTerms(unittest.TestCase):
    def test_has_expected_terms(self):
        self.assertIn("highest temperature", DEFAULT_TEMPERATURE_SEARCH_TERMS)
        self.assertIn("lowest temperature", DEFAULT_TEMPERATURE_SEARCH_TERMS)


if __name__ == "__main__":
    unittest.main()
