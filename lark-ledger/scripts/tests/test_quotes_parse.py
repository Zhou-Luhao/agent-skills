"""行情解析与回补窗口：纯逻辑测试，不触网、不读配置、不读时钟。"""

import datetime
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import quotes

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures"


def load_fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def holding(record_id, code, latest=0.0, latest_date="", status="持有"):
    """持仓行（与 lark 记录同形：record_id + fields）。"""
    return {
        "record_id": record_id,
        "fields": {
            "持仓名称": "测试持仓",
            "代码": code,
            "品种": ["股票"] if code.startswith(("sh", "sz")) else ["基金"],
            "当前份额": 100.0,
            "成本价": 1400.0,
            "最新价": latest,
            "最新价日期": latest_date,
            "状态": [status],
        },
    }


def quote_row(record_id, code, day, status="待回补", price=None):
    """行情快照行；待回补时价格为空。"""
    return {
        "record_id": record_id,
        "fields": {
            "代码-日期": "%s_%s" % (code, day),
            "代码": code,
            "日期": day,
            "收盘价/净值": price,
            "状态": [status],
        },
    }


def update_fields(plan, table, record_id):
    for name, row_id, fields in plan["update"]:
        if name == table and row_id == record_id:
            return fields
    return None


def created_for(plan, code, day):
    key = "%s_%s" % (code, day)
    for table, fields in plan["create"]:
        if table == "行情快照" and fields["代码-日期"] == key:
            return fields
    return None


class CodeKindTest(unittest.TestCase):
    def test_code_kind_routing(self):
        self.assertEqual(quotes.code_kind("sh600519"), "stock")
        self.assertEqual(quotes.code_kind("sz000001"), "stock")
        self.assertEqual(quotes.code_kind("110011"), "fund")

    def test_code_kind_rejects_unknown(self):
        for code in ("hk00700", "", "600519.SH", "sh60051", "60051", 600519):
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    quotes.code_kind(code)


class ParseStockTest(unittest.TestCase):
    def test_parse_stock_fixture(self):
        parsed = quotes.parse_stock_kline(load_fixture("sina_kline.json"))

        self.assertEqual(
            sorted(parsed),
            [
                "2026-11-11",
                "2026-11-12",
                "2026-11-13",
                "2026-11-16",
                "2026-11-17",
            ],
        )
        self.assertEqual(parsed["2026-11-17"], 1457.7)
        for value in parsed.values():
            self.assertIsInstance(value, float)

    def test_parse_stock_accepts_decoded_list(self):
        decoded = json.loads(load_fixture("sina_kline.json"))
        self.assertEqual(
            quotes.parse_stock_kline(decoded)["2026-11-17"], 1457.7
        )

    def test_parse_stock_skips_malformed_rows(self):
        parsed = quotes.parse_stock_kline(
            '[{"day": "2026-11-17", "close": "1457.700"},'
            ' {"day": "2026-11-16"}, {"close": "1.0"}]'
        )
        self.assertEqual(parsed, {"2026-11-17": 1457.7})

    def test_parse_stock_rejects_bad_json(self):
        with self.assertRaises(ValueError):
            quotes.parse_stock_kline("<html>busy</html>")

    def test_parse_stock_rejects_non_list(self):
        with self.assertRaises(ValueError):
            quotes.parse_stock_kline('{"day": "2026-11-17"}')

    def test_parse_stock_skips_non_finite(self):
        # NaN/Inf 不能被当成 0.0 落表：整行跳过。
        parsed = quotes.parse_stock_kline(
            '[{"day": "2026-11-17", "close": "nan"},'
            ' {"day": "2026-11-16", "close": "inf"},'
            ' {"day": "2026-11-15", "close": "1457.7"}]'
        )
        self.assertEqual(parsed, {"2026-11-15": 1457.7})


class ParseFundTest(unittest.TestCase):
    def test_parse_fund_fixture(self):
        parsed = quotes.parse_fund_nav(
            json.loads(load_fixture("eastmoney_nav.json"))
        )

        self.assertEqual(
            parsed, {"2026-11-13": 4.0771, "2026-11-16": 4.0854}
        )
        # 11-17 当日净值尚未公布：缺失是常态，不是错误。
        self.assertNotIn("2026-11-17", parsed)

    def test_parse_fund_empty(self):
        self.assertEqual(
            quotes.parse_fund_nav({"Data": {"LSJZList": []}}), {}
        )
        self.assertEqual(quotes.parse_fund_nav({"Data": {}}), {})
        self.assertEqual(quotes.parse_fund_nav({}), {})

    def test_parse_fund_raises_on_error_envelope(self):
        # ErrCode != 0 是接口报错，不能伪装成"净值未公布"。
        envelope = {
            "ErrCode": 1,
            "ErrMsg": "参数错误：fundCode 非法",
            "Data": {"LSJZList": [{"FSRQ": "2026-11-16", "DWJZ": "4.0854"}]},
        }
        with self.assertRaises(ValueError) as raised:
            quotes.parse_fund_nav(envelope)

        message = str(raised.exception)
        self.assertIn("1", message)
        self.assertIn("参数错误", message)

    def test_parse_fund_zero_error_code_is_ok(self):
        envelope = {
            "ErrCode": 0,
            "Data": {"LSJZList": [{"FSRQ": "2026-11-16", "DWJZ": "4.0854"}]},
        }
        self.assertEqual(
            quotes.parse_fund_nav(envelope), {"2026-11-16": 4.0854}
        )

    def test_parse_fund_skips_non_finite(self):
        envelope = {
            "Data": {
                "LSJZList": [
                    {"FSRQ": "2026-11-16", "DWJZ": "nan"},
                    {"FSRQ": "2026-11-15", "DWJZ": "inf"},
                    {"FSRQ": "2026-11-13", "DWJZ": "4.0771"},
                ]
            }
        }
        self.assertEqual(
            quotes.parse_fund_nav(envelope), {"2026-11-13": 4.0771}
        )

    def test_parse_fund_skips_malformed_rows(self):
        envelope = {
            "Data": {
                "LSJZList": [
                    {"FSRQ": "2026-11-16", "DWJZ": "4.0854"},
                    {"FSRQ": "2026-11-15", "DWJZ": "bad"},
                    {"FSRQ": "2026-11-14"},
                    "not-a-row",
                    {"FSRQ": "2026-11-13", "DWJZ": "4.0771"},
                ]
            }
        }
        self.assertEqual(
            quotes.parse_fund_nav(envelope),
            {"2026-11-16": 4.0854, "2026-11-13": 4.0771},
        )


class WindowTest(unittest.TestCase):
    def test_window_union_pending(self):
        window = quotes.quote_window(
            datetime.date(2026, 11, 20), [datetime.date(2026, 11, 16)]
        )

        self.assertEqual(
            window,
            [
                datetime.date(2026, 11, 16),
                datetime.date(2026, 11, 18),
                datetime.date(2026, 11, 19),
                datetime.date(2026, 11, 20),
            ],
        )

    def test_window_excludes_old_pending(self):
        window = quotes.quote_window(
            datetime.date(2026, 11, 20), [datetime.date(2026, 11, 1)]
        )

        self.assertNotIn(datetime.date(2026, 11, 1), window)
        self.assertEqual(len(window), 3)

    def test_window_boundary_and_future_pending(self):
        today = datetime.date(2026, 11, 20)
        window = quotes.quote_window(
            today,
            [
                datetime.date(2026, 11, 13),  # 7 天前：仍回补
                datetime.date(2026, 11, 12),  # 8 天前：出窗
                datetime.date(2026, 11, 21),  # 未来：忽略
            ],
        )

        self.assertIn(datetime.date(2026, 11, 13), window)
        self.assertNotIn(datetime.date(2026, 11, 12), window)
        self.assertNotIn(datetime.date(2026, 11, 21), window)
        self.assertEqual(window, sorted(window))
        self.assertEqual(len(window), len(set(window)))

    def test_window_dedups_today(self):
        window = quotes.quote_window(
            datetime.date(2026, 11, 20), [datetime.date(2026, 11, 20)]
        )
        self.assertEqual(window.count(datetime.date(2026, 11, 20)), 1)


class ExpiryTest(unittest.TestCase):
    def test_expired_after_7_days(self):
        rows = [
            quote_row("r1", "sh600519", "2026-11-09"),  # 8 天前 → 无数据
            quote_row("r2", "sh600519", "2026-11-10"),  # 恰 7 天 → 保留
            quote_row("r3", "sh600519", "2026-11-01", status="已落", price=1.0),
            quote_row("r4", "110011", "2026-11-20"),  # 未来
        ]

        keys = quotes.expired_keys(rows, datetime.date(2026, 11, 17))

        self.assertEqual(keys, ["sh600519_2026-11-09"])

    def test_expired_keys_accepts_lark_ms_date(self):
        # 快照表「日期」是 lark 的 UTC+8 毫秒时间戳；expired_keys 接受行数组。
        moment = datetime.datetime(
            2026, 11, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=8))
        )
        rows = [
            {
                "record_id": "r1",
                "fields": {
                    "代码-日期": "sh600519_2026-11-01",
                    "代码": "sh600519",
                    "日期": int(moment.timestamp() * 1000),
                    "状态": ["待回补"],
                },
            }
        ]

        self.assertEqual(
            quotes.expired_keys(rows, datetime.date(2026, 11, 17)),
            ["sh600519_2026-11-01"],
        )


class ShouldRunTest(unittest.TestCase):
    def test_should_run_window(self):
        def at(hour, minute):
            return datetime.datetime(2026, 11, 17, hour, minute)

        self.assertFalse(quotes.should_run(at(15, 4), None))
        self.assertTrue(quotes.should_run(at(15, 5), None))
        self.assertTrue(quotes.should_run(at(23, 59), None))
        self.assertFalse(quotes.should_run(at(0, 30), None))

    def test_should_run_accepts_tz_aware_now(self):
        # add_flow._now() 返回 Asia/Shanghai 的 aware datetime。
        aware = datetime.datetime(
            2026, 11, 17, 15, 5,
            tzinfo=datetime.timezone(datetime.timedelta(hours=8)),
        )
        self.assertTrue(quotes.should_run(aware, None))

    def test_should_run_once_per_day_via_flag(self):
        now = datetime.datetime(2026, 11, 17, 16, 0)

        self.assertFalse(quotes.should_run(now, "2026-11-17"))
        self.assertTrue(quotes.should_run(now, "2026-11-16"))


class PlanUpdatesTest(unittest.TestCase):
    CODE = "sh600519"
    HOLDING_ID = "rec_h1"

    def holdings(self, latest=0.0, latest_date=""):
        return {
            self.HOLDING_ID: holding(
                self.HOLDING_ID, self.CODE, latest=latest, latest_date=latest_date
            )
        }

    def test_plan_updates_pending_then_filled(self):
        # 第一天：窗口内拿不到值 → 落空值待回补行。
        first = quotes.plan_updates(
            self.holdings(),
            {},
            [datetime.date(2026, 11, 14), datetime.date(2026, 11, 15),
             datetime.date(2026, 11, 16)],
            {},
            datetime.date(2026, 11, 16),
        )
        pending = created_for(first, self.CODE, "2026-11-16")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["状态"], ["待回补"])
        self.assertIsNone(pending["收盘价/净值"])
        self.assertEqual(pending["日期"], "2026-11-16")
        self.assertEqual(len(first["create"]), 3)

        # 第二天：昨天的待回补行被新价格填充 → update 为已落，不重复建行。
        rows = {
            "sh600519_2026-11-16": {
                "record_id": "rec_pending",
                "fields": dict(pending),
            }
        }
        second = quotes.plan_updates(
            self.holdings(),
            rows,
            [datetime.date(2026, 11, 15), datetime.date(2026, 11, 16),
             datetime.date(2026, 11, 17)],
            {self.CODE: {"2026-11-16": 1450.0}},
            datetime.date(2026, 11, 17),
        )

        self.assertEqual(
            update_fields(second, "行情快照", "rec_pending"),
            {"收盘价/净值": 1450.0, "状态": ["已落"]},
        )
        self.assertIsNone(created_for(second, self.CODE, "2026-11-16"))
        self.assertIsNotNone(created_for(second, self.CODE, "2026-11-15"))
        self.assertIsNotNone(created_for(second, self.CODE, "2026-11-17"))
        # 回补成功的新值同时成为持仓最新价。
        self.assertEqual(
            update_fields(second, "持仓", self.HOLDING_ID),
            {"最新价": 1450.0, "最新价日期": "2026-11-16"},
        )

    def test_plan_updates_holding_latest(self):
        rows = {
            "sh600519_2026-11-15": quote_row(
                "r15", self.CODE, "2026-11-15", status="已落", price=1440.0
            ),
            "sh600519_2026-11-16": quote_row(
                "r16", self.CODE, "2026-11-16", status="已落", price=1450.0
            ),
        }

        plan = quotes.plan_updates(
            self.holdings(latest=1440.0, latest_date="2026-11-15"),
            rows,
            [],
            {},
            datetime.date(2026, 11, 17),
        )

        self.assertEqual(
            update_fields(plan, "持仓", self.HOLDING_ID),
            {"最新价": 1450.0, "最新价日期": "2026-11-16"},
        )

    def test_plan_updates_never_downgrades_latest(self):
        rows = {
            "sh600519_2026-11-16": quote_row(
                "r16", self.CODE, "2026-11-16", status="已落", price=1450.0
            )
        }

        plan = quotes.plan_updates(
            self.holdings(latest=1460.0, latest_date="2026-11-17"),
            rows,
            [],
            {},
            datetime.date(2026, 11, 17),
        )

        self.assertIsNone(update_fields(plan, "持仓", self.HOLDING_ID))

    def test_plan_updates_never_downgrades_latest_ms_date(self):
        # 真实 Base 读回的「最新价日期」是毫秒时间戳，不能让守卫失效。
        rows = {
            "sh600519_2026-11-16": quote_row(
                "r16", self.CODE, "2026-11-16", status="已落", price=1450.0
            )
        }
        latest_ms = 1794844800000  # 2026-11-17 00:00 UTC+8

        plan = quotes.plan_updates(
            self.holdings(latest=1460.0, latest_date=latest_ms),
            rows,
            [],
            {},
            datetime.date(2026, 11, 17),
        )

        self.assertIsNone(update_fields(plan, "持仓", self.HOLDING_ID))

    def test_plan_updates_expired_pending_to_no_data(self):
        rows = {
            "sh600519_2026-11-01": quote_row("r_old", self.CODE, "2026-11-01"),
            "sh600519_2026-11-10": quote_row("r_edge", self.CODE, "2026-11-10"),
        }

        plan = quotes.plan_updates(
            {}, rows, [], {}, datetime.date(2026, 11, 17)
        )

        self.assertEqual(
            update_fields(plan, "行情快照", "r_old"), {"状态": ["无数据"]}
        )
        self.assertIsNone(update_fields(plan, "行情快照", "r_edge"))

    def test_plan_updates_expiry_beats_stale_value(self):
        # 即使调用方把 >7 天的日期塞进窗口，过期仍是终态。
        rows = {
            "sh600519_2026-11-01": quote_row("r_old", self.CODE, "2026-11-01")
        }

        plan = quotes.plan_updates(
            self.holdings(),
            rows,
            [datetime.date(2026, 11, 1)],
            {self.CODE: {"2026-11-01": 1400.0}},
            datetime.date(2026, 11, 17),
        )

        self.assertEqual(
            update_fields(plan, "行情快照", "r_old"), {"状态": ["无数据"]}
        )
        self.assertIsNone(created_for(plan, self.CODE, "2026-11-01"))
        self.assertIsNone(update_fields(plan, "持仓", self.HOLDING_ID))

    def test_plan_updates_only_held_codes_inside_window(self):
        plan = quotes.plan_updates(
            self.holdings(),
            {},
            [datetime.date(2026, 11, 17)],
            {
                self.CODE: {
                    "2026-11-11": 1442.5,  # 窗口外：不建行
                    "2026-11-17": 1457.7,
                },
                "110011": {"2026-11-17": 4.0854},  # 非持仓代码：忽略
            },
            datetime.date(2026, 11, 17),
        )

        self.assertEqual(len(plan["create"]), 1)
        created = created_for(plan, self.CODE, "2026-11-17")
        self.assertEqual(created["收盘价/净值"], 1457.7)
        self.assertEqual(created["状态"], ["已落"])
        self.assertIsNone(created_for(plan, "110011", "2026-11-17"))
        self.assertIsNone(created_for(plan, self.CODE, "2026-11-11"))

    def test_plan_updates_keeps_pending_when_no_value(self):
        # 重试循环的安全不变量：窗口内的既有待回补行没抓到值时，
        # 绝不能被空值 + 已落覆盖（否则该日期再也不会重试）。
        rows = {
            "sh600519_2026-11-15": quote_row(
                "rec_pending_keep", self.CODE, "2026-11-15"
            )
        }

        plan = quotes.plan_updates(
            self.holdings(),
            rows,
            [datetime.date(2026, 11, 15), datetime.date(2026, 11, 16)],
            {},
            datetime.date(2026, 11, 17),
        )

        self.assertIsNone(
            update_fields(plan, "行情快照", "rec_pending_keep")
        )
        self.assertIsNone(created_for(plan, self.CODE, "2026-11-15"))
        self.assertIsNotNone(created_for(plan, self.CODE, "2026-11-16"))
        self.assertEqual(plan["update"], [])

    def test_plan_updates_refreshes_filled_price_without_status(self):
        # 已落行刷新价格：只写价格，不重复写状态。
        rows = {
            "sh600519_2026-11-16": quote_row(
                "rec_filled", self.CODE, "2026-11-16",
                status="已落", price=1440.0,
            )
        }

        plan = quotes.plan_updates(
            self.holdings(latest=1440.0, latest_date="2026-11-16"),
            rows,
            [datetime.date(2026, 11, 16)],
            {self.CODE: {"2026-11-16": 1457.7}},
            datetime.date(2026, 11, 17),
        )

        fields = update_fields(plan, "行情快照", "rec_filled")
        self.assertEqual(fields, {"收盘价/净值": 1457.7})
        self.assertNotIn("状态", fields)
        self.assertIsNone(created_for(plan, self.CODE, "2026-11-16"))
        self.assertEqual(
            update_fields(plan, "持仓", self.HOLDING_ID),
            {"最新价": 1457.7},
        )

    def test_plan_updates_deterministic(self):
        # 行按日期倒序、窗口倒序，两个 holdings 字典插入顺序相反。
        rows = {
            "sh600519_2026-11-06": quote_row(
                "r_old_b", self.CODE, "2026-11-06"
            ),
            "sh600519_2026-11-02": quote_row(
                "r_old_a", self.CODE, "2026-11-02"
            ),
        }
        holdings_a = {
            "rec_b": holding("rec_b", "110011"),
            "rec_a": holding("rec_a", self.CODE),
        }
        holdings_b = {
            "rec_a": holding("rec_a", self.CODE),
            "rec_b": holding("rec_b", "110011"),
        }
        window = [datetime.date(2026, 11, 17), datetime.date(2026, 11, 16)]
        fetched = {
            "110011": {"2026-11-16": 4.0854},
            self.CODE: {"2026-11-17": 1457.7},
        }

        first = quotes.plan_updates(
            holdings_a, rows, window, fetched, datetime.date(2026, 11, 17)
        )
        second = quotes.plan_updates(
            holdings_b, rows, window, fetched, datetime.date(2026, 11, 17)
        )

        self.assertEqual(first, second)
        self.assertEqual(
            [fields["代码-日期"] for _, fields in first["create"]],
            [
                "110011_2026-11-16",
                "110011_2026-11-17",
                "sh600519_2026-11-16",
                "sh600519_2026-11-17",
            ],
        )
        self.assertEqual(
            [
                record_id
                for table, record_id, _ in first["update"]
                if table == "行情快照"
            ],
            ["r_old_a", "r_old_b"],
        )
        self.assertEqual(
            [
                record_id
                for table, record_id, _ in first["update"]
                if table == "持仓"
            ],
            ["rec_a", "rec_b"],
        )


if __name__ == "__main__":
    unittest.main()
