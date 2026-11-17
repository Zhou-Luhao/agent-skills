"""assets 资产与 init 流程：全部 mock lark 调用与 config 落盘，不触网、不碰真实配置。"""

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import config
import init_flow
import ledger
import lark


@contextlib.contextmanager
def captured_output():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


# spec「数据模型」逐表逐字段的（字段名, 类型），顺序即建表顺序与字段顺序。
EXPECTED_TABLES = [
    (
        "账户",
        [
            ("账户名称", "text"),
            ("类型", "select"),
            ("初始余额", "number"),
            ("当前余额", "number"),
            ("年利率", "number"),
            ("还款方式", "select"),
            ("状态", "select"),
            ("备注", "text"),
        ],
    ),
    (
        "分类",
        [
            ("分类", "text"),
            ("类型", "select"),
            ("一级", "text"),
            ("二级", "text"),
        ],
    ),
    (
        "持仓",
        [
            ("持仓名称", "text"),
            ("代码", "text"),
            ("品种", "select"),
            ("理财账户", "link"),
            ("当前份额", "number"),
            ("成本价", "number"),
            ("最新价", "number"),
            ("最新价日期", "datetime"),
            ("市值", "formula"),
            ("浮动盈亏", "formula"),
            ("状态", "select"),
            ("期初份额", "number"),
            ("期初成本", "number"),
        ],
    ),
    (
        "收支明细",
        [
            ("摘要", "text"),
            ("日期", "datetime"),
            ("类型", "select"),
            ("金额", "number"),
            ("账户", "link"),
            ("转入账户", "link"),
            ("分类", "link"),
            ("持仓", "link"),
            ("成交价", "number"),
            ("份额", "number"),
            ("备注", "text"),
            ("创建时间", "created_at"),
        ],
    ),
    (
        "行情快照",
        [
            ("代码-日期", "text"),
            ("代码", "text"),
            ("日期", "datetime"),
            ("收盘价/净值", "number"),
            ("状态", "select"),
            ("更新时间", "updated_at"),
        ],
    ),
    (
        "资产快照",
        [
            ("日期-账户", "text"),
            ("日期", "datetime"),
            ("账户", "link"),
            ("余额/市值", "number"),
            ("备注", "text"),
        ],
    ),
]

# spec 里的单选选项，逐字逐序。
EXPECTED_SELECT_OPTIONS = {
    ("账户", "类型"): ["现金", "微信支付宝", "银行卡", "理财", "负债"],
    ("账户", "还款方式"): ["等额本息", "等额本金", "随借随还"],
    ("账户", "状态"): ["在用", "已销户"],
    ("持仓", "品种"): ["股票", "基金"],
    ("持仓", "状态"): ["持有", "已清仓"],
    ("收支明细", "类型"): ["收入", "支出", "转账"],
    ("分类", "类型"): ["收入", "支出"],
    ("行情快照", "状态"): ["已落", "待回补", "无数据"],
}

# spec 里的关联方向，必须指向更早建的表。
EXPECTED_LINKS = {
    ("持仓", "理财账户"): "账户",
    ("收支明细", "账户"): "账户",
    ("收支明细", "转入账户"): "账户",
    ("收支明细", "分类"): "分类",
    ("收支明细", "持仓"): "持仓",
    ("资产快照", "账户"): "账户",
}

# 金额/价格精度与样式（spec + task brief）。
EXPECTED_NUMBER_STYLES = {
    ("账户", "初始余额"): {"type": "currency", "precision": 2, "currency_code": "CNY"},
    ("账户", "当前余额"): {"type": "currency", "precision": 2, "currency_code": "CNY"},
    ("账户", "年利率"): {"type": "plain", "percentage": True},
    ("持仓", "成本价"): {"type": "plain", "precision": 4},
    ("持仓", "最新价"): {"type": "plain", "precision": 4},
    ("持仓", "期初成本"): {"type": "plain", "precision": 4},
    ("收支明细", "金额"): {"type": "plain", "precision": 2},
    ("收支明细", "成交价"): {"type": "plain", "precision": 4},
    ("收支明细", "份额"): {"type": "plain", "precision": 4},
    ("行情快照", "收盘价/净值"): {"type": "plain", "precision": 4},
    ("资产快照", "余额/市值"): {"type": "plain", "precision": 2},
}

EXPECTED_FORMULAS = {
    ("持仓", "市值"): "[当前份额]*[最新价]",
    ("持仓", "浮动盈亏"): "([最新价]-[成本价])*[当前份额]",
}

EXPECTED_DATETIMES = [
    ("持仓", "最新价日期"),
    ("收支明细", "日期"),
    ("行情快照", "日期"),
    ("资产快照", "日期"),
]


def load_schema_asset():
    path = pathlib.Path(__file__).resolve().parents[2] / "assets" / "schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_categories_asset():
    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "assets"
        / "categories.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def full_name(entry):
    """分类全名：支出·餐饮·午餐；无二级时不留分隔符尾巴。"""
    parts = [entry.get("类型"), entry.get("一级"), entry.get("二级")]
    return "·".join(part for part in parts if part)


class SchemaAssetTest(unittest.TestCase):
    def setUp(self):
        self.schema = load_schema_asset()
        self.tables = self.schema["tables"]
        self.fields = {table["name"]: table["fields"] for table in self.tables}

    def test_table_order_and_names(self):
        self.assertEqual(
            [table["name"] for table in self.tables],
            [name for name, _ in EXPECTED_TABLES],
        )

    def test_every_field_has_spec_name_and_type_in_order(self):
        for table_name, expected_fields in EXPECTED_TABLES:
            actual = [
                (field["name"], field["type"])
                for field in self.fields[table_name]
            ]
            self.assertEqual(actual, expected_fields, table_name)

    def test_primary_field_is_first(self):
        # Base 建表时首字段即主字段，spec 的主字段都排第一。
        for table_name, expected_fields in EXPECTED_TABLES:
            self.assertEqual(
                self.fields[table_name][0]["name"], expected_fields[0][0], table_name
            )

    def test_select_options_exactly_match_spec(self):
        for (table_name, field_name), options in EXPECTED_SELECT_OPTIONS.items():
            field = self._field(table_name, field_name)
            self.assertEqual(field["type"], "select")
            self.assertEqual(
                [option["name"] for option in field["options"]],
                options,
                "%s.%s" % (table_name, field_name),
            )

    def test_no_extra_select_fields(self):
        actual = {
            (table_name, field["name"])
            for table_name, fields in self.fields.items()
            for field in fields
            if field["type"] == "select"
        }
        self.assertEqual(actual, set(EXPECTED_SELECT_OPTIONS))

    def test_links_point_to_earlier_tables(self):
        seen = []
        for table in self.tables:
            for field in table["fields"]:
                if field["type"] == "link":
                    self.assertIn(
                        field["link_table"],
                        seen,
                        "%s.%s 指向了未先建的表" % (table["name"], field["name"]),
                    )
                # 所有 link 都应在期望映射里，方向逐条核对
            seen.append(table["name"])
        for (table_name, field_name), target in EXPECTED_LINKS.items():
            self.assertEqual(
                self._field(table_name, field_name)["link_table"], target
            )

    def test_number_styles(self):
        for (table_name, field_name), style in EXPECTED_NUMBER_STYLES.items():
            field = self._field(table_name, field_name)
            self.assertEqual(field["type"], "number")
            self.assertEqual(
                field["style"], style, "%s.%s" % (table_name, field_name)
            )

    def test_annual_rate_description_documents_fraction_storage(self):
        # percentage=true 存的是小数：4.2% ⇒ 0.042，字段描述必须写清楚。
        field = self._field("账户", "年利率")
        self.assertEqual(
            field["description"],
            "小数存储：4.2% 记作 0.042（读取后 ×100 才是百分数）",
        )
        self.assertEqual(field["style"], {"type": "plain", "percentage": True})

    def test_conventions_block_pins_annual_rate_unit(self):
        # 顶层 conventions 只给脚本/文档看，不会进入 +table-create。
        self.assertEqual(
            self.schema["conventions"],
            {"账户.年利率": "fraction（0.042 表示 4.2%）"},
        )

    def test_formula_expressions(self):
        for (table_name, field_name), expression in EXPECTED_FORMULAS.items():
            field = self._field(table_name, field_name)
            self.assertEqual(field["type"], "formula")
            self.assertEqual(
                field["expression"],
                expression,
                "%s.%s" % (table_name, field_name),
            )

    def test_datetime_format(self):
        for table_name, field_name in EXPECTED_DATETIMES:
            field = self._field(table_name, field_name)
            self.assertEqual(field["type"], "datetime")
            self.assertEqual(field["style"], {"format": "yyyy-MM-dd"})

    def test_auto_time_fields_have_no_style(self):
        self.assertEqual(
            self._field("收支明细", "创建时间"), {"type": "created_at", "name": "创建时间"}
        )
        self.assertEqual(
            self._field("行情快照", "更新时间"), {"type": "updated_at", "name": "更新时间"}
        )

    def _field(self, table_name, field_name):
        for field in self.fields[table_name]:
            if field["name"] == field_name:
                return field
        self.fail("缺少字段 %s.%s" % (table_name, field_name))


class CategoriesAssetTest(unittest.TestCase):
    def setUp(self):
        self.categories = load_categories_asset()

    def test_every_entry_has_type_and_level_one(self):
        for entry in self.categories:
            self.assertEqual(set(entry), {"类型", "一级", "二级"})
            self.assertIn(entry["类型"], ("收入", "支出"))
            self.assertTrue(entry["一级"])
            self.assertIsInstance(entry["二级"], str)

    def test_expense_categories_verbatim(self):
        expense = [entry for entry in self.categories if entry["类型"] == "支出"]
        self.assertEqual(
            list(dict.fromkeys(entry["一级"] for entry in expense)),
            [
                "餐饮",
                "交通",
                "购物",
                "居住",
                "医疗",
                "娱乐",
                "人情",
                "金融",
                "教育",
                "其他",
            ],
        )
        self.assertEqual(
            [(entry["一级"], entry["二级"]) for entry in expense],
            [
                ("餐饮", "早餐"),
                ("餐饮", "午餐"),
                ("餐饮", "晚餐"),
                ("餐饮", "奶茶咖啡"),
                ("餐饮", "零食水果"),
                ("餐饮", "聚餐"),
                ("交通", "公共交通"),
                ("交通", "打车"),
                ("交通", "加油"),
                ("交通", "停车"),
                ("购物", "服饰"),
                ("购物", "数码"),
                ("购物", "日用百货"),
                ("购物", "美妆"),
                ("居住", "房租"),
                ("居住", "水电燃气"),
                ("居住", "物业"),
                ("居住", "维修"),
                ("医疗", "门诊"),
                ("医疗", "药品"),
                ("医疗", "体检"),
                ("娱乐", "电影"),
                ("娱乐", "游戏"),
                ("娱乐", "旅行"),
                ("娱乐", "订阅会员"),
                ("人情", "红包"),
                ("人情", "礼物"),
                ("人情", "请客"),
                ("金融", "利息支出"),
                ("金融", "手续费"),
                ("教育", "书籍"),
                ("教育", "课程"),
                ("其他", ""),
            ],
        )

    def test_income_categories_verbatim(self):
        income = [entry for entry in self.categories if entry["类型"] == "收入"]
        self.assertEqual(
            [(entry["一级"], entry["二级"]) for entry in income],
            [
                ("固定收入", "工资"),
                ("固定收入", "奖金"),
                ("固定收入", "补贴"),
                ("理财收益", "分红"),
                ("理财收益", "利息"),
                ("理财收益", "基金收益"),
                ("理财收益", "股票收益"),
                ("报销", ""),
                ("红包", ""),
                ("其他", ""),
            ],
        )

    def test_total_count(self):
        self.assertEqual(len(self.categories), 43)


class InitTestBase(unittest.TestCase):
    """把 lark 全部函数与 config 读写都换成 mock，任何测试都不触网。"""

    def setUp(self):
        self.created_tables = []
        self._patches = []
        # 保存未被 mock 的 load_config，供“配置文件存在但内容非法”的用例直读临时文件。
        self._real_load_config = config.load_config

        def patch(target, **kwargs):
            patcher = mock.patch(target, **kwargs)
            self._patches.append(patcher)
            return patcher.start()

        self.addCleanup(self._stop_patches)

        # 绝不读写真实 ~/.lark-ledger/config.json
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = pathlib.Path(self.tmp.name) / "config.json"
        patch("config.CONFIG_PATH", new=self.config_path)

        self.auth_ok = patch("init_flow.lark.auth_ok", return_value=True)
        self.load_config = patch(
            "init_flow.config.load_config",
            side_effect=config.ConfigError("还没有配置"),
        )
        self.save_config = patch("init_flow.config.save_config")
        self.sleep = patch("time.sleep")
        self.create_base_library = patch(
            "init_flow.lark.create_base_library",
            return_value={"app_token": "app1", "node_token": "nod1"},
        )
        self.create_base_folder = patch(
            "init_flow.lark.create_base_folder", return_value="app2"
        )
        self.create_table = patch(
            "init_flow.lark.create_table", side_effect=self._create_table
        )
        self.delete_table = patch("init_flow.lark.delete_table")
        self.list_tables = patch("init_flow.lark.list_tables", return_value=[])
        self.list_fields = patch(
            "init_flow.lark.list_fields", side_effect=self._list_fields
        )
        self.create_records = patch(
            "init_flow.lark.create_records", return_value=["rec1"]
        )

    def _stop_patches(self):
        for patcher in reversed(self._patches):
            patcher.stop()

    def _schema_table(self, name):
        for table in init_flow.load_schema()["tables"]:
            if table["name"] == name:
                return table
        self.fail("schema 里没有表 %s" % name)

    def _fields_for(self, table_id, skip=()):
        name = table_id[len("tbl_"):]
        return [
            {"field_id": "fld_%s_%s" % (name, field["name"]), "name": field["name"]}
            for field in self._schema_table(name)["fields"]
            if field["name"] not in skip
        ]

    def _create_table(self, app_token, name, fields):
        self.created_tables.append({"name": name, "fields": fields})
        return "tbl_" + name

    def _list_fields(self, app_token, table_id):
        return self._fields_for(table_id)

    def _existing_config(self):
        cfg = config.empty_config("app1", "nod1")
        for table in init_flow.load_schema()["tables"]:
            name = table["name"]
            cfg["tables"][name] = {
                "table_id": "tbl_" + name,
                "fields": {
                    field["name"]: "fld_%s_%s" % (name, field["name"])
                    for field in table["fields"]
                },
            }
        return cfg

    def _use_existing_config(self, cfg):
        # 幂等分支以配置文件存在为前提，落一份真实文件到临时路径。
        self.config_path.write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
        )
        self.load_config.side_effect = None
        self.load_config.return_value = cfg
        self.list_tables.return_value = [
            {"table_id": entry["table_id"], "name": name}
            for name, entry in cfg["tables"].items()
        ]


class InitFlowTest(InitTestBase):
    def test_creates_base_in_library_by_default(self):
        cfg = init_flow.run_init(None)

        self.create_base_library.assert_called_once_with("个人账本")
        self.assertFalse(self.create_base_folder.called)
        self.assertEqual(cfg["app_token"], "app1")
        self.assertEqual(cfg["node_token"], "nod1")

    def test_base_name_is_configurable(self):
        init_flow.run_init(None, base_name="家庭账本")

        self.create_base_library.assert_called_once_with("家庭账本")

    def test_folder_token_uses_drive(self):
        cfg = init_flow.run_init("fld1")

        self.create_base_folder.assert_called_once_with("个人账本", "fld1")
        self.assertFalse(self.create_base_library.called)
        self.assertEqual(cfg["app_token"], "app2")
        self.assertIsNone(cfg["node_token"])

    def test_tables_created_in_schema_order(self):
        init_flow.run_init(None)

        schema = init_flow.load_schema()["tables"]
        self.assertEqual(
            [call.args[1] for call in self.create_table.call_args_list],
            [table["name"] for table in schema],
        )
        for call, table in zip(
            self.create_table.call_args_list, schema
        ):
            self.assertEqual(call.args[0], "app1")
            self.assertEqual(call.args[2], table["fields"])

    def test_removes_platform_default_table_before_schema(self):
        # base/wiki 创建 bitable 时会附带一张平台默认表；fresh init 必须删掉。
        self.list_tables.return_value = [
            {"table_id": "tbl_default", "name": "数据表"}
        ]

        init_flow.run_init(None)

        self.delete_table.assert_called_once_with("app1", "tbl_default")
        self.assertEqual(
            len(self.create_table.call_args_list),
            len(init_flow.load_schema()["tables"]),
        )

    def test_folder_init_removes_platform_default_table_too(self):
        self.create_base_folder.return_value = "app2"
        self.list_tables.return_value = [
            {"table_id": "tbl_default", "name": "数据表"}
        ]

        init_flow.run_init("fld1")

        self.delete_table.assert_called_once_with("app2", "tbl_default")
        self.assertEqual(len(self.create_table.call_args_list), 6)

    def test_categories_written_with_full_name(self):
        init_flow.run_init(None)

        self.create_records.assert_called_once()
        call = self.create_records.call_args
        self.assertEqual(call.args[0], "app1")
        self.assertEqual(call.args[1], "tbl_分类")
        records = call.args[2]

        categories = init_flow.load_categories()
        self.assertEqual(len(records), len(categories))
        self.assertEqual(
            {record["分类"] for record in records},
            {full_name(entry) for entry in categories},
        )
        self.assertIn("支出·餐饮·午餐", {record["分类"] for record in records})
        self.assertIn("收入·理财收益·基金收益", {record["分类"] for record in records})
        for record in records:
            self.assertEqual(record["类型"], [record["分类"].split("·")[0]])
            self.assertEqual(
                record["分类"],
                "·".join(
                    part
                    for part in (
                        record["类型"][0],
                        record["一级"],
                        record["二级"],
                    )
                    if part
                ),
            )

    def test_created_field_without_id_raises_initerror(self):
        # 建表返回的字段若没有 field_id，不能把一份残缺配置落盘。
        def fields_without_id(app_token, table_id):
            return [
                {
                    "name": field["name"],
                    "field_id": None
                    if (table_id == "tbl_账户" and field["name"] == "账户名称")
                    else "fld_x",
                }
                for field in self._schema_table(table_id[len("tbl_"):])["fields"]
            ]

        self.list_fields.side_effect = fields_without_id

        with self.assertRaises(init_flow.InitError) as ctx:
            init_flow.run_init(None)

        self.assertIn("账户名称", str(ctx.exception))
        self.save_config.assert_not_called()

    def test_schema_without_category_table_raises_initerror(self):
        # 资产被改坏时不能用 KeyError 崩掉，应报数据错误。
        schema_path = pathlib.Path(self.tmp.name) / "schema.json"
        schema_path.write_text(
            json.dumps(
                {
                    "tables": [
                        {
                            "name": "账户",
                            "fields": [{"type": "text", "name": "账户名称"}],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with mock.patch("init_flow.SCHEMA_PATH", schema_path):
            with self.assertRaises(init_flow.InitError) as ctx:
                init_flow.run_init(None)

        self.assertIn("分类", str(ctx.exception))
        self.save_config.assert_not_called()

    def test_field_read_retry_recovers_after_async_delay(self):
        # Base 的表/字段写入异步生效：第一遍读不到 市值，重试后应成功。
        attempts = {"count": 0}

        def flaky(app_token, table_id):
            items = self._fields_for(table_id)
            if table_id == "tbl_持仓":
                attempts["count"] += 1
                if attempts["count"] == 1:
                    return [item for item in items if item["name"] != "市值"]
            return items

        self.list_fields.side_effect = flaky

        cfg = init_flow.run_init(None)

        self.assertEqual(
            cfg["tables"]["持仓"]["fields"]["市值"], "fld_持仓_市值"
        )
        self.assertEqual(self.sleep.call_count, 1)

    def test_field_read_missing_after_retries_names_base_and_tables(self):
        self.list_fields.side_effect = lambda app_token, table_id: self._fields_for(
            table_id, skip=("年利率",) if table_id == "tbl_账户" else ()
        )

        with self.assertRaises(init_flow.InitError) as ctx:
            init_flow.run_init(None)

        message = str(ctx.exception)
        self.assertIn("年利率", message)
        self.assertIn("重试 3 次", message)
        self.assertIn("app_token=app1", message)
        self.assertIn("已建表 账户", message)
        self.assertEqual(self.sleep.call_count, 2)
        self.save_config.assert_not_called()

    def test_mid_init_failure_reports_created_base(self):
        self.create_records.side_effect = lark.LarkError("写分类失败", {"code": 1})

        with self.assertRaises(lark.LarkError) as ctx:
            init_flow.run_init(None)

        message = str(ctx.exception)
        self.assertIn("写分类失败", message)
        self.assertIn("app_token=app1", message)
        self.assertIn("node_token=nod1", message)
        self.assertIn(
            "已建表 账户、分类、持仓、收支明细、行情快照、资产快照", message
        )
        self.assertEqual(ctx.exception.payload, {"code": 1})
        self.save_config.assert_not_called()

    def test_base_creation_failure_is_wrapped_with_partial_note(self):
        # Base 创建也在 try 内：拿不到 token 时也要交代可能已留下空 Base。
        self.create_base_library.side_effect = lark.LarkError(
            "建 Base 失败", {"code": 2}
        )

        with self.assertRaises(lark.LarkError) as ctx:
            init_flow.run_init(None)

        message = str(ctx.exception)
        self.assertIn("建 Base 失败", message)
        self.assertIn("未拿到 app_token", message)
        self.assertEqual(ctx.exception.payload, {"code": 2})
        self.save_config.assert_not_called()

    def test_create_table_failure_names_only_created_tables(self):
        def fail_on_holding(app_token, name, fields):
            self.created_tables.append({"name": name, "fields": fields})
            if name == "持仓":
                raise lark.LarkError("建表失败")
            return "tbl_" + name

        self.create_table.side_effect = fail_on_holding

        with self.assertRaises(lark.LarkError) as ctx:
            init_flow.run_init(None)

        message = str(ctx.exception)
        self.assertIn("app_token=app1", message)
        self.assertIn("已建表 账户、分类）", message)
        self.assertNotIn("持仓", message)
        self.save_config.assert_not_called()

    def test_config_saved_with_ids(self):
        cfg = init_flow.run_init(None)

        self.assertTrue(cfg.created)
        self.assertEqual(cfg.base_name, "个人账本")
        self.save_config.assert_called_once_with(cfg)
        self.assertEqual(cfg["version"], 1)
        self.assertIsNone(cfg["last_quotes_date"])
        self.assertIsNone(cfg["last_snapshot_date"])
        self.assertEqual(
            cfg["tables"]["账户"]["table_id"], "tbl_账户"
        )
        self.assertEqual(
            cfg["tables"]["账户"]["fields"]["账户名称"], "fld_账户_账户名称"
        )
        self.assertEqual(
            cfg["tables"]["行情快照"]["fields"]["收盘价/净值"],
            "fld_行情快照_收盘价/净值",
        )
        self.assertEqual(len(cfg["tables"]), 6)

    def test_idempotent_when_config_exists(self):
        cfg = self._existing_config()
        self._use_existing_config(cfg)

        result = init_flow.run_init(None)

        self.assertFalse(result.created)
        self.assertEqual(result, cfg)
        for mocked in (
            self.create_base_library,
            self.create_base_folder,
            self.create_table,
            self.create_records,
            self.save_config,
        ):
            mocked.assert_not_called()
        self.list_fields.assert_any_call("app1", "tbl_账户")

    def test_missing_table_raises_initerror(self):
        cfg = self._existing_config()
        self._use_existing_config(cfg)
        self.list_tables.return_value = [
            item
            for item in self.list_tables.return_value
            if item["name"] != "资产快照"
        ]

        with self.assertRaises(init_flow.InitError) as ctx:
            init_flow.run_init(None)

        self.assertIn("资产快照", str(ctx.exception))
        self.create_table.assert_not_called()
        self.save_config.assert_not_called()

    def test_missing_field_raises_initerror(self):
        cfg = self._existing_config()
        self._use_existing_config(cfg)
        self.list_fields.side_effect = lambda app_token, table_id: self._fields_for(
            table_id, skip=("年利率",) if table_id == "tbl_账户" else ()
        )

        with self.assertRaises(init_flow.InitError) as ctx:
            init_flow.run_init(None)

        self.assertIn("年利率", str(ctx.exception))
        self.save_config.assert_not_called()

    def test_missing_config_table_id_raises_initerror(self):
        cfg = self._existing_config()
        cfg["tables"]["账户"]["table_id"] = ""
        self._use_existing_config(cfg)

        with self.assertRaises(init_flow.InitError):
            init_flow.run_init(None)

        self.create_table.assert_not_called()

    def test_requires_login_before_creating(self):
        self.auth_ok.return_value = False

        with self.assertRaises(init_flow.NotLoggedInError):
            init_flow.run_init(None)

        self.create_base_library.assert_not_called()

    def test_unreadable_existing_config_is_not_treated_as_first_run(self):
        # 配置存在但解析失败时不能悄悄新建一个 Base。
        self.config_path.write_text("{bad json", encoding="utf-8")
        self.load_config.side_effect = self._real_load_config

        with self.assertRaises(config.ConfigError):
            init_flow.run_init(None)

        self.create_base_library.assert_not_called()
        self.save_config.assert_not_called()

    def test_non_object_existing_config_is_not_treated_as_first_run(self):
        # `null`/`[]` 都是合法 JSON；存在这种文件时也不能新建 Base。
        for payload in ("null", "[]"):
            with self.subTest(payload=payload):
                self.config_path.write_text(payload, encoding="utf-8")
                self.load_config.side_effect = self._real_load_config

                with self.assertRaises(config.ConfigError):
                    init_flow.run_init(None)

                self.create_base_library.assert_not_called()
                self.save_config.assert_not_called()


class LedgerCliTest(InitTestBase):
    def test_init_success_exit_0(self):
        with captured_output() as (out, err):
            code = ledger.main(["init"])

        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertIn("初始化完成", out.getvalue())

    def test_validated_init_does_not_claim_creation(self):
        cfg = self._existing_config()
        cfg["base_name"] = "个人账本"
        self._use_existing_config(cfg)

        with captured_output() as (out, err):
            code = ledger.main(["init"])

        self.assertEqual(code, 0)
        output = out.getvalue()
        self.assertIn("校验通过", output)
        self.assertIn("个人账本", output)
        self.assertNotIn("初始化完成", output)
        self.assertNotIn("预置分类已写入", output)

    def test_not_logged_in_exit_3(self):
        self.auth_ok.return_value = False

        with captured_output() as (out, err):
            code = ledger.main(["init"])

        self.assertEqual(code, 3)
        self.assertIn("lark-cli auth login", err.getvalue())
        self.create_base_library.assert_not_called()
        self.save_config.assert_not_called()

    def test_lark_error_exit_4(self):
        self.create_base_library.side_effect = lark.LarkError("权限不足")

        with captured_output() as (out, err):
            code = ledger.main(["init"])

        self.assertEqual(code, 4)
        self.assertIn("权限不足", err.getvalue())

    def test_init_error_exit_5(self):
        cfg = self._existing_config()
        self._use_existing_config(cfg)
        self.list_tables.return_value = []

        with captured_output() as (out, err):
            code = ledger.main(["init"])

        self.assertEqual(code, 5)
        self.assertIn("账户", err.getvalue())

    def test_config_error_exit_3(self):
        self.config_path.write_text("{bad json", encoding="utf-8")
        self.load_config.side_effect = self._real_load_config

        with captured_output() as (out, err):
            code = ledger.main(["init"])

        self.assertEqual(code, 3)
        self.assertIn("配置", err.getvalue())

    def test_mid_init_failure_exit_4_names_base(self):
        self.create_records.side_effect = lark.LarkError("写分类失败")

        with captured_output() as (out, err):
            code = ledger.main(["init"])

        self.assertEqual(code, 4)
        self.assertIn("app_token=app1", err.getvalue())
        self.save_config.assert_not_called()

    def test_bad_args_exit_2(self):
        with captured_output():
            self.assertEqual(ledger.main([]), 2)
        with captured_output():
            self.assertEqual(ledger.main(["nope"]), 2)

    def test_folder_token_flag(self):
        with captured_output():
            code = ledger.main(["init", "--folder-token", "fld9"])

        self.assertEqual(code, 0)
        self.create_base_folder.assert_called_once_with("个人账本", "fld9")

    def test_base_name_flag(self):
        with captured_output():
            code = ledger.main(["init", "--base-name", "家庭账本"])

        self.assertEqual(code, 0)
        self.create_base_library.assert_called_once_with("家庭账本")


if __name__ == "__main__":
    unittest.main()
