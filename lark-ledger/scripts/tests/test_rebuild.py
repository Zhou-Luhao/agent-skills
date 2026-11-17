"""edit / delete / rebuild 一致性引擎：纯重放、差异报告、整组删除与 CLI。

全部 hermetic：mock 所有 lark 调用、把 config.CONFIG_PATH 指到临时文件；
不触网、不读真实配置、不依赖真实时钟。

关键约束（Task 7 评审结论）：rebuild 必须逐笔重放明细对账户/持仓的
变动，不能对明细行做朴素求和——理财买入的两条行（银行卡→理财、理财自转）
只有前者改变现金，后者净额为 0。
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
import rebuild_flow

DATE = "2026-11-17"


@contextlib.contextmanager
def captured_output():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


# ---------------------------------------------------------------------------
# 构造与飞书 CellValue 同形的记录
# ---------------------------------------------------------------------------


def account_record(
    record_id, name, type_, opening, current=None, rate=None
):
    fields = {
        "账户名称": name,
        "类型": [type_],
        "初始余额": float(opening),
        "当前余额": float(opening if current is None else current),
        "状态": ["在用"],
        "备注": "",
    }
    if rate is not None:
        fields["年利率"] = float(rate)
    return {"record_id": record_id, "fields": fields}


def holding_record(
    record_id,
    name,
    opening_shares,
    opening_cost,
    shares=None,
    cost=None,
    account_id="a_invest",
    code="sh600519",
    breed="股票",
    status="持有",
):
    return {
        "record_id": record_id,
        "fields": {
            "持仓名称": name,
            "代码": code,
            "品种": [breed],
            "理财账户": [{"id": account_id}],
            "当前份额": float(
                opening_shares if shares is None else shares
            ),
            "成本价": float(opening_cost if cost is None else cost),
            "期初份额": float(opening_shares),
            "期初成本": float(opening_cost),
            "状态": [status],
        },
    }


def entry_row(
    record_id,
    type_,
    amount,
    account,
    date=DATE,
    to=None,
    category=None,
    holding=None,
    price=None,
    shares=None,
    note="",
    summary="",
    created=1794844800000,
):
    fields = {
        "摘要": summary,
        "日期": date + " 00:00",
        "类型": [type_],
        "金额": float(amount),
        "账户": [{"id": account}],
        "备注": note,
        "创建时间": int(created),
    }
    if to is not None:
        fields["转入账户"] = [{"id": to}]
    if category is not None:
        fields["分类"] = [{"id": category}]
    if holding is not None:
        fields["持仓"] = [{"id": holding}]
        fields["成交价"] = float(price)
        fields["份额"] = float(shares)
    return {"record_id": record_id, "fields": fields}


def category_record(record_id, full_name, type_):
    return {"record_id": record_id, "fields": {"分类": full_name, "类型": [type_]}}


def diff_item(diff, field, object_contains):
    for item in diff:
        if item["字段"] == field and object_contains in item["对象"]:
            return item
    return None


# ---------------------------------------------------------------------------
# 纯逻辑：replay
# ---------------------------------------------------------------------------


class ReplayTest(unittest.TestCase):
    def test_replay_from_opening(self):
        accounts = {
            "a1": account_record("a1", "现金", "现金", 1000)["fields"],
        }
        entries = [
            entry_row("e1", "收入", 200, "a1", category="c_salary"),
            entry_row("e2", "支出", 35, "a1", category="c_food"),
        ]

        replayed = posting.replay(entries, accounts, {})

        self.assertEqual(replayed["accounts"]["a1"], 1165.0)
        self.assertEqual(replayed["diagnostics"], [])

    def test_replay_order_independent_of_input_order(self):
        accounts = {"a1": account_record("a1", "现金", "现金", 1000)["fields"]}
        entries = [
            entry_row("e1", "收入", 200, "a1"),
            entry_row("e2", "支出", 35, "a1"),
        ]

        forward = posting.replay(entries, accounts, {})
        backward = posting.replay(list(reversed(entries)), accounts, {})

        self.assertEqual(forward["accounts"], backward["accounts"])

    def test_replay_invest_cost(self):
        accounts = {
            "a_bank": account_record("a_bank", "招行", "银行卡", 100000)["fields"],
            "a_invest": account_record("a_invest", "华泰", "理财", 50000)["fields"],
        }
        holdings = {
            "h1": holding_record("h1", "茅台", 1000, 10)["fields"],
        }
        tag = " #G0000abcd"
        entries = [
            entry_row(
                "e1",
                "转账",
                6000,
                "a_bank",
                to="a_invest",
                note=tag,
                created=100,
            ),
            entry_row(
                "e2",
                "转账",
                6000,
                "a_invest",
                to="a_invest",
                holding="h1",
                price=12,
                shares=500,
                note=tag,
                created=101,
            ),
        ]

        replayed = posting.replay(entries, accounts, holdings)

        self.assertEqual(
            replayed["holdings"]["h1"],
            {"shares": 1500.0, "cost": 10.6667, "status": "持有"},
        )
        # 买入现金：银行卡 −6000；理财账户「当前余额」是账户内闲置现金，
        # 买入不改变它（钱进了持仓）。行级求和会错算成 56000，这里必须 50000。
        self.assertEqual(replayed["accounts"]["a_bank"], 94000.0)
        self.assertEqual(replayed["accounts"]["a_invest"], 50000.0)
        self.assertEqual(replayed["diagnostics"], [])

    def test_replay_dividend_credits_investment_cash(self):
        # 分红没有持仓交易行，是真正的现金收入，必须进入理财账户闲置现金。
        accounts = {
            "a_invest": account_record("a_invest", "华泰", "理财", 50000)["fields"],
        }
        entries = [
            entry_row("e1", "收入", 500, "a_invest", category="c_sell"),
        ]

        replayed = posting.replay(entries, accounts, {})

        self.assertEqual(replayed["accounts"]["a_invest"], 50500.0)

    def test_replay_ordering_uses_created_time(self):
        # 先卖后买与先买后卖会影响加权成本：顺序必须由 创建时间 决定。
        def build(buy_created, sell_created):
            tag_buy, tag_sell = " #G00000001", " #G00000002"
            return [
                entry_row(
                    "b1", "转账", 6000, "a_bank", to="a_invest",
                    note=tag_buy, created=buy_created,
                ),
                entry_row(
                    "b2", "转账", 6000, "a_invest", to="a_invest",
                    holding="h1", price=12, shares=500,
                    note=tag_buy, created=buy_created + 1,
                ),
                entry_row(
                    "s1", "转账", 6000, "a_invest", to="a_bank",
                    note=tag_sell, created=sell_created,
                ),
                entry_row(
                    "s2", "收入", 1000, "a_invest",
                    note=tag_sell, created=sell_created + 1,
                ),
                entry_row(
                    "s3", "转账", 6000, "a_invest", to="a_invest",
                    holding="h1", price=12, shares=500,
                    note=tag_sell, created=sell_created + 2,
                ),
            ]

        accounts = {
            "a_bank": account_record("a_bank", "招行", "银行卡", 100000)["fields"],
            "a_invest": account_record("a_invest", "华泰", "理财", 50000)["fields"],
        }
        holdings = {"h1": holding_record("h1", "茅台", 1000, 10)["fields"]}

        buy_first = posting.replay(build(100, 300), accounts, holdings)
        sell_first = posting.replay(build(300, 100), accounts, holdings)

        self.assertEqual(buy_first["holdings"]["h1"]["cost"], 10.6667)
        self.assertEqual(buy_first["holdings"]["h1"]["shares"], 1000.0)
        # 先卖 500（成本不变 10），再买 500@12 ⇒ (5000+6000)/1000 = 11。
        self.assertEqual(sell_first["holdings"]["h1"]["cost"], 11.0)
        self.assertEqual(sell_first["holdings"]["h1"]["shares"], 1000.0)

    def test_replay_loan_split_rows(self):
        accounts = {
            "a_bank": account_record("a_bank", "招行", "银行卡", 10000)["fields"],
            "a_loan": account_record(
                "a_loan", "房贷", "负债", -500000, rate=0.042
            )["fields"],
        }
        tag = " #G00000009"
        entries = [
            entry_row("e1", "支出", 100, "a_bank", note=tag, created=100),
            entry_row(
                "e2", "转账", 900, "a_bank", to="a_loan",
                note=tag, created=101,
            ),
        ]

        replayed = posting.replay(entries, accounts, {})

        self.assertEqual(replayed["accounts"]["a_bank"], 9000.0)
        self.assertEqual(replayed["accounts"]["a_loan"], -499100.0)

    def test_replay_loan_repay_from_investment_account_not_tainted(self):
        # 余额宝（类型=理财）也可以作为贷款还款的付款账户：add 不限制付款方
        # 类型，principal 行是 tagged 的 理财→负债 转账、没有持仓交易行。
        # 这不是「丢了买入成交行」，不能被 _missing_trade_row 误判为 tainted。
        accounts = {
            "a_yuebao": account_record(
                "a_yuebao", "余额宝", "理财", 10000
            )["fields"],
            "a_loan": account_record(
                "a_loan", "房贷", "负债", -500000, rate=0.042
            )["fields"],
        }
        entry = {
            "kind": "loan_repay",
            "date": DATE,
            "amount": 3000,
            "account": "a_yuebao",
            "loan_account": "a_loan",
            "interest_category": "c_food",
            "note": "",
        }
        plan = posting.plan_add(entry, copy.deepcopy(accounts), {})
        rows = []
        for index, (table, fields) in enumerate(plan["create"]):
            self.assertEqual(table, "收支明细")
            item = dict(fields)
            item["日期"] = item["日期"] + " 00:00"
            item["创建时间"] = 1000 + index
            rows.append({"record_id": "r%d" % index, "fields": item})

        replayed = posting.replay(rows, copy.deepcopy(accounts), {})

        self.assertFalse(replayed["tainted"])
        self.assertEqual(replayed["diagnostics"], [])
        # 付款账户 −总额；负债账户只被本金推动（利息 1750、本金 1250）。
        self.assertEqual(replayed["accounts"]["a_yuebao"], 7000.0)
        self.assertEqual(replayed["accounts"]["a_loan"], -498750.0)

    def test_replay_missing_account_reports_diagnostic(self):
        accounts = {"a1": account_record("a1", "现金", "现金", 1000)["fields"]}
        entries = [entry_row("e1", "支出", 35, "ghost")]

        replayed = posting.replay(entries, accounts, {})

        self.assertEqual(replayed["accounts"]["a1"], 1000.0)
        self.assertTrue(replayed["diagnostics"])
        self.assertIn("ghost", " ".join(replayed["diagnostics"]))

    def test_replay_missing_holding_reports_diagnostic(self):
        accounts = {
            "a_bank": account_record("a_bank", "招行", "银行卡", 100000)["fields"],
            "a_invest": account_record("a_invest", "华泰", "理财", 50000)["fields"],
        }
        tag = " #G0000000a"
        entries = [
            entry_row(
                "e1", "转账", 6000, "a_bank", to="a_invest",
                note=tag, created=100,
            ),
            entry_row(
                "e2", "转账", 6000, "a_invest", to="a_invest",
                holding="ghost", price=12, shares=500,
                note=tag, created=101,
            ),
        ]

        replayed = posting.replay(entries, accounts, {})

        self.assertEqual(replayed["accounts"]["a_bank"], 94000.0)
        self.assertTrue(replayed["diagnostics"])
        self.assertIn("ghost", " ".join(replayed["diagnostics"]))

    def test_replay_matches_plan_add_for_every_kind(self):
        """重放结果必须与 add 的 plan_add 联动值完全一致（含理财/贷款）。"""

        def rows_from_plan(plan):
            rows = []
            for index, (table, fields) in enumerate(plan["create"]):
                self.assertEqual(table, "收支明细")
                item = dict(fields)
                if isinstance(item.get("日期"), str) and len(item["日期"]) == 10:
                    item["日期"] = item["日期"] + " 00:00"
                item["创建时间"] = 1000 + index
                rows.append({"record_id": "r%d" % index, "fields": item})
            return rows

        def account(name, balance, type_="现金", rate=None):
            fields = {
                "账户名称": name,
                "类型": [type_],
                "初始余额": float(balance),
                "当前余额": float(balance),
                "状态": ["在用"],
            }
            if rate is not None:
                fields["年利率"] = rate
            return fields

        def holding(name, shares, cost, account_id):
            return {
                "持仓名称": name,
                "代码": "sh600519",
                "品种": ["股票"],
                "理财账户": [{"id": account_id}],
                "当前份额": float(shares),
                "成本价": float(cost),
                "期初份额": float(shares),
                "期初成本": float(cost),
                "状态": ["持有"],
            }

        cash = {"a1": account("现金", 1000)}
        bank = {"a_bank": account("招行", 100000, "银行卡")}
        invest = {
            "a_bank": account("招行", 100000, "银行卡"),
            "a_invest": account("华泰", 50000, "理财"),
        }
        fund = {"h1": holding("茅台", 1000, 10, "a_invest")}
        loan = {
            "a_bank": account("招行", 10000, "银行卡"),
            "a_loan": account("房贷", -500000, "负债", 0.042),
        }

        cases = [
            (
                "income",
                {
                    "kind": "income", "date": DATE, "amount": 200,
                    "account": "a1", "category": "c_salary", "note": "",
                },
                cash,
                {},
            ),
            (
                "expense",
                {
                    "kind": "expense", "date": DATE, "amount": 35,
                    "account": "a1", "category": "c_food", "note": "",
                },
                cash,
                {},
            ),
            (
                "transfer",
                {
                    "kind": "transfer", "date": DATE, "amount": 100,
                    "account": "a1", "to_account": "a2", "note": "",
                },
                {
                    "a1": account("现金", 1000),
                    "a2": account("招行", 1000, "银行卡"),
                },
                {},
            ),
            (
                "invest_buy",
                {
                    "kind": "invest_buy", "date": DATE, "holding": "h1",
                    "price": 12, "shares": 500, "account": "a_invest",
                    "from_account": "a_bank", "note": "",
                },
                invest,
                fund,
            ),
            (
                "invest_sell",
                {
                    "kind": "invest_sell", "date": DATE, "holding": "h1",
                    "price": 12, "shares": 500, "account": "a_invest",
                    "to_account": "a_bank", "pnl_category": "c_sell",
                    "note": "",
                },
                invest,
                fund,
            ),
            (
                "dividend",
                {
                    "kind": "dividend", "date": DATE, "amount": 500,
                    "account": "a_invest", "pnl_category": "c_sell",
                    "note": "",
                },
                invest,
                fund,
            ),
            (
                "loan_repay",
                {
                    "kind": "loan_repay", "date": DATE, "amount": 3000,
                    "account": "a_bank", "loan_account": "a_loan",
                    "interest_category": "c_food", "note": "",
                },
                loan,
                {},
            ),
        ]

        for kind, entry, accounts, holdings in cases:
            with self.subTest(kind=kind):
                accounts = copy.deepcopy(accounts)
                holdings = copy.deepcopy(holdings)
                plan = posting.plan_add(entry, accounts, holdings)
                replayed = posting.replay(
                    rows_from_plan(plan), accounts, holdings
                )
                for table, record_id, fields in plan["update"]:
                    if table == "账户":
                        self.assertAlmostEqual(
                            replayed["accounts"][record_id],
                            fields["当前余额"],
                            places=9,
                        )
                    else:
                        state = replayed["holdings"][record_id]
                        if "当前份额" in fields:
                            self.assertAlmostEqual(
                                state["shares"], fields["当前份额"], places=9
                            )
                        if "成本价" in fields:
                            self.assertAlmostEqual(
                                state["cost"], fields["成本价"], places=9
                            )
                self.assertEqual(replayed["diagnostics"], [])

    def test_replay_missing_trade_account_taints(self):
        accounts = {
            "a_bank": account_record(
                "a_bank", "招行", "银行卡", 100000
            )["fields"],
        }
        holdings = {"h1": holding_record("h1", "茅台", 1000, 10)["fields"]}
        tag = " #G0000ab12"
        entries = [
            entry_row(
                "e1", "转账", 6000, "a_bank", to="a_missing",
                note=tag, created=100,
            ),
            entry_row(
                "e2", "转账", 6000, "a_missing", to="a_missing",
                holding="h1", price=12, shares=500,
                note=tag, created=101,
            ),
        ]

        replayed = posting.replay(entries, accounts, holdings)

        self.assertTrue(replayed["tainted"])
        self.assertIn("a_missing", " ".join(replayed["diagnostics"]))
        # 账户缺失时持仓不能被静默更新。
        self.assertEqual(
            replayed["holdings"]["h1"]["shares"], 1000.0
        )

    def test_replay_sell_trade_row_missing_taints_not_row_sum(self):
        # tagged 的 理财→银行卡 卖出转账 + 收入 P&L 行，持仓成交行被删：
        # 行级求和会得到理财 40000 / 银行 112000；必须判 tainted 并整组跳过。
        accounts = {
            "a_bank": account_record("a_bank", "招行", "银行卡", 100000)[
                "fields"
            ],
            "a_invest": account_record("a_invest", "华泰", "理财", 50000)[
                "fields"
            ],
        }
        holdings = {"h1": holding_record("h1", "茅台", 1000, 10)["fields"]}
        tag = " #G0000ca11"
        entries = [
            entry_row(
                "s1", "转账", 12000, "a_invest", to="a_bank",
                note=tag, created=100,
            ),
            entry_row(
                "s2", "收入", 2000, "a_invest", category="c_sell",
                note=tag, created=101,
            ),
        ]

        replayed = posting.replay(entries, accounts, holdings)

        self.assertTrue(replayed["tainted"])
        self.assertIn(
            "疑似缺少持仓交易行", " ".join(replayed["diagnostics"])
        )
        self.assertEqual(replayed["accounts"]["a_invest"], 50000.0)
        self.assertEqual(replayed["accounts"]["a_bank"], 100000.0)
        self.assertEqual(replayed["holdings"]["h1"]["shares"], 1000.0)

    def test_group_rows_by_tag_and_single_without_tag(self):
        tagged_a = entry_row("e1", "支出", 10, "a1", note="x #G0000beef")
        tagged_b = entry_row("e2", "支出", 20, "a1", note="#G0000beef")
        plain = entry_row("e3", "支出", 30, "a1")

        self.assertEqual(
            [
                row["record_id"]
                for row in posting.group_rows(
                    [tagged_a, tagged_b, plain], tagged_a
                )
            ],
            ["e1", "e2"],
        )
        self.assertEqual(
            [row["record_id"] for row in posting.group_rows([tagged_a, plain], plain)],
            ["e3"],
        )

# ---------------------------------------------------------------------------
# 流程级：rebuild / delete / edit（mock 全部 I/O）
# ---------------------------------------------------------------------------


class RebuildFlowTestBase(unittest.TestCase):
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
            account_record("a_cash", "现金", "现金", 1000),
        ]
        self.holdings = []
        self.categories = [
            category_record("c_food", "支出·餐饮·午餐", "支出"),
            category_record("c_salary", "收入·工资", "收入"),
            category_record("c_sell", "收入·理财收益·股票收益", "收入"),
        ]
        self.entries = []

        self.calls = []
        self.searches = []
        self.fail_update = False

        patch("lark.run_cli", side_effect=AssertionError("测试不应调用 lark-cli"))
        patch("lark.search_records", side_effect=self._search)
        patch("lark.create_records", side_effect=AssertionError("不应创建记录"))
        patch("lark.update_records", side_effect=self._update)
        patch("lark.update_record", side_effect=AssertionError("应使用批量更新"))
        patch("lark.delete_records", side_effect=self._delete)

    def _stop_patches(self):
        for patcher in reversed(self._patches):
            patcher.stop()

    def _search(
        self, app_token, table_id, conditions=None, limit=2000, offset=0
    ):
        self.searches.append(table_id)
        if table_id == "tbl_account":
            records = self.accounts
        elif table_id == "tbl_holding":
            records = self.holdings
        elif table_id == "tbl_category":
            records = self.categories
        elif table_id == "tbl_entry":
            records = self.entries
        else:
            self.fail("不应搜索表 %s" % table_id)
        return [
            copy.deepcopy(item)
            for item in records[offset : offset + limit]
        ]

    def _update(self, app_token, table_id, updates):
        self.calls.append(("update", table_id, copy.deepcopy(updates)))
        if self.fail_update and table_id != "tbl_entry":
            raise lark.LarkError("模拟余额更新失败")
        if table_id == "tbl_account":
            records = self.accounts
        elif table_id == "tbl_holding":
            records = self.holdings
        elif table_id == "tbl_entry":
            records = self.entries
        else:
            self.fail("更新了未预期的表 %s" % table_id)
        for record_id, fields in updates.items():
            for item in records:
                if item["record_id"] == record_id:
                    item["fields"].update(copy.deepcopy(fields))
                    break
            else:
                self.fail("更新了不存在的记录 %s" % record_id)

    def _delete(self, app_token, table_id, record_ids):
        self.calls.append(("delete", table_id, tuple(record_ids)))
        self.entries = [
            item for item in self.entries if item["record_id"] not in record_ids
        ]

    # -- 便捷断言 ---------------------------------------------------------

    def record(self, records, record_id):
        for item in records:
            if item["record_id"] == record_id:
                return item
        self.fail("没有记录 %s" % record_id)

    def balance(self, record_id):
        return self.record(self.accounts, record_id)["fields"]["当前余额"]

    def updates_to(self, table_id):
        return [
            call[2]
            for call in self.calls
            if call[0] == "update" and call[1] == table_id
        ]

    def update_fields(self, table_id, record_id):
        for update in self.updates_to(table_id):
            if record_id in update:
                return update[record_id]
        self.fail("没有更新 %s %s" % (table_id, record_id))

    # -- 场景 -------------------------------------------------------------

    def simple_scenario(self, cash_current=1165):
        """期初 1000 + 收入 200 − 支出 35 = 1165。"""
        self.accounts = [
            account_record("a_cash", "现金", "现金", 1000, cash_current),
        ]
        self.entries = [
            entry_row(
                "e_income", "收入", 200, "a_cash",
                category="c_salary", created=100,
            ),
            entry_row(
                "e_expense", "支出", 35, "a_cash",
                category="c_food", created=200,
            ),
        ]

    def invest_scenario(self):
        """期初 1000 股 @10，买入 500 股 @12。"""
        self.accounts = [
            account_record("a_bank", "招行工资卡", "银行卡", 100000, 94000),
            account_record("a_invest", "华泰证券", "理财", 50000, 50000),
        ]
        self.holdings = [
            holding_record("h1", "贵州茅台", 1000, 10, shares=1500, cost=10.6667)
        ]
        tag = " #G0000abcd"
        self.entries = [
            entry_row(
                "e_transfer", "转账", 6000, "a_bank", to="a_invest",
                note=tag, created=100,
            ),
            entry_row(
                "e_trade", "转账", 6000, "a_invest", to="a_invest",
                holding="h1", price=12, shares=500,
                note=tag, created=101,
            ),
        ]

    def loan_scenario(self):
        """房贷组：利息 100 + 本金 900，同一 #G tag。"""
        self.accounts = [
            account_record("a_bank", "招行工资卡", "银行卡", 10000, 9000),
            account_record(
                "a_loan", "房贷", "负债", -500000, -499100, rate=0.042
            ),
        ]
        tag = " #G0000f00d"
        self.entries = [
            entry_row(
                "e_int", "支出", 100, "a_bank",
                category="c_food", note="本金900.00+利息100.00" + tag,
                created=100,
            ),
            entry_row(
                "e_prin", "转账", 900, "a_bank", to="a_loan",
                note="本金900.00+利息100.00" + tag, created=101,
            ),
        ]

    def sell_scenario(self):
        """清仓卖出 1000 股 @12：三条明细同组（转账 + 已实现盈亏 + 自转）。"""
        self.accounts = [
            account_record("a_bank", "招行工资卡", "银行卡", 100000, 112000),
            # 理财账户「当前余额」是闲置现金，卖出不改变它。
            account_record("a_invest", "华泰证券", "理财", 50000, 50000),
        ]
        self.holdings = [
            holding_record(
                "h1", "贵州茅台", 1000, 10, shares=0, cost=10,
                status="已清仓",
            )
        ]
        tag = " #G0000c0de"
        self.entries = [
            entry_row(
                "e_transfer", "转账", 12000, "a_invest", to="a_bank",
                note=tag, created=100,
            ),
            entry_row(
                "e_pnl", "收入", 2000, "a_invest",
                category="c_sell", note=tag, created=101,
            ),
            entry_row(
                "e_trade", "转账", 12000, "a_invest", to="a_invest",
                holding="h1", price=12, shares=1000,
                note=tag, created=102,
            ),
        ]


class RebuildTest(RebuildFlowTestBase):
    def test_rebuild_after_external_edit(self):
        # 用户在手机端把当前余额手改成 9999，流水应为 1165。
        self.simple_scenario(cash_current=9999)

        result = rebuild_flow.run_rebuild(self.cfg, apply=False)

        item = diff_item(result["diff"], "当前余额", "现金")
        self.assertIsNotNone(item)
        self.assertEqual(item["现值"], 9999.0)
        self.assertEqual(item["应为"], 1165.0)
        self.assertFalse(result["applied"])
        self.assertEqual(self.updates_to("tbl_account"), [])

    def test_rebuild_apply_writes_1165(self):
        self.simple_scenario(cash_current=9999)

        result = rebuild_flow.run_rebuild(self.cfg, apply=True)

        self.assertTrue(result["applied"])
        self.assertEqual(
            self.update_fields("tbl_account", "a_cash")["当前余额"], 1165.0
        )
        self.assertEqual(self.balance("a_cash"), 1165.0)

    def test_rebuild_detects_deleted_row(self):
        # 外部删掉支出 35：当前 1165，流水应为 1200，差额 +35。
        self.simple_scenario(cash_current=1165)
        self.entries = [row for row in self.entries if row["record_id"] != "e_expense"]

        result = rebuild_flow.run_rebuild(self.cfg, apply=False)

        item = diff_item(result["diff"], "当前余额", "现金")
        self.assertIsNotNone(item)
        self.assertEqual(item["现值"], 1165.0)
        self.assertEqual(item["应为"], 1200.0)
        self.assertAlmostEqual(item["应为"] - item["现值"], 35.0, places=2)

    def test_rebuild_dry_run_no_writes(self):
        self.simple_scenario(cash_current=9999)

        rebuild_flow.run_rebuild(self.cfg, apply=False)

        self.assertEqual(
            [call for call in self.calls if call[0] in ("update", "delete")], []
        )

    def test_rebuild_consistent_has_empty_diff_and_no_writes(self):
        self.simple_scenario(cash_current=1165)

        result = rebuild_flow.run_rebuild(self.cfg, apply=True)

        self.assertEqual(result["diff"], [])
        self.assertFalse(result["applied"])
        self.assertEqual(self.calls, [])

    def test_rebuild_holding_diff_and_apply(self):
        self.invest_scenario()
        # 持仓份额被外部改坏。
        self.record(self.holdings, "h1")["fields"]["当前份额"] = 999.0

        dry = rebuild_flow.run_rebuild(self.cfg, apply=False)
        item = diff_item(dry["diff"], "当前份额", "贵州茅台")
        self.assertIsNotNone(item)
        self.assertEqual(item["现值"], 999.0)
        self.assertEqual(item["应为"], 1500.0)

        rebuild_flow.run_rebuild(self.cfg, apply=True)
        fields = self.update_fields("tbl_holding", "h1")
        self.assertEqual(fields["当前份额"], 1500.0)
        self.assertEqual(fields["成本价"], 10.6667)

    def test_rebuild_holding_cost_diff(self):
        self.invest_scenario()
        self.record(self.holdings, "h1")["fields"]["成本价"] = 11.0

        result = rebuild_flow.run_rebuild(self.cfg, apply=False)

        item = diff_item(result["diff"], "成本价", "贵州茅台")
        self.assertIsNotNone(item)
        self.assertEqual(item["现值"], 11.0)
        self.assertEqual(item["应为"], 10.6667)

    def test_rebuild_writes_only_share_cost_status_fields(self):
        self.invest_scenario()
        self.record(self.holdings, "h1")["fields"]["当前份额"] = 999.0
        self.record(self.holdings, "h1")["fields"]["状态"] = ["已清仓"]
        self.record(self.accounts, "a_bank")["fields"]["当前余额"] = 1.0

        rebuild_flow.run_rebuild(self.cfg, apply=True)

        allowed = {"当前余额", "当前份额", "成本价", "状态"}
        holding_update = self.update_fields("tbl_holding", "h1")
        self.assertEqual(set(holding_update), allowed - {"当前余额"})
        self.assertEqual(holding_update["状态"], ["持有"])
        for update in self.updates_to("tbl_account"):
            self.assertLessEqual(set(update["a_bank"]), allowed)
        # 公式字段永不写入。
        for update in self.updates_to("tbl_holding"):
            for forbidden in ("市值", "浮动盈亏"):
                self.assertNotIn(forbidden, update["h1"])

    def test_rebuild_status_diff_and_apply(self):
        # 外部把状态改成已清仓：份额仍是 1500，rebuild 应改回 持有。
        self.invest_scenario()
        self.record(self.holdings, "h1")["fields"]["状态"] = ["已清仓"]

        dry = rebuild_flow.run_rebuild(self.cfg, apply=False)
        item = diff_item(dry["diff"], "状态", "贵州茅台")
        self.assertIsNotNone(item)
        self.assertEqual(item["现值"], "已清仓")
        self.assertEqual(item["应为"], "持有")

        rebuild_flow.run_rebuild(self.cfg, apply=True)
        self.assertEqual(
            self.record(self.holdings, "h1")["fields"]["状态"], ["持有"]
        )

    def test_rebuild_derives_cleared_status_from_zero_shares(self):
        # 期初 0 股且无交易：重放后份额为 0，状态必须是已清仓。
        self.accounts = [
            account_record("a_invest", "华泰证券", "理财", 50000, 50000)
        ]
        self.holdings = [
            holding_record(
                "h1", "贵州茅台", 0, 0, shares=0, cost=0, status="持有"
            )
        ]
        self.entries = []

        dry = rebuild_flow.run_rebuild(self.cfg, apply=False)

        item = diff_item(dry["diff"], "状态", "贵州茅台")
        self.assertIsNotNone(item)
        self.assertEqual(item["现值"], "持有")
        self.assertEqual(item["应为"], "已清仓")

    def test_rebuild_missing_account_does_not_crash(self):
        self.simple_scenario(cash_current=9999)
        self.entries.append(
            entry_row("e_ghost", "支出", 10, "a_missing", created=300)
        )

        result = rebuild_flow.run_rebuild(self.cfg, apply=False)

        # 其它账户照常重放，缺失对象只报诊断，不让整次 rebuild 崩掉。
        self.assertEqual(
            diff_item(result["diff"], "当前余额", "现金")["应为"], 1165.0
        )
        self.assertTrue(result["diagnostics"])
        self.assertTrue(result["tainted"])
        self.assertIn("a_missing", " ".join(result["diagnostics"]))

    def test_rebuild_apply_refuses_when_direction_row_missing(self):
        self.invest_scenario()
        self.entries = [
            row for row in self.entries if row["record_id"] != "e_transfer"
        ]

        with self.assertRaises(rebuild_flow.TaintedReplay) as caught:
            rebuild_flow.run_rebuild(self.cfg, apply=True)

        self.assertTrue(
            any(
                "买卖方向" in message
                for message in caught.exception.diagnostics
            )
        )
        self.assertEqual(
            [call for call in self.calls if call[0] in ("update", "delete")],
            [],
        )

    def test_rebuild_trade_row_missing_is_tainted_not_row_sum(self):
        # 方向行还在、持仓交易行被删：绝不能回退成行级求和（理财现金 +6000）。
        self.invest_scenario()
        self.entries = [
            row for row in self.entries if row["record_id"] != "e_trade"
        ]

        result = rebuild_flow.run_rebuild(self.cfg, apply=False)

        self.assertTrue(result["tainted"])
        self.assertIn(
            "疑似缺少持仓交易行", " ".join(result["diagnostics"])
        )
        self.assertIsNone(
            diff_item(result["diff"], "当前余额", "华泰证券")
        )

    def test_rebuild_apply_refuses_even_when_diff_is_empty(self):
        # 账实恰好一致也不能在不可信重放下写回（旧缺陷：打印“账实一致”并退出 0）。
        self.simple_scenario(cash_current=1165)
        self.entries.append(
            entry_row("e_ghost", "支出", 10, "a_missing", created=300)
        )

        with self.assertRaises(rebuild_flow.TaintedReplay):
            rebuild_flow.run_rebuild(self.cfg, apply=True)

        self.assertEqual(
            [call for call in self.calls if call[0] in ("update", "delete")],
            [],
        )

    def test_rebuild_apply_refuses_when_sell_trade_row_missing(self):
        # 复现 re-review 场景：当前值恰好是行级求和结果（理财 40000、银行
        # 112000、份额 1000），apply 绝不能把它们写回去。
        self.accounts = [
            account_record("a_bank", "招行工资卡", "银行卡", 100000, 112000),
            account_record("a_invest", "华泰证券", "理财", 50000, 40000),
        ]
        self.holdings = [
            holding_record(
                "h1", "贵州茅台", 1000, 10, shares=1000, cost=10
            )
        ]
        tag = " #G0000ca11"
        self.entries = [
            entry_row(
                "s1", "转账", 12000, "a_invest", to="a_bank",
                note=tag, created=100,
            ),
            entry_row(
                "s2", "收入", 2000, "a_invest", category="c_sell",
                note=tag, created=101,
            ),
        ]

        with self.assertRaises(rebuild_flow.TaintedReplay):
            rebuild_flow.run_rebuild(self.cfg, apply=True)

        self.assertEqual(
            [call for call in self.calls if call[0] in ("update", "delete")],
            [],
        )

    def test_rebuild_non_dict_fields_is_diagnosed(self):
        self.simple_scenario(cash_current=1165)
        self.entries.append(
            {"record_id": "e_bad", "fields": "not-a-dict"}
        )

        result = rebuild_flow.run_rebuild(self.cfg, apply=False)

        self.assertTrue(result["tainted"])
        self.assertIn("fields 不是对象", " ".join(result["diagnostics"]))
        with self.assertRaises(rebuild_flow.TaintedReplay):
            rebuild_flow.run_rebuild(self.cfg, apply=True)

    def test_rebuild_paginates_beyond_one_page(self):
        self.accounts = [
            account_record("a_cash", "现金", "现金", 1000, 1000)
        ]
        self.entries = [
            entry_row(
                "e%03d" % index, "支出", 1, "a_cash", created=1000 + index
            )
            for index in range(230)
        ]

        result = rebuild_flow.run_rebuild(self.cfg, apply=False)

        item = diff_item(result["diff"], "当前余额", "现金")
        self.assertIsNotNone(item)
        # 230 笔各 1 元；只读前 200 行会得到 800，读全才是 770。
        self.assertEqual(item["应为"], 770.0)
        self.assertGreaterEqual(self.searches.count("tbl_entry"), 2)


class DeleteTest(RebuildFlowTestBase):
    def test_delete_group_removes_all(self):
        self.loan_scenario()

        with captured_output() as (out, err):
            result = rebuild_flow.run_delete(self.cfg, "e_int", yes=True)

        self.assertIn("e_int", out.getvalue())

        self.assertEqual(result["deleted"], ["e_int", "e_prin"])
        deletes = [call for call in self.calls if call[0] == "delete"]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][1], "tbl_entry")
        self.assertEqual(set(deletes[0][2]), {"e_int", "e_prin"})
        self.assertEqual(self.entries, [])
        # 两条腿都反向：银行卡回到期初，负债回到期初。
        self.assertEqual(self.balance("a_bank"), 10000.0)
        self.assertEqual(self.balance("a_loan"), -500000.0)

    def test_delete_group_requires_yes(self):
        self.loan_scenario()

        with captured_output() as (out, err):
            with self.assertRaises(rebuild_flow.ConfirmationRequired) as caught:
                rebuild_flow.run_delete(self.cfg, "e_int", yes=False)

        self.assertIn("--yes", str(caught.exception))
        output = out.getvalue()
        self.assertIn("e_int", output)
        self.assertIn("e_prin", output)
        self.assertIn("900.00", output)
        self.assertEqual(
            [call for call in self.calls if call[0] in ("delete", "update")], []
        )
        self.assertEqual(len(self.entries), 2)

    def test_delete_single_no_tag(self):
        self.simple_scenario()
        self.entries = [
            row for row in self.entries if row["record_id"] == "e_expense"
        ]

        with captured_output():
            result = rebuild_flow.run_delete(
                self.cfg, "e_expense", yes=False
            )

        self.assertEqual(result["deleted"], ["e_expense"])
        self.assertEqual(self.entries, [])
        self.assertEqual(self.balance("a_cash"), 1000.0)

    def test_delete_sell_group_three_rows(self):
        self.sell_scenario()

        with captured_output():
            result = rebuild_flow.run_delete(self.cfg, "e_trade", yes=True)

        self.assertEqual(
            set(result["deleted"]), {"e_transfer", "e_pnl", "e_trade"}
        )
        self.assertEqual(self.entries, [])
        self.assertEqual(self.balance("a_bank"), 100000.0)
        self.assertEqual(self.balance("a_invest"), 50000.0)
        self.assertEqual(
            self.record(self.holdings, "h1")["fields"]["当前份额"], 1000.0
        )
        self.assertEqual(
            self.record(self.holdings, "h1")["fields"]["成本价"], 10.0
        )
        # 清仓状态必须随份额一起回滚，否则行情回补会跳过它。
        self.assertEqual(
            self.record(self.holdings, "h1")["fields"]["状态"], ["持有"]
        )
        holding_update = self.update_fields("tbl_holding", "h1")
        self.assertEqual(holding_update["状态"], ["持有"])
        self.assertEqual(holding_update["当前份额"], 1000.0)

    def test_delete_refuses_before_deleting_when_replay_tainted(self):
        self.loan_scenario()
        self.entries.append(
            entry_row("e_ghost", "支出", 10, "a_missing", created=300)
        )

        with captured_output():
            with self.assertRaises(rebuild_flow.TaintedReplay):
                rebuild_flow.run_delete(self.cfg, "e_int", yes=True)

        self.assertEqual(len(self.entries), 3)
        self.assertEqual(
            [call for call in self.calls if call[0] in ("delete", "update")],
            [],
        )

    def test_delete_update_failure_reports_partial(self):
        self.loan_scenario()
        self.fail_update = True

        with captured_output():
            with self.assertRaises(add_flow.PartialWriteError) as caught:
                rebuild_flow.run_delete(self.cfg, "e_int", yes=True)

        exc = caught.exception
        # 明细已经删掉，报告里必须明确写出这一步已完成，不能伪装成全失败。
        self.assertTrue(any("删除明细" in step for step in exc.completed))
        self.assertTrue(exc.unfinished)
        self.assertIn("更新「账户」", exc.failed_step)
        self.assertEqual(self.entries, [])

    def test_delete_unknown_record_raises(self):
        self.simple_scenario()

        with self.assertRaises(posting.PostingError):
            rebuild_flow.run_delete(self.cfg, "nope", yes=True)

        self.assertEqual(
            [call for call in self.calls if call[0] in ("delete", "update")], []
        )


class EditTest(RebuildFlowTestBase):
    def test_edit_amount_relinks_balance(self):
        self.simple_scenario()
        self.entries = [
            row for row in self.entries if row["record_id"] == "e_expense"
        ]

        result = rebuild_flow.run_edit(self.cfg, "e_expense", amount=40)

        self.assertEqual(
            self.record(self.entries, "e_expense")["fields"]["金额"], 40.0
        )
        self.assertEqual(self.update_fields("tbl_entry", "e_expense")["金额"], 40.0)
        self.assertEqual(self.balance("a_cash"), 960.0)
        self.assertEqual(result["updated"], 1)

    def test_edit_same_value_writes_nothing_to_accounts(self):
        self.simple_scenario()
        self.entries = [
            row for row in self.entries if row["record_id"] == "e_expense"
        ]
        # 只剩支出 35 时，期初 1000 − 35 = 965 才是当前一致值。
        self.record(self.accounts, "a_cash")["fields"]["当前余额"] = 965.0

        rebuild_flow.run_edit(self.cfg, "e_expense", amount=35)

        self.assertEqual(self.updates_to("tbl_account"), [])
        self.assertEqual(
            self.record(self.entries, "e_expense")["fields"]["金额"], 35.0
        )

    def test_edit_account_moves_balance(self):
        self.simple_scenario()
        self.entries = [
            row for row in self.entries if row["record_id"] == "e_expense"
        ]
        self.accounts.append(account_record("a_cash2", "备用金", "现金", 1000))

        rebuild_flow.run_edit(self.cfg, "e_expense", account="备用金")

        self.assertEqual(
            self.record(self.entries, "e_expense")["fields"]["账户"],
            [{"id": "a_cash2"}],
        )
        self.assertEqual(self.balance("a_cash"), 1000.0)
        self.assertEqual(self.balance("a_cash2"), 965.0)

    def test_edit_note_preserves_group_tag(self):
        self.loan_scenario()

        rebuild_flow.run_edit(self.cfg, "e_int", note="改后的备注")

        note = self.record(self.entries, "e_int")["fields"]["备注"]
        self.assertIn("改后的备注", note)
        self.assertIn("#G0000f00d", note)
        # 组身份没被破坏：再删整组仍然两条一起走。
        self.assertEqual(
            len(posting.group_rows(self.entries, self.record(self.entries, "e_int"))),
            2,
        )

    def test_edit_date_and_category(self):
        self.simple_scenario()
        self.entries = [
            row for row in self.entries if row["record_id"] == "e_expense"
        ]

        rebuild_flow.run_edit(
            self.cfg,
            "e_expense",
            date="2026-11-18",
            category="支出·餐饮·午餐",
        )

        fields = self.record(self.entries, "e_expense")["fields"]
        self.assertEqual(fields["日期"], "2026-11-18 00:00")
        self.assertEqual(fields["分类"], [{"id": "c_food"}])

    def test_edit_rejects_wrong_category_direction(self):
        self.simple_scenario()
        self.entries = [
            row for row in self.entries if row["record_id"] == "e_expense"
        ]

        with self.assertRaises(posting.PostingError):
            rebuild_flow.run_edit(self.cfg, "e_expense", category="收入·工资")

        self.assertEqual(
            [call for call in self.calls if call[0] == "update"], []
        )

    def test_edit_amount_matches_from_scratch_rebuild(self):
        self.simple_scenario()
        self.entries = [
            row for row in self.entries if row["record_id"] == "e_expense"
        ]
        self.record(self.accounts, "a_cash")["fields"]["当前余额"] = 965.0

        rebuild_flow.run_edit(self.cfg, "e_expense", amount=40)

        self.assertEqual(
            rebuild_flow.run_rebuild(self.cfg, apply=False)["diff"], []
        )

    def test_edit_account_matches_from_scratch_rebuild(self):
        self.simple_scenario()
        self.entries = [
            row for row in self.entries if row["record_id"] == "e_expense"
        ]
        self.record(self.accounts, "a_cash")["fields"]["当前余额"] = 965.0
        self.accounts.append(
            account_record("a_cash2", "备用金", "现金", 1000)
        )

        rebuild_flow.run_edit(self.cfg, "e_expense", account="备用金")

        self.assertEqual(
            rebuild_flow.run_rebuild(self.cfg, apply=False)["diff"], []
        )

    def test_edit_date_reorders_buys_and_matches_rebuild(self):
        # 期初 100@10；11-10 买 100@20；11-15 卖 100@15；11-20 买 100@10。
        # 把第二笔买入改到 11-12 会排到卖出之前，加权成本随之变化。
        self.accounts = [
            account_record("a_bank", "招行工资卡", "银行卡", 100000, 98500),
            account_record("a_invest", "华泰证券", "理财", 50000, 50000),
        ]
        self.holdings = [
            holding_record("h1", "贵州茅台", 100, 10, shares=200, cost=12.5)
        ]
        tag_a, tag_s, tag_b = (
            " #G0000000a",
            " #G0000000b",
            " #G0000000c",
        )
        self.entries = [
            entry_row(
                "a1", "转账", 2000, "a_bank", to="a_invest",
                note=tag_a, date="2026-11-10", created=100,
            ),
            entry_row(
                "a2", "转账", 2000, "a_invest", to="a_invest",
                holding="h1", price=20, shares=100,
                note=tag_a, date="2026-11-10", created=101,
            ),
            entry_row(
                "s1", "转账", 1500, "a_invest", to="a_bank",
                note=tag_s, date="2026-11-15", created=200,
            ),
            entry_row(
                "s2", "收入", 500, "a_invest", category="c_sell",
                note=tag_s, date="2026-11-15", created=201,
            ),
            entry_row(
                "s3", "转账", 1500, "a_invest", to="a_invest",
                holding="h1", price=15, shares=100,
                note=tag_s, date="2026-11-15", created=202,
            ),
            entry_row(
                "b1", "转账", 1000, "a_bank", to="a_invest",
                note=tag_b, date="2026-11-20", created=300,
            ),
            entry_row(
                "b2", "转账", 1000, "a_invest", to="a_invest",
                holding="h1", price=10, shares=100,
                note=tag_b, date="2026-11-20", created=301,
            ),
        ]
        # 先确认手搭的场景与 rebuild 一致。
        self.assertEqual(
            rebuild_flow.run_rebuild(self.cfg, apply=False)["diff"], []
        )

        rebuild_flow.run_edit(self.cfg, "b2", date="2026-11-12")

        holding = self.record(self.holdings, "h1")["fields"]
        self.assertEqual(holding["当前份额"], 200.0)
        self.assertAlmostEqual(holding["成本价"], 13.3333, places=4)
        self.assertEqual(
            rebuild_flow.run_rebuild(self.cfg, apply=False)["diff"], []
        )

    def test_edit_refuses_before_writing_when_replay_tainted(self):
        self.simple_scenario(cash_current=1165)
        self.entries.append(
            entry_row("e_ghost", "支出", 10, "a_missing", created=300)
        )

        with self.assertRaises(rebuild_flow.TaintedReplay):
            rebuild_flow.run_edit(self.cfg, "e_expense", amount=40)

        # 投影重放不可信：明细行与余额都没有写。
        self.assertEqual(
            self.record(self.entries, "e_expense")["fields"]["金额"], 35.0
        )
        self.assertEqual(
            [call for call in self.calls if call[0] == "update"], []
        )

    def test_edit_requires_at_least_one_field(self):
        self.simple_scenario()

        with self.assertRaises(posting.PostingError):
            rebuild_flow.run_edit(self.cfg, "e_expense")

        self.assertEqual(self.calls, [])

    def test_edit_unknown_record_raises(self):
        self.simple_scenario()

        with self.assertRaises(posting.PostingError):
            rebuild_flow.run_edit(self.cfg, "nope", amount=1)

        self.assertEqual(self.calls, [])

    def test_edit_update_failure_reports_partial(self):
        self.simple_scenario()
        self.fail_update = True

        with self.assertRaises(add_flow.PartialWriteError) as caught:
            rebuild_flow.run_edit(self.cfg, "e_expense", amount=40)

        exc = caught.exception
        # 明细行已改成功，报告必须写明这一步，不能伪装成全失败。
        self.assertTrue(any("更新明细" in step for step in exc.completed))
        self.assertEqual(
            self.record(self.entries, "e_expense")["fields"]["金额"], 40.0
        )

    def test_edit_invalid_amount_raises(self):
        self.simple_scenario()

        with self.assertRaises(posting.PostingError):
            rebuild_flow.run_edit(self.cfg, "e_expense", amount=0)

        self.assertEqual(
            [call for call in self.calls if call[0] == "update"], []
        )


# ---------------------------------------------------------------------------
# CLI 接线
# ---------------------------------------------------------------------------


class LedgerCliRebuildTest(RebuildFlowTestBase):
    def test_cli_rebuild_dry_run(self):
        self.simple_scenario(cash_current=9999)

        with captured_output() as (out, err):
            code = ledger.main(["rebuild"])

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        output = out.getvalue()
        self.assertIn("当前余额", output)
        self.assertIn("9999", output)
        self.assertIn("1165", output)
        self.assertIn("dry-run", output)
        self.assertIn("--apply", output)
        self.assertEqual(self.updates_to("tbl_account"), [])

    def test_cli_rebuild_apply(self):
        self.simple_scenario(cash_current=9999)

        with captured_output() as (out, err):
            code = ledger.main(["rebuild", "--apply"])

        self.assertEqual(code, 0)
        self.assertIn("已修正", out.getvalue())
        self.assertEqual(self.balance("a_cash"), 1165.0)

    def test_cli_rebuild_consistent(self):
        self.simple_scenario(cash_current=1165)

        with captured_output() as (out, err):
            code = ledger.main(["rebuild"])

        self.assertEqual(code, 0)
        self.assertIn("无需修正", out.getvalue())

    def test_cli_delete_group_without_yes_exit_2(self):
        self.loan_scenario()

        with captured_output() as (out, err):
            code = ledger.main(["delete", "e_int"])

        self.assertEqual(code, 2)
        self.assertIn("--yes", err.getvalue())
        self.assertEqual(
            [call for call in self.calls if call[0] in ("delete", "update")], []
        )
        self.assertEqual(len(self.entries), 2)

    def test_cli_delete_group_with_yes_exit_0(self):
        self.loan_scenario()

        with captured_output() as (out, err):
            code = ledger.main(["delete", "e_int", "--yes"])

        self.assertEqual(code, 0)
        self.assertEqual(self.entries, [])
        self.assertIn("已删除", out.getvalue())

    def test_cli_rebuild_tainted_dry_run_exit_0(self):
        self.invest_scenario()
        self.entries = [
            row for row in self.entries if row["record_id"] != "e_transfer"
        ]

        with captured_output() as (out, err):
            code = ledger.main(["rebuild"])

        self.assertEqual(code, 0)
        self.assertIn("诊断", out.getvalue())
        self.assertIn("重放不可信", out.getvalue())
        self.assertNotIn("账实一致", out.getvalue())
        self.assertEqual(
            [call for call in self.calls if call[0] in ("update", "delete")],
            [],
        )

    def test_cli_rebuild_tainted_apply_exit_5(self):
        self.invest_scenario()
        self.entries = [
            row for row in self.entries if row["record_id"] != "e_transfer"
        ]

        with captured_output() as (out, err):
            code = ledger.main(["rebuild", "--apply"])

        self.assertEqual(code, 5)
        self.assertIn("诊断", out.getvalue())
        self.assertIn("未写入任何数据", out.getvalue())
        self.assertEqual(
            [call for call in self.calls if call[0] in ("update", "delete")],
            [],
        )

    def test_cli_edit_tainted_exit_5(self):
        self.simple_scenario(cash_current=1165)
        self.entries.append(
            entry_row("e_ghost", "支出", 10, "a_missing", created=300)
        )

        with captured_output() as (out, err):
            code = ledger.main(["edit", "e_expense", "--amount", "40"])

        self.assertEqual(code, 5)
        self.assertIn("诊断", out.getvalue())
        self.assertIn("未写入任何数据", out.getvalue())
        self.assertEqual(
            self.record(self.entries, "e_expense")["fields"]["金额"], 35.0
        )
        self.assertEqual(
            [call for call in self.calls if call[0] == "update"], []
        )

    def test_cli_delete_tainted_exit_5(self):
        self.loan_scenario()
        self.entries.append(
            entry_row("e_ghost", "支出", 10, "a_missing", created=300)
        )

        with captured_output() as (out, err):
            code = ledger.main(["delete", "e_int", "--yes"])

        self.assertEqual(code, 5)
        self.assertIn("诊断", out.getvalue())
        self.assertIn("未写入任何数据", out.getvalue())
        self.assertEqual(len(self.entries), 3)
        self.assertEqual(
            [call for call in self.calls if call[0] in ("delete", "update")],
            [],
        )

    def test_cli_edit_amount(self):
        self.simple_scenario()
        self.entries = [
            row for row in self.entries if row["record_id"] == "e_expense"
        ]

        with captured_output() as (out, err):
            code = ledger.main(["edit", "e_expense", "--amount", "40"])

        self.assertEqual(code, 0)
        self.assertIn("已修改", out.getvalue())
        self.assertEqual(self.balance("a_cash"), 960.0)

    def test_cli_edit_unknown_exit_2(self):
        self.simple_scenario()

        with captured_output() as (out, err):
            code = ledger.main(["edit", "nope", "--amount", "1"])

        self.assertEqual(code, 2)
        self.assertIn("参数错误", err.getvalue())
        self.assertEqual(
            [call for call in self.calls if call[0] == "update"], []
        )


if __name__ == "__main__":
    unittest.main()