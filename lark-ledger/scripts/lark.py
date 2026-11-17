"""lark-cli 子进程封装：所有飞书读写都从这里走。

- 统一以 `lark-cli <args> --format json` 调用，解析 `{"ok": true, "data": {...}}`
  信封；退出码非 0 或 `ok != true` 抛 `LarkError`（`.payload` 带原始信封）。
- 批量写入按 CLI 上限 200 条/次自动分批。
- 条件查询走 `base +record-list --filter-json`（v1.0.100 的 `+record-search`
  只支持关键词检索，不能只传 filter），并把 CLI 的“行矩阵”输出还原成
  `[{"record_id": ..., "fields": {...}}]`。
- 日期写入用 `to_ms` 生成 UTC+8 当日 0 点毫秒；读到的毫秒值用
  `from_ms` 转回 `"YYYY-MM-DD"`。
"""

import datetime
import json
import subprocess
from typing import Dict, List, Optional

CLI = "lark-cli"
TIMEOUT_SECONDS = 60
BATCH_LIMIT = 200
# --format json 的 inline 输出上限是 200 行（ndjson 才能一次 2000 行）。
INLINE_PAGE_LIMIT = 200

_TZ = datetime.timezone(datetime.timedelta(hours=8))
_EPOCH_UTC = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


class LarkError(RuntimeError):
    """lark-cli 调用失败或返回了无法解析的结果。"""

    def __init__(self, message: str, payload: Optional[dict] = None) -> None:
        super().__init__(message)
        self.payload = {} if payload is None else payload


def _json_arg(value: dict) -> str:
    """把 Python 对象序列化成紧凑 JSON，供 --json/--filter-json 使用。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _diagnostics(proc: subprocess.CompletedProcess, command: List[str]) -> dict:
    return {
        "args": command,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _failure_message(proc: subprocess.CompletedProcess, envelope: Optional[dict]) -> str:
    parts = ["lark-cli 调用失败"]
    if proc.returncode:
        parts.append("（exit %d）" % proc.returncode)
    if isinstance(envelope, dict):
        error = envelope.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("msg")
            if message:
                parts.append("：%s" % message)
            if error.get("code"):
                parts.append(" [code %s]" % error["code"])
        elif error:
            parts.append("：%s" % error)
    stderr = (proc.stderr or "").strip()
    if stderr:
        parts.append("；stderr: %s" % stderr)
    return "".join(parts)


def run_cli(args: List[str]) -> dict:
    """执行 lark-cli 并返回信封里的 data；失败抛 LarkError。"""
    command = [CLI, *args, "--format", "json"]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        raise LarkError(
            "执行 lark-cli 失败：%s" % exc,
            {"args": command, "error": str(exc)},
        ) from exc

    stdout = (proc.stdout or "").strip()
    try:
        envelope = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LarkError(
            "lark-cli 输出不是合法 JSON：%r；stderr: %s"
            % (stdout[:500], (proc.stderr or "").strip()),
            _diagnostics(proc, command),
        ) from exc

    if not isinstance(envelope, dict):
        raise LarkError(
            "lark-cli 输出不是 JSON 对象：%r" % (stdout[:500],),
            _diagnostics(proc, command),
        )

    if proc.returncode != 0 or envelope.get("ok") is not True:
        raise LarkError(_failure_message(proc, envelope), envelope)

    data = envelope.get("data")
    if not isinstance(data, dict):
        raise LarkError(
            "lark-cli 返回缺少 data 对象：%r" % (stdout[:500],), envelope
        )
    return data


def auth_ok() -> bool:
    """用户身份是否已登录；任何异常都按未登录处理。"""
    # `auth status` 只认 --json，不认 --format json，所以不能走 run_cli。
    command = [CLI, "auth", "status", "--json"]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            return False
        payload = json.loads((proc.stdout or "").strip())
    except (
        OSError,
        subprocess.SubprocessError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return False
    if not isinstance(payload, dict):
        return False
    identities = payload.get("identities")
    if not isinstance(identities, dict):
        return False
    user = identities.get("user")
    if not isinstance(user, dict):
        return False
    return user.get("status") == "ready"


def to_ms(date_str: str) -> int:
    """`"YYYY-MM-DD"` → UTC+8 当日 0 点毫秒时间戳。"""
    moment = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=_TZ)
    return int(moment.timestamp() * 1000)


def from_ms(ms: int) -> str:
    """UTC+8 毫秒时间戳 → `"YYYY-MM-DD"`。"""
    moment = _EPOCH_UTC + datetime.timedelta(milliseconds=ms)
    return moment.astimezone(_TZ).strftime("%Y-%m-%d")


def _required_text(data: dict, key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise LarkError(
            "%s 返回缺少 %s：%s" % (context, key, _json_arg(data)), data
        )
    return value


def _nested_dict(data: dict, key: str) -> dict:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def create_base_library(title: str) -> dict:
    """在“我的文档库”创建多维表格，返回 app_token 与 wiki node_token。"""
    data = run_cli(
        [
            "wiki",
            "+node-create",
            "--space-id",
            "my_library",
            "--obj-type",
            "bitable",
            "--title",
            title,
        ]
    )
    return {
        "app_token": _required_text(data, "obj_token", "wiki +node-create"),
        "node_token": _required_text(data, "node_token", "wiki +node-create"),
    }


def create_base_folder(title: str, folder_token: str) -> str:
    """在指定云空间文件夹创建多维表格，返回 app_token。"""
    data = run_cli(
        ["base", "+base-create", "--name", title, "--folder-token", folder_token]
    )
    app_token = data.get("app_token")
    if not app_token:
        app_token = _nested_dict(data, "app").get("app_token")
    if not app_token:
        app_token = _nested_dict(data, "base").get("app_token")
    if not isinstance(app_token, str) or not app_token:
        raise LarkError(
            "base +base-create 返回缺少 app_token：%s" % _json_arg(data), data
        )
    return app_token


def create_table(app_token: str, name: str, fields: List[dict]) -> str:
    """创建数据表，返回 table_id。"""
    data = run_cli(
        [
            "base",
            "+table-create",
            "--base-token",
            app_token,
            "--name",
            name,
            "--fields",
            _json_arg(fields),
        ]
    )
    table = _nested_dict(data, "table")
    table_id = (
        data.get("table_id")
        or data.get("id")
        or table.get("table_id")
        or table.get("id")
    )
    if not isinstance(table_id, str) or not table_id:
        raise LarkError(
            "base +table-create 返回缺少 table_id：%s" % _json_arg(data), data
        )
    return table_id


def _items(data: dict, key: str) -> List[dict]:
    for name in (key, "items"):
        if name in data:
            value = data[name]
            if not isinstance(value, list):
                raise LarkError(
                    "lark-cli 返回的 %s 不是列表：%s" % (name, _json_arg(data)),
                    data,
                )
            return [dict(item) for item in value if isinstance(item, dict)]
    raise LarkError(
        "lark-cli 返回缺少 %s/items 列表：%s" % (key, _json_arg(data)), data
    )


def _with_id_alias(item: dict, alias: str) -> dict:
    """CLI 的列表项主键叫 `id`，补一个语义化别名（table_id/field_id）。"""
    if not item.get(alias) and item.get("id"):
        item[alias] = item["id"]
    return item


def list_tables(app_token: str) -> List[dict]:
    """列出 Base 里的所有表（保留 CLI 原始字段，并补 table_id 别名）。"""
    data = run_cli(["base", "+table-list", "--base-token", app_token])
    return [_with_id_alias(item, "table_id") for item in _items(data, "tables")]


def list_fields(app_token: str, table_id: str) -> List[dict]:
    """列出表字段（保留 CLI 原始字段，并补 field_id 别名）。"""
    data = run_cli(
        [
            "base",
            "+field-list",
            "--base-token",
            app_token,
            "--table-id",
            table_id,
        ]
    )
    return [_with_id_alias(item, "field_id") for item in _items(data, "fields")]


def _record_ids(data: dict) -> List[str]:
    ids = data.get("record_id_list")
    if isinstance(ids, list) and ids and all(
        isinstance(item, str) and item for item in ids
    ):
        return list(ids)

    records = data.get("records")
    if isinstance(records, list) and records:
        extracted = [
            record.get("record_id")
            for record in records
            if isinstance(record, dict)
        ]
        if all(isinstance(item, str) and item for item in extracted):
            return extracted

    raise LarkError(
        "lark-cli 返回缺少 record_id 列表：%s" % _json_arg(data), data
    )


def create_records(
    app_token: str, table_id: str, records: List[dict]
) -> List[str]:
    """批量创建记录（每批 ≤200），返回按输入顺序排列的 record_id。"""
    record_ids: List[str] = []
    for start in range(0, len(records), BATCH_LIMIT):
        batch = records[start : start + BATCH_LIMIT]
        data = run_cli(
            [
                "base",
                "+record-batch-create",
                "--base-token",
                app_token,
                "--table-id",
                table_id,
                "--json",
                _json_arg({"create_records": batch}),
            ]
        )
        batch_ids = _record_ids(data)
        if len(batch_ids) != len(batch):
            raise LarkError(
                "record-batch-create 返回的 record_id 数量与请求不一致："
                "请求 %d 条，返回 %d 条" % (len(batch), len(batch_ids)),
                data,
            )
        record_ids.extend(batch_ids)
    return record_ids


def update_record(
    app_token: str, table_id: str, record_id: str, fields: dict
) -> None:
    """更新单条记录的指定字段。"""
    update_records(app_token, table_id, {record_id: fields})


def update_records(
    app_token: str, table_id: str, updates: Dict[str, dict]
) -> None:
    """批量更新记录（每批 ≤200）；updates 为 record_id → 字段字典。"""
    items = list(updates.items())
    for start in range(0, len(items), BATCH_LIMIT):
        batch = dict(items[start : start + BATCH_LIMIT])
        run_cli(
            [
                "base",
                "+record-batch-update",
                "--base-token",
                app_token,
                "--table-id",
                table_id,
                "--json",
                _json_arg({"update_records": batch}),
            ]
        )


def delete_records(
    app_token: str, table_id: str, record_ids: List[str]
) -> None:
    """删除记录；high-risk-write，按调用方意图显式带 --yes。"""
    if not record_ids:
        return
    args = [
        "base",
        "+record-delete",
        "--base-token",
        app_token,
        "--table-id",
        table_id,
    ]
    for record_id in record_ids:
        args.extend(["--record-id", record_id])
    args.append("--yes")
    run_cli(args)


def _records_from_matrix(data: dict) -> List[dict]:
    """把 record-list 的 `{data: [[...]], fields: [...], record_id_list: [...]}` 还原成记录字典。"""
    rows = data.get("data")
    field_names = data.get("fields")
    record_ids = data.get("record_id_list")
    if not isinstance(rows, list) or not isinstance(field_names, list):
        raise LarkError(
            "lark-cli 记录输出缺少 data/fields/record_id_list：%s"
            % _json_arg(data),
            data,
        )
    # 空表时 CLI 可能连 record_id_list 都不返回，这仍是合法的零行结果。
    if record_ids is None and not rows:
        record_ids = []
    if not isinstance(record_ids, list) or len(rows) != len(record_ids):
        raise LarkError(
            "lark-cli 记录输出缺少 data/fields/record_id_list：%s"
            % _json_arg(data),
            data,
        )

    records: List[dict] = []
    for record_id, row in zip(record_ids, rows):
        if (
            not isinstance(record_id, str)
            or not record_id
            or not isinstance(row, list)
            or len(row) != len(field_names)
        ):
            raise LarkError(
                "lark-cli 记录行列与字段数不匹配：%s" % _json_arg(data), data
            )
        records.append(
            {"record_id": record_id, "fields": dict(zip(field_names, row))}
        )
    return records


def search_records(
    app_token: str,
    table_id: str,
    conditions: Optional[List[list]] = None,
    limit: int = 2000,
    offset: int = 0,
) -> List[dict]:
    """按条件查询记录，最多返回 limit 条（自动按 200 行/页翻页）。

    conditions 形如 `[["字段名", "==", 值]]`；None 表示不过滤。
    `offset` 是起始行偏移，调用方可以循环翻页把整表读尽。
    """
    if limit <= 0:
        return []

    records: List[dict] = []
    start = max(0, int(offset))
    while len(records) < limit:
        page_size = min(INLINE_PAGE_LIMIT, limit - len(records))
        args = [
            "base",
            "+record-list",
            "--base-token",
            app_token,
            "--table-id",
            table_id,
            "--limit",
            str(page_size),
            "--offset",
            str(start),
        ]
        if conditions is not None:
            args.extend(
                [
                    "--filter-json",
                    _json_arg({"logic": "and", "conditions": conditions}),
                ]
            )
        data = run_cli(args)
        page = _records_from_matrix(data)
        records.extend(page)
        start += len(page)
        if not data.get("has_more") or not page:
            break
    return records[:limit]
