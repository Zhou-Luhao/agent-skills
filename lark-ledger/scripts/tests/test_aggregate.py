"""报告聚合：收支口径、净值序列、持仓与负债视图。

全部是纯函数用例：不 mock 网络、不读配置、不读时钟；行数据按
`{"record_id", "fields"}`（或裸字段 dict）直接传入。
"""

import datetime
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import aggregate


UTC8 = datetime.timezone(datetime.timedelta(hours=8))

START = datetime.date(2026, 11, 16)
END = datetime.date(2026, 11, 22)


def category(record_id, first, second, full=None):
    """分类行：一级/二级/全名，与分类表字段一致。"""
    return {
        "record_id": record_id,
        "fields": {
            "分类": full if full is not None else "·".join(["分类", first, second]),
            "一级": first,
            "二级": second,
        },
    }


def entry(record_id, day, type_, amount, category_id=None, summary="测试"):
    """收支明细行；日期默认用 "YYYY-MM-DD" 字符串。"""
    fields = {
        "摘要": summary,
        "日期": day,
        "类型": [type_],
        "金额": amount,
        "备注": "",
    }
    if category_id is not None:
        fields["分类"] = [{"id": category_id}]
    return {"record_id": record_id, "fields": fields}


def category_index(*rows):
    return {row["record_id"]: row for row in rows}


def to_ms(day):
    moment = datetime.datetime(day.year, day.month, day.day, tzinfo=UTC8)
    return int(moment.timestamp() * 1000)


class SummarizeTransfersTest(unittest.TestCase):
    """转账不是收入也不是支出：理财买卖、贷款本金、还信用卡都不得变成支出。"""

    def test_transfers_excluded(self):
        rows = [
            entry("r1", "2026-11-17", "支出", 35.0, "c-lunch"),
            entry("r2", "2026-11-17", "转账", 10000.0),
        ]
        categories = category_index(category("c-lunch", "餐饮", "午餐"))

        result = aggregate.summarize(rows, categories, START, END)

        self.assertEqual(result["income"], 0.0)
        self.assertEqual(result["expense"], 35.0)
        self.assertEqual(result["net"], -35.0)
        self.assertEqual(
            result["by_category"],
            [{"一级": "餐饮", "二级": "午餐", "金额": 35.0}],
        )
        self.assertEqual(result["income_by_category"], [])

    def test_loan_interest_counted_principal_not(self):
        # 11 月房贷：利息 2300（支出）+ 本金 6200（转账）。
        rows = [
            entry("r1", "2026-11-17", "支出", 2300.0, "c-interest",
                  summary="11月房贷：本金6200+利息2300"),
            entry("r2", "2026-11-17", "转账", 6200.0,
                  summary="11月房贷：本金6200+利息2300"),
        ]
        categories = category_index(
            category("c-interest", "金融", "利息支出",
                     full="支出·金融·利息支出")
        )

        result = aggregate.summarize(rows, categories, START, END)

        self.assertEqual(result["expense"], 2300.0)
        self.assertEqual(result["income"], 0.0)
        self.assertEqual(result["net"], -2300.0)
        self.assertEqual(
            result["by_category"],
            [{"一级": "金融", "二级": "利息支出", "金额": 2300.0}],
        )


class SummarizeRealizedPnlTest(unittest.TestCase):
    """已实现盈亏是收入行，允许为负；负收入直接冲减收入合计。"""

    def test_realized_gain_in_income(self):
        rows = [
            entry("r1", "2026-11-17", "支出", 35.0, "c-lunch"),
            entry("r2", "2026-11-18", "收入", 2000.0, "c-fund",
                  summary="赎回已实现盈亏"),
        ]
        categories = category_index(
            category("c-lunch", "餐饮", "午餐"),
            category("c-fund", "理财收益", "基金收益",
                     full="收入·理财收益·基金收益"),
        )

        result = aggregate.summarize(rows, categories, START, END)

        self.assertEqual(result["income"], 2000.0)
        self.assertEqual(result["expense"], 35.0)
        self.assertEqual(result["net"], 1965.0)
        self.assertEqual(
            result["income_by_category"],
            [{"一级": "理财收益", "二级": "基金收益", "金额": 2000.0}],
        )

    def test_realized_loss_reduces_income(self):
        rows = [
            entry("r1", "2026-11-17", "收入", 1000.0, "c-salary"),
            entry("r2", "2026-11-18", "收入", -2000.0, "c-fund"),
        ]
        categories = category_index(
            category("c-salary", "工资", "月薪"),
            category("c-fund", "理财收益", "基金收益"),
        )

        result = aggregate.summarize(rows, categories, START, END)

        self.assertEqual(result["income"], -1000.0)
        self.assertEqual(result["expense"], 0.0)
        self.assertEqual(result["net"], -1000.0)
        self.assertEqual(
            result["income_by_category"],
            [
                {"一级": "工资", "二级": "月薪", "金额": 1000.0},
                {"一级": "理财收益", "二级": "基金收益", "金额": -2000.0},
            ],
        )


class PeriodRangeTest(unittest.TestCase):
    def test_period_week_iso(self):
        # 2026-11-17 是周二，所在 ISO 周为 11-16（周一）~11-22（周日）。
        self.assertEqual(
            aggregate.period_range("week", datetime.date(2026, 11, 17)),
            (datetime.date(2026, 11, 16), datetime.date(2026, 11, 22)),
        )

    def test_period_week_sunday_anchor(self):
        self.assertEqual(
            aggregate.period_range("week", datetime.date(2026, 11, 22)),
            (datetime.date(2026, 11, 16), datetime.date(2026, 11, 22)),
        )

    def test_period_month(self):
        self.assertEqual(
            aggregate.period_range("month", datetime.date(2026, 11, 17)),
            (datetime.date(2026, 11, 1), datetime.date(2026, 11, 30)),
        )

    def test_period_month_leap_february(self):
        self.assertEqual(
            aggregate.period_range("month", datetime.date(2024, 2, 10)),
            (datetime.date(2024, 2, 1), datetime.date(2024, 2, 29)),
        )

    def test_period_range_rejects_unknown_period(self):
        with self.assertRaises(aggregate.AggregateError):
            aggregate.period_range("day", datetime.date(2026, 11, 17))


class SummarizeBoundsTest(unittest.TestCase):
    def test_date_range_is_inclusive(self):
        rows = [
            entry("before", "2026-11-15", "支出", 1.0, "c-a"),
            entry("start", "2026-11-16", "支出", 2.0, "c-a"),
            entry("middle", "2026-11-18", "支出", 3.0, "c-a"),
            entry("end", "2026-11-22", "支出", 4.0, "c-a"),
            entry("after", "2026-11-23", "支出", 5.0, "c-a"),
        ]
        categories = category_index(category("c-a", "餐饮", "午餐"))

        result = aggregate.summarize(rows, categories, START, END)

        self.assertEqual(result["expense"], 9.0)
        self.assertEqual(result["net"], -9.0)

    def test_ms_date_cells_are_supported(self):
        # 飞书读回的日期单元格是毫秒时间戳（UTC+8 当日 0 点）。
        rows = [entry("r1", to_ms(datetime.date(2026, 11, 17)), "支出", 8.0, "c-a")]
        categories = category_index(category("c-a", "餐饮", "午餐"))

        result = aggregate.summarize(rows, categories, START, END)

        self.assertEqual(result["expense"], 8.0)

    def test_bare_fields_rows_are_supported(self):
        rows = [
            {
                "摘要": "午饭",
                "日期": "2026-11-17",
                "类型": ["支出"],
                "金额": 20.0,
                "分类": [{"id": "c-a"}],
            }
        ]
        categories = {"c-a": {"一级": "餐饮", "二级": "午餐"}}

        result = aggregate.summarize(rows, categories, START, END)

        self.assertEqual(result["expense"], 20.0)


class SummarizeCategoryTest(unittest.TestCase):
    def test_missing_category_record_does_not_crash(self):
        rows = [
            entry("r1", "2026-11-17", "支出", 35.0, "c-deleted"),
            entry("r2", "2026-11-17", "支出", 5.0, None),
        ]

        result = aggregate.summarize(rows, {}, START, END)

        self.assertEqual(result["expense"], 40.0)
        self.assertEqual(
            result["by_category"],
            [{"一级": "未分类", "二级": "", "金额": 40.0}],
        )

    def test_category_falls_back_to_full_name_when_levels_missing(self):
        rows = [entry("r1", "2026-11-17", "支出", 35.0, "c-a")]
        categories = {
            "c-a": {"分类": "支出·餐饮·午餐", "一级": "", "二级": ""}
        }

        result = aggregate.summarize(rows, categories, START, END)

        self.assertEqual(
            result["by_category"],
            [{"一级": "餐饮", "二级": "午餐", "金额": 35.0}],
        )

    def test_by_category_sorted_by_amount_desc_then_name(self):
        rows = [
            entry("r1", "2026-11-17", "支出", 50.0, "c-food"),
            entry("r2", "2026-11-17", "支出", 200.0, "c-rent"),
            entry("r3", "2026-11-17", "支出", 50.0, "c-transit"),
        ]
        categories = category_index(
            category("c-food", "餐饮", "午餐"),
            category("c-rent", "居住", "房租"),
            category("c-transit", "交通", "地铁"),
        )

        result = aggregate.summarize(rows, categories, START, END)

        self.assertEqual(
            result["by_category"],
            [
                {"一级": "居住", "二级": "房租", "金额": 200.0},
                # 50 并列时按一级/二级稳定排序：交通 < 餐饮。
                {"一级": "交通", "二级": "地铁", "金额": 50.0},
                {"一级": "餐饮", "二级": "午餐", "金额": 50.0},
            ],
        )

    def test_unknown_types_are_ignored(self):
        rows = [
            entry("r1", "2026-11-17", "", 10.0, "c-a"),
            entry("r2", "2026-11-17", "调整", 20.0, "c-a"),
            entry("r3", "2026-11-17", "收入", 30.0, "c-a"),
        ]
        categories = category_index(category("c-a", "餐饮", "午餐"))

        result = aggregate.summarize(rows, categories, START, END)

        self.assertEqual(result["expense"], 0.0)
        self.assertEqual(result["income"], 30.0)
        self.assertEqual(result["net"], 30.0)


class SummarizeRoundingTest(unittest.TestCase):
    def test_no_float_artifacts(self):
        rows = [
            entry("r1", "2026-11-17", "支出", 0.1, "c-a"),
            entry("r2", "2026-11-17", "支出", 0.2, "c-a"),
            entry("r3", "2026-11-17", "收入", 0.3, "c-b"),
        ]
        categories = category_index(
            category("c-a", "餐饮", "午餐"),
            category("c-b", "其他", "意外"),
        )

        result = aggregate.summarize(rows, categories, START, END)

        self.assertEqual(result["expense"], 0.3)
        self.assertEqual(result["income"], 0.3)
        self.assertEqual(result["net"], 0.0)
        self.assertEqual(result["by_category"][0]["金额"], 0.3)

    def test_empty_entries(self):
        result = aggregate.summarize([], {}, START, END)

        self.assertEqual(
            result,
            {
                "income": 0.0,
                "expense": 0.0,
                "net": 0.0,
                "by_category": [],
                "income_by_category": [],
            },
        )

    def test_inverted_range_rejected(self):
        with self.assertRaises(aggregate.AggregateError):
            aggregate.summarize([], {}, END, START)


class NetWorthSeriesTest(unittest.TestCase):
    def test_net_worth_series_sums_by_date(self):
        rows = [
            {"fields": {"日期": "2026-11-15", "余额/市值": 1000.0}},
            {"fields": {"日期": "2026-11-16", "余额/市值": 2000.2}},
            {"fields": {"日期": "2026-11-15", "余额/市值": -500.5}},
            {"fields": {"日期": "2026-11-16", "余额/市值": 1000.1}},
        ]

        series = aggregate.net_worth_series(rows)

        self.assertEqual(
            series,
            [("2026-11-15", 499.5), ("2026-11-16", 3000.3)],
        )

    def test_net_worth_series_negative_liability_subtracts(self):
        rows = [
            {"fields": {"日期": "2026-11-17", "余额/市值": 800000.0}},
            {"fields": {"日期": "2026-11-17", "余额/市值": -500000.0}},
        ]

        self.assertEqual(
            aggregate.net_worth_series(rows), [("2026-11-17", 300000.0)]
        )

    def test_net_worth_series_sorted_and_ms_dates(self):
        rows = [
            {"fields": {"日期": to_ms(datetime.date(2026, 11, 17)),
                        "余额/市值": 3.0}},
            {"fields": {"日期": "2026-11-15", "余额/市值": 1.0}},
        ]

        self.assertEqual(
            aggregate.net_worth_series(rows),
            [("2026-11-15", 1.0), ("2026-11-17", 3.0)],
        )

    def test_net_worth_series_empty(self):
        self.assertEqual(aggregate.net_worth_series([]), [])

    def test_net_worth_series_missing_value_raises(self):
        rows = [{"fields": {"日期": "2026-11-17", "余额/市值": ""}}]

        with self.assertRaises(aggregate.AggregateError):
            aggregate.net_worth_series(rows)


class HoldingsViewTest(unittest.TestCase):
    def test_holdings_view_math(self):
        holdings = [
            {
                "record_id": "h1",
                "fields": {
                    "持仓名称": "贵州茅台",
                    "代码": "sh600519",
                    "当前份额": 1000.0,
                    "成本价": 10.0,
                    "最新价": 14.57,
                    "状态": ["持有"],
                },
            }
        ]

        view = aggregate.holdings_view(holdings)

        self.assertEqual(
            view,
            [
                {
                    "名称": "贵州茅台",
                    "代码": "sh600519",
                    "份额": 1000.0,
                    "最新价": 14.57,
                    "市值": 14570.0,
                    "浮盈": 4570.0,
                }
            ],
        )

    def test_holdings_view_rounding(self):
        holdings = [
            {
                "fields": {
                    "持仓名称": "基金",
                    "代码": "110011",
                    "当前份额": 100.5,
                    "成本价": 1.0,
                    "最新价": 1.23456,
                }
            }
        ]

        view = aggregate.holdings_view(holdings)

        self.assertEqual(view[0]["份额"], 100.5)
        self.assertEqual(view[0]["最新价"], 1.2346)
        self.assertEqual(view[0]["市值"], 124.07)
        self.assertEqual(view[0]["浮盈"], 23.57)

    def test_holdings_view_uses_formula_cells(self):
        holdings = [
            {
                "fields": {
                    "持仓名称": "股票",
                    "代码": "sz000001",
                    "当前份额": 10.0,
                    "最新价": 20.0,
                    "市值": 199.99,
                    "浮动盈亏": 12.34,
                }
            }
        ]

        view = aggregate.holdings_view(holdings)

        self.assertEqual(view[0]["市值"], 199.99)
        self.assertEqual(view[0]["浮盈"], 12.34)

    def test_holdings_view_missing_price_is_zero(self):
        holdings = [
            {
                "fields": {
                    "持仓名称": "未估值",
                    "代码": "sh600000",
                    "当前份额": 100.0,
                    "成本价": 9.0,
                }
            }
        ]

        view = aggregate.holdings_view(holdings)

        self.assertEqual(view[0]["最新价"], 0.0)
        self.assertEqual(view[0]["市值"], 0.0)
        self.assertEqual(view[0]["浮盈"], 0.0)

    def test_holdings_view_sorted_by_code(self):
        holdings = [
            {"fields": {"持仓名称": "B", "代码": "sz000002", "当前份额": 1.0}},
            {"fields": {"持仓名称": "A", "代码": "110011", "当前份额": 1.0}},
        ]

        view = aggregate.holdings_view(holdings)

        self.assertEqual([row["代码"] for row in view], ["110011", "sz000002"])

    def test_holdings_view_excludes_closed(self):
        holdings = [
            {"fields": {"持仓名称": "已清仓", "代码": "sh600001",
                        "当前份额": 0.0, "状态": ["已清仓"]}},
            {"fields": {"持仓名称": "仍持有", "代码": "sh600002",
                        "当前份额": 10.0, "状态": ["持有"]}},
        ]

        view = aggregate.holdings_view(holdings)

        self.assertEqual([row["名称"] for row in view], ["仍持有"])

    def test_holdings_view_empty(self):
        self.assertEqual(aggregate.holdings_view([]), [])


class LiabilitiesViewTest(unittest.TestCase):
    def test_liabilities_view_shows_abs_principal_and_percent_rate(self):
        accounts = [
            {
                "fields": {
                    "账户名称": "房贷",
                    "类型": ["负债"],
                    "当前余额": -500000.0,
                    "年利率": 0.042,
                }
            },
            {
                "fields": {
                    "账户名称": "招行工资卡",
                    "类型": ["银行卡"],
                    "当前余额": 10000.0,
                }
            },
            {
                "fields": {
                    "账户名称": "车贷",
                    "类型": ["负债"],
                    "当前余额": -80000.0,
                    "年利率": 0.0435,
                }
            },
        ]

        view = aggregate.liabilities_view(accounts)

        self.assertEqual(
            view,
            [
                {"名称": "房贷", "剩余本金": 500000.0, "年利率": 4.2},
                {"名称": "车贷", "剩余本金": 80000.0, "年利率": 4.35},
            ],
        )

    def test_liabilities_view_missing_rate_is_zero(self):
        accounts = [
            {"fields": {"账户名称": "借呗", "类型": ["负债"], "当前余额": -1200.0}}
        ]

        view = aggregate.liabilities_view(accounts)

        self.assertEqual(
            view, [{"名称": "借呗", "剩余本金": 1200.0, "年利率": 0.0}]
        )

    def test_liabilities_view_accepts_categories_argument(self):
        accounts = [
            {"fields": {"账户名称": "房贷", "类型": ["负债"],
                        "当前余额": -1000.0, "年利率": 0.042}}
        ]
        categories = {"c-a": {"一级": "金融", "二级": "利息支出"}}

        self.assertEqual(
            aggregate.liabilities_view(accounts, categories),
            [{"名称": "房贷", "剩余本金": 1000.0, "年利率": 4.2}],
        )

    def test_liabilities_view_sorted_by_name(self):
        accounts = [
            {"fields": {"账户名称": "B贷", "类型": ["负债"], "当前余额": -1.0}},
            {"fields": {"账户名称": "A贷", "类型": ["负债"], "当前余额": -2.0}},
        ]

        view = aggregate.liabilities_view(accounts)

        self.assertEqual([row["名称"] for row in view], ["A贷", "B贷"])


if __name__ == "__main__":
    unittest.main()