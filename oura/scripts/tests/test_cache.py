import datetime
import os
import tempfile
import time
import unittest
from unittest.mock import patch
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import oura_client

D = datetime.date


class MergeTest(unittest.TestCase):
    def test_id_dedupe_overwrites_by_id(self):
        old = [{"id": "a", "score": 1}, {"id": "b", "score": 2}]
        new = [{"id": "a", "score": 9}]
        out = oura_client.merge_documents("daily_sleep", old, new)
        self.assertEqual({d["id"]: d["score"] for d in out}, {"a": 9, "b": 2})

    def test_sleep_keeps_multiple_docs_per_day(self):
        old = [{"id": "s1", "day": "2026-11-10", "type": "long_sleep"}]
        new = [{"id": "s2", "day": "2026-11-10", "type": "sleep"}]
        out = oura_client.merge_documents("sleep", old, new)
        self.assertEqual(len(out), 2)

    def test_timeseries_dedupes_by_timestamp_not_id(self):
        old = [{"timestamp": "2026-11-10T00:00:00+00:00", "bpm": 50}]
        new = [{"timestamp": "2026-11-10T00:00:00+00:00", "bpm": 55}]
        out = oura_client.merge_documents("heartrate", old, new)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["bpm"], 55)

    def test_timeseries_dedupe_does_not_collapse_distinct_rows(self):
        new = [{"timestamp": "2026-11-10T0%d:00:00+00:00" % i, "bpm": 50 + i}
               for i in range(5)]
        out = oura_client.merge_documents("heartrate", [], new)
        self.assertEqual(len(out), 5)


class FreshnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"OURA_CACHE_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_no_cache_needs_fetch(self):
        self.assertTrue(oura_client.needs_fetch("daily_sleep", D(2026, 11, 1), D(2026, 11, 2)))

    def test_covered_range_needs_no_fetch(self):
        oura_client.save_cache("daily_sleep", {
            "fetched_at": time.time(), "documents": [],
            "fetched_ranges": [{"start": "2026-11-01", "end": "2026-11-05",
                                "fetched_at": time.time()}]})
        self.assertFalse(oura_client.needs_fetch("daily_sleep", D(2026, 11, 2), D(2026, 11, 3)))

    def test_uncovered_range_needs_fetch(self):
        oura_client.save_cache("daily_sleep", {
            "fetched_at": time.time(), "documents": [],
            "fetched_ranges": [{"start": "2026-11-01", "end": "2026-11-05",
                                "fetched_at": time.time()}]})
        self.assertTrue(oura_client.needs_fetch("daily_sleep", D(2026, 11, 10), D(2026, 11, 12)))

    def test_window_including_today_always_refetches(self):
        today = D.today()
        oura_client.save_cache("daily_sleep", {
            "fetched_at": time.time(), "documents": [],
            "fetched_ranges": [{"start": str(today), "end": str(today),
                                "fetched_at": time.time()}]})
        self.assertTrue(oura_client.needs_fetch("daily_sleep", today, today))

    def test_stale_range_older_than_ttl_needs_fetch(self):
        oura_client.save_cache("daily_sleep", {
            "fetched_at": time.time(), "documents": [],
            "fetched_ranges": [{"start": "2026-11-01", "end": "2026-11-05",
                                "fetched_at": time.time() - 7 * 86400}]})
        self.assertTrue(oura_client.needs_fetch("daily_sleep", D(2026, 11, 2), D(2026, 11, 3)))


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"OURA_CACHE_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_record_range_marks_window_covered(self):
        oura_client.record_range("daily_sleep", D(2026, 10, 1), D(2026, 10, 5))
        self.assertFalse(oura_client.needs_fetch("daily_sleep", D(2026, 10, 2), D(2026, 10, 4)))

    def test_save_then_load_roundtrip(self):
        oura_client.save_cache("heartrate", {
            "fetched_at": 1.0, "fetched_ranges": [], "documents": [{"timestamp": "t"}]})
        loaded = oura_client.load_cache("heartrate")
        self.assertEqual(loaded["documents"], [{"timestamp": "t"}])


if __name__ == "__main__":
    unittest.main()