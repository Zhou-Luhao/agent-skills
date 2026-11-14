#!/usr/bin/env python3
"""validate_skills.py 的单元测试。

运行：python -m unittest discover -s .github/scripts/tests -v
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import validate_skills as vs


WORKFLOW_WITH_MATRIX = """\
name: T
on: [push]
jobs:
  test:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        python-version: {matrix}
    steps:
      - run: echo hi
"""

WORKFLOW_WITHOUT_MATRIX = """\
name: T
on: [push]
jobs:
  lint:
    runs-on: ubuntu-24.04
    steps:
      - run: echo hi
"""


def write_skill(
    root: pathlib.Path,
    dirname: str = "demo",
    *,
    name: str | None = None,
    description: str | None = "Demo skill used by the unit tests.",
    compat: str | None = "Requires Python 3.9+ (stdlib only)",
) -> pathlib.Path:
    """在 root 下造一个技能目录，返回其路径。"""
    skill_dir = root / dirname
    skill_dir.mkdir(parents=True, exist_ok=True)

    front = ["---", f"name: {dirname if name is None else name}"]
    if description is not None:
        front.append(f"description: {description}")
    if compat is not None:
        front.append(f"compatibility: {compat}")
    front += ["---", "", "# Body", ""]

    (skill_dir / "SKILL.md").write_text("\n".join(front), encoding="utf-8")
    (skill_dir / "README.md").write_text("# readme\n", encoding="utf-8")
    return skill_dir


def write_workflows(tmp: pathlib.Path, entries: dict[str, str | None]) -> pathlib.Path:
    """entries: {文件名: matrix 字面量 或 None 表示无矩阵}。"""
    wf = tmp / "workflows"
    wf.mkdir()
    for filename, matrix in entries.items():
        template = WORKFLOW_WITHOUT_MATRIX if matrix is None else WORKFLOW_WITH_MATRIX
        text = template if matrix is None else template.format(matrix=matrix)
        (wf / filename).write_text(text, encoding="utf-8")
    return wf


class VersionParsingTest(unittest.TestCase):
    def test_major_minor(self):
        self.assertEqual(vs.version_of("3.9"), (3, 9))
        self.assertEqual(vs.version_of("3.10"), (3, 10))

    def test_patch_level_is_dropped(self):
        self.assertEqual(vs.version_of("3.10.4"), (3, 10))

    def test_unparsable_returns_none(self):
        for text in ("", "abc", "3", "v3.9", "3.x", ">= 3.9"):
            with self.subTest(text=text):
                self.assertIsNone(vs.version_of(text))

    def test_fmt_version(self):
        self.assertEqual(vs.fmt_version((3, 9)), "3.9")
        self.assertEqual(vs.fmt_version((3, 10)), "3.10")


class PythonFloorTest(unittest.TestCase):
    TEXT = "---\nname: x\nlicense: MIT\ncompatibility: Requires Python 3.9+\n---\n\nbody\n"

    def test_no_matrix_skips_check(self):
        errors, warnings = vs.check_python_floor("Requires Python 3.9+", self.TEXT, None, "")
        self.assertEqual((errors, warnings), ([], []))

    def test_compat_without_python_skips_check(self):
        errors, warnings = vs.check_python_floor("Requires git and jq", self.TEXT, (3, 10), "w.yml")
        self.assertEqual((errors, warnings), ([], []))

    def test_equal_floor_is_clean(self):
        errors, warnings = vs.check_python_floor("Requires Python 3.9+", self.TEXT, (3, 9), "w.yml")
        self.assertEqual((errors, warnings), ([], []))

    def test_declared_lower_than_matrix_is_error(self):
        errors, warnings = vs.check_python_floor("Requires Python 3.9+", self.TEXT, (3, 10), "w.yml")
        self.assertEqual(warnings, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("3.9", errors[0][0])
        self.assertIn("3.10", errors[0][0])

    def test_declared_higher_than_matrix_is_warning(self):
        errors, warnings = vs.check_python_floor("Requires Python 3.9+", self.TEXT, (3, 8), "w.yml")
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_312_is_not_below_39(self):
        """字符串比较会认为 "3.12" < "3.9"，元组比较不会。"""
        errors, warnings = vs.check_python_floor("Requires Python 3.12+", self.TEXT, (3, 9), "w.yml")
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)

    def test_finding_points_at_compatibility_line(self):
        errors, _ = vs.check_python_floor("Requires Python 3.9+", self.TEXT, (3, 10), "w.yml")
        self.assertEqual(errors[0][1], 4)


class CollectFloorTest(unittest.TestCase):
    def _floor(self, entries: dict[str, str | None]):
        with tempfile.TemporaryDirectory() as tmp:
            wf = write_workflows(pathlib.Path(tmp), entries)
            with patch.object(vs, "WORKFLOWS_DIR", wf):
                return vs.collect_ci_python_floor()

    def test_reads_matrix(self):
        floor, source = self._floor({"a.yml": '["3.9", "3.10", "3.11"]'})
        self.assertEqual(floor, (3, 9))
        self.assertIn("a.yml", source)

    def test_string_sort_trap(self):
        floor, _ = self._floor({"a.yml": '["3.10", "3.9"]'})
        self.assertEqual(floor, (3, 9))

    def test_min_across_workflows(self):
        floor, source = self._floor({"a.yml": '["3.10", "3.12"]', "b.yml": '["3.9"]'})
        self.assertEqual(floor, (3, 9))
        self.assertIn("a.yml", source)
        self.assertIn("b.yml", source)

    def test_workflow_without_matrix_is_ignored(self):
        self.assertEqual(self._floor({"a.yml": None}), (None, ""))

    def test_empty_workflows_dir(self):
        self.assertEqual(self._floor({}), (None, ""))

    def test_missing_dir(self):
        with patch.object(vs, "WORKFLOWS_DIR", pathlib.Path("/nonexistent/oura-test")):
            self.assertEqual(vs.collect_ci_python_floor(), (None, ""))


class ValidateSkillTest(unittest.TestCase):
    def _validate(self, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = write_skill(pathlib.Path(tmp), **kwargs)
            return vs.validate_skill(skill_dir, (3, 9), "w.yml")

    def test_clean_skill(self):
        self.assertEqual(self._validate(), ([], []))

    def test_floor_mismatch_becomes_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = write_skill(pathlib.Path(tmp))
            errors, warnings = vs.validate_skill(skill_dir, (3, 10), "w.yml")
        self.assertEqual(len(errors), 1)
        self.assertIn("compatibility", errors[0][0])
        self.assertEqual(warnings, [])

    def test_name_must_match_directory(self):
        errors, _ = self._validate(name="other")
        self.assertTrue(any("必须与目录名" in message for message, _ in errors))

    def test_missing_description(self):
        errors, _ = self._validate(description=None)
        self.assertTrue(any("description" in message for message, _ in errors))

    def test_missing_compatibility_is_allowed(self):
        self.assertEqual(self._validate(compat=None), ([], []))

    def test_missing_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = pathlib.Path(tmp) / "demo"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
            errors, warnings = vs.validate_skill(skill_dir, (3, 9), "w.yml")
        self.assertEqual(len(errors), 1)
        self.assertEqual(warnings, [])


class ParseFrontmatterTest(unittest.TestCase):
    def test_valid(self):
        block, body_start = vs.parse_frontmatter("---\nname: x\n---\n\nbody\n")
        self.assertEqual(block, "name: x")
        self.assertEqual(body_start, 4)

    def test_unclosed_raises(self):
        with self.assertRaises(ValueError):
            vs.parse_frontmatter("---\nname: x\n\nbody\n")


if __name__ == "__main__":
    unittest.main()