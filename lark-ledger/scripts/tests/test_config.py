"""config 模块：本地配置的读写与形状。"""

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import config


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "nested" / "config.json"

    def test_save_load_roundtrip(self):
        cfg = config.empty_config("appX", "nodeX")
        cfg["tables"]["账户"] = {"table_id": "tbl1", "fields": {"余额": "fld1"}}

        config.save_config(cfg, self.path)

        self.assertEqual(config.load_config(self.path), cfg)

    def test_load_missing_raises(self):
        with self.assertRaises(config.ConfigError):
            config.load_config(self.path)

    def test_load_corrupt_raises(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{bad json", encoding="utf-8")

        with self.assertRaises(config.ConfigError):
            config.load_config(self.path)

    def test_empty_config_shape(self):
        cfg = config.empty_config("appX", None)

        self.assertEqual(cfg, {
            "version": 1,
            "app_token": "appX",
            "node_token": None,
            "tables": {},
            "last_quotes_date": None,
            "last_snapshot_date": None,
        })
        # 每个配置对象互不共享嵌套字典
        self.assertIsNot(cfg["tables"], config.empty_config("appX", None)["tables"])

    def test_config_path_points_into_home(self):
        self.assertEqual(
            config.CONFIG_PATH,
            pathlib.Path.home() / ".lark-ledger" / "config.json",
        )


if __name__ == "__main__":
    unittest.main()