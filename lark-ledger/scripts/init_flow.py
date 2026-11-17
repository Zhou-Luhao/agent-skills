"""init 流程：建 Base → 按 schema 建表 → 写预置分类 → 落本地配置。

所有飞书读写都走 `lark` 封装的 lark-cli 子进程；本模块只做编排与校验。
重复 init 走幂等分支：已有配置时只比对表/字段，不重建、不改写配置。
"""

import json
import pathlib
import sys
import time
from typing import List, Optional

import config
import lark

ASSETS_DIR = pathlib.Path(__file__).resolve().parents[1] / "assets"
SCHEMA_PATH = ASSETS_DIR / "schema.json"
CATEGORIES_PATH = ASSETS_DIR / "categories.json"
DEFAULT_BASE_NAME = "个人账本"

# Base 的表/字段写入是异步生效的：建完表后统一回读字段 id，缺字段时短退避重试，
# 而不是逐表写完立刻读回（lark-base skill 明确要求不要逐项写后立即读）。
FIELD_READ_ATTEMPTS = 3
FIELD_READ_BACKOFF_SECONDS = 0.5
# 新 Base 的默认表可能延迟出现在 table-list 里；建完 schema 表后按 1s 退避
# 重列几次再放弃（与 lark-cli +base-create 清理默认表的等待节奏一致）。
STRAY_TABLE_ATTEMPTS = 3
STRAY_TABLE_DELAY_SECONDS = 1.0


class InitError(RuntimeError):
    """assets 数据损坏，或已有 Base 的表结构与 schema 不一致。"""


class NotLoggedInError(RuntimeError):
    """lark-cli 没有以用户身份登录。"""


class InitResult(dict):
    """init 结果：既是落盘/复用的配置，也带本次是新建还是校验的信息。"""

    def __init__(self, cfg: dict, created: bool, base_name: str) -> None:
        super().__init__(cfg)
        self.created = created
        self.base_name = base_name


def _load_json(path: pathlib.Path, what: str):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InitError("读不到%s %s：%s" % (what, path, exc)) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InitError("%s不是合法 JSON %s：%s" % (what, path, exc)) from exc


def load_schema() -> dict:
    """读取并校验 assets/schema.json。"""
    schema = _load_json(SCHEMA_PATH, "表结构定义")
    tables = schema.get("tables") if isinstance(schema, dict) else None
    if not isinstance(tables, list) or not tables:
        raise InitError("%s 缺少非空的 tables 列表" % SCHEMA_PATH)
    for table in tables:
        if not isinstance(table, dict) or not table.get("name"):
            raise InitError("%s 里有表缺少 name" % SCHEMA_PATH)
        fields = table.get("fields")
        if not isinstance(fields, list) or not fields:
            raise InitError("表 %s 缺少非空的 fields 列表" % table["name"])
        for field in fields:
            if not isinstance(field, dict) or not field.get("name"):
                raise InitError("表 %s 里有字段缺少 name" % table["name"])
    return schema


def load_categories() -> List[dict]:
    """读取并校验 assets/categories.json。"""
    categories = _load_json(CATEGORIES_PATH, "预置分类")
    if not isinstance(categories, list):
        raise InitError("%s 不是数组" % CATEGORIES_PATH)
    for entry in categories:
        if not isinstance(entry, dict):
            raise InitError("%s 里有非对象项" % CATEGORIES_PATH)
        if entry.get("类型") not in ("收入", "支出"):
            raise InitError(
                "%s 里有分类的类型不是收入/支出：%r" % (CATEGORIES_PATH, entry)
            )
        if not entry.get("一级"):
            raise InitError("%s 里有分类缺少一级：%r" % (CATEGORIES_PATH, entry))
    return categories


def category_full_name(entry: dict) -> str:
    """分类主字段值：`支出·餐饮·午餐`；没有二级时不留分隔符尾巴。"""
    return "·".join(
        part
        for part in (entry.get("类型"), entry.get("一级"), entry.get("二级"))
        if part
    )


def _category_records(categories: List[dict]) -> List[dict]:
    return [
        {
            "分类": category_full_name(entry),
            "类型": [entry["类型"]],
            "一级": entry["一级"],
            "二级": entry.get("二级") or "",
        }
        for entry in categories
    ]


def _table_name(item: dict) -> Optional[str]:
    for key in ("name", "table_name"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _field_name(item: dict) -> Optional[str]:
    for key in ("name", "field_name"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _field_id(item: dict) -> Optional[str]:
    for key in ("field_id", "id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _verify_existing(cfg: dict, schema: dict) -> dict:
    """校验已有配置指向的 Base：表齐全、字段齐全，齐全则原样返回。"""
    app_token = cfg.get("app_token")
    if not isinstance(app_token, str) or not app_token:
        raise InitError("本地配置缺少 app_token，无法校验已有 Base")

    local_tables = cfg.get("tables")
    if not isinstance(local_tables, dict):
        local_tables = {}

    remote_tables = set()
    for item in lark.list_tables(app_token):
        name = _table_name(item)
        if name is not None:
            remote_tables.add(name)

    problems: List[str] = []
    for table in schema["tables"]:
        name = table["name"]
        entry = local_tables.get(name)
        table_id = entry.get("table_id") if isinstance(entry, dict) else None
        if not table_id:
            problems.append("配置里缺少表 %s 的 table_id" % name)
            continue
        if name not in remote_tables:
            problems.append("Base 里缺少表 %s" % name)
            continue
        remote_fields = {
            _field_name(item)
            for item in lark.list_fields(app_token, table_id)
            if _field_name(item) is not None
        }
        missing = [
            field["name"]
            for field in table["fields"]
            if field["name"] not in remote_fields
        ]
        if missing:
            problems.append("表 %s 缺字段：%s" % (name, "、".join(missing)))

    if problems:
        raise InitError(
            "已有 Base 与 schema 不一致，未做任何改动：\n- "
            + "\n- ".join(problems)
        )
    return cfg


def _collect_field_ids(app_token: str, cfg: dict, schema: dict) -> None:
    """建完所有表后统一回读字段 id。

    Base 的表/字段更新是异步生效的，刚建完就逐表读回可能读不到最新状态；
    因此先完成全部建表，再按 schema 一次性比对，缺字段时短退避重试。
    """
    missing = {}
    for attempt in range(FIELD_READ_ATTEMPTS):
        if attempt:
            time.sleep(FIELD_READ_BACKOFF_SECONDS * attempt)
        missing = {}
        for table in schema["tables"]:
            name = table["name"]
            entry = cfg["tables"][name]
            field_ids = {}
            for item in lark.list_fields(app_token, entry["table_id"]):
                field_name = _field_name(item)
                field_id = _field_id(item)
                if field_name is not None and field_id is not None:
                    field_ids[field_name] = field_id
            entry["fields"] = field_ids
            absent = [
                field["name"]
                for field in table["fields"]
                if field["name"] not in field_ids
            ]
            if absent:
                missing[name] = absent
        if not missing:
            return
    detail = "；".join(
        "%s 缺 %s" % (name, "、".join(names))
        for name, names in missing.items()
    )
    raise InitError(
        "字段回读重试 %d 次仍不齐：%s" % (FIELD_READ_ATTEMPTS, detail)
    )


def _remove_stray_tables(app_token: str, schema: dict) -> None:
    """删掉新建 Base 附带的平台默认表，保证最终只有 schema 里的 6 张表。

    必须在 schema 表建完之后调用：飞书不允许删除 Base 的最后一张表，先删
    默认表会直接失败。默认表可能延迟出现在 table-list 里，因此按
    STRAY_TABLE_DELAY_SECONDS 重列几次；重试后仍删不掉只告警，不中断 init。
    """
    schema_names = {table["name"] for table in schema["tables"]}
    remaining: List[str] = []
    for attempt in range(STRAY_TABLE_ATTEMPTS):
        try:
            items = lark.list_tables(app_token)
        except lark.LarkError as exc:
            remaining = ["重新列出表失败：%s" % exc]
        else:
            strays = []
            for item in items:
                name = _table_name(item)
                if name in schema_names:
                    continue
                table_id = item.get("table_id") or item.get("id")
                strays.append(
                    (
                        name or "（未命名表）",
                        table_id if isinstance(table_id, str) else "",
                    )
                )
            if not strays:
                # 默认表可能延迟可见：最后一轮之前再等 1s 重列。
                if attempt + 1 < STRAY_TABLE_ATTEMPTS:
                    time.sleep(STRAY_TABLE_DELAY_SECONDS)
                    continue
                return
            remaining = []
            for name, table_id in strays:
                if not table_id:
                    remaining.append("%s（缺少 table_id）" % name)
                    continue
                try:
                    lark.delete_table(app_token, table_id)
                except lark.LarkError as exc:
                    remaining.append("%s：%s" % (name, exc))
            if not remaining:
                # 删除成功即可返回；失败才进入下一轮重列重试。
                return
        if attempt + 1 < STRAY_TABLE_ATTEMPTS:
            time.sleep(STRAY_TABLE_DELAY_SECONDS)
    if remaining:
        print(
            "警告：init 无法删除 Base 里多余的表（%s）；已继续初始化，"
            "但账本会多出这些表，请到飞书手动删除。" % "；".join(remaining),
            file=sys.stderr,
        )


def _partial_note(
    app_token: str, node_token: Optional[str], created_tables: List[str]
) -> str:
    tables = "、".join(created_tables) if created_tables else "无"
    if not app_token:
        return (
            "init 在创建 Base 阶段失败，未拿到 app_token，无法确认是否已留下"
            "空 Base；请到飞书核对后删除或补配置再重试"
        )
    return (
        "Base 已创建（app_token=%s，node_token=%s，已建表 %s），init 未完成；"
        "请删除该 Base 或手工补配置后重试（直接重跑 init 会再建一个 Base）"
        % (app_token, node_token or "无", tables)
    )


def _raise_with_partial_state(exc: Exception, note: str) -> None:
    """保留原异常类型（决定 CLI 退出码），并在消息里说明已创建的资源。"""
    message = "%s；%s" % (exc, note)
    if isinstance(exc, lark.LarkError):
        raise lark.LarkError(message, exc.payload) from exc
    if isinstance(exc, (InitError, config.ConfigError)):
        raise type(exc)(message) from exc
    raise InitError(
        "%s（未预期的 %s）" % (message, type(exc).__name__)
    ) from exc


def _create_all(
    schema: dict, categories: List[dict], base_name: str, folder_token: Optional[str]
) -> dict:
    app_token = ""
    node_token: Optional[str] = None
    created_tables: List[str] = []
    try:
        # Base 创建也放在 try 里：create 返回前失败时可能已经留下空 Base，
        # 由 _raise_with_partial_state 统一交代。
        if folder_token:
            app_token = lark.create_base_folder(base_name, folder_token)
        else:
            created = lark.create_base_library(base_name)
            app_token = created["app_token"]
            node_token = created["node_token"]

        cfg = config.empty_config(app_token, node_token)
        cfg["base_name"] = base_name
        # 先建齐 6 张表（link 只指向更早的表），字段 id 留到最后统一回读。
        for table in schema["tables"]:
            name = table["name"]
            table_id = lark.create_table(app_token, name, table["fields"])
            cfg["tables"][name] = {"table_id": table_id, "fields": {}}
            created_tables.append(name)

        # 平台建 Base 时会附带一张默认表。必须等 6 张 schema 表建完再删：
        # 飞书不允许删除 Base 的最后一张表。
        _remove_stray_tables(app_token, schema)

        _collect_field_ids(app_token, cfg, schema)

        category_table = cfg["tables"].get("分类")
        if not isinstance(category_table, dict) or not category_table.get("table_id"):
            raise InitError("schema 里缺少「分类」表，无法写入预置分类")
        lark.create_records(
            app_token,
            category_table["table_id"],
            _category_records(categories),
        )
        config.save_config(cfg)
        return cfg
    except Exception as exc:  # 任何中断都要交代已建资源
        _raise_with_partial_state(
            exc, _partial_note(app_token, node_token, created_tables)
        )


def run_init(
    folder_token: Optional[str] = None, base_name: str = DEFAULT_BASE_NAME
) -> InitResult:
    """建账本（或在已有配置上校验），返回配置 + created/base_name 状态。"""
    if not lark.auth_ok():
        raise NotLoggedInError(
            "lark-cli 未登录：请先在终端执行 `lark-cli auth login`"
            "（授予 base / wiki / drive 权限）后重试。"
        )

    schema = load_schema()
    # 首次 init 与否只看配置文件是否存在：存在但解析失败必须报错，
    # 绝不能静默新建第二个 Base。
    if config.CONFIG_PATH.exists():
        cfg = _verify_existing(config.load_config(), schema)
        stored_name = cfg.get("base_name")
        return InitResult(
            cfg,
            False,
            stored_name if isinstance(stored_name, str) else "",
        )
    return InitResult(
        _create_all(schema, load_categories(), base_name, folder_token),
        True,
        base_name,
    )
