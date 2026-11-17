"""资产快照：净值口径、周触发、重复日期幂等与 CLI 接线。

全部 hermetic：mock 所有 lark 调用，config.CONFIG_PATH 指到临时文件，
时钟由测试注入（snapshot_flow._now），不读真实配置、不触网。
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
import snapshot_flow


UTC8 = datetime.timezone(datetime.timedelta(hours=8))
DATE = "2026-11-18"
SNAPSHOT_TABLE = "资产快照"


@contextlib.contextmanager
def captured_output():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def account(record_id, name, type_, balance, status="在用"):
    return {
        "record_id": record_id,
        "fields": {
            "账户名称": name,
            "类型": [type_],
            "初始余额": float(balance),
            "当前余额": float(balance),
            "状态": [status],
            "备注": "",
        },
    }


def holding(record_id, account_id, shares, latest, status="持有"):
    return {
        "record_id": record_id,
        "fields": {
            "持仓名称": "测试持仓",
            "代码": "sh600519",
            "品种": ["股票"],
            "理财账户": [{"id": account_id}],
            "当前份额": float(shares),
            "最新价": float(latest),
            "状态": [status],
        },
    }


def snapshot_row(record_id, key, date, account_id, value):
    return {
        "record_id": record_id,
        "fields": {
            "日期-账户": key,
            "日期": date,
            "账户": [{"id": account_id}],
            "余额/市值": float(value),
            "备注": "",
        },
    }


def rows_by_key(plan):
    return {fields["日期-账户"]: fields for _, fields in plan["create"]}


class PlanSnapshotTest(unittest.TestCase):
    """纯逻辑：每账户一行、净值口径、已存在键转 update。"""

    def accounts(self):
        return {
            "recW": account("recW", "华泰证券", "理财", 1000),
            "recB": account("recB", "招行工资卡", "银行卡", 5000),
            "recC": account("recC", "现金", "现金", 200),
            "recL": account("recL", "房贷", "负债", -300000),
        }

    def holdings(self):
        return {
            "recH1": holding("recH1", "recW", 1000, 14.57),
        }

    def test_wealth_value_includes_market_value(self):
        # 现金 1000 + 1000 股 × 14.57 = 15570.0：现金与市值都不能少算/多算。
        plan = snapshot_flow.plan_snapshot(
            self.accounts(), self.holdings(), DATE
        )
        rows = rows_by_key(plan)
        self.assertEqual(len(plan["create"]), 4)
        self.assertEqual(plan["update"], [])
        self.assertEqual(rows["%s_华泰证券" % DATE]["余额/市值"], 15570.0)
        # 非理财账户只按当前余额，不加任何持仓市值。
        self.assertEqual(rows["%s_招行工资卡" % DATE]["余额/市值"], 5000.0)
        self.assertEqual(rows["%s_现金" % DATE]["余额/市值"], 200.0)
        self.assertEqual(rows["%s_房贷" % DATE]["余额/市值"], -300000.0)
        # 聚合即净资产：15570 + 5000 + 200 - 300000，市值没有被重复计入。
        total = sum(fields["余额/市值"] for fields in rows.values())
        self.assertEqual(total, -279230.0)

    def test_wealth_row_fields(self):
        plan = snapshot_flow.plan_snapshot(
            self.accounts(), self.holdings(), DATE
        )
        fields = rows_by_key(plan)["%s_华泰证券" % DATE]
        self.assertEqual(fields["日期"], DATE)  # CellValue 补 00:00 由写入层负责
        self.assertEqual(fields["账户"], [{"id": "recW"}])
        self.assertEqual(fields["备注"], "")

    def test_liability_negative(self):
        plan = snapshot_flow.plan_snapshot(
            {"recL": account("recL", "车贷", "负债", -500000)},
            {},
            DATE,
        )
        rows = rows_by_key(plan)
        self.assertEqual(rows["%s_车贷" % DATE]["余额/市值"], -500000.0)

    def test_holdings_go_to_their_own_account(self):
        holdings = {
            "recH1": holding("recH1", "recW", 1000, 10.0),
            "recH2": holding("recH2", "recW2", 500, 2.0),
        }
        accounts = {
            "recW": account("recW", "华泰证券", "理财", 100),
            "recW2": account("recW2", "天天基金", "理财", 0),
        }
        rows = rows_by_key(
            snapshot_flow.plan_snapshot(accounts, holdings, DATE)
        )
        self.assertEqual(rows["%s_华泰证券" % DATE]["余额/市值"], 10100.0)
        self.assertEqual(rows["%s_天天基金" % DATE]["余额/市值"], 1000.0)

    def test_value_rounded_to_two(self):
        accounts = {"recW": account("recW", "理财账户", "理财", 0.005)}
        holdings = {"recH1": holding("recH1", "recW", 3, 1.005)}
        rows = rows_by_key(
            snapshot_flow.plan_snapshot(accounts, holdings, DATE)
        )
        self.assertEqual(rows["%s_理财账户" % DATE]["余额/市值"], 3.02)

    def test_closed_accounts_and_their_holdings_skipped(self):
        accounts = {
            "recW": account("recW", "在用理财", "理财", 100),
            "recX": account("recX", "已销户理财", "理财", 999, status="已销户"),
        }
        holdings = {
            "recH1": holding("recH1", "recW", 10, 2.0),
            "recH2": holding("recH2", "recX", 100, 5.0),
        }
        plan = snapshot_flow.plan_snapshot(accounts, holdings, DATE)
        rows = rows_by_key(plan)
        self.assertEqual(list(rows), ["%s_在用理财" % DATE])
        self.assertEqual(rows["%s_在用理财" % DATE]["余额/市值"], 120.0)

    def test_existing_key_updates(self):
        existing = [
            snapshot_row(
                "recS1", "%s_华泰证券" % DATE, DATE, "recW", 1.0
            )
        ]
        plan = snapshot_flow.plan_snapshot(
            self.accounts(), self.holdings(), DATE, rows=existing
        )
        # 只有华泰一行已存在：它转 update，其余 3 个账户仍 create。
        self.assertEqual(len(plan["create"]), 3)
        created_keys = {fields["日期-账户"] for _, fields in plan["create"]}
        self.assertNotIn("%s_华泰证券" % DATE, created_keys)
        self.assertEqual(len(plan["update"]), 1)
        table, record_id, fields = plan["update"][0]
        self.assertEqual(table, SNAPSHOT_TABLE)
        self.assertEqual(record_id, "recS1")
        self.assertEqual(fields["余额/市值"], 15570.0)
        self.assertEqual(fields["日期-账户"], "%s_华泰证券" % DATE)
        # 更新不碰备注，避免覆盖用户手写说明。
        self.assertNotIn("备注", fields)

    def test_duplicate_active_names_rejected(self):
        accounts = {
            "rec1": account("rec1", "同名", "现金", 1),
            "rec2": account("rec2", "同名", "现金", 2),
        }
        with self.assertRaises(snapshot_flow.SnapshotError):
            snapshot_flow.plan_snapshot(accounts, {}, DATE)

    def test_invalid_date_rejected(self):
        with self.assertRaises(snapshot_flow.SnapshotError):
            snapshot_flow.plan_snapshot({}, {}, "2026/11/18")


class DueWeeklyTest(unittest.TestCase):
    """新一周的首次记账回补上周日；本周已有快照则跳过。"""

    def test_due_weekly_new_week(self):
        # 2026-11-18 是 47 周周三；上周三是 11-11，应回补 11-15（周日）。
        result = snapshot_flow.due_weekly(
            datetime.date(2026, 11, 18), "2026-11-11"
        )
        self.assertEqual(result, "2026-11-15")

    def test_due_weekly_same_week_none(self):
        self.assertIsNone(
            snapshot_flow.due_weekly(
                datetime.date(2026, 11, 18), "2026-11-16"
            )
        )
        self.assertIsNone(
            snapshot_flow.due_weekly(
                datetime.date(2026, 11, 18), "2026-11-18"
            )
        )

    def test_due_weekly_no_previous_snapshot(self):
        self.assertEqual(
            snapshot_flow.due_weekly(datetime.date(2026, 11, 18), None),
            "2026-11-15",
        )

    def test_due_weekly_already_backfilled_sunday_not_due(self):
        # 周一首次 add 已补过 11-15；同周后续 add 不能反复重写。
        self.assertIsNone(
            snapshot_flow.due_weekly(
                datetime.date(2026, 11, 18), "2026-11-15"
            )
        )
        self.assertIsNone(
            snapshot_flow.due_weekly(
                datetime.date(2026, 11, 18), "2026-11-17"
            )
        )

    def test_due_weekly_monday(self):
        # 周一的上周日就是昨天；不能算成当天。
        self.assertEqual(
            snapshot_flow.due_weekly(datetime.date(2026, 11, 16), None),
            "2026-11-15",
        )

    def test_due_weekly_accepts_datetime_and_iso_text(self):
        self.assertEqual(
            snapshot_flow.due_weekly(
                datetime.datetime(2026, 11, 18, 16, 0, tzinfo=UTC8),
                "2026-11-11",
            ),
            "2026-11-15",
        )


class SnapshotFlowTest(unittest.TestCase):
    """I/O 编排：分页读取、落表、周触发与失败边界。"""

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
            "资产快照": {"table_id": "tbl_snapshot", "fields": {}},
        }
        self._save_cfg(self.cfg)

        self.accounts = [
            account("recW", "华泰证券", "理财", 1000),
            account("recB", "招行工资卡", "银行卡", 5000),
        ]
        self.holdings = [holding("recH1", "recW", 1000, 14.57)]
        self.snapshots = []

        self.search_calls = []
        self.created = []
        self.updated = []
        self._next_id = 0

        patch("lark.run_cli", side_effect=AssertionError("测试不应调用 lark-cli"))
        patch("lark.search_records", side_effect=self._search)
        patch("lark.create_records", side_effect=self._create)
        patch("lark.update_record", side_effect=self._update)

    def _stop_patches(self):
        for patcher in reversed(self._patches):
            patcher.stop()

    def _save_cfg(self, cfg):
        self.config_path.write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
        )

    def _search(self, app_token, table_id, conditions=None, limit=200, offset=0):
        self.search_calls.append((table_id, limit, offset))
        tables = {
            "tbl_account": self.accounts,
            "tbl_holding": self.holdings,
            "tbl_snapshot": self.snapshots,
        }
        rows = tables[table_id]
        return copy.deepcopy(rows[offset : offset + limit])

    def _create(self, app_token, table_id, records):
        self.created.append((table_id, copy.deepcopy(records)))
        ids = []
        for fields in records:
            self._next_id += 1
            record_id = "recS%d" % self._next_id
            self.snapshots.append(
                {"record_id": record_id, "fields": copy.deepcopy(fields)}
            )
            ids.append(record_id)
        return ids

    def _update(self, app_token, table_id, record_id, fields):
        self.updated.append((table_id, record_id, copy.deepcopy(fields)))
        for row in self.snapshots:
            if row["record_id"] == record_id:
                row["fields"].update(fields)

    def test_run_snapshot_writes_rows_and_returns_summary(self):
        result = snapshot_flow.run_snapshot(self.cfg, DATE)
        self.assertEqual(result, {"written": 2, "date": DATE})
        self.assertEqual([table for table, _ in self.created], ["tbl_snapshot"])
        self.assertEqual([table for table, _, _ in self.updated], [])
        rows = {
            fields["日期-账户"]: fields
            for _, batch in self.created
            for fields in batch
        }
        self.assertEqual(rows["%s_华泰证券" % DATE]["余额/市值"], 15570.0)
        # 日期写成飞书 datetime CellValue；关联字段用 [{"id": ...}]。
        self.assertEqual(rows["%s_华泰证券" % DATE]["日期"], DATE + " 00:00")
        self.assertEqual(rows["%s_华泰证券" % DATE]["账户"], [{"id": "recW"}])

    def test_run_snapshot_defaults_to_today_utc8(self):
        # 2026-11-17T16:30Z = 2026-11-18 00:30 UTC+8。
        with mock.patch(
            "snapshot_flow._now",
            return_value=datetime.datetime(
                2026, 11, 17, 16, 30, tzinfo=datetime.timezone.utc
            ),
        ):
            result = snapshot_flow.run_snapshot(self.cfg)
        self.assertEqual(result["date"], DATE)

    def test_repeated_snapshot_date_is_idempotent(self):
        snapshot_flow.run_snapshot(self.cfg, DATE)
        snapshot_flow.run_snapshot(self.cfg, DATE)
        self.assertEqual(len(self.snapshots), 2)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(len(self.updated), 2)
        keys = {row["fields"]["日期-账户"] for row in self.snapshots}
        self.assertEqual(len(keys), 2)

    def test_existing_row_updated_not_duplicated(self):
        existing = snapshot_row(
            "recOld", "%s_华泰证券" % DATE, DATE, "recW", 1.0
        )
        existing["fields"]["备注"] = "手工备注"
        self.snapshots.append(existing)
        result = snapshot_flow.run_snapshot(self.cfg, DATE)
        self.assertEqual(result["written"], 2)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(len([batch for _, batch in self.created]), 1)
        self.assertEqual(len(self.created[0][1]), 1)  # 只新建银行卡一行
        self.assertEqual(self.updated[0][1], "recOld")
        self.assertEqual(self.updated[0][2]["余额/市值"], 15570.0)
        self.assertNotIn("备注", self.updated[0][2])
        self.assertEqual(
            self.snapshots[0]["fields"]["备注"], "手工备注"
        )
        self.assertEqual(len(self.snapshots), 2)

    def test_only_active_accounts_written(self):
        self.accounts.append(
            account("recX", "已销户", "理财", 100, status="已销户")
        )
        snapshot_flow.run_snapshot(self.cfg, DATE)
        keys = {row["fields"]["日期-账户"] for row in self.snapshots}
        self.assertEqual(
            keys, {"%s_华泰证券" % DATE, "%s_招行工资卡" % DATE}
        )

    def test_search_paginates_until_short_page(self):
        self.accounts = [
            account("rec%03d" % index, "账户%03d" % index, "现金", index)
            for index in range(450)
        ]
        rows = snapshot_flow._search_all("app1", "tbl_account")
        self.assertEqual(len(rows), 450)
        self.assertEqual(
            [(limit, offset) for _, limit, offset in self.search_calls],
            [(200, 0), (200, 200), (200, 400)],
        )
        # 每页都是 200 条时不能提前收尾，也不能只读第一页。
        self.assertEqual(len({row["record_id"] for row in rows}), 450)

    def test_search_rejects_stalled_pagination(self):
        page = [
            account("rec%03d" % index, "账户%03d" % index, "现金", 1)
            for index in range(200)
        ]
        with mock.patch(
            "lark.search_records", side_effect=lambda *a, **k: copy.deepcopy(page)
        ):
            with self.assertRaises(lark.LarkError):
                snapshot_flow._search_all("app1", "tbl_account")

    def test_maybe_weekly_updates_config(self):
        self.cfg["last_snapshot_date"] = "2026-11-11"
        now = datetime.datetime(2026, 11, 18, 16, 0, tzinfo=UTC8)
        result = snapshot_flow.maybe_weekly(self.cfg, now)
        self.assertEqual(result["date"], "2026-11-15")
        self.assertEqual(self.cfg["last_snapshot_date"], "2026-11-15")
        persisted = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["last_snapshot_date"], "2026-11-15")
        keys = {row["fields"]["日期-账户"] for row in self.snapshots}
        self.assertEqual(
            keys, {"2026-11-15_华泰证券", "2026-11-15_招行工资卡"}
        )

    def test_maybe_weekly_second_call_same_week_skips(self):
        self.cfg["last_snapshot_date"] = "2026-11-11"
        now = datetime.datetime(2026, 11, 18, 16, 0, tzinfo=UTC8)
        first = snapshot_flow.maybe_weekly(self.cfg, now)
        self.assertEqual(first["date"], "2026-11-15")
        calls_after_first = len(self.search_calls)
        with captured_output():
            second = snapshot_flow.maybe_weekly(self.cfg, now)
        self.assertIsNone(second)
        self.assertEqual(len(self.search_calls), calls_after_first)
        self.assertEqual(len(self.created), 1)

    def test_maybe_weekly_same_week_skips(self):
        self.cfg["last_snapshot_date"] = "2026-11-16"
        now = datetime.datetime(2026, 11, 18, 16, 0, tzinfo=UTC8)
        self.assertIsNone(snapshot_flow.maybe_weekly(self.cfg, now))
        self.assertEqual(self.snapshots, [])
        self.assertEqual(self.search_calls, [])
        self.assertEqual(self.cfg["last_snapshot_date"], "2026-11-16")

    def test_maybe_weekly_swallows_errors_and_does_not_stamp(self):
        self.cfg["last_snapshot_date"] = "2026-11-11"
        self._save_cfg(self.cfg)
        now = datetime.datetime(2026, 11, 18, 16, 0, tzinfo=UTC8)
        with mock.patch(
            "lark.create_records",
            side_effect=lark.LarkError("飞书挂了"),
        ):
            with captured_output() as (out, err):
                result = snapshot_flow.maybe_weekly(self.cfg, now)
        self.assertEqual(result["errors"], [result["warning"]])
        self.assertIn("资产快照", result["warning"])
        self.assertIn("警告", err.getvalue())
        self.assertEqual(self.cfg["last_snapshot_date"], "2026-11-11")
        persisted = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["last_snapshot_date"], "2026-11-11")

    def test_maybe_weekly_save_failure_keeps_old_date(self):
        self.cfg["last_snapshot_date"] = "2026-11-11"
        self._save_cfg(self.cfg)
        now = datetime.datetime(2026, 11, 18, 16, 0, tzinfo=UTC8)
        with mock.patch(
            "config.save_config",
            side_effect=config.ConfigError("写不了配置"),
        ):
            with captured_output() as (out, err):
                result = snapshot_flow.maybe_weekly(self.cfg, now)
        self.assertIn("warning", result)
        self.assertEqual(self.cfg["last_snapshot_date"], "2026-11-11")

    def test_maybe_weekly_missing_table_is_warning_only(self):
        self.cfg["last_snapshot_date"] = "2026-11-11"
        self._save_cfg(self.cfg)
        del self.cfg["tables"]["资产快照"]
        now = datetime.datetime(2026, 11, 18, 16, 0, tzinfo=UTC8)
        with captured_output() as (out, err):
            result = snapshot_flow.maybe_weekly(self.cfg, now)
        self.assertIn("warning", result)
        self.assertEqual(self.cfg["last_snapshot_date"], "2026-11-11")

    def test_run_snapshot_missing_table_raises_config_error(self):
        del self.cfg["tables"]["资产快照"]
        with self.assertRaises(config.ConfigError):
            snapshot_flow.run_snapshot(self.cfg, DATE)


class SnapshotCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = pathlib.Path(self.tmp.name) / "config.json"
        self.cfg = config.empty_config("app1", "nod1")
        self.cfg["tables"] = {
            "账户": {"table_id": "tbl_account", "fields": {}},
            "持仓": {"table_id": "tbl_holding", "fields": {}},
            "资产快照": {"table_id": "tbl_snapshot", "fields": {}},
        }
        self.config_path.write_text(
            json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8"
        )
        patcher = mock.patch("config.CONFIG_PATH", new=self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cli_snapshot_with_date(self):
        with mock.patch(
            "snapshot_flow.run_snapshot",
            return_value={"written": 3, "date": DATE},
        ) as run:
            with captured_output() as (out, err):
                code = ledger.main(["snapshot", "--date", DATE])
        self.assertEqual(code, 0)
        run.assert_called_once()
        self.assertEqual(run.call_args[0][1], DATE)
        self.assertIn(DATE, out.getvalue())
        self.assertIn("3", out.getvalue())

    def test_cli_snapshot_defaults_to_today(self):
        with mock.patch(
            "snapshot_flow.run_snapshot",
            return_value={"written": 0, "date": DATE},
        ) as run:
            with captured_output():
                code = ledger.main(["snapshot"])
        self.assertEqual(code, 0)
        self.assertIsNone(run.call_args[0][1])

    def test_cli_rejects_bad_date(self):
        with mock.patch("snapshot_flow.run_snapshot") as run:
            with captured_output() as (out, err):
                code = ledger.main(["snapshot", "--date", "2026/11/18"])
        self.assertEqual(code, 2)
        self.assertFalse(run.called)
        self.assertIn("YYYY-MM-DD", err.getvalue())

    def test_cli_maps_snapshot_error_to_data_exit(self):
        with mock.patch(
            "snapshot_flow.run_snapshot",
            side_effect=snapshot_flow.SnapshotError("账户缺当前余额"),
        ):
            with captured_output() as (out, err):
                code = ledger.main(["snapshot", "--date", DATE])
        self.assertEqual(code, 5)
        self.assertIn("账户缺当前余额", err.getvalue())

    def test_cli_maps_lark_error_to_lark_exit(self):
        with mock.patch(
            "snapshot_flow.run_snapshot",
            side_effect=lark.LarkError("record-batch-create 失败"),
        ):
            with captured_output() as (out, err):
                code = ledger.main(["snapshot", "--date", DATE])
        self.assertEqual(code, 4)
        self.assertIn("record-batch-create 失败", err.getvalue())

    def test_cli_missing_config_is_not_ready(self):
        self.config_path.unlink()
        with captured_output() as (out, err):
            code = ledger.main(["snapshot"])
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
