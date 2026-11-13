import datetime
import json
import pathlib
import unittest
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import aggregate

D = datetime.date
FIX = json.loads(
    (pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "daily_full.json").read_text())


class PreviousDayTest(unittest.TestCase):
    """KPI 卡需要「较前一日」的对比值。"""

    def test_daily_includes_previous_day_scores(self):
        docs = json.loads(json.dumps(FIX))
        docs["daily_sleep"].append({"id": "ds-prev", "day": "2026-11-09", "score": 70})
        rep = aggregate.build_daily(docs, D(2026, 11, 10))
        self.assertEqual(rep["previous"]["date"], "2026-11-09")
        self.assertEqual(rep["previous"]["scores"]["sleep"], 70)

    def test_previous_missing_is_none(self):
        rep = aggregate.build_daily(FIX, D(2026, 11, 10))
        self.assertIsNone(rep["previous"]["scores"]["sleep"])


class SleepFilterTest(unittest.TestCase):
    def test_excludes_deleted_and_rest(self):
        docs = [{"id": "1", "type": "long_sleep"}, {"id": "2", "type": "deleted"},
                {"id": "3", "type": "rest"}]
        main, naps = aggregate.filter_sleep_docs(docs)
        self.assertEqual(main["id"], "1")
        self.assertEqual(naps, [])

    def test_naps_exclude_and_do_not_join_main(self):
        docs = [{"id": "1", "type": "long_sleep", "total_sleep_duration": 25000},
                {"id": "2", "type": "sleep", "total_sleep_duration": 3000},
                {"id": "3", "type": "late_nap", "total_sleep_duration": 1800}]
        main, naps = aggregate.filter_sleep_docs(docs)
        self.assertEqual(main["total_sleep_duration"], 25000)
        self.assertEqual([n["id"] for n in naps], ["2", "3"])

    def test_main_prefers_long_sleep_even_if_not_first(self):
        docs = [{"id": "nap", "type": "sleep"}, {"id": "main", "type": "long_sleep"}]
        main, _ = aggregate.filter_sleep_docs(docs)
        self.assertEqual(main["id"], "main")


class DailyReportTest(unittest.TestCase):
    def test_scores_and_fields(self):
        rep = aggregate.build_daily(FIX, D(2026, 11, 10))
        self.assertEqual(rep["period"], "daily")
        self.assertEqual(rep["start"], "2026-11-10")
        self.assertEqual(rep["scores"]["sleep"]["value"], 82)
        self.assertEqual(rep["scores"]["readiness"]["value"], 77)
        self.assertEqual(rep["scores"]["activity"]["value"], 90)
        self.assertEqual(rep["scores"]["activity"]["steps"], 8432)
        self.assertEqual(rep["sleep_main"]["efficiency"], 89)
        self.assertEqual(rep["sleep_main"]["total_sleep_duration"], 25200)
        self.assertEqual(rep["sleep_main"]["phases"], "4411222331")

    def test_missing_day_reports_empty_not_crash(self):
        rep = aggregate.build_daily(FIX, D(2026, 11, 20))
        self.assertIsNone(rep["scores"]["sleep"]["value"])
        self.assertIsNone(rep["sleep_main"])
        self.assertEqual(rep["missing_days"], ["2026-11-20"])
        self.assertEqual(rep["missing_domains"], [])

    def test_partial_domain_marked_missing(self):
        docs = {k: v for k, v in FIX.items() if k != "daily_spo2"}
        rep = aggregate.build_daily(docs, D(2026, 11, 10))
        self.assertIn("spo2", rep["missing_domains"])
        self.assertIsNone(rep["spo2"])

    def test_events_listed_for_day(self):
        rep = aggregate.build_daily(FIX, D(2026, 11, 10))
        self.assertEqual(len(rep["events"]["workout"]), 1)
        self.assertEqual(rep["events"]["workout"][0]["label"], "晨跑")

    def test_context_excludes_pii(self):
        rep = aggregate.build_daily(FIX, D(2026, 11, 10))
        self.assertEqual(rep["context"], {"age": 30})
        self.assertNotIn("email", json.dumps(rep))
        self.assertNotIn("biological_sex", json.dumps(rep))

    def test_naps_separated_from_main_sleep(self):
        rep = aggregate.build_daily(FIX, D(2026, 11, 10))
        self.assertEqual(rep["sleep_main"]["total_sleep_duration"], 25200)
        self.assertEqual(len(rep["naps"]), 1)
        self.assertEqual(rep["naps"][0]["duration"], 900)

    def test_range_domain_uses_day_span(self):
        rep = aggregate.build_daily(FIX, D(2026, 11, 10))
        self.assertEqual(len(rep["rest_mode"]), 1)

    def test_timeseries_split_by_local_day(self):
        rep = aggregate.build_daily(FIX, D(2026, 11, 10))
        ts = rep["timeseries"]["heartrate"]
        self.assertTrue(ts["min"] and ts["avg"] and ts["max"])
        self.assertEqual(len(ts["curve"]), 2)

    def test_deleted_sleep_does_not_leak_into_main(self):
        docs = json.loads(json.dumps(FIX))
        docs["sleep"].append({"id": "del-1", "day": "2026-11-10", "type": "deleted",
                              "total_sleep_duration": 99999})
        rep = aggregate.build_daily(docs, D(2026, 11, 10))
        self.assertEqual(rep["sleep_main"]["total_sleep_duration"], 25200)


if __name__ == "__main__":
    unittest.main()