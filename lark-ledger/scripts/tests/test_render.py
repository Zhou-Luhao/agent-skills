"""报表渲染：自包含 HTML 与 Markdown 摘要。

全部是纯函数用例：报告 dict 直接构造，不 mock 网络、不读配置、不读时钟
（``generated_at`` 只从 ``meta`` 取）。断言集中在四件事：

- 自包含：无 ``<script>``、无 http(s) 外链、无 CDN / ``@import`` / ``url()``；
- 转义：分类名、持仓名、负债名、meta 日期里的 HTML 标签一律转义；
- 图表：净资产折线（``<polyline>``）与支出横向条形（``<rect>``）都是内联 SVG；
- 降级：空序列渲染占位文案，不抛错；Markdown 表格里的 ``|`` 必须转义。
"""

import copy
import pathlib
import re
import sys
import unittest
from html.parser import HTMLParser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import render


class ExternalRefParser(HTMLParser):
    """Collect any attribute that would make the file depend on another one."""

    def __init__(self):
        super().__init__()
        self.refs = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key in ("href", "src", "xlink:href"):
                self.refs.append((tag, key, value))


def base_data():
    """Task 13/15 约定的 report dict，数字取周报样例。"""
    return {
        "meta": {
            "period": "week",
            "start": "2026-11-16",
            "end": "2026-11-22",
            "generated_at": "2026-11-22 20:00",
        },
        "summary": {
            "income": 31500.0,
            "expense": 2335.3,
            "net": 29164.7,
            "by_category": [
                {"一级": "金融", "二级": "利息支出", "金额": 2300.0},
                {"一级": "餐饮", "二级": "午餐", "金额": 35.3},
            ],
            "income_by_category": [
                {"一级": "工资", "二级": "月薪", "金额": 30000.0},
                {"一级": "理财收益", "二级": "基金收益", "金额": 1500.0},
            ],
        },
        "net_worth": [
            ["2026-11-16", 100000.0],
            ["2026-11-20", 128000.5],
            ["2026-11-22", 131164.7],
        ],
        "holdings": [
            {
                "名称": "贵州茅台",
                "代码": "sh600519",
                "份额": 1000.0,
                "最新价": 14.57,
                "市值": 14570.0,
                "浮盈": 4570.0,
            }
        ],
        "liabilities": [
            {"名称": "房贷", "剩余本金": 500000.0, "年利率": 4.2},
        ],
    }


class HtmlRenderTest(unittest.TestCase):
    def test_html_self_contained(self):
        html = render.render_html(base_data())
        lowered = html.lower()

        for token in ("<script", "http://", "https://", "cdn", "<link",
                      "@import", "url(", "xmlns"):
            self.assertNotIn(token, lowered, "HTML 不应包含 %r" % token)
        self.assertTrue(lowered.lstrip().startswith("<!doctype html>"))
        self.assertIn("<style>", lowered)
        self.assertIn('charset="utf-8"', lowered)

        parser = ExternalRefParser()
        parser.feed(html)
        self.assertEqual(parser.refs, [], "HTML 不应引用任何外部文件")

    def test_html_escapes_user_text(self):
        data = base_data()
        payload = "<img src=x onerror=alert(1)>"
        data["summary"]["by_category"][0]["一级"] = payload
        data["summary"]["by_category"][0]["二级"] = '<script>alert("x")</script>'
        data["summary"]["income_by_category"][0]["一级"] = payload
        data["holdings"][0]["名称"] = payload
        data["liabilities"][0]["名称"] = "<b>贷</b>"
        data["meta"]["start"] = "<i>2026-11-16</i>"
        data["meta"]["generated_at"] = "<em>20:00</em>"

        html = render.render_html(data)

        self.assertIn("&lt;img", html)
        self.assertNotIn(payload, html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;b&gt;贷&lt;/b&gt;", html)
        self.assertIn("&lt;i&gt;2026-11-16&lt;/i&gt;", html)
        self.assertIn("&lt;em&gt;20:00&lt;/em&gt;", html)
        # 转义后的文本里会有 "onerror="，但它绝不能落在真实标签属性上。
        self.assertIsNone(
            re.search(r"<[a-zA-Z][^>]*\son[a-z]+\s*=", html),
            "输出中出现了内联事件处理器属性",
        )

    def test_html_contains_svg_polyline(self):
        html = render.render_html(base_data())

        self.assertIn("<svg", html)
        self.assertIn("<polyline", html)
        self.assertIn('aria-label="净资产走势"', html)
        self.assertIn('aria-label="支出分类"', html)
        self.assertIn("<rect", html)

    def test_html_contains_key_numbers_and_tables(self):
        html = render.render_html(base_data())

        for number in ("31,500.00", "2,335.30", "29,164.70", "131,164.70"):
            self.assertIn(number, html)
        for text in ("收入", "支出", "结余", "期末净资产",
                     "支出分类", "收入分类", "持仓", "负债"):
            self.assertIn(text, html)
        self.assertIn("贵州茅台", html)
        self.assertIn("sh600519", html)
        self.assertIn("房贷", html)
        self.assertIn("500,000.00", html)
        self.assertIn("金融", html)
        self.assertIn("利息支出", html)

    def test_html_empty_series_no_crash(self):
        data = base_data()
        data["net_worth"] = []
        data["summary"]["by_category"] = []
        data["summary"]["income_by_category"] = []

        html = render.render_html(data)

        self.assertIsInstance(html, str)
        self.assertGreaterEqual(html.count("暂无数据"), 2)
        self.assertIn("31,500.00", html)

    def test_html_empty_holdings_and_liabilities(self):
        data = base_data()
        data["holdings"] = []
        data["liabilities"] = []

        html = render.render_html(data)

        self.assertIn("暂无持仓", html)
        self.assertIn("暂无负债", html)

    def test_html_single_net_worth_point(self):
        data = base_data()
        data["net_worth"] = [["2026-11-22", 131164.7]]

        html = render.render_html(data)

        self.assertIn("<polyline", html)
        self.assertIn("131,164.70", html)

    def test_html_month_title_and_generated_at(self):
        data = base_data()
        data["meta"]["period"] = "month"

        html = render.render_html(data)

        self.assertIn("财务月报", html)
        self.assertIn("2026-11-22 20:00", html)
        self.assertIn("2026-11-16", html)
        self.assertIn("2026-11-22", html)

    def test_html_deterministic(self):
        data = base_data()

        self.assertEqual(render.render_html(data), render.render_html(data))
        self.assertEqual(render.render_html(data), render.render_html(copy.deepcopy(data)))

    def test_empty_data_no_crash(self):
        for payload in ({}, {"meta": {}, "summary": {}}):
            html = render.render_html(payload)
            self.assertIsInstance(html, str)
            self.assertIn("暂无数据", html)
            self.assertIn("—", html)

    def test_html_escapes_svg_labels(self):
        data = base_data()
        data["summary"]["by_category"][0]["一级"] = "<b>餐饮</b>"

        html = render.render_html(data)

        self.assertIn("&lt;b&gt;餐饮&lt;/b&gt;", html)
        self.assertNotIn("<b>餐饮</b>", html)

    def test_html_zero_amount_category_no_crash(self):
        """A zero-amount category must not reach a 0/0 bar-width division."""
        data = base_data()
        data["summary"]["by_category"] = [
            {"一级": "其他", "二级": "冲销", "金额": 0}
        ]
        data["summary"]["expense"] = 0.0

        html = render.render_html(data)

        self.assertIsInstance(html, str)
        self.assertIn("其他", html)
        self.assertIn("0.00", html)
        self.assertIn("暂无数据", html)  # 条形图为空 → 占位

    def test_html_negative_net_worth_series(self):
        data = base_data()
        data["net_worth"] = [
            ["2026-11-16", -50000.0],
            ["2026-11-22", -12000.5],
        ]

        html = render.render_html(data)

        self.assertIn("<polyline", html)
        self.assertIn("-12,000.50", html)
        self.assertIn("-5.0万", html)
        self.assertNotIn("nan", html.lower())

    def test_html_escapes_code_liability_and_axis_date(self):
        payload = "<img src=x onerror=alert(1)>"
        data = base_data()
        data["holdings"][0]["代码"] = payload
        data["liabilities"][0]["名称"] = '<script>alert("liab")</script>'
        data["net_worth"][1][0] = "2026-11-20<b>"

        html = render.render_html(data)

        self.assertIn("&lt;img", html)
        self.assertNotIn(payload, html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>alert", html)
        self.assertIn("20&lt;b&gt;", html)  # SVG 横轴日期
        self.assertNotIn("20<b>", html)


class MarkdownRenderTest(unittest.TestCase):
    def test_markdown_totals(self):
        md = render.render_markdown(base_data())

        for number in ("31,500.00", "2,335.30", "29,164.70", "131,164.70"):
            self.assertIn(number, md)
        for text in ("# 财务周报", "收入", "支出", "结余", "期末净资产"):
            self.assertIn(text, md)

    def test_markdown_tables(self):
        md = render.render_markdown(base_data())

        for header in ("支出分类", "收入分类", "净资产", "持仓", "负债"):
            self.assertIn(header, md)
        self.assertIn("| 名称 | 代码 | 份额 | 最新价 | 市值（元） | 浮盈（元） |", md)
        self.assertIn("| 名称 | 剩余本金（元） | 年利率 |", md)
        # 转义后表头与分隔行的列数保持一致，表格不会塌。
        expense_headers = [
            line for line in md.splitlines()
            if line.startswith("| 一级 |") and "占比" in line
        ]
        self.assertEqual(len(expense_headers), 1)
        self.assertEqual(expense_headers[0].count("|"), 5)

    def test_markdown_escapes_pipe_and_newline(self):
        data = base_data()
        data["summary"]["by_category"][0]["一级"] = "餐饮|外卖"
        data["summary"]["by_category"][0]["二级"] = "午餐\n加餐"
        data["holdings"][0]["名称"] = "茅台|A"
        data["holdings"][0]["代码"] = "a\\|b"
        data["liabilities"][0]["名称"] = "房|贷"
        data["meta"]["start"] = "2026-11-16\n# 注入"

        md = render.render_markdown(data)

        self.assertIn("餐饮\\|外卖", md)
        self.assertIn("午餐 加餐", md)
        self.assertIn("茅台\\|A", md)
        self.assertIn("房\\|贷", md)
        # 反斜杠先加倍，原有的 "\|" 才不会让管道符拆列。
        self.assertIn("a\\\\\\|b", md)
        self.assertNotIn("餐饮|外卖", md)
        self.assertNotIn("茅台|A", md)
        self.assertNotIn("\n# 注入", md)

    def test_markdown_empty_series_no_crash(self):
        data = base_data()
        data["net_worth"] = []
        data["summary"]["by_category"] = []
        data["summary"]["income_by_category"] = []
        data["holdings"] = []
        data["liabilities"] = []

        md = render.render_markdown(data)

        self.assertIsInstance(md, str)
        self.assertIn("暂无数据", md)
        self.assertIn("暂无持仓", md)
        self.assertIn("暂无负债", md)
        self.assertIn("31,500.00", md)

    def test_markdown_deterministic(self):
        data = base_data()

        self.assertEqual(render.render_markdown(data), render.render_markdown(data))

    def test_markdown_empty_data_no_crash(self):
        md = render.render_markdown({})

        self.assertIsInstance(md, str)
        self.assertIn("暂无数据", md)

    def test_markdown_month_title(self):
        data = base_data()
        data["meta"]["period"] = "month"

        md = render.render_markdown(data)

        self.assertIn("# 财务月报", md)


if __name__ == "__main__":
    unittest.main()
