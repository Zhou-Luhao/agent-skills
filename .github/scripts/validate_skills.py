#!/usr/bin/env python3
"""校验 <skill>/SKILL.md 的 YAML frontmatter。

规则来自 https://agentskills.io/specification 与本仓库 README 的约定：
  - name         必填，≤64 字符，小写字母/数字/连字符，首尾不能是连字符，且与目录名一致
  - description  必填，≤1024 字符，不能为空
  - license      可选，字符串
  - compatibility 可选，≤500 字符
  - metadata     可选，string -> string 的映射
  - allowed-tools 可选，空格分隔的字符串
frontmatter 之后必须有正文。

用法：python .github/scripts/validate_skills.py
退出码：0 = 通过（含"没有技能"的情况），1 = 有错误，2 = 环境问题。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    print("::error title=Missing dependency::缺少 PyYAML，CI 中请先执行 pip install pyyaml")
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parents[2]

NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
NAME_MAX = 64
DESC_MAX = 1024
COMPAT_MAX = 500


def parse_frontmatter(text: str) -> tuple[str, int]:
    """返回 (frontmatter 文本, 正文起始行号)；不合法时抛 ValueError。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("文件必须以 `---` 开头的 YAML frontmatter 起始")
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[1:idx]), idx + 2
    raise ValueError("frontmatter 缺少闭合的 `---`")


def key_line(text: str, key: str) -> int:
    """找到某个 frontmatter 键所在行号，用于 GitHub 注解定位。"""
    for idx, line in enumerate(text.splitlines(), start=1):
        if line.startswith(f"{key}:"):
            return idx
    return 1


def check_types(data: dict, text: str) -> list[tuple[str, int]]:
    errors: list[tuple[str, int]] = []

    license_value = data.get("license")
    if license_value is not None and not isinstance(license_value, str):
        errors.append(("`license` 必须是字符串", key_line(text, "license")))

    compat = data.get("compatibility")
    if compat is not None:
        if not isinstance(compat, str):
            errors.append(("`compatibility` 必须是字符串", key_line(text, "compatibility")))
        elif len(compat) > COMPAT_MAX:
            errors.append(
                (f"`compatibility` 长度 {len(compat)} 超过 {COMPAT_MAX}", key_line(text, "compatibility"))
            )

    metadata = data.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            errors.append(("`metadata` 必须是键值映射", key_line(text, "metadata")))
        else:
            bad = [k for k, v in metadata.items() if not isinstance(v, str)]
            if bad:
                errors.append(
                    (f"`metadata` 的值必须是字符串，问题键：{', '.join(map(str, bad))}",
                     key_line(text, "metadata"))
                )

    allowed_tools = data.get("allowed-tools")
    if allowed_tools is not None and not isinstance(allowed_tools, str):
        errors.append(("`allowed-tools` 必须是空格分隔的字符串", key_line(text, "allowed-tools")))

    return errors


def validate_skill(skill_dir: Path) -> list[tuple[str, int]]:
    skill_md = skill_dir / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")

    try:
        block, body_start = parse_frontmatter(text)
    except ValueError as exc:
        return [(str(exc), 1)]

    errors: list[tuple[str, int]] = []

    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        detail = " ".join(str(exc).split())
        return [(f"frontmatter 不是合法 YAML：{detail}", 1)]

    if not isinstance(data, dict):
        return [("frontmatter 必须是键值映射", 1)]

    name = data.get("name")
    if name is None:
        errors.append(("缺少必填字段 `name`", 1))
    elif not isinstance(name, str):
        errors.append(("`name` 必须是字符串", key_line(text, "name")))
    elif len(name) > NAME_MAX:
        errors.append((f"`name` 长度 {len(name)} 超过 {NAME_MAX}", key_line(text, "name")))
    elif not NAME_RE.match(name):
        errors.append(
            (f"`name` ({name!r}) 只能用小写字母、数字、连字符，且首尾不能是连字符",
             key_line(text, "name"))
        )
    elif name != skill_dir.name:
        errors.append(
            (f"`name` ({name!r}) 必须与目录名 ({skill_dir.name!r}) 一致", key_line(text, "name"))
        )

    description = data.get("description")
    if description is None:
        errors.append(("缺少必填字段 `description`", 1))
    elif not isinstance(description, str):
        errors.append(("`description` 必须是字符串", key_line(text, "description")))
    elif not description.strip():
        errors.append(("`description` 不能为空", key_line(text, "description")))
    elif len(description) > DESC_MAX:
        errors.append(
            (f"`description` 长度 {len(description)} 超过 {DESC_MAX}", key_line(text, "description"))
        )

    errors.extend(check_types(data, text))

    line_count = len(text.splitlines())
    if body_start > line_count:
        errors.append(("frontmatter 之后缺少正文内容", max(1, line_count)))

    return errors


def main() -> int:
    skill_dirs = sorted(
        p for p in ROOT.iterdir()
        if p.is_dir() and not p.name.startswith((".", "_")) and (p / "SKILL.md").is_file()
    )

    total_errors = 0
    rows: list[str] = []
    warnings: list[str] = []

    for skill_dir in skill_dirs:
        rel = skill_dir.relative_to(ROOT).as_posix()
        errors = validate_skill(skill_dir)
        total_errors += len(errors)

        for message, line in errors:
            print(f"::error file={rel}/SKILL.md,line={line}::[{rel}] {message}")

        if not (skill_dir / "README.md").is_file():
            warnings.append(rel)
            print(f"::warning file={rel}/SKILL.md::[{rel}] 缺少面向人类的 README.md")

        status = "❌" if errors else "✅"
        rows.append(f"| `{rel}` | {status} | {len(errors)} |")
        if not errors:
            print(f"OK   {rel}")

    if not skill_dirs:
        message = "未发现任何 <skill>/SKILL.md，本次没有技能可校验。"
        print(f"::warning title=No skills discovered::{message}")
    else:
        print(f"\n检查了 {len(skill_dirs)} 个技能，{total_errors} 个错误。")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = ["### SKILL.md frontmatter 校验", ""]
        if skill_dirs:
            lines += ["| 技能 | 结果 | 错误数 |", "| --- | --- | --- |", *rows, ""]
        else:
            lines += [f"> ⚠️ {message}", ""]
        if warnings:
            lines += [f"> ⚠️ 缺少 README.md：{', '.join(f'`{w}`' for w in warnings)}", ""]
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    return 1 if total_errors else 0


if __name__ == "__main__":
    sys.exit(main())