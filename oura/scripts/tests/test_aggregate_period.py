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


class BoundsTest(unittest.TestCase):
    def test_week_bounds_monday_to_sunday(self):
        self.assertEqual(aggregate.week_bounds(D(2026, 11, 11)),
                         (D(2026, 11, 9), D(2026, 11, 15)))

    def test_week_bounds_on_sunday(self):
        self.assertEqual(aggregate.week_bounds(D(2026, 11, 15)),
                         (D(2026, 11, 9), D(2026, 11, 15)))

    def test_month_bounds(self):
        self.assertEqual(aggregate.month_bounds(D(2026, 2, 15)),
                         (D(2026, 2, 1), D(2026, 2, 28)))


class PeriodTest(unittest.TestCase):
    def test_partial_period_flags_and_elapsed_days(self):
        rep = aggregate.build_period(FIX, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=None, today=D(2026, 11, 11))
        self.assertTrue(rep["is_partial"])
        self.assertEqual(rep["elapsed_days"], 3)

    def test_complete_period_not_partial(self):
        rep = aggregate.build_period(FIX, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=None, today=D(2026, 11, 20))
        self.assertFalse(rep["is_partial"])
        self.assertEqual(rep["elapsed_days"], 7)

    def test_summaries_use_population_std(self):
        rep = aggregate.build_period(FIX, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=None, today=D(2026, 11, 20))
        self.assertIn("std", rep["summaries"]["sleep_score"])
        self.assertEqual(rep["summaries"]["sleep_score"]["mean"], 82.0)

    def test_comparison_aligns_previous_by_elapsed_days(self):
        rep = aggregate.build_period(FIX, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=FIX, today=D(2026, 11, 11))
        self.assertEqual(rep["comparison"]["prev_days"], 3)
        self.assertIn("note", rep["comparison"])

    def test_missing_days_listed(self):
        rep = aggregate.build_period(FIX, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=None, today=D(2026, 11, 20))
        self.assertEqual(len(rep["missing_days"]), 6)   # 仅 11-10 有数据

    def test_trend_and_sleep_totals_cover_every_day(self):
        rep = aggregate.build_period(FIX, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=None, today=D(2026, 11, 20))
        self.assertEqual(len(rep["trend"]), 7)
        self.assertEqual(len(rep["sleep_totals"]), 7)

    def test_trend_carries_per_day_detail_for_report_table(self):
        rep = aggregate.build_period(FIX, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=None, today=D(2026, 11, 20))
        row = [r for r in rep["trend"] if r["day"] == "2026-11-10"][0]
        for key in ("sleep_duration", "deep", "rem", "efficiency", "steps",
                    "active_calories", "lowest_hr", "hrv", "spo2",
                    "vascular_age", "bedtime_start", "bedtime_end"):
            self.assertIn(key, row)
        self.assertEqual(row["deep"], 5400)
        self.assertEqual(row["steps"], 8432)

    def test_best_worst_by_sleep_score(self):
        rep = aggregate.build_period(FIX, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=None, today=D(2026, 11, 20))
        self.assertEqual(rep["best_worst"]["best"]["day"], "2026-11-10")
        self.assertIsNone(rep["best_worst"]["worst"])

    def test_timeseries_split_by_local_day(self):
        tz = aggregate._local_tz()
        ts = datetime.datetime(2026, 11, 10, 23, 30, tzinfo=datetime.timezone.utc)
        self.assertEqual(aggregate.local_day_of(ts.isoformat()),
                         ts.astimezone(tz).date())

    def test_local_day_handles_z_suffix_and_millis(self):
        self.assertIsNotNone(aggregate.local_day_of("2026-11-09T12:18:05.500Z"))


if __name__ == "__main__":
    unittest.main()

class PeriodScoreCardTest(unittest.TestCase):
    """周报顶部分数卡必须反映周期均值，不是周期首日。"""

    def test_score_cards_use_period_mean_not_first_day(self):
        docs = json.loads(json.dumps(FIX))
        docs["daily_sleep"].append({"id": "ds-11-11", "day": "2026-11-11", "score": 60})
        docs["daily_readiness"].append({"id": "dr-11-11", "day": "2026-11-11", "score": 70})
        docs["daily_activity"].append({"id": "da-11-11", "day": "2026-11-11", "score": 80})
        rep = aggregate.build_period(docs, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=None, today=D(2026, 11, 20))
        self.assertEqual(rep["scores"]["sleep"]["value"], 71.0)      # (82+60)/2
        self.assertEqual(rep["scores"]["readiness"]["value"], 73.5)  # (77+70)/2
        self.assertEqual(rep["scores"]["activity"]["value"], 85.0)   # (90+80)/2

    def test_steps_summarised_over_period(self):
        docs = json.loads(json.dumps(FIX))
        docs["daily_activity"].append({"id": "da-11-11", "day": "2026-11-11",
                                       "score": 80, "steps": 1000})
        rep = aggregate.build_period(docs, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=None, today=D(2026, 11, 20))
        self.assertEqual(rep["summaries"]["steps_avg"]["mean"], 4716.0)  # (8432+1000)/2

    def test_score_card_present_even_when_first_day_empty(self):
        # 11-09 没有任何数据，卡片仍应有值（旧实现会是 None）
        rep = aggregate.build_period(FIX, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=None, today=D(2026, 11, 20))
        self.assertEqual(rep["scores"]["sleep"]["value"], 82.0)
        self.assertEqual(rep["start"], "2026-11-09")


class EnhancedTagTest(unittest.TestCase):
    def test_enhanced_tag_uses_day_span(self):
        docs = json.loads(json.dumps(FIX))
        docs["enhanced_tag"] = [{"id": "et-1", "start_day": "2026-11-10",
                                 "end_day": "2026-11-10",
                                 "custom_name": "ALCOHOL", "comment": "两杯红酒"}]
        rep = aggregate.build_daily(docs, D(2026, 11, 10))
        self.assertEqual(len(rep["events"]["enhanced_tag"]), 1)
        self.assertEqual(rep["events"]["enhanced_tag"][0]["custom_name"], "ALCOHOL")

    def test_enhanced_tag_multi_day_span_covers_both_days(self):
        docs = json.loads(json.dumps(FIX))
        docs["enhanced_tag"] = [{"id": "et-2", "start_day": "2026-11-10",
                                 "end_day": "2026-11-12", "custom_name": "TRAVEL"}]
        rep10 = aggregate.build_daily(docs, D(2026, 11, 10))
        rep11 = aggregate.build_daily(docs, D(2026, 11, 11))
        self.assertEqual(len(rep10["events"]["enhanced_tag"]), 1)
        self.assertEqual(len(rep11["events"]["enhanced_tag"]), 1)


class PeriodEventsTest(unittest.TestCase):
    def test_period_summarises_events(self):
        rep = aggregate.build_period(FIX, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=None, today=D(2026, 11, 20))
        summary = rep["events_summary"]
        self.assertEqual(summary["workout"]["count"], 1)
        self.assertEqual(summary["workout"]["total_calories"], 320)
        self.assertEqual(summary["workout"]["total_duration"], 2100)   # 35 分钟
        self.assertEqual(summary["session"]["count"], 1)
        self.assertEqual(summary["tag"]["count"], 1)


class ComparisonWindowTest(unittest.TestCase):
    """C1: 环比必须对齐「上期前 N 天」，不是「上期末尾 N 天」。"""

    def test_weekly_comparison_uses_previous_week_start(self):
        rep = aggregate.build_period(FIX, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=FIX, today=D(2026, 11, 11))
        comparison = rep["comparison"]
        self.assertEqual(comparison["prev_start"], "2026-11-02")
        self.assertEqual(comparison["prev_end"], "2026-11-04")

    def test_monthly_comparison_uses_previous_month_start(self):
        rep = aggregate.build_period(FIX, D(2026, 11, 1), D(2026, 11, 30),
                                     prev_docs=FIX, today=D(2026, 11, 11))
        comparison = rep["comparison"]
        self.assertEqual(comparison["prev_start"], "2026-10-01")
        self.assertEqual(comparison["prev_end"], "2026-10-11")


class MissingDaysTest(unittest.TestCase):
    """I1: 未来日期不是「缺失」。"""

    def test_partial_period_does_not_report_future_days_as_missing(self):
        rep = aggregate.build_period(FIX, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=None, today=D(2026, 11, 11))
        self.assertNotIn("2026-11-12", rep["missing_days"])
        self.assertNotIn("2026-11-15", rep["missing_days"])
        self.assertIn("2026-11-09", rep["missing_days"])

    def test_future_days_listed_separately(self):
        rep = aggregate.build_period(FIX, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=None, today=D(2026, 11, 11))
        self.assertEqual(rep["pending_days"],
                         ["2026-11-12", "2026-11-13", "2026-11-14", "2026-11-15"])


class SleepDurationSummaryTest(unittest.TestCase):
    """C2: 周期报告需要平均睡眠时长，而不是第一天。"""

    def test_period_summarises_average_sleep_duration(self):
        rep = aggregate.build_period(FIX, D(2026, 11, 9), D(2026, 11, 15),
                                     prev_docs=None, today=D(2026, 11, 20))
        self.assertEqual(rep["summaries"]["sleep_duration_avg"]["mean"], 25200.0)


class TimeseriesOnlyDayTest(unittest.TestCase):
    """M8: 只有时序数据的日子不算「无数据日」。"""

    def test_heartrate_only_day_is_not_missing(self):
        docs = {"heartrate": [{"timestamp": "2026-11-10T10:00:00.000Z", "bpm": 60}]}
        rep = aggregate.build_daily(docs, D(2026, 11, 10))
        self.assertEqual(rep["missing_days"], [])

    def test_curve_is_sorted(self):
        docs = {"heartrate": [
            {"timestamp": "2026-11-10T12:00:00.000Z", "bpm": 70},
            {"timestamp": "2026-11-10T08:00:00.000Z", "bpm": 55},
        ]}
        rep = aggregate.build_daily(docs, D(2026, 11, 10))
        curve = rep["timeseries"]["heartrate"]["curve"]
        self.assertEqual([v for _t, v in curve], [55, 70])


class TimestampRobustnessTest(unittest.TestCase):
    """M9: 声称 Python 3.9+，而 3.9 的 fromisoformat 只接受 3/6 位小数秒。"""

    def test_parses_single_digit_fraction(self):
        self.assertIsNotNone(aggregate.local_day_of("2026-11-09T12:18:05.5Z"))

    def test_parses_seven_digit_fraction(self):
        self.assertIsNotNone(aggregate.local_day_of("2026-11-09T12:18:05.1234567Z"))

    def test_parses_space_separator(self):
        self.assertIsNotNone(aggregate.local_day_of("2026-11-09 12:18:05Z"))

    def test_no_offset_treated_as_utc_for_z(self):
        self.assertEqual(aggregate.local_day_of("2026-11-09T12:18:05Z"),
                         datetime.datetime(2026, 11, 9, 12, 18, 5,
                                           tzinfo=datetime.timezone.utc)
                         .astimezone(aggregate._local_tz()).date())
