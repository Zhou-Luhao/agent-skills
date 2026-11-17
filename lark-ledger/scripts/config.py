"""本地配置：单文件 JSON，路径 ~/.lark-ledger/config.json。

配置文件记录 Base 定位（app_token / node_token）、各表的 table_id 与字段 id，
以及行情/快照的最近执行日期。不含任何凭据。
"""

import json
import pathlib
from typing import Optional

CONFIG_PATH = pathlib.Path.home() / ".lark-ledger" / "config.json"


class ConfigError(RuntimeError):
    """配置缺失或无法解析。"""


def load_config(path: Optional[pathlib.Path] = None) -> dict:
    """读取配置；文件不存在、不是合法 JSON 或顶层不是对象时抛 ConfigError。"""
    target = CONFIG_PATH if path is None else pathlib.Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"读不到配置文件 {target}：{exc}") from exc
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON {target}：{exc}") from exc
    if not isinstance(cfg, dict):
        raise ConfigError(
            "配置文件顶层必须是 JSON 对象 %s，实际是 %s"
            % (target, type(cfg).__name__)
        )
    return cfg


def save_config(cfg: dict, path: Optional[pathlib.Path] = None) -> None:
    """写入配置，必要时创建父目录。"""
    target = CONFIG_PATH if path is None else pathlib.Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        raise ConfigError(f"写不了配置文件 {target}：{exc}") from exc


def empty_config(app_token: str, node_token: Optional[str]) -> dict:
    """初始化后的空配置：只定位 Base，还没有表与日期。"""
    return {
        "version": 1,
        "app_token": app_token,
        "node_token": node_token,
        "tables": {},
        "last_quotes_date": None,
        "last_snapshot_date": None,
    }