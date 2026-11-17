"""add 流程：分类实时解析、写入顺序、部分失败报告与 CLI 接线。

全部 hermetic：mock 所有 lark 调用、把 config.CONFIG_PATH 指到临时文件、
时钟用 add_flow._now / 注入的 now 固定，不读真实配置、不触网。
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

import add_flow
import config
import lark
import ledger
import posting
import quotes_flow
import snapshot_flow


UTC8 = datetime.timezone(datetime.timedelta(hours=8))
# 2026-11-17T16:30Z = 2026-11-18 00:30 UTC+8：用来证明默认日期按 UTC+8 取。
FIXED_INSTANT = datetime.datetime(2026, 11, 17, 16, 30, tzinfo=datetime.timezone.utc)


@contextlib.contextmanager
def captured_output():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def account(record_id, name, type_, balance):
    return {
        "record_id": record_id,
        "fields": {
            "账户名称": name,
            "类型": [type_],
            "初始余额": float(balance),
            "当前余额": float(balance),
            "状态": ["在用"],
            "备注": "",
        },
    }


def category(record_id, full_name, type_):
    return {
        "record_id": record_id,
        "fields": {"分类": full_name, "类型": [type_]},
    }


class AddFlowTestBase(unittest.TestCase):
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

        self.cfg = config.empty_config("app1", "nod1")
        self.cfg["tables"] = {
            "账户": {"table_id": "tbl_account", "fields": {}},
            "持仓": {"table_id": "tbl_holding", "fields": {}},
            "分类": {"table_id": "tbl_category", "fields": {}},
            "收支明细": {"table_id": "tbl_entry", "fields": {}},
        }
        self.config_path.write_text(
            json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8"
        )

        self.accounts = [
            account("recA1", "现金", "现金", 1000),
            account("recA2", "招行工资卡", "银行卡", 1000),
            account("recA3", "华泰证券", "理财", 50000),
        ]
        self.categories = [
            category("recC1", "支出·餐饮·午餐", "支出"),
            category("recC2", "收入·工资", "收入"),
            # 用户可自建无前缀分类：方向只能看「类型」，不能猜名字或金额符号。
            category("recC3", "咖啡", "支出"),
            {"record_id": "recC4", "fields": {"分类": "待定"}},
        ]
        self.entry_rows = []
        self.calls = []  # ("create", table_id) / ("update", table_id, record_ids)
        self.search_calls = []
        self.fail_update = False
        self._next_id = 0

        # 任何真实 lark-cli 调用都必须让测试炸掉。
        patch("lark.run_cli", side_effect=AssertionError("测试不应调用 lark-cli"))
        self.search_records = patch(
            "lark.search_records", side_effect=self._search
        )
        self.create_records = patch(
            "lark.create_records", side_effect=self._create
        )
        self.update_records = patch(
            "lark.update_records", side_effect=self._update
        )

        # CLI 缺省日期取 _now()；固定成 FIXED_INSTANT，断言不依赖真实时钟。
        self.now = patch("add_flow._now", return_value=FIXED_INSTANT)
        self.quotes = patch(
            "quotes_flow.maybe_run", side_effect=lambda cfg, now=None: None
        )
        self.snapshot = patch(
            "snapshot_flow.maybe_weekly", side_effect=lambda cfg, now=None: None
        )

    def _stop_patches(self):
        for patcher in reversed(self._patches):
            patcher.stop()

    def _search(self, app_token, table_id, conditions=None, limit=2000):
        self.search_calls.append(table_id)
        if table_id == "tbl_account":
            return [copy.deepcopy(item) for item in self.accounts[:limit]]
        if table_id == "tbl_category":
            return [copy.deepcopy(item) for item in self.categories[:limit]]
        if table_id == "tbl_holding":
            return []
        self.fail("不应搜索表 %s" % table_id)

    def _create(self, app_token, table_id, records):
        self.calls.append(("create", table_id))
        if table_id != "tbl_entry":
            self.fail("创建了未预期的表 %s" % table_id)
        ids = []
        for fields in records:
            self._next_id += 1
            record_id = "recNew%d" % self._next_id
            self.entry_rows.append(
                {"record_id": record_id, "fields": copy.deepcopy(fields)}
            )
            ids.append(record_id)
        return ids

    def _update(self, app_token, table_id, updates):
        self.calls.append(("update", table_id, tuple(updates)))
        if self.fail_update:
            raise lark.LarkError("模拟账户余额更新失败")
        if table_id != "tbl_account":
            self.fail("更新了未预期的表 %s" % table_id)
        for record_id, fields in updates.items():
            for item in self.accounts:
                if item["record_id"] == record_id:
                    item["fields"].update(fields)
                    break
            else:
                self.fail("更新了不存在的账户 %s" % record_id)

    def entry(self, **overrides):
        data = {
            "kind": "expense",
            "date": "2026-11-17",
            "amount": 35,
            "account": "现金",
            "category": "支出·餐饮·午餐",
            "summary": "午饭",
            "note": "",
        }
        data.update(overrides)
        return data

    def entry_fields(self):
        self.assertEqual(len(self.entry_rows), 1)
        return self.entry_rows[0]["fields"]

    def account_balance(self, record_id):
        for item in self.accounts:
            if item["record_id"] == record_id:
                return item["fields"]["当前余额"]
        self.fail("没有账户 %s" % record_id)


class ResolveCategoryTest(AddFlowTestBase):
    def test_category_resolved_live(self):
        # 每次解析都重新搜表：表里改了分类名，下一次解析立刻看到。
        first = add_flow.resolve_category(self.cfg, "支出·餐饮·午餐")
        self.categories[0]["fields"]["分类"] = "支出·餐饮·晚餐"
        second = add_flow.resolve_category(self.cfg, "支出·餐饮·晚餐")

        self.assertEqual(first, "recC1")
        self.assertEqual(second, "recC1")
        self.assertEqual(self.search_calls.count("tbl_category"), 2)

    def test_unknown_category_raises(self):
        with self.assertRaises(posting.PostingError) as caught:
            add_flow.resolve_category(self.cfg, "支出·餐饮·不存在的分类")

        message = str(caught.exception)
        self.assertIn("分类不存在", message)
        self.assertIn("分类表", message)
        self.assertIn("添加", message)
        self.assertFalse(self.create_records.called)

    def test_resolve_category_never_creates(self):
        add_flow.resolve_category(self.cfg, "支出·餐饮·午餐")

        self.assertFalse(self.create_records.called)
        self.assertFalse(self.update_records.called)


class ExecuteTest(AddFlowTestBase):
    def test_write_order_details_first(self):
        result = add_flow.execute(self.cfg, self.entry())

        self.assertEqual(
            [(call[0], call[1]) for call in self.calls],
            [("create", "tbl_entry"), ("update", "tbl_account")],
        )
        self.assertEqual(result["created"], ["recNew1"])
        self.assertEqual(result["updated"], 1)

    def test_entry_fields_use_cell_values(self):
        add_flow.execute(self.cfg, self.entry())

        fields = self.entry_fields()
        # 日期是 datetime 单元格：写 "YYYY-MM-DD 00:00"，不是毫秒。
        self.assertEqual(fields["日期"], "2026-11-17 00:00")
        self.assertEqual(fields["类型"], ["支出"])
        self.assertEqual(fields["金额"], 35.0)
        self.assertEqual(fields["账户"], [{"id": "recA1"}])
        self.assertEqual(fields["分类"], [{"id": "recC1"}])
        self.assertEqual(fields["摘要"], "午饭")
        self.assertEqual(fields["备注"], "")
        self.assertEqual(self.account_balance("recA1"), 965.0)

    def test_income_credits_account(self):
        result = add_flow.execute(
            self.cfg,
            self.entry(
                kind="income",
                amount=5000,
                category="收入·工资",
                summary="工资",
            ),
        )

        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.entry_fields()["类型"], ["收入"])
        self.assertEqual(self.entry_fields()["分类"], [{"id": "recC2"}])
        self.assertEqual(self.account_balance("recA1"), 6000.0)

    def test_transfer_updates_both_accounts_and_has_no_category(self):
        result = add_flow.execute(
            self.cfg,
            {
                "kind": "transfer",
                "date": "2026-11-17",
                "amount": 100,
                "account": "现金",
                "to_account": "招行工资卡",
                "summary": "充值",
                "note": "",
            },
        )

        fields = self.entry_fields()
        self.assertEqual(fields["类型"], ["转账"])
        self.assertEqual(fields["账户"], [{"id": "recA1"}])
        self.assertEqual(fields["转入账户"], [{"id": "recA2"}])
        self.assertNotIn("分类", fields)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(self.account_balance("recA1"), 900.0)
        self.assertEqual(self.account_balance("recA2"), 1100.0)

    def test_partial_failure_reports(self):
        self.fail_update = True

        with self.assertRaises(add_flow.PartialWriteError) as caught:
            add_flow.execute(self.cfg, self.entry())

        exc = caught.exception
        self.assertIsInstance(exc.cause, lark.LarkError)
        self.assertEqual(len(exc.completed), 1)
        self.assertIn("收支明细", exc.completed[0])
        self.assertTrue(exc.unfinished)
        # 精确指出可能已落库的明细和失败步骤，CLI 才能给出可核对的信息。
        self.assertEqual(exc.created_ids, ["recNew1"])
        self.assertIn("更新「账户」recA1", exc.failed_step)
        self.assertIn("已完成", str(exc))
        self.assertIn("未完成", str(exc))
        # 明细先落库：失败时它已经存在，不能被报告成“什么都没发生”。
        self.assertEqual(len(self.entry_rows), 1)
        self.assertEqual(self.account_balance("recA1"), 1000.0)

    def test_create_failure_reports_nothing_completed(self):
        self.create_records.side_effect = lark.LarkError("模拟明细写入失败")

        with self.assertRaises(add_flow.PartialWriteError) as caught:
            add_flow.execute(self.cfg, self.entry())

        self.assertEqual(caught.exception.completed, [])
        self.assertEqual(caught.exception.created_ids, [])
        self.assertIn("创建「收支明细」", caught.exception.failed_step)
        self.assertFalse(self.update_records.called)
        self.assertEqual(self.entry_rows, [])

    def test_missing_entry_table_config_fails_before_any_write(self):
        del self.cfg["tables"]["收支明细"]

        with self.assertRaises(config.ConfigError):
            add_flow.execute(self.cfg, self.entry())

        self.assertFalse(self.create_records.called)

    def test_bad_entry_fails_before_any_write(self):
        with self.assertRaises(posting.PostingError):
            add_flow.execute(self.cfg, self.entry(date="2026/11/17"))

        self.assertFalse(self.create_records.called)
        self.assertFalse(self.update_records.called)

    def test_expense_with_income_category_raises(self):
        with self.assertRaises(posting.PostingError) as caught:
            add_flow.execute(self.cfg, self.entry(category="收入·工资"))

        message = str(caught.exception)
        self.assertIn("收入·工资", message)
        self.assertIn("收入", message)
        self.assertFalse(self.create_records.called)
        self.assertFalse(self.update_records.called)

    def test_income_with_expense_category_raises(self):
        with self.assertRaises(posting.PostingError) as caught:
            add_flow.execute(
                self.cfg,
                self.entry(kind="income", amount=5000, category="支出·餐饮·午餐"),
            )

        self.assertIn("支出·餐饮·午餐", str(caught.exception))
        self.assertFalse(self.create_records.called)
        self.assertFalse(self.update_records.called)

    def test_explicit_kind_category_without_type_raises(self):
        with self.assertRaises(posting.PostingError) as caught:
            add_flow.execute(self.cfg, self.entry(category="待定"))

        self.assertIn("待定", str(caught.exception))
        self.assertIn("类型", str(caught.exception))
        self.assertFalse(self.create_records.called)

    def test_irrelevant_fields_rejected(self):
        with self.assertRaises(posting.PostingError):
            add_flow.execute(self.cfg, self.entry(to_account="招行工资卡"))

        with self.assertRaises(posting.PostingError):
            add_flow.execute(
                self.cfg,
                {
                    "kind": "transfer",
                    "date": "2026-11-17",
                    "amount": 10,
                    "account": "现金",
                    "to_account": "招行工资卡",
                    "category": "支出·餐饮·午餐",
                },
            )

        self.assertFalse(self.create_records.called)

    def test_hooks_run_in_order_after_writes(self):
        # 钩子写进同一个调用日志，才能证明“先写入、后钩子”而不是只证明钩子顺序。
        self.quotes.side_effect = lambda cfg, now=None: self.calls.append(
            ("quotes", cfg, now)
        )
        self.snapshot.side_effect = lambda cfg, now=None: self.calls.append(
            ("snapshot", cfg, now)
        )

        add_flow.execute(self.cfg, self.entry(), now=FIXED_INSTANT)

        self.assertEqual(
            [call[0] for call in self.calls],
            ["create", "update", "quotes", "snapshot"],
        )
        self.assertIs(self.calls[2][1], self.cfg)
        self.assertEqual(self.calls[2][2], FIXED_INSTANT)
        self.assertEqual(self.calls[3][2], FIXED_INSTANT)

    def test_hook_failure_never_fails_add(self):
        self.quotes.side_effect = RuntimeError("行情源不可用")

        with captured_output() as (out, err):
            result = add_flow.execute(self.cfg, self.entry(), now=FIXED_INSTANT)

        self.assertEqual(result["created"], ["recNew1"])
        self.assertIn("警告", err.getvalue())
        # 行情失败后仍继续尝试快照。
        self.assertTrue(self.snapshot.called)


class LedgerCliAddTest(AddFlowTestBase):
    def test_cli_add_exit_0(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "expense",
                    "--date",
                    "2026-11-17",
                    "--amount",
                    "35",
                    "--account",
                    "现金",
                    "--category",
                    "支出·餐饮·午餐",
                    "--summary",
                    "午饭",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertIn("已记账", out.getvalue())
        self.assertIn("recNew1", out.getvalue())
        self.assertEqual(self.entry_fields()["日期"], "2026-11-17 00:00")
        self.assertEqual(self.account_balance("recA1"), 965.0)

    def test_cli_missing_account_exit_2(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--amount",
                    "35",
                    "--category",
                    "支出·餐饮·午餐",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("--account", err.getvalue())
        # argparse 在 _run_add 之前就退出：不读配置、不查表、不写任何东西。
        self.assertEqual(self.search_calls, [])
        self.assertFalse(self.create_records.called)

    def test_cli_default_date_today_utc8(self):
        # FIXED_INSTANT 在 UTC 是 11-17 16:30，UTC+8 已是 11-18。
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "expense",
                    "--amount",
                    "35",
                    "--account",
                    "现金",
                    "--category",
                    "支出·餐饮·午餐",
                    "--summary",
                    "午饭",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(self.entry_fields()["日期"], "2026-11-18 00:00")

    def test_today_utc8_converts_from_instant(self):
        with mock.patch("add_flow._now", return_value=FIXED_INSTANT):
            self.assertEqual(add_flow.today_utc8(), "2026-11-18")

    def test_cli_unknown_category_exit_2(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "expense",
                    "--amount",
                    "35",
                    "--account",
                    "现金",
                    "--category",
                    "支出·餐饮·不存在",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("分类", err.getvalue())
        self.assertFalse(self.create_records.called)

    def test_cli_partial_failure_exit_4_reports_steps(self):
        self.fail_update = True

        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "expense",
                    "--date",
                    "2026-11-17",
                    "--amount",
                    "35",
                    "--account",
                    "现金",
                    "--category",
                    "支出·餐饮·午餐",
                ]
            )

        self.assertEqual(code, 4)
        report = err.getvalue()
        self.assertIn("已完成", report)
        self.assertIn("未完成", report)
        self.assertIn("失败步骤：更新「账户」recA1", report)
        self.assertIn("可能已写入的明细：recNew1", report)
        self.assertEqual(len(self.entry_rows), 1)

    def test_cli_auto_prefixless_expense_category(self):
        # 回归：用户自建的无前缀「支出」分类 + 正数金额，必须记支出、账户减少。
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--amount",
                    "35",
                    "--account",
                    "现金",
                    "--category",
                    "咖啡",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(self.entry_fields()["类型"], ["支出"])
        self.assertEqual(self.entry_fields()["分类"], [{"id": "recC3"}])
        self.assertEqual(self.account_balance("recA1"), 965.0)

    def test_cli_auto_income_category(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--amount",
                    "5000",
                    "--account",
                    "现金",
                    "--category",
                    "收入·工资",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(self.entry_fields()["类型"], ["收入"])
        self.assertEqual(self.entry_fields()["分类"], [{"id": "recC2"}])
        self.assertEqual(self.account_balance("recA1"), 6000.0)

    def test_cli_auto_category_without_type_exit_2(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--amount",
                    "35",
                    "--account",
                    "现金",
                    "--category",
                    "待定",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("方向不明确", err.getvalue())
        self.assertIn("--kind income|expense", err.getvalue())
        self.assertFalse(self.create_records.called)

    def test_cli_auto_without_category_exit_2(self):
        with captured_output() as (out, err):
            code = ledger.main(
                ["add", "--amount", "35", "--account", "现金"]
            )

        self.assertEqual(code, 2)
        self.assertIn("方向不明确", err.getvalue())
        self.assertEqual(self.search_calls, [])
        self.assertFalse(self.create_records.called)

    def test_cli_auto_does_not_use_amount_sign(self):
        # 负金额不能把支出分类翻成收入：方向只看分类类型，金额本身不合法。
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--amount=-35",
                    "--account",
                    "现金",
                    "--category",
                    "咖啡",
                ]
            )

        self.assertEqual(code, 2)
        self.assertEqual(self.entry_rows, [])
        self.assertFalse(self.create_records.called)

    def test_cli_auto_with_to_account_is_transfer(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--amount",
                    "100",
                    "--account",
                    "现金",
                    "--to-account",
                    "招行工资卡",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(self.entry_fields()["类型"], ["转账"])
        self.assertEqual(self.account_balance("recA1"), 900.0)
        self.assertEqual(self.account_balance("recA2"), 1100.0)

    def test_cli_explicit_kind_mismatched_category_exit_2(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "expense",
                    "--amount",
                    "35",
                    "--account",
                    "现金",
                    "--category",
                    "收入·工资",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("收入·工资", err.getvalue())
        self.assertFalse(self.create_records.called)

    def test_cli_unknown_account_exit_5(self):
        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "expense",
                    "--amount",
                    "35",
                    "--account",
                    "不存在的卡",
                    "--category",
                    "支出·餐饮·午餐",
                ]
            )

        self.assertEqual(code, 5)
        self.assertIn("数据错误", err.getvalue())

    def test_cli_missing_current_balance_exit_5(self):
        # Base 里缺单元格是数据问题，不是用法错误。
        del self.accounts[0]["fields"]["当前余额"]

        with captured_output() as (out, err):
            code = ledger.main(
                [
                    "add",
                    "--kind",
                    "expense",
                    "--amount",
                    "35",
                    "--account",
                    "现金",
                    "--category",
                    "支出·餐饮·午餐",
                ]
            )

        self.assertEqual(code, 5)
        self.assertIn("数据错误", err.getvalue())
        self.assertIn("当前余额", err.getvalue())
        self.assertFalse(self.create_records.called)


if __name__ == "__main__":
    unittest.main()
