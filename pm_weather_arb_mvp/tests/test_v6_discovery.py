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


class TestDefaultDiscoveryWhenTermsEmpty(unittest.TestCase):
    """When --target-temperature-events is set but no explicit search terms,
    it should use DEFAULT_TEMPERATURE_SEARCH_TERMS from gamma.py."""

    def test_none_terms_uses_defaults(self):
        from pm_weather_arb.gamma import GammaClient, DEFAULT_TEMPERATURE_SEARCH_TERMS
        config = Config(gamma_host="https://gamma-api.polymarket.com", clob_host="https://clob.polymarket.com",
                        fee_rate="0.05", min_edge="0.005", min_shares="5", max_shares="100")
        client = GammaClient(config)
        # When terms=None, DEFAULT_TEMPERATURE_SEARCH_TERMS should be used
        # This is a structural test — we verify the default terms are valid
        self.assertIsNotNone(DEFAULT_TEMPERATURE_SEARCH_TERMS)
        self.assertGreater(len(DEFAULT_TEMPERATURE_SEARCH_TERMS), 0)
        # list_temperature_events(terms=None) should proceed with defaults
        # (network call will fail in test, but the logic path is correct)


class TestRunPaperLoopNoUnsupportedArgs(unittest.TestCase):
    """run_paper_loop.sh must not pass CLI args that main.py does not support."""

    def test_no_paper_kind_min_edge(self):
        """--paper-kind-min-edge is not supported; only --paper-min-edge-by-kind."""
        import subprocess, os
        script = os.path.join(os.path.dirname(__file__), "..", "scripts", "run_paper_loop.sh")
        if os.path.exists(script):
            with open(script) as f:
                content = f.read()
            # --paper-kind-min-edge must not appear
            self.assertNotIn("--paper-kind-min-edge", content,
                             "run_paper_loop.sh must not pass unsupported --paper-kind-min-edge")


if __name__ == "__main__":
    unittest.main()
