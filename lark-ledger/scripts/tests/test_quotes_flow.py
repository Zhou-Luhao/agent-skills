"""行情 I/O 流程：触发窗口、逐源容错、落表与配置写入。

全部 hermetic：mock quotes.fetch_stock/fetch_fund 与所有 lark 调用，
config.CONFIG_PATH 指到临时文件，now 由测试注入，不触网、不读真实配置。
"""

import contextlib
import copy
import datetime
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import config
import lark
import ledger
import quotes
import quotes_flow


UTC8 = datetime.timezone(datetime.timedelta(hours=8))
TODAY = datetime.date(2026, 11, 18)
NOW_AFTER_WINDOW = datetime.datetime(2026, 11, 18, 16, 0, tzinfo=UTC8)
NOW_BEFORE_WINDOW = datetime.datetime(2026, 11, 18, 14, 0, tzinfo=UTC8)

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures"


def load_fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


@contextlib.contextmanager
def captured_output():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def holding(record_id, code, latest=0.0, latest_date="", status="持有"):
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


class QuotesFlowTestBase(unittest.TestCase):
    def setUp(self):
        self._patches = []

        def patch(target, **kwargs):
            patcher = mock.patch(target, **kwargs)
            self._patches.append(patcher)
            return patcher.start()

        self.addCleanup(self._stop_patches)

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = pathlib.Path(self.tmp.name) / "config.json"
        patch("config.CONFIG_PATH", new=self.config_path)

        self.cfg = {
            "version": 1,
            "app_token": "app1",
            "tables": {
                "持仓": {"table_id": "tbl_holding", "fields": {}},
                "行情快照": {"table_id": "tbl_quote", "fields": {}},
            },
            "last_quotes_date": None,
        }
        self.config_path.write_text(
            json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8"
        )

        self.holdings = []
        self.quote_rows = []
        self.search_calls = []
        self.created = []  # (table_id, [record dict])
        self.updated = []  # (table_id, record_id, fields)
        self._next_id = 0

        # 任何真实 lark-cli 调用都必须让测试炸掉。
        patch("lark.run_cli", side_effect=AssertionError("测试不应调用 lark-cli"))
        patch("lark.search_records", side_effect=self._search)
        patch("lark.create_records", side_effect=self._create)
        patch("lark.update_record", side_effect=self._update)

        self.fetch_stock = patch("quotes.fetch_stock", side_effect=self._no_fetch)
        self.fetch_fund = patch("quotes.fetch_fund", side_effect=self._no_fetch)

    def _stop_patches(self):
        for patcher in reversed(self._patches):
            patcher.stop()

    def _search(self, app_token, table_id, conditions=None, limit=2000, offset=0):
        self.search_calls.append(table_id)
        if table_id == "tbl_holding":
            return copy.deepcopy(self.holdings)
        if table_id == "tbl_quote":
            return copy.deepcopy(self.quote_rows)
        raise AssertionError("未知的 table_id：%r" % (table_id,))

    def _create(self, app_token, table_id, records):
        self.created.append((table_id, copy.deepcopy(records)))
        ids = []
        for _ in records:
            self._next_id += 1
            ids.append("rec_new_%d" % self._next_id)
        return ids

    def _update(self, app_token, table_id, record_id, fields):
        self.updated.append((table_id, record_id, copy.deepcopy(fields)))

    @staticmethod
    def _no_fetch(*args, **kwargs):
        raise AssertionError("测试没有为 fetch_* 配置返回值")

    def created_rows(self, table_id="tbl_quote"):
        rows = []
        for table, records in self.created:
            if table == table_id:
                rows.extend(records)
        return rows

    def created_by_key(self, code, day):
        key = "%s_%s" % (code, day)
        for fields in self.created_rows():
            if fields.get("代码-日期") == key:
                return fields
        return None

    def updated_fields(self, record_id, table_id="tbl_quote"):
        for table, row_id, fields in self.updated:
            if table == table_id and row_id == record_id:
                return fields
        return None


class RunQuotesTest(QuotesFlowTestBase):
    def test_once_per_day(self):
        """同日第二次 maybe_run：不查表、不 fetch。"""
        self.holdings = [holding("recH1", "sh600519")]
        self.fetch_stock.side_effect = lambda code: {
            "2026-11-16": 1.0,
            "2026-11-17": 2.0,
            "2026-11-18": 3.0,
        }

        first = quotes_flow.maybe_run(self.cfg, NOW_AFTER_WINDOW)
        self.assertNotIn("skipped", first)
        self.assertEqual(self.fetch_stock.call_count, 1)

        self.fetch_stock.reset_mock()
        searches_after_first = len(self.search_calls)

        second = quotes_flow.maybe_run(self.cfg, NOW_AFTER_WINDOW)
        self.assertTrue(second.get("skipped"))
        self.assertEqual(self.fetch_stock.call_count, 0)
        self.assertEqual(len(self.search_calls), searches_after_first)

    def test_outside_window_skips(self):
        """15:05 前且非 force：整轮跳过，不 fetch、不写、不改配置。"""
        self.holdings = [holding("recH1", "sh600519")]

        result = quotes_flow.run_quotes(self.cfg, now=NOW_BEFORE_WINDOW)

        self.assertTrue(result.get("skipped"))
        self.assertEqual(self.fetch_stock.call_count, 0)
        self.assertEqual(self.search_calls, [])
        self.assertEqual(self.created, [])
        self.assertEqual(self.updated, [])
        self.assertIsNone(self.cfg["last_quotes_date"])

    def test_force_bypasses_window(self):
        """force 绕过时间窗，即使 14:00 也照常拉取。"""
        self.holdings = [holding("recH1", "sh600519")]
        self.fetch_stock.side_effect = lambda code: {"2026-11-18": 3.0}

        result = quotes_flow.run_quotes(
            self.cfg, force=True, now=NOW_BEFORE_WINDOW
        )

        self.assertFalse(result.get("skipped", False))
        self.assertEqual(self.fetch_stock.call_count, 1)
        self.assertIsNotNone(self.created_by_key("sh600519", "2026-11-18"))

    def test_source_failure_not_fatal(self):
        """基金源报错只记警告：股票照样落表，失败源不伪造待回补行。"""
        self.holdings = [
            holding("recH1", "sh600519"),
            holding("recH2", "110011"),
        ]
        self.fetch_stock.side_effect = lambda code: {"2026-11-18": 3.0}
        self.fetch_fund.side_effect = quotes.QuoteSourceError("东财超时")

        result = quotes_flow.run_quotes(
            self.cfg, force=True, now=NOW_AFTER_WINDOW
        )

        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("110011", result["errors"][0])
        self.assertGreater(result["updated"], 0)
        stock_row = self.created_by_key("sh600519", "2026-11-18")
        self.assertIsNotNone(stock_row)
        self.assertEqual(stock_row["收盘价/净值"], 3.0)
        self.assertEqual(stock_row["状态"], ["已落"])
        # 源故障 ≠ 净值未公布：不能给失败代码造待回补行。
        self.assertIsNone(self.created_by_key("110011", "2026-11-18"))

    def test_maybe_run_swallows_errors(self):
        """任何底层失败都不得从 maybe_run 逃出去阻塞记账。"""
        for failure in (
            lark.LarkError("飞书挂了"),
            quotes.QuoteSourceError("行情挂了"),
            ValueError("坏数据"),
        ):
            with self.subTest(failure=type(failure).__name__):
                with mock.patch(
                    "quotes_flow.run_quotes", side_effect=failure
                ):
                    with captured_output():
                        result = quotes_flow.maybe_run(
                            self.cfg, NOW_AFTER_WINDOW
                        )
                self.assertIsInstance(result, dict)
                self.assertIn("warning", result)

    def test_maybe_run_noop_when_gate_closed(self):
        """非交易时段 maybe_run 直接返回，不查表。"""
        result = quotes_flow.maybe_run(self.cfg, NOW_BEFORE_WINDOW)

        self.assertTrue(result.get("skipped"))
        self.assertEqual(self.search_calls, [])

    def test_config_last_quotes_date_updated(self):
        """成功跑完一轮才写 last_quotes_date。"""
        self.holdings = [holding("recH1", "sh600519")]
        self.fetch_stock.side_effect = lambda code: {"2026-11-18": 3.0}

        quotes_flow.run_quotes(self.cfg, force=True, now=NOW_AFTER_WINDOW)

        reloaded = config.load_config()
        self.assertEqual(reloaded["last_quotes_date"], "2026-11-18")

    def test_config_not_updated_when_write_fails(self):
        """写表失败时不得把这一轮记成已运行，下次还能重试。"""
        self.holdings = [holding("recH1", "sh600519")]
        self.fetch_stock.side_effect = lambda code: {"2026-11-18": 3.0}
        with mock.patch(
            "lark.create_records", side_effect=lark.LarkError("写表失败")
        ):
            with self.assertRaises(lark.LarkError):
                quotes_flow.run_quotes(
                    self.cfg, force=True, now=NOW_AFTER_WINDOW
                )

        reloaded = config.load_config()
        self.assertIsNone(reloaded["last_quotes_date"])

    def test_backfill_pending_row(self):
        """昨日待回补行今天查到值 → 原地 update 成已落，不新建重复行（验收 5）。"""
        self.holdings = [holding("recH1", "sh600519")]
        self.quote_rows = [quote_row("recQ1", "sh600519", "2026-11-17")]
        self.fetch_stock.side_effect = lambda code: {
            "2026-11-17": 1450.1,
            "2026-11-18": 1460.0,
        }

        quotes_flow.run_quotes(self.cfg, force=True, now=NOW_AFTER_WINDOW)

        fields = self.updated_fields("recQ1")
        self.assertEqual(fields["收盘价/净值"], 1450.1)
        self.assertEqual(fields["状态"], ["已落"])
        self.assertIsNone(self.created_by_key("sh600519", "2026-11-17"))
        # 持仓联动：最新价取最新已落日期。
        holding_fields = self.updated_fields("recH1", "tbl_holding")
        self.assertEqual(holding_fields["最新价"], 1460.0)
        self.assertEqual(holding_fields["最新价日期"], "2026-11-18 00:00")

    def test_fund_window_uses_window_bounds(self):
        """基金请求窗口 = quote_window 的首尾日期。"""
        self.holdings = [holding("recH2", "110011")]
        self.quote_rows = [quote_row("recQ1", "110011", "2026-11-13")]
        self.fetch_fund.side_effect = lambda code, start, end: {}

        quotes_flow.run_quotes(self.cfg, force=True, now=NOW_AFTER_WINDOW)

        # 11-13 距今 5 天（≤7），进入窗口；首尾为 11-13 与 11-18。
        self.fetch_fund.assert_called_once_with(
            "110011", "2026-11-13", "2026-11-18"
        )

    def test_cli_source_failure_exits_zero(self):
        """源告警不影响退出码：CLI 仍返回 0。"""
        self.holdings = [
            holding("recH1", "sh600519"),
            holding("recH2", "110011"),
        ]
        self.fetch_stock.side_effect = lambda code: {"2026-11-18": 3.0}
        self.fetch_fund.side_effect = quotes.QuoteSourceError("东财 502")

        with mock.patch("quotes_flow._now", return_value=NOW_AFTER_WINDOW):
            with captured_output() as (out, err):
                code = ledger.main(["quotes", "--force"])

        self.assertEqual(code, 0)
        self.assertIn("行情", out.getvalue())
        self.assertIn("东财 502", err.getvalue())

    def test_cli_config_failure_exits_four(self):
        """配置/飞书失败导致一条都没法跑时退出 4。"""
        self.config_path.write_text(
            json.dumps({"app_token": "app1", "tables": {}}), encoding="utf-8"
        )

        with mock.patch("quotes_flow._now", return_value=NOW_AFTER_WINDOW):
            with captured_output() as (out, err):
                code = ledger.main(["quotes", "--force"])

        self.assertEqual(code, 4)
        self.assertIn("配置错误", err.getvalue())


class FetchLayerTest(unittest.TestCase):
    """网络层：仅用 fixture/mock，不触网。"""

    def test_fetch_stock_parses_with_browser_ua(self):
        with mock.patch(
            "quotes._http_get", return_value=load_fixture("sina_kline.json")
        ) as http_get:
            parsed = quotes.fetch_stock("sh600519")

        self.assertEqual(parsed["2026-11-17"], 1457.7)
        url, headers = http_get.call_args[0]
        self.assertIn("symbol=sh600519", url)
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertIn("Chrome/131", headers["User-Agent"])

    def test_fetch_fund_parses_with_referer(self):
        with mock.patch(
            "quotes._http_get",
            return_value=load_fixture("eastmoney_nav.json"),
        ) as http_get:
            parsed = quotes.fetch_fund("110011", "2026-11-13", "2026-11-18")

        self.assertEqual(parsed["2026-11-16"], 4.0854)
        url, headers = http_get.call_args[0]
        self.assertIn("fundCode=110011", url)
        self.assertIn("startDate=2026-11-13", url)
        self.assertIn("endDate=2026-11-18", url)
        self.assertEqual(
            headers["Referer"], "https://fundf10.eastmoney.com/jjjz_110011.html"
        )

    def test_fetch_fund_nonzero_errcode_raises_quote_error(self):
        with mock.patch(
            "quotes._http_get",
            return_value='{"ErrCode": 1, "ErrMsg": "system busy"}',
        ):
            with self.assertRaises(quotes.QuoteSourceError):
                quotes.fetch_fund("110011", "2026-11-13", "2026-11-18")

    def test_fetch_stock_unparseable_body_raises_quote_error(self):
        with mock.patch(
            "quotes._http_get", return_value="<html>busy</html>"
        ):
            with self.assertRaises(quotes.QuoteSourceError):
                quotes.fetch_stock("sh600519")

    def test_fetch_transport_error_raises_quote_error(self):
        with mock.patch(
            "quotes._http_get",
            side_effect=quotes.QuoteSourceError("timed out"),
        ):
            with self.assertRaises(quotes.QuoteSourceError):
                quotes.fetch_stock("sh600519")

    def test_fetch_rejects_wrong_code_kind(self):
        with self.assertRaises(ValueError):
            quotes.fetch_stock("110011")
        with self.assertRaises(ValueError):
            quotes.fetch_fund("sh600519", "2026-11-13", "2026-11-18")


if __name__ == "__main__":
    unittest.main()