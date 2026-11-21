import datetime
import json
import pathlib
import unittest
from html.parser import HTMLParser
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import aggregate
import render

D = datetime.date
FIX = json.loads(
    (pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "daily_full.json").read_text())


def _daily():
    return aggregate.build_daily(FIX, D(2026, 11, 10))


def _weekly():
    return aggregate.build_period(FIX, D(2026, 11, 9), D(2026, 11, 15),
                                  prev_docs=FIX, today=D(2026, 11, 20))


class NoExternalRefParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.bad = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ("href", "src", "xlink:href") and value and not value.startswith("#"):
                self.bad.append((tag, key, value))


class HtmlTest(unittest.TestCase):
    def test_doctype_and_inline_style(self):
        html = render.render_html(_daily())
        self.assertTrue(html.lstrip().startswith("<!doctype html>"))
        self.assertIn("<style>", html)
        self.assertIn('charset="utf-8"', html.lower())

    def test_no_external_references(self):
        for report in (_daily(), _weekly()):
            parser = NoExternalRefParser()
            parser.feed(render.render_html(report))
            self.assertEqual(parser.bad, [])

    def test_no_script_tag(self):
        self.assertNotIn("<script", render.render_html(_daily()))

    def test_scores_present(self):
        html = render.render_html(_daily())
        self.assertIn("82", html)
        self.assertIn("77", html)
        self.assertIn("90", html)

    def test_missing_domain_shows_placeholder(self):
        docs = {k: v for k, v in FIX.items() if k != "daily_spo2"}
        html = render.render_html(aggregate.build_daily(docs, D(2026, 11, 10)))
        self.assertIn("无数据", html)

    def test_user_text_is_escaped(self):
        docs = json.loads(json.dumps(FIX))
        docs["workout"][0]["label"] = '<script>alert("x")</script>'
        docs["tag"][0]["text"] = '<img src=x onerror="alert(1)">'
        html = render.render_html(aggregate.build_daily(docs, D(2026, 11, 10)))
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;", html)

    def test_pii_never_rendered(self):
        html = render.render_html(_daily())
        self.assertNotIn("agent@example.com", html)
        self.assertNotIn("biological_sex", html)

    def test_seconds_rendered_as_hours_minutes(self):
        html = render.render_html(_daily())
        self.assertIn("7 小时", html)

    def test_weekly_has_trend_and_comparison(self):
        html = render.render_html(_weekly())
        self.assertIn('aria-label="每日评分趋势"', html)
        self.assertIn("环比", html)

    def test_weekly_cards_labeled_as_mean(self):
        html = render.render_html(_weekly())
        self.assertIn("周期均值", html)

    def test_weekly_reports_averaged_steps_not_first_day(self):
        md = render.render_markdown(_weekly())
        self.assertIn("步数（日均）", md)

    def test_dark_dashboard_tokens(self):
        html = render.render_html(_daily())
        # 深色看板：暗底 + 面板 + 三色分类 + 评分色带
        self.assertIn("#0a0d14", html.lower())
        self.assertIn("#11151f", html.lower())
        for tone in ("#4d8df6", "#38d39f", "#a78bfa"):
            self.assertIn(tone, html.lower())
        self.assertIn("kpi", html)

    def test_daily_report_has_kpi_grid_and_sections(self):
        html = render.render_html(_daily())
        for section in ("睡眠结构", "睡眠分期", "评分构成", "心率", "扩展指标", "数据说明"):
            self.assertIn(section, html)

    def test_kpi_delta_shown_when_previous_exists(self):
        docs = json.loads(json.dumps(FIX))
        docs["daily_sleep"].append({"id": "ds-prev", "day": "2026-11-09", "score": 70})
        html = render.render_html(aggregate.build_daily(docs, D(2026, 11, 10)))
        self.assertIn("较前一日", html)
        self.assertIn("+12", html)

    def test_weekly_lists_missing_days(self):
        html = render.render_html(_weekly())
        self.assertIn("无数据", html)
        self.assertIn("2026-11-09", html)


class MarkdownTest(unittest.TestCase):
    def test_markdown_has_table_and_interpretation_slot(self):
        md = render.render_markdown(_daily())
        self.assertIn("|", md)
        self.assertIn("## 解读", md)
        self.assertIn("7 小时", md)

    def test_markdown_escapes_pipe_in_user_text(self):
        docs = json.loads(json.dumps(FIX))
        docs["tag"][0]["text"] = "a|b"
        md = render.render_markdown(aggregate.build_daily(docs, D(2026, 11, 10)))
        self.assertIn("a\\|b", md)

    def test_markdown_has_agent_append_marker(self):
        md = render.render_markdown(_daily())
        self.assertIn("<!-- AGENT-APPEND-BELOW -->", md)


if __name__ == "__main__":
    unittest.main()

class PeriodMarkdownTest(unittest.TestCase):
    """C2/I7: 周报 MD 不得输出首日数值或内部 snake_case 键名。"""

    def test_weekly_markdown_has_no_day_one_sleep_rows(self):
        md = render.render_markdown(_weekly())
        self.assertNotIn("| 总睡眠 |", md)
        self.assertIn("平均睡眠时长", md)

    def test_weekly_markdown_uses_chinese_labels(self):
        md = render.render_markdown(_weekly())
        for internal in ("sleep_score", "readiness_score", "activity_score",
                         "resting_hr", "steps_avg", "sleep_duration_avg"):
            self.assertNotIn(internal, md)

    def test_daily_markdown_still_shows_single_day_sleep(self):
        md = render.render_markdown(_daily())
        self.assertIn("总睡眠", md)

    def test_weekly_summary_formats_durations(self):
        md = render.render_markdown(_weekly())
        self.assertNotIn("25200.0", md)
        self.assertIn("7 小时", md)

    def test_every_data_domain_appears_even_when_empty(self):
        """18 个数据域都要在报告里有位置，空则标注「无数据」而不是消失。"""
        html = render.render_html(_daily())
        for caption in ("锻炼", "会话", "标签", "增强标签", "设备", "休息模式"):
            self.assertIn(caption, html)

    def test_battery_is_rendered(self):
        html = render.render_html(_daily())
        self.assertIn("电量", html)
        self.assertIn("76", html)   # fixture 的最低电量

    def test_missing_domains_are_rendered(self):
        docs = {k: v for k, v in FIX.items() if k != "daily_spo2"}
        html = render.render_html(aggregate.build_daily(docs, D(2026, 11, 10)))
        self.assertIn("缺失数据域", html)


class ChineseLabelsTest(unittest.TestCase):
    """评分构成与缺失数据域必须显示中文，API 原始键名不得进入产物。"""

    CONTRIB_ZH = (
        # sleep
        "深睡", "睡眠效率", "入睡耗时", "REM 睡眠", "安稳度", "睡眠时机", "总睡眠时长",
        # readiness
        "活动平衡", "体温", "HRV 平衡", "前日活动", "前夜睡眠", "恢复指数",
        "静息心率", "睡眠平衡", "睡眠规律性",
        # activity
        "达成每日目标", "每小时活动", "恢复时间", "保持活跃", "训练频率", "训练量",
    )
    CONTRIB_RAW = (
        "deep_sleep", "efficiency", "latency", "rem_sleep", "restfulness",
        "timing", "total_sleep", "activity_balance", "body_temperature",
        "hrv_balance", "previous_day_activity", "previous_night",
        "recovery_index", "resting_heart_rate", "sleep_balance",
        "sleep_regularity", "meet_daily_targets", "move_every_hour",
        "recovery_time", "stay_active", "training_frequency", "training_volume",
    )

    def test_contributors_render_chinese(self):
        html = render.render_html(_daily())
        for label in self.CONTRIB_ZH:
            self.assertIn(label, html)

    def test_contributor_raw_keys_absent(self):
        html = render.render_html(_daily())
        for raw in self.CONTRIB_RAW:
            self.assertNotIn(raw, html)

    def test_missing_domains_render_chinese(self):
        docs = {k: v for k, v in FIX.items()
                if k not in ("daily_spo2", "daily_stress", "daily_resilience",
                             "vO2_max", "sleep_time")}
        html = render.render_html(aggregate.build_daily(docs, D(2026, 11, 10)))
        self.assertIn("缺失数据域（当日无数据）：血氧、压力、恢复力、最大摄氧量、就寝建议", html)
        for raw in ("spo2、", "stress、", "resilience", "vo2max", "sleep_time"):
            self.assertNotIn(raw, html)


class EnumValueLabelsTest(unittest.TestCase):
    """API 原始枚举值（状态/等级/类型/颜色等）也要显示中文。"""

    ZH_VALUES = (
        "理想",        # sleep_time.status = ideal
        "正常",        # daily_stress.day_summary = normal
        "稳健",        # daily_resilience.level = solid
        "冥想",        # session.type = meditation
        "良好",        # session.mood = good
        "中等",        # workout.intensity = moderate
        "传承系列",    # ring_configuration.design = heritage
        "银色",        # ring_configuration.color = silver
        "第三代",      # ring_configuration.hardware_type = gen3
    )
    RAW_VALUES = ("ideal", "solid", "meditation", "running", "moderate",
                  "heritage", "silver", "gen3")

    def test_fixture_enum_values_render_chinese(self):
        html = render.render_html(_daily())
        for label in self.ZH_VALUES:
            self.assertIn(label, html)

    def test_fixture_raw_enum_values_absent(self):
        # 只看值单元格：CSS 里本来就有 `1px solid`，裸子串匹配会误报。
        html = render.render_html(_daily())
        for raw in self.RAW_VALUES:
            self.assertNotIn(">%s<" % raw, html)

    def test_workout_activity_used_when_label_missing(self):
        import copy
        docs = copy.deepcopy(FIX)
        docs["workout"][0]["label"] = None
        html = render.render_html(aggregate.build_daily(docs, D(2026, 11, 10)))
        self.assertIn("跑步", html)
        self.assertNotIn(">running<", html)

    def test_ring5_hardware_renders_chinese(self):
        import copy
        docs = copy.deepcopy(FIX)
        docs["ring_configuration"][0]["hardware_type"] = "or5"
        html = render.render_html(aggregate.build_daily(docs, D(2026, 11, 10)))
        self.assertIn("第五代", html)
        self.assertNotIn(">or5<", html)

    def test_unknown_enum_value_falls_back_to_raw(self):
        import copy
        docs = copy.deepcopy(FIX)
        docs["daily_resilience"][0]["level"] = "mystery_level"
        html = render.render_html(aggregate.build_daily(docs, D(2026, 11, 10)))
        self.assertIn("mystery_level", html)


class ComparisonDeltaTest(unittest.TestCase):
    """环比里的时长差必须以「小时/分」呈现，不能把秒数当小数打印。"""

    def _weekly_with_delta(self):
        report = _weekly()
        report["comparison"] = {
            "note": "本期截至 2026-11-21（6 天），与上期同天数对比",
            "deltas": {"sleep_duration_avg": -1852.0, "sleep_score": -1.17},
        }
        return report

    def test_markdown_formats_duration_delta(self):
        md = render.render_markdown(self._weekly_with_delta())
        self.assertIn("-31 分", md)
        self.assertNotIn("-1852.00", md)
        self.assertIn("-1.17", md)

    def test_html_formats_duration_delta(self):
        html = render.render_html(self._weekly_with_delta())
        self.assertIn("-31 分", html)
        self.assertNotIn("-1852.00", html)


class SkillVersionTest(unittest.TestCase):
    """技能版本号：代码常量、SKILL.md 元数据、报告产物三处一致。"""

    def test_html_footer_shows_version(self):
        html = render.render_html(_daily())
        self.assertIn("oura skill v%s" % render.SKILL_VERSION, html)

    def test_markdown_shows_version(self):
        md = render.render_markdown(_daily())
        self.assertIn("v%s" % render.SKILL_VERSION, md)

    def test_skill_md_metadata_matches_code(self):
        path = pathlib.Path(__file__).resolve().parents[2] / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn('version: "%s"' % render.SKILL_VERSION, text)


class CredentialScanTest(unittest.TestCase):
    """M12: 规格要求「全产物」不得含凭据。"""

    SECRET = "s3cr3t-client-secret-value"

    def test_report_files_contain_no_credentials(self):
        import os
        import tempfile
        with patch_env():
            report = _daily()
            html = render.render_html(report)
            md = render.render_markdown(report)
        for blob in (html, md):
            self.assertNotIn(self.SECRET, blob)
            self.assertNotIn("agent@example.com", blob)
            self.assertNotIn("biological_sex", blob)


def patch_env():
    import contextlib
    import os

    @contextlib.contextmanager
    def _ctx():
        overrides = {"OURA_CLIENT_SECRET": CredentialScanTest.SECRET,
                     "OURA_CLIENT_ID": "cid-scan"}
        saved = {k: os.environ.get(k) for k in overrides}
        os.environ.update(overrides)
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    return _ctx()
