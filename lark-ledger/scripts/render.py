"""报表渲染：自包含 HTML 看板与 Markdown 摘要。

输入是 Task 15（report_flow）聚合好的 report dict：

    {"meta": {"period", "start", "end", "generated_at"},
     "summary": {"income", "expense", "net", "by_category",
                 "income_by_category"},
     "net_worth": [[date, total], ...],
     "holdings": [{名称, 代码, 份额, 最新价, 市值, 浮盈}],
     "liabilities": [{名称, 剩余本金, 年利率}]}

设计约束：

- **自包含**：HTML 内联全部 CSS，图表是 Python 拼接的内联 SVG；零 ``<script>``、
  零 http(s) 外链、零 CDN / ``@import`` / ``url()``，离线可读。SVG 刻意不写
  ``xmlns``（HTML5 内联 SVG 不需要），否则会引入 ``http://``。
- **强制转义**：分类名、持仓名、负债名、meta 日期等所有自由文本在进入
  HTML/SVG 前过 :func:`esc`（``html.escape(quote=True)``）。
- **Markdown 表格安全**：``|`` 转义成 ``\\|``、换行折成空格，名称不会撑破表格。
- **确定性**：不读时钟、不生成随机 id；``generated_at`` 只从 ``meta`` 取，
  同样输入必得同样输出。缺失/空的序列渲染占位文案而不是抛错。

本模块不 import ``aggregate``（保持渲染层独立），只做展示格式转换。
"""

from __future__ import annotations

import html
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 浅色财务报告配色：纸面 + 白卡 + 单一强调色；不依赖外部字体。
PAPER = "#f5f6f8"
CARD = "#ffffff"
INK = "#16181d"
MUTED = "#6b7280"
LINE = "#e4e7ec"
TRACK = "#f0f2f5"
GREEN = "#0f9f6e"
RED = "#d64545"
BLUE = "#2f6fdb"
AMBER = "#b7791f"

PLACEHOLDER = "暂无数据"

MONTHS = {
    "week": "财务周报",
    "month": "财务月报",
}

DOCS = (
    "收支口径：仅统计「收入 / 支出」明细；转账（含理财申赎、贷款本金、信用卡还款）不计入。",
    "净资产 = 资产 − 负债，按资产快照日期聚合；报告期末的实时值由 report_flow 追加。",
    "分类名、持仓名、账户名等自由文本均按纯文本转义展示。",
    "个人财务数据，仅供参考，不构成投资建议。",
)


# ------------------------------------------------------------------ formatting


def esc(value: object) -> str:
    """Escape any value for HTML/SVG text and attribute context."""
    return html.escape(str(value), quote=True)


def _as_float(value: object) -> Optional[float]:
    """Best-effort number; bool/non-finite/unparseable returns None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _text(value: object, fallback: str = "—") -> str:
    """Plain text cell: blank/None falls back, never renders literal ``None``."""
    if value is None or isinstance(value, bool):
        return fallback
    text = str(value).strip()
    return text or fallback


def _money(value: object) -> str:
    """Thousands-separated, 2-decimal amount; unknown values render ``—``."""
    number = _as_float(value)
    if number is None:
        return "—"
    rounded = round(number, 2)
    if rounded == 0:
        rounded = 0.0  # 归一化 -0.00
    return "{:,.2f}".format(rounded)


def _signed_money(value: object) -> str:
    """Money with an explicit ``+`` for positive deltas."""
    number = _as_float(value)
    if number is None:
        return "—"
    shown = _money(number)
    if round(number, 2) > 0:
        return "+" + shown
    return shown


def _qty(value: object) -> str:
    """Share count / price: up to 4 decimals, trailing zeros trimmed."""
    number = _as_float(value)
    if number is None:
        return "—"
    rounded = round(number, 4)
    if rounded == 0:
        rounded = 0.0
    shown = "{:,.4f}".format(rounded).rstrip("0").rstrip(".")
    return shown or "0"


def _rate(value: object) -> str:
    """Annual rate stored as a percentage number (4.2 ⇒ ``4.20%``)."""
    number = _as_float(value)
    if number is None:
        return "—"
    rounded = round(number, 2)
    if rounded == 0:
        rounded = 0.0
    return "{:,.2f}%".format(rounded)


def _share(part: object, total: object) -> str:
    """Percentage share of a positive total, else ``—``."""
    numerator = _as_float(part)
    denominator = _as_float(total)
    if numerator is None or denominator is None or denominator <= 0:
        return "—"
    return "%.1f%%" % (numerator / denominator * 100.0)


def _compact(value: object) -> str:
    """Axis label: 13.1万 / 1.20亿 / 860, keeping chart gutters narrow."""
    number = _as_float(value)
    if number is None:
        return "—"
    magnitude = abs(number)
    if magnitude >= 1e8:
        return "%.2f亿" % (number / 1e8)
    if magnitude >= 1e4:
        return "%.1f万" % (number / 1e4)
    return "%.0f" % number


def _clip(value: object, limit: int) -> str:
    """Trim a chart label *before* escaping so an entity is never split."""
    text = _text(value)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _period_title(period: object) -> str:
    return MONTHS.get(_text(period, ""), "财务报告")


def _span(meta: Dict[str, Any], separator: str = " — ") -> str:
    start = _text(meta.get("start"))
    end = _text(meta.get("end"))
    if start == "—" and end == "—":
        return "报告期间"
    if start == end:
        return start
    if start == "—":
        return end
    if end == "—":
        return start
    return "%s%s%s" % (start, separator, end)


# ------------------------------------------------------------------ normalizers


def _net_worth_points(rows: object) -> List[Tuple[str, float]]:
    """``[[date, total], ...]`` (also tolerant of dict rows) → clean points."""
    points: List[Tuple[str, float]] = []
    if not isinstance(rows, (list, tuple)):
        return points
    for row in rows:
        if isinstance(row, dict):
            day = row.get("日期", row.get("date"))
            value = row.get("余额/市值", row.get("value"))
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            day, value = row[0], row[1]
        else:
            continue
        amount = _as_float(value)
        if amount is None:
            continue
        points.append((_text(day), amount))
    return points


def _category_rows(rows: object) -> List[Dict[str, Any]]:
    """Category aggregates → normalised rows sorted by amount then label."""
    cleaned: List[Dict[str, Any]] = []
    if not isinstance(rows, (list, tuple)):
        return cleaned
    for row in rows:
        if not isinstance(row, dict):
            continue
        amount = _as_float(row.get("金额"))
        if amount is None:
            continue
        first = _text(row.get("一级"), "")
        second = _text(row.get("二级"), "")
        if first and second and second != first:
            label = "%s · %s" % (first, second)
        else:
            label = first or second or "未分类"
        cleaned.append({"一级": first, "二级": second, "label": label,
                        "金额": amount})
    cleaned.sort(key=lambda item: (-item["金额"], item["label"]))
    return cleaned


def _holding_rows(rows: object) -> List[Dict[str, Any]]:
    cleaned: List[Dict[str, Any]] = []
    if not isinstance(rows, (list, tuple)):
        return cleaned
    for row in rows:
        if isinstance(row, dict):
            cleaned.append(row)
    return cleaned


def _liability_rows(rows: object) -> List[Dict[str, Any]]:
    return _holding_rows(rows)


# ------------------------------------------------------------------ SVG charts


def _svg_open(width: int, height: int, label: str) -> str:
    """Open an inline SVG. No ``xmlns``: it would add an http:// URL."""
    return (
        '<svg width="%d" height="%d" viewBox="0 0 %d %d" role="img" '
        'aria-label="%s" style="display:block;width:100%%;height:auto">'
        % (width, height, width, height, esc(label))
    )


def _svg_empty(width: int, height: int, label: str) -> str:
    return (
        "%s<text x=\"0\" y=\"18\" font-size=\"12\" fill=\"%s\">%s</text></svg>"
        % (_svg_open(width, height, label), MUTED, esc(PLACEHOLDER))
    )


def _grid(parts: List[str], width: int, height: int, pad_left: int,
          pad_right: int, pad_top: int, pad_bottom: int) -> None:
    plot_h = height - pad_top - pad_bottom
    for row in range(4):
        y = pad_top + plot_h * row / 3.0
        parts.append(
            '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" '
            'stroke-width="1"/>' % (pad_left, y, width - pad_right, y, LINE)
        )


def svg_net_worth_line(points: object, width: int = 760,
                       height: int = 220) -> str:
    """Net-worth trend: area wash + ``<polyline>``; empty → placeholder."""
    series = _net_worth_points(points)
    if not series:
        return _svg_empty(width, height, "净资产走势")

    pad_left, pad_right, pad_top, pad_bottom = 62, 18, 16, 28
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    values = [amount for _day, amount in series]
    low, high = min(values), max(values)

    xs = ([pad_left + plot_w / 2.0] if len(series) == 1 else
          [pad_left + plot_w * index / float(len(series) - 1)
           for index in range(len(series))])
    if high == low:
        ys = [pad_top + plot_h / 2.0] * len(series)
    else:
        ys = [pad_top + plot_h * (1.0 - (amount - low) / (high - low))
              for _day, amount in series]

    parts = [_svg_open(width, height, "净资产走势")]
    _grid(parts, width, height, pad_left, pad_right, pad_top, pad_bottom)

    parts.append('<text x="2" y="%.1f" font-size="10" fill="%s">%s</text>'
                 % (pad_top + 4, MUTED, esc(_compact(high))))
    parts.append('<text x="2" y="%.1f" font-size="10" fill="%s">%s</text>'
                 % (pad_top + plot_h, MUTED, esc(_compact(low))))

    baseline = pad_top + plot_h
    area = " ".join("%.1f,%.1f" % (x, y) for x, y in zip(xs, ys))
    parts.append('<polygon points="%.1f,%.1f %s %.1f,%.1f" fill="%s" '
                 'opacity="0.10"/>'
                 % (xs[0], baseline, area, xs[-1], baseline, BLUE))

    coords = " ".join("%.1f,%.1f" % (x, y) for x, y in zip(xs, ys))
    parts.append('<polyline fill="none" stroke="%s" stroke-width="2" '
                 'stroke-linejoin="round" stroke-linecap="round" '
                 'points="%s"/>' % (BLUE, coords))

    for x, y in zip(xs, ys):
        parts.append('<circle cx="%.1f" cy="%.1f" r="3" fill="%s" '
                     'stroke="%s" stroke-width="1.5"/>' % (x, y, CARD, BLUE))

    step = max(1, len(series) // 6)
    indices = list(range(0, len(series), step))
    if indices[-1] != len(series) - 1:
        indices.append(len(series) - 1)
    for index in indices:
        parts.append(
            '<text x="%.1f" y="%d" font-size="10" fill="%s" '
            'text-anchor="middle">%s</text>'
            % (xs[index], height - 8, MUTED, esc(series[index][0][-5:]))
        )

    parts.append('<text x="%.1f" y="%.1f" font-size="11" fill="%s" '
                 'font-weight="600" text-anchor="end">%s</text>'
                 % (xs[-1], max(12.0, ys[-1] - 9), INK,
                    esc(_money(series[-1][1]))))
    parts.append("</svg>")
    return "".join(parts)


def svg_expense_bars(rows: object, width: int = 760, limit: int = 8) -> str:
    """Horizontal bars for the top expense categories; empty → placeholder."""
    items = [item for item in _category_rows(rows) if item["金额"] > 0][:limit]
    if not items:
        return _svg_empty(width, 64, "支出分类")

    row_h = 30
    height = 18 + row_h * len(items)
    label_w = 170
    value_w = 122

    parts = [_svg_open(width, height, "支出分类")]
    track_x = label_w
    track_w = width - label_w - value_w
    high = max(item["金额"] for item in items)

    for index, item in enumerate(items):
        y = 10 + index * row_h
        bar_h = 12
        bar_y = y + (row_h - bar_h) / 2.0
        parts.append('<text x="0" y="%.1f" font-size="11.5" fill="%s">%s</text>'
                     % (bar_y + bar_h - 1.5, INK,
                        esc(_clip(item["label"], 12))))
        parts.append('<rect x="%d" y="%.1f" width="%d" height="%d" rx="3" '
                     'fill="%s"/>' % (track_x, bar_y, track_w, bar_h, TRACK))
        bar_w = max(2.0, track_w * item["金额"] / high)
        parts.append('<rect x="%d" y="%.1f" width="%.1f" height="%d" rx="3" '
                     'fill="%s" opacity="0.9"/>'
                     % (track_x, bar_y, bar_w, bar_h, BLUE))
        parts.append('<text x="%d" y="%.1f" font-size="11" fill="%s">%s</text>'
                     % (width - value_w + 8, bar_y + bar_h - 1.5, MUTED,
                        esc(_money(item["金额"]))))
    parts.append("</svg>")
    return "".join(parts)


# ------------------------------------------------------------------ HTML shell


STYLE = """
:root{
--paper:__PAPER__;--card:__CARD__;--ink:__INK__;--muted:__MUTED__;
--line:__LINE__;--green:__GREEN__;--red:__RED__;--blue:__BLUE__;--amber:__AMBER__;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
 font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
 "Hiragino Sans GB","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1040px;margin:0 auto;padding:34px 26px 64px}
header{display:flex;align-items:baseline;justify-content:space-between;
 gap:16px;flex-wrap:wrap;border-bottom:1px solid var(--line);padding-bottom:14px}
.brand{font-size:11.5px;font-weight:700;letter-spacing:.12em;color:var(--muted);
 text-transform:uppercase}
.meta{font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
h1{font-size:29px;line-height:1.25;font-weight:700;letter-spacing:-.01em;
 margin:22px 0 4px;font-variant-numeric:tabular-nums}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:22px 0 8px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;
 padding:14px 16px 13px;border-top:3px solid var(--blue);min-height:98px}
.kpi.income{border-top-color:var(--green)}
.kpi.expense{border-top-color:var(--red)}
.kpi.net{border-top-color:var(--amber)}
.kpi .k{font-size:11.5px;color:var(--muted);letter-spacing:.03em}
.kpi .v{font-size:25px;font-weight:700;line-height:1.25;margin-top:7px;
 font-variant-numeric:tabular-nums;word-break:break-all}
.kpi .v small{font-size:11.5px;font-weight:500;color:var(--muted);margin-left:4px}
.kpi .d{font-size:11.5px;color:var(--muted);margin-top:5px;line-height:1.45}
h2{font-size:15px;font-weight:700;margin:30px 0 10px;letter-spacing:.01em}
.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;
 padding:18px 18px 14px;margin:0 0 14px;overflow-x:auto}
.panel .cap{font-size:12px;color:var(--muted);margin-bottom:12px;
 display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);
 white-space:nowrap}
th{font-size:11px;letter-spacing:.05em;color:var(--muted);font-weight:600;
 text-transform:uppercase}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--paper)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.pos{color:var(--green)}
td.neg{color:var(--red)}
.empty{color:var(--muted);font-size:13px;margin:8px 0 4px}
.note{color:var(--muted);font-size:12px;margin:10px 0 0;line-height:1.7}
ul.docs{margin:4px 0 0;padding-left:18px;color:var(--muted);font-size:12px;
 line-height:1.85}
footer{margin-top:30px;padding-top:14px;border-top:1px solid var(--line);
 color:var(--muted);font-size:11.5px;display:flex;justify-content:space-between;
 gap:14px;flex-wrap:wrap}
@media (max-width:760px){.kpis{grid-template-columns:repeat(2,1fr)}
 .wrap{padding:24px 16px 48px}h1{font-size:24px}}
@media print{body{background:#fff}.panel,.kpi{border-color:#ddd;break-inside:avoid}
 .wrap{max-width:none;padding:0}@page{size:A4;margin:12mm}}
"""

for _token, _value in (("__PAPER__", PAPER), ("__CARD__", CARD), ("__INK__", INK),
                       ("__MUTED__", MUTED), ("__LINE__", LINE),
                       ("__GREEN__", GREEN), ("__RED__", RED), ("__BLUE__", BLUE),
                       ("__AMBER__", AMBER)):
    STYLE = STYLE.replace(_token, _value)


def _kpi(label: str, shown: str, note: str, tone: str = "") -> str:
    """KPI card; ``shown``/``note`` are already-escaped display strings."""
    return (
        '<div class="kpi%s"><div class="k">%s</div>'
        '<div class="v">%s<small>元</small></div><div class="d">%s</div></div>'
        % (" " + tone if tone else "", esc(label), shown, note or "&nbsp;")
    )


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]],
           numeric_from: int = 1) -> str:
    """Table of pre-escaped cell HTML. Callers escape every dynamic cell."""
    head = "".join(
        "<th%s>%s</th>" % (' class="num"' if index >= numeric_from else "",
                           esc(name))
        for index, name in enumerate(headers)
    )
    body = []
    for row in rows:
        cells = "".join(
            '<td%s>%s</td>' % (' class="num"' if index >= numeric_from else "", cell)
            for index, cell in enumerate(row)
        )
        body.append("<tr>%s</tr>" % cells)
    return ("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
            % (head, "".join(body)))


def _panel(title: str, body: str, cap: str = "") -> str:
    caption = ('<div class="cap"><span>%s</span><span>%s</span></div>'
               % (esc(title), esc(cap)) if cap else
               '<div class="cap"><span>%s</span></div>' % esc(title))
    return '<div class="panel">%s%s</div>' % (caption, body)


def _category_table(rows: Sequence[Dict[str, Any]], total: Optional[float],
                    include_share: bool) -> str:
    if not rows:
        return '<p class="empty">%s</p>' % esc(PLACEHOLDER)
    body = []
    for row in rows:
        cells = [
            esc(_text(row["一级"], "—")),
            esc(_text(row["二级"], "—")),
            esc(_money(row["金额"])),
        ]
        if include_share:
            cells.append(esc(_share(row["金额"], total)))
        body.append(cells)
    headers = ["一级", "二级", "金额（元）"] + (
        ["占比"] if include_share else []
    )
    return _table(headers, body, numeric_from=2)


def _holdings_table(rows: Sequence[Dict[str, Any]]) -> str:
    if not rows:
        return '<p class="empty">暂无持仓</p>'
    body = []
    for row in rows:
        pnl = _as_float(row.get("浮盈"))
        pnl_class = ""
        if pnl is not None and pnl > 0:
            pnl_class = " pos"
        elif pnl is not None and pnl < 0:
            pnl_class = " neg"
        body.append([
            esc(_text(row.get("名称"), "未命名持仓")),
            esc(_text(row.get("代码"), "—")),
            esc(_qty(row.get("份额"))),
            esc(_qty(row.get("最新价"))),
            esc(_money(row.get("市值"))),
            '<span class="num%s">%s</span>' % (pnl_class, esc(_money(pnl))),
        ])
    return _table(["名称", "代码", "份额", "最新价", "市值（元）", "浮盈（元）"],
                  body, numeric_from=2)


def _liabilities_table(rows: Sequence[Dict[str, Any]]) -> str:
    if not rows:
        return '<p class="empty">暂无负债</p>'
    body = [
        [
            esc(_text(row.get("名称"), "未命名负债")),
            esc(_money(row.get("剩余本金"))),
            esc(_rate(row.get("年利率"))),
        ]
        for row in rows
    ]
    return _table(["名称", "剩余本金（元）", "年利率"], body, numeric_from=1)


def _net_worth_panel(points: Sequence[Tuple[str, float]]) -> str:
    chart = svg_net_worth_line(points)
    if not points:
        cap = "报告期尚无资产快照"
    else:
        cap = "共 %d 个快照点 · 期末 %s 元" % (len(points), _money(points[-1][1]))
    return ('<h2>净资产走势</h2>'
            '<div class="panel"><div class="cap"><span>资产 − 负债</span>'
            "<span>%s</span></div>%s</div>" % (esc(cap), chart))


def _docs_panel() -> str:
    items = "".join("<li>%s</li>" % esc(line) for line in DOCS)
    return '<h2>口径说明</h2><div class="panel"><ul class="docs">%s</ul></div>' % items


# --------------------------------------------------------------------- render


def render_html(data: dict) -> str:
    """Render an aggregated report as one self-contained HTML file."""
    data = data if isinstance(data, dict) else {}
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}

    title = _period_title(meta.get("period"))
    span = _span(meta)
    generated_at = _text(meta.get("generated_at"), "—")

    income = _as_float(summary.get("income"))
    expense = _as_float(summary.get("expense"))
    net = _as_float(summary.get("net"))
    points = _net_worth_points(data.get("net_worth"))
    current = points[-1][1] if points else None

    expense_rows = _category_rows(summary.get("by_category"))
    income_rows = _category_rows(summary.get("income_by_category"))
    holdings = _holding_rows(data.get("holdings"))
    liabilities = _liability_rows(data.get("liabilities"))

    delta_note = "报告期末"
    if len(points) >= 2:
        delta_note = "较期初 %s 元" % _signed_money(points[-1][1] - points[0][1])

    kpis = "".join([
        _kpi("收入", esc(_money(income)), "统计区间内已入账收入", "income"),
        _kpi("支出", esc(_money(expense)), "已排除转账与贷款本金", "expense"),
        _kpi("结余", esc(_money(net)), "收入 − 支出", "net"),
        _kpi("当前净资产", esc(_money(current)), esc(delta_note)),
    ])

    body = [
        "<header>",
        '<div class="brand">lark-ledger · 财务账本</div>',
        '<div class="meta">生成于 %s</div>' % esc(generated_at),
        "</header>",
        "<h1>%s：%s</h1>" % (esc(title), esc(span)),
        '<div class="kpis">%s</div>' % kpis,
        _net_worth_panel(points),
        "<h2>支出分析</h2>",
        _panel("支出分类（按金额降序）", svg_expense_bars(expense_rows),
               "单位：元 · 最多 8 项"),
        _panel("支出分类明细", _category_table(expense_rows, expense, True),
               "占比按支出合计"),
        "<h2>收入分析</h2>",
        _panel("收入分类明细", _category_table(income_rows, income, False),
               "负收入直接冲减合计"),
        "<h2>持仓</h2>",
        _panel("当前持仓", _holdings_table(holdings), "份额 × 最新价 = 市值"),
        "<h2>负债</h2>",
        _panel("负债概览", _liabilities_table(liabilities), "剩余本金取绝对值"),
        _docs_panel(),
        '<footer><span>由 lark-ledger 生成 · 数据来自飞书多维表格</span>'
        "<span>%s</span></footer>" % esc(generated_at),
    ]

    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>%s · %s</title><style>%s</style></head><body>"
        '<div class="wrap">%s</div></body></html>'
        % (esc(title), esc(span), STYLE, "".join(body))
    )


# ------------------------------------------------------------------- markdown


def _md_cell(value: object) -> str:
    """Escape a value for a Markdown table cell (pipes and newlines)."""
    text = "—" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return text.replace("|", "\\|")


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> List[str]:
    lines = [
        "| " + " | ".join(_md_cell(header) for header in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_md_cell(cell) for cell in row) + " |")
    return lines


def render_markdown(data: dict) -> str:
    """Render a compact Chinese Markdown summary, safe to paste into Feishu."""
    data = data if isinstance(data, dict) else {}
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}

    title = _period_title(meta.get("period"))
    span = _span(meta, " ~ ")
    generated_at = _text(meta.get("generated_at"), "—")

    income = _as_float(summary.get("income"))
    expense = _as_float(summary.get("expense"))
    net = _as_float(summary.get("net"))
    points = _net_worth_points(data.get("net_worth"))
    current = points[-1][1] if points else None
    expense_rows = _category_rows(summary.get("by_category"))
    income_rows = _category_rows(summary.get("income_by_category"))
    holdings = _holding_rows(data.get("holdings"))
    liabilities = _liability_rows(data.get("liabilities"))

    lines: List[str] = ["# %s：%s" % (_md_cell(title), _md_cell(span)), ""]
    lines.append("> 生成于 %s" % _md_cell(generated_at))
    lines.append("")

    lines.append("## 核心指标")
    lines.append("")
    lines.extend(_md_table(
        ["指标", "金额（元）"],
        [
            ["收入", _money(income)],
            ["支出", _money(expense)],
            ["结余", _money(net)],
            ["当前净资产", _money(current)],
        ],
    ))
    lines.append("")

    lines.append("## 支出分类")
    lines.append("")
    if expense_rows:
        lines.extend(_md_table(
            ["一级", "二级", "金额（元）", "占比"],
            [[_text(row["一级"], "—"), _text(row["二级"], "—"),
              _money(row["金额"]), _share(row["金额"], expense)] for row in expense_rows],
        ))
    else:
        lines.append(PLACEHOLDER)
    lines.append("")

    lines.append("## 收入分类")
    lines.append("")
    if income_rows:
        lines.extend(_md_table(
            ["一级", "二级", "金额（元）"],
            [[_text(row["一级"], "—"), _text(row["二级"], "—"),
              _money(row["金额"])] for row in income_rows],
        ))
    else:
        lines.append(PLACEHOLDER)
    lines.append("")

    lines.append("## 净资产走势")
    lines.append("")
    if points:
        lines.extend(_md_table(
            ["日期", "净资产（元）"],
            [[day, _money(amount)] for day, amount in points],
        ))
    else:
        lines.append(PLACEHOLDER)
    lines.append("")

    lines.append("## 持仓")
    lines.append("")
    if holdings:
        lines.extend(_md_table(
            ["名称", "代码", "份额", "最新价", "市值（元）", "浮盈（元）"],
            [[_text(row.get("名称"), "未命名持仓"), _text(row.get("代码"), "—"),
              _qty(row.get("份额")), _qty(row.get("最新价")),
              _money(row.get("市值")), _money(row.get("浮盈"))]
             for row in holdings],
        ))
    else:
        lines.append("暂无持仓")
    lines.append("")

    lines.append("## 负债")
    lines.append("")
    if liabilities:
        lines.extend(_md_table(
            ["名称", "剩余本金（元）", "年利率"],
            [[_text(row.get("名称"), "未命名负债"),
              _money(row.get("剩余本金")), _rate(row.get("年利率"))]
             for row in liabilities],
        ))
    else:
        lines.append("暂无负债")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("> 由 lark-ledger 脚本生成；数字来自飞书多维表格聚合，"
                 "解读由 agent 补充。")
    lines.append("")
    return "\n".join(lines)