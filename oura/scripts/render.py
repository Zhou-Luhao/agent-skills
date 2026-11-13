"""Rendering: escaping helpers, inline SVG charts, HTML dashboard and Markdown.

Charts are generated as strings so a report stays a single self-contained file:
no chart library, no CDN, no <script>. Every dynamic value that reaches the
output — including SVG text nodes and attribute values — goes through ``esc``.

Visual language: dark data dashboard. Dark navy surface, panel cards, one colour
per metric family, score pills that read their own severity.
"""
from __future__ import annotations

import html
from typing import Dict, List, Optional, Sequence, Tuple

from aggregate import SUMMARY_LABELS

# Dark dashboard palette.
BG = "#0a0d14"
PANEL = "#11151f"
PANEL_2 = "#161b28"
BORDER = "#1f2637"
INK = "#e8ecf3"
MUTED = "#7d8798"
SLEEP = "#4d8df6"
READINESS = "#38d39f"
ACTIVITY = "#a78bfa"
HR = "#f0705a"
SPO2 = "#38bdf8"
CARDIO = "#f5a524"

# Hypnogram stages: 1=deep, 2=light, 3=REM, 4=awake.
PHASE_COLORS = {
    "1": "#3b82f6",
    "2": "#64748b",
    "3": "#a78bfa",
    "4": "#f5a524",
}
PHASE_LABELS = {"1": "深睡", "2": "浅睡", "3": "REM", "4": "清醒"}
PHASE_LANES = {"1": 0, "2": 1, "3": 2, "4": 3}
LANE_LABELS = ["深睡", "浅睡", "REM", "清醒"]
PHASE_KEYS = ["deep", "light", "rem", "awake"]
UNKNOWN_PHASE_COLOR = "#2a3244"

DURATION_SUMMARY_KEYS = {"sleep_duration_avg"}


def esc(value: object) -> str:
    """Escape any value for HTML/SVG text and attribute context."""
    return html.escape(str(value), quote=True)


def render_label(key: str) -> str:
    """Human-facing label for an internal summary key."""
    return SUMMARY_LABELS.get(key, key)


def summary_value(key: str, value: object) -> str:
    """Format a summary number for display: durations are seconds internally."""
    if value is None:
        return "—"
    if key in DURATION_SUMMARY_KEYS:
        return fmt_duration(value)
    return str(value)


def fmt_duration(seconds: object) -> str:
    """Seconds to a human-readable Chinese duration. Aggregation keeps seconds."""
    if seconds is None:
        return "—"
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "—"
    if total < 0:
        return "—"
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return "%d 小时 %d 分" % (hours, minutes)
    if hours:
        return "%d 小时" % hours
    return "%d 分" % minutes


def score_tone(value: object) -> str:
    """Colour band for a 0-100 score."""
    try:
        score = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return MUTED
    if score >= 85:
        return READINESS
    if score >= 70:
        return SLEEP
    if score >= 60:
        return "#f5c451"
    return HR


def _svg_open(width: int, height: int, label: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" role="img" aria-label="%s" '
        'style="display:block;width:100%%;height:auto">'
        % (width, height, width, height, esc(label))
    )


def _grid(parts: List[str], width: int, height: int,
          pad_left: int, pad_right: int, pad_top: int, pad_bottom: int) -> None:
    plot_h = height - pad_top - pad_bottom
    for row in range(4):
        y = pad_top + plot_h * row / 3.0
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>'
                     % (pad_left, y, width - pad_right, y, BORDER))


# ------------------------------------------------------------------ SVG charts


def svg_stacked_phases(phases: str, width: int = 760, height: int = 10) -> str:
    """Compact hypnogram strip: one rect per 5-minute phase character."""
    parts = [_svg_open(width, height, "睡眠分期")]
    text = str(phases or "")
    if not text:
        parts.append('<text x="0" y="8" font-size="10" fill="%s">无分期数据</text>' % MUTED)
        parts.append("</svg>")
        return "".join(parts)
    step = width / float(len(text))
    for index, char in enumerate(text):
        color = PHASE_COLORS.get(char, UNKNOWN_PHASE_COLOR)
        parts.append('<rect x="%.2f" y="0" width="%.2f" height="%d" fill="%s"/>'
                     % (index * step, step + 0.4, height, color))
    parts.append("</svg>")
    return "".join(parts)


def svg_phase_legend() -> str:
    items = "".join(
        '<span class="sw"><i style="background:%s"></i>%s</span>'
        % (PHASE_COLORS[key], PHASE_LABELS[key]) for key in ("1", "2", "3", "4"))
    return '<div class="legend">%s</div>' % items


def _minutes_since_midnight(stamp: object) -> Optional[int]:
    from aggregate import _local_tz, _parse_iso
    moment = _parse_iso(stamp)
    if moment is None:
        return None
    local = moment.astimezone(_local_tz())
    return local.hour * 60 + local.minute


def svg_hypnogram_lanes(phases: str, bedtime_start: object, step_minutes: int = 5,
                        width: int = 760, height: int = 150) -> str:
    """Lane hypnogram: one row per stage, x axis is clock time."""
    text = str(phases or "")
    start = _minutes_since_midnight(bedtime_start)
    if not text or start is None:
        return svg_stacked_phases(text, width=width, height=12)

    lane_h = (height - 30) / 4.0
    total_minutes = len(text) * step_minutes
    parts = [_svg_open(width, height, "睡眠分期")]

    for lane in range(4):
        y = lane * lane_h
        parts.append('<rect x="0" y="%.1f" width="%d" height="%.1f" fill="%s" rx="3"/>'
                     % (y, width, lane_h - 3, PANEL_2))
        parts.append('<text x="4" y="%.1f" font-size="9.5" fill="%s">%s</text>'
                     % (y + lane_h - 7, MUTED, LANE_LABELS[lane]))

    pad_left = 34.0
    usable = width - pad_left

    def x_at(minute: float) -> float:
        return pad_left + usable * (minute / float(total_minutes)) if total_minutes else pad_left

    index = 0
    while index < len(text):
        char = text[index]
        if char not in PHASE_LANES:
            index += 1
            continue
        run_end = index
        while run_end < len(text) and text[run_end] == char:
            run_end += 1
        x1 = x_at(index * step_minutes)
        x2 = x_at(run_end * step_minutes)
        lane = PHASE_LANES[char]
        y = lane * lane_h
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" rx="2"/>'
                     % (x1, y + 4, max(1.5, x2 - x1), lane_h - 10, PHASE_COLORS[char]))
        index = run_end

    axis_y = height - 12
    for ticks in range(5):
        minute = total_minutes * ticks / 4.0
        clock = (start + int(minute)) % (24 * 60)
        parts.append('<text x="%.1f" y="%d" font-size="9" fill="%s" text-anchor="middle">'
                     '%02d:%02d</text>' % (x_at(minute), axis_y, MUTED, clock // 60, clock % 60))
    parts.append("</svg>")
    return "".join(parts)


def svg_stage_columns(stages: Sequence[Tuple[str, float]], width: int = 360,
                      height: int = 190) -> str:
    """Sleep-stage composition as one stacked column."""
    parts = [_svg_open(width, height, "睡眠结构")]
    total = sum(max(0.0, float(v or 0)) for _k, v in stages) or 1.0
    bar_w = width - 28.0
    y, plot_h = 6.0, height - 16
    color_map = dict(zip([label for label in ("深睡", "浅睡", "REM", "清醒")],
                         [PHASE_COLORS[k] for k in ("1", "2", "3", "4")]))
    for label, value in stages:
        share = max(0.0, float(value or 0)) / total
        seg_h = plot_h * share
        parts.append('<rect x="14" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>'
                     % (y, bar_w, max(0.0, seg_h - 1), color_map.get(label, UNKNOWN_PHASE_COLOR)))
        if seg_h > 18:
            parts.append('<text x="22" y="%.1f" font-size="10" fill="#ffffff">%s %s</text>'
                         % (y + seg_h / 2 + 3.5, esc(label), esc(fmt_duration(value))))
        y += seg_h
    parts.append("</svg>")
    return "".join(parts)


def svg_multi_columns(rows: Sequence[dict], series: Sequence[Tuple[str, str, str]],
                      width: int = 760, height: int = 210, threshold: Optional[float] = None,
                      label: str = "趋势") -> str:
    """Grouped columns: one group per day, one bar per series."""
    parts = [_svg_open(width, height, label)]
    pad_left, pad_right, pad_top, pad_bottom = 32, 10, 12, 26
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    _grid(parts, width, height, pad_left, pad_right, pad_top, pad_bottom)

    if not rows:
        parts.append('<text x="%d" y="%d" font-size="11" fill="%s">无数据</text>'
                     % (pad_left, pad_top + plot_h / 2, MUTED))
        parts.append("</svg>")
        return "".join(parts)

    values = [float(r.get(key) or 0) for r in rows for _label, key, _color in series]
    high = max(values + ([threshold] if threshold else [0])) or 1.0
    group_w = plot_w / float(len(rows))
    bars_total = len(series)
    bar_w = max(2.0, group_w * 0.62 / bars_total)

    for index, row in enumerate(rows):
        base_x = pad_left + group_w * index + group_w * 0.19
        for series_index, (_label, key, color) in enumerate(series):
            value = float(row.get(key) or 0)
            bar_h = plot_h * (value / high)
            x = base_x + series_index * bar_w
            y = pad_top + plot_h - bar_h
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" '
                         'rx="2" opacity="0.92"/>' % (x, y, max(1.0, bar_w - 1.5),
                                                      max(0.0, bar_h), color))
        parts.append('<text x="%.1f" y="%d" font-size="9" fill="%s" text-anchor="middle">%s</text>'
                     % (pad_left + group_w * index + group_w / 2.0, height - 8, MUTED,
                        esc(str(row.get("day", ""))[5:])))

    if threshold:
        ty = pad_top + plot_h * (1 - threshold / high)
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1" '
                     'stroke-dasharray="4 4" opacity="0.65"/>'
                     % (pad_left, ty, width - pad_right, ty, MUTED))
    parts.append("</svg>")
    return "".join(parts)


def svg_stacked_day_columns(rows: Sequence[dict], width: int = 760,
                            height: int = 200) -> str:
    """Per-day stacked sleep-stage columns (deep/light/REM/awake)."""
    parts = [_svg_open(width, height, "睡眠结构")]
    pad_left, pad_right, pad_top, pad_bottom = 32, 10, 12, 26
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    _grid(parts, width, height, pad_left, pad_right, pad_top, pad_bottom)

    series = [(label, key, PHASE_COLORS[phase])
              for label, key, phase in zip(LANE_LABELS, PHASE_KEYS, ("1", "2", "3", "4"))]
    totals = [sum(float(r.get(key) or 0) for _l, key, _c in series) for r in rows]
    high = max(totals + [1.0])
    slot = plot_w / float(len(rows) or 1)
    bar_w = max(3.0, slot * 0.4)

    for index, row in enumerate(rows):
        x = pad_left + slot * index + (slot - bar_w) / 2.0
        y = pad_top + plot_h
        for _label, key, color in series:
            value = float(row.get(key) or 0)
            seg_h = plot_h * (value / high)
            y -= seg_h
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s"/>'
                         % (x, y, bar_w, max(0.0, seg_h - 0.8), color))
        parts.append('<text x="%.1f" y="%d" font-size="9" fill="%s" text-anchor="middle">%s</text>'
                     % (x + bar_w / 2.0, height - 8, MUTED, esc(str(row.get("day", ""))[5:])))
    parts.append("</svg>")
    return "".join(parts)


def svg_line(points: Sequence[Tuple[str, float]], width: int = 760, height: int = 200,
             label: str = "趋势", color: str = HR) -> str:
    """Single-series line chart."""
    parts = [_svg_open(width, height, label)]
    pad_left, pad_right, pad_top, pad_bottom = 32, 10, 12, 24
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    _grid(parts, width, height, pad_left, pad_right, pad_top, pad_bottom)

    values = [(str(name), float(value)) for name, value in points]
    if not values:
        parts.append('<text x="%d" y="%d" font-size="11" fill="%s">无数据</text>'
                     % (pad_left, pad_top + plot_h / 2, MUTED))
        parts.append("</svg>")
        return "".join(parts)

    low = min(v for _n, v in values)
    high = max(v for _n, v in values)
    span = (high - low) or 1.0
    xs = ([pad_left + plot_w / 2.0] if len(values) == 1 else
          [pad_left + plot_w * i / float(len(values) - 1) for i in range(len(values))])
    ys = [pad_top + plot_h * (1 - (v - low) / span) for _n, v in values]

    coords = " ".join("%.1f,%.1f" % (x, y) for x, y in zip(xs, ys))
    parts.append('<polyline fill="none" stroke="%s" stroke-width="1.6" '
                 'stroke-linejoin="round" stroke-linecap="round" points="%s"/>' % (color, coords))
    parts.append('<text x="2" y="%.1f" font-size="9" fill="%s">%.0f</text>'
                 % (pad_top + 4, MUTED, high))
    parts.append('<text x="2" y="%.1f" font-size="9" fill="%s">%.0f</text>'
                 % (pad_top + plot_h, MUTED, low))
    step = max(1, len(values) // 6)
    for index in range(0, len(values), step):
        parts.append('<text x="%.1f" y="%d" font-size="9" fill="%s" text-anchor="middle">%s</text>'
                     % (xs[index], height - 6, MUTED, esc(str(values[index][0])[-5:])))
    parts.append("</svg>")
    return "".join(parts)


def svg_bars(values: Sequence[Tuple[str, float]], width: int = 760, height: int = 200,
             label: str = "柱状图", color: str = ACTIVITY) -> str:
    """Vertical bars, scaled to the largest value."""
    parts = [_svg_open(width, height, label)]
    pad_left, pad_right, pad_top, pad_bottom = 32, 10, 12, 24
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    _grid(parts, width, height, pad_left, pad_right, pad_top, pad_bottom)

    series = [(str(name), float(value)) for name, value in values]
    if not series:
        parts.append('<text x="%d" y="%d" font-size="11" fill="%s">无数据</text>'
                     % (pad_left, pad_top + plot_h / 2, MUTED))
        parts.append("</svg>")
        return "".join(parts)

    high = max(v for _n, v in series) or 1.0
    slot = plot_w / float(len(series))
    bar_w = max(3.0, slot * 0.42)
    for index, (_name, value) in enumerate(series):
        bar_h = plot_h * (value / high)
        x = pad_left + slot * index + (slot - bar_w) / 2.0
        y = pad_top + plot_h - bar_h
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" rx="2" '
                     'opacity="0.9"/>' % (x, y, bar_w, bar_h, color))
    step = max(1, len(series) // 8)
    for index in range(0, len(series), step):
        parts.append('<text x="%.1f" y="%d" font-size="9" fill="%s" text-anchor="middle">%s</text>'
                     % (pad_left + slot * index + slot / 2.0, height - 6, MUTED,
                        esc(series[index][0][-5:])))
    parts.append("</svg>")
    return "".join(parts)


def svg_gantt(bars: Sequence[Tuple[str, int, int]], width: int = 760, height: int = 130,
              label: str = "睡眠时段") -> str:
    """Horizontal gantt of sleep windows; x axis is clock time (0-24h)."""
    parts = [_svg_open(width, height, label)]
    if not bars:
        parts.append('<text x="0" y="14" font-size="11" fill="%s">无数据</text>' % MUTED)
        parts.append("</svg>")
        return "".join(parts)

    row_h = (height - 18) / float(len(bars))
    for tick in range(0, 25, 4):
        x = width * tick / 24.0
        parts.append('<line x1="%.1f" y1="0" x2="%.1f" y2="%d" stroke="%s" stroke-width="1"/>'
                     % (x, x, height - 14, BORDER))
        parts.append('<text x="%.1f" y="%d" font-size="9" fill="%s" text-anchor="middle">'
                     '%02d:00</text>' % (x, height - 4, MUTED, tick))

    for index, (_day, start_minute, end_minute) in enumerate(bars):
        y = index * row_h
        box_h = row_h - 4
        if end_minute >= start_minute:
            segments = [(start_minute, end_minute)]
        else:
            # Sleep that crosses midnight: draw it as two pieces so the bar
            # never runs off the right edge of a 0-24h axis.
            segments = [(start_minute, 1440), (0, end_minute)]
        for seg_start, seg_end in segments:
            x1 = width * (seg_start / 1440.0)
            x2 = width * (seg_end / 1440.0)
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="%s" '
                         'rx="3" opacity="0.85"/>'
                         % (x1, y + 1, max(2.0, x2 - x1), box_h, SLEEP))
    parts.append("</svg>")
    return "".join(parts)


def svg_hbars(items: Sequence[Tuple[str, float]], width: int = 760) -> str:
    """Horizontal contributor bars, colour-banded by score."""
    if not items:
        return ""
    rows = []
    for name, value in items:
        try:
            pct = max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError):
            continue
        rows.append(
            '<div class="hbar"><span class="lbl">%s</span>'
            '<span class="track"><i style="width:%.1f%%;background:%s"></i></span>'
            '<span class="num">%s</span></div>'
            % (esc(name), pct, score_tone(pct), esc(value)))
    return '<div class="hbars">%s</div>' % "".join(rows)


# ------------------------------------------------------------------ HTML shell

STYLE = """
:root{
--bg:__BG__;--panel:__PANEL__;--panel2:__PANEL_2__;--border:__BORDER__;
--ink:__INK__;--muted:__MUTED__;--sleep:__SLEEP__;--readiness:__READINESS__;
--activity:__ACTIVITY__;--hr:__HR__;--spo2:__SPO2__;--cardio:__CARDIO__;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:13.5px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
 "Hiragino Sans GB","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
.top{display:flex;align-items:center;justify-content:space-between;gap:16px;
 padding:14px 26px;border-bottom:1px solid var(--border);background:var(--panel);
 position:sticky;top:0;z-index:5}
.brand{display:flex;align-items:center;gap:9px;font-weight:600;letter-spacing:.04em;font-size:13px}
.brand .dot{width:15px;height:15px;border-radius:50%;border:2px solid var(--sleep);
 display:inline-block}
.brand span.pill{font-weight:400;color:var(--muted);font-size:12px;letter-spacing:0}
nav{display:flex;gap:18px;font-size:12px;color:var(--muted)}
nav a{color:inherit;text-decoration:none}
nav a:hover{color:var(--ink)}
.wrap{max-width:1180px;margin:0 auto;padding:26px 26px 60px}
h1{font-size:30px;line-height:1.2;font-weight:700;letter-spacing:-.01em;margin:14px 0 8px}
.meta{color:var(--muted);font-size:12.5px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 0}
.chip{font-size:11.5px;color:var(--muted);background:var(--panel);
 border:1px solid var(--border);border-radius:999px;padding:4px 11px;display:inline-flex;
 align-items:center;gap:6px}
.chip b{color:var(--ink);font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin:22px 0 6px}
.kpi{background:var(--panel);border:1px solid var(--border);border-radius:14px;
 padding:14px 15px;position:relative;overflow:hidden;min-height:104px}
.kpi::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent-sleep)}
.kpi.k-sleep::before{background:var(--sleep)}
.kpi.k-read::before{background:var(--readiness)}
.kpi.k-act::before{background:var(--activity)}
.kpi.k-hr::before{background:var(--hr)}
.kpi.k-spo2::before{background:var(--spo2)}
.kpi.k-age::before{background:var(--cardio)}
.kpi .k{font-size:11px;color:var(--muted);letter-spacing:.02em}
.kpi .v{font-size:29px;font-weight:700;line-height:1.15;margin-top:6px;
 font-variant-numeric:tabular-nums}
.kpi .v small{font-size:11.5px;font-weight:500;color:var(--muted);margin-left:3px}
.kpi .d{font-size:11px;color:var(--muted);margin-top:5px;line-height:1.45}
.kpi .d b{font-weight:600}
h2{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
 font-weight:600;margin:30px 0 12px}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:14px;
 padding:16px 18px;margin:0 0 14px}
.panel .cap{font-size:11.5px;color:var(--muted);margin-bottom:10px;display:flex;
 justify-content:space-between;gap:10px;flex-wrap:wrap}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.kv{display:grid;grid-template-columns:repeat(4,1fr);gap:14px 18px}
.kv .item .k{font-size:11px;color:var(--muted);letter-spacing:.02em}
.kv .item .v{font-size:16px;font-weight:600;margin-top:3px;font-variant-numeric:tabular-nums}
.kv .item .v small{font-size:11px;font-weight:400;color:var(--muted);margin-left:3px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th,td{text-align:left;padding:9px 8px;border-bottom:1px solid var(--border);white-space:nowrap}
th{font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
 font-weight:600}
tbody tr:hover{background:var(--panel2)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;min-width:34px;text-align:center;border-radius:6px;
 padding:2px 8px;font-weight:600;font-size:12px;color:#0a0d14}
.na{color:var(--muted);font-size:12.5px;margin:6px 0}
.legend{display:flex;gap:16px;font-size:11.5px;color:var(--muted);margin-top:10px}
.sw{display:inline-flex;align-items:center;gap:5px}
.sw i{width:9px;height:9px;border-radius:2px;display:inline-block}
.hbars{display:grid;gap:7px}
.hbar{display:grid;grid-template-columns:120px 1fr 42px;align-items:center;gap:10px;
 font-size:12px}
.hbar .lbl{color:var(--muted)}
.hbar .track{height:6px;background:var(--panel2);border-radius:4px;overflow:hidden}
.hbar .track i{display:block;height:100%;border-radius:4px}
.hbar .num{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.note{border-left:2px solid var(--cardio);background:var(--panel2);border-radius:8px;
 padding:10px 14px;margin:10px 0;font-size:12.5px;color:var(--ink)}
ul.docs{margin:6px 0 0;padding-left:18px;font-size:12px;color:var(--muted);line-height:1.9}
footer{margin-top:34px;padding-top:14px;border-top:1px solid var(--border);
 font-size:11px;color:var(--muted);display:flex;justify-content:space-between;
 gap:14px;flex-wrap:wrap}
@media (max-width:1000px){.kpis{grid-template-columns:repeat(3,1fr)}
 .kv{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}}
@media print{body{background:#fff;color:#111}.top{display:none}
 .panel,.kpi{border-color:#ddd;background:#fff;break-inside:avoid}
 h1{color:#111}.wrap{max-width:none;padding:0}nav{display:none}
 @page{size:A4 landscape;margin:12mm}}
"""
for _token, _value in (("__BG__", BG), ("__PANEL_2__", PANEL_2), ("__PANEL__", PANEL),
                       ("__BORDER__", BORDER), ("__INK__", INK), ("__MUTED__", MUTED),
                       ("__SLEEP__", SLEEP), ("__READINESS__", READINESS),
                       ("__ACTIVITY__", ACTIVITY), ("__HR__", HR), ("__SPO2__", SPO2),
                       ("__CARDIO__", CARDIO)):
    STYLE = STYLE.replace(_token, _value)


def _kpi(label: str, value: object, unit: str = "", note: str = "",
         tone: str = "sleep") -> str:
    if value is None:
        shown = "—"
    elif isinstance(value, float):
        shown = esc(round(value, 1))
    else:
        shown = esc(value)
    unit_html = "<small>%s</small>" % esc(unit) if unit else ""
    return ('<div class="kpi k-%s"><div class="k">%s</div><div class="v">%s%s</div>'
            '<div class="d">%s</div></div>'
            % (tone, esc(label), shown, unit_html, note or "&nbsp;"))


def _score_note(current: object, previous: object, label: str = "较前一日") -> str:
    try:
        delta = float(current) - float(previous)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    sign = "+" if delta > 0 else ""
    color = READINESS if delta > 0 else (HR if delta < 0 else MUTED)
    return ('<b style="color:%s">%s%s</b> %s' % (color, sign, round(delta), esc(label)))


def _kv_item(label: str, value: object, unit: str = "") -> str:
    shown = "—" if value is None else esc(value)
    suffix = "<small>%s</small>" % esc(unit) if unit else ""
    return ('<div class="item"><div class="k">%s</div><div class="v">%s%s</div></div>'
            % (esc(label), shown, suffix))


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]],
           numeric_from: int = 1) -> str:
    head = "".join('<th%s>%s</th>' % (' class="num"' if index >= numeric_from else "",
                                      esc(name))
                   for index, name in enumerate(headers))
    body = []
    for row in rows:
        cells = "".join('<td%s>%s</td>' % (' class="num"' if index >= numeric_from else "",
                                           cell)
                        for index, cell in enumerate(row))
        body.append("<tr>%s</tr>" % cells)
    return ("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
            % (head, "".join(body)))


def _score_pill(value: object) -> str:
    if value is None:
        return '<span class="na">—</span>'
    return ('<span class="pill" style="background:%s">%s</span>'
            % (score_tone(value), esc(value)))


def _na(text: str = "无数据") -> str:
    return '<p class="na">%s</p>' % esc(text)


# ------------------------------------------------------------------- sections


def _kpi_grid(report: dict) -> str:
    scores = report.get("scores") or {}
    previous = (report.get("previous") or {}).get("scores") or {}
    main = report.get("sleep_main") or {}
    spo2 = report.get("spo2") or {}
    cardio = report.get("cardio_age") or {}
    activity = scores.get("activity") or {}

    sleep_v = (scores.get("sleep") or {}).get("value")
    readiness_v = (scores.get("readiness") or {}).get("value")
    activity_v = activity.get("value")

    if report.get("period") == "daily":
        sleep_note = _score_note(sleep_v, previous.get("sleep")) or "—"
        readiness_note = _score_note(readiness_v, previous.get("readiness")) or "—"
        activity_note = _score_note(activity_v, previous.get("activity")) or "—"
    else:
        sleep_note = "周期均值"
        readiness_note = "周期均值"
        activity_note = "周期均值"

    steps = activity.get("steps")
    hr_note = ("日均 %s 步" % esc(steps)) if steps is not None else "—"

    return '<div class="kpis">%s</div>' % "".join([
        _kpi("睡眠评分", sleep_v, "/100", sleep_note, "sleep"),
        _kpi("准备度", readiness_v, "/100", readiness_note, "read"),
        _kpi("活动评分", activity_v, "/100", activity_note, "act"),
        _kpi("夜间最低心率", main.get("lowest_hr"), "bpm", hr_note, "hr"),
        _kpi("睡眠血氧", spo2.get("average"), "%",
             "紊乱指数 %s" % esc(spo2.get("bdi")) if spo2.get("bdi") is not None else "—",
             "spo2"),
        _kpi("心血管年龄", cardio.get("vascular_age"), "岁",
             "脉搏波 %s" % esc(cardio.get("pulse_wave_velocity"))
             if cardio.get("pulse_wave_velocity") is not None else "—", "age"),
    ])


def _sleep_panel(report: dict) -> str:
    main = report.get("sleep_main")
    if not main:
        return '<h2>睡眠</h2><div class="panel">%s</div>' % _na("当晚没有可用的睡眠记录")

    stages = [(label, float(main.get(key) or 0))
              for label, key in zip(LANE_LABELS, PHASE_KEYS)]
    composition = svg_stage_columns(stages)
    hypnogram = svg_hypnogram_lanes(main.get("phases") or "", main.get("bedtime_start"))

    facts = "".join([
        _kv_item("就寝", str(main.get("bedtime_start") or "—")[11:16]),
        _kv_item("起床", str(main.get("bedtime_end") or "—")[11:16]),
        _kv_item("实际睡眠", fmt_duration(main.get("total_sleep_duration"))),
        _kv_item("睡眠效率", main.get("efficiency"), "%"),
        _kv_item("深睡", fmt_duration(main.get("deep"))),
        _kv_item("REM", fmt_duration(main.get("rem"))),
        _kv_item("浅睡", fmt_duration(main.get("light"))),
        _kv_item("清醒", fmt_duration(main.get("awake"))),
        _kv_item("入睡耗时", fmt_duration(main.get("latency"))),
        _kv_item("平均心率", main.get("avg_hr"), "bpm"),
        _kv_item("最低心率", main.get("lowest_hr"), "bpm"),
        _kv_item("平均 HRV", main.get("hrv"), "ms"),
    ])

    parts = ['<h2>睡眠</h2>',
             '<div class="grid2">',
             '<div class="panel"><div class="cap">睡眠结构</div>%s</div>' % composition,
             '<div class="panel"><div class="cap">关键指标</div><div class="kv">%s</div></div>'
             % facts,
             '</div>',
             '<div class="panel"><div class="cap">睡眠分期（5 分钟粒度）</div>%s%s</div>'
             % (hypnogram, svg_phase_legend()),
             '<div class="panel"><div class="cap">休息模式</div>%s</div>'
             % (_table(["起始日", "结束日"],
                       [[esc(p.get("start_day")), esc(p.get("end_day") or "进行中")]
                        for p in (report.get("rest_mode") or [])])
                if (report.get("rest_mode") or []) else _na())]

    naps = report.get("naps") or []
    if naps:
        rows = [["小睡", esc(fmt_duration(n.get("duration")))] for n in naps]
        parts.append('<div class="panel"><div class="cap">小睡（已计入 Oura 分数，'
                     '不并入主睡眠）</div>%s</div>' % _table(["类型", "时长"], rows))
    return "".join(parts)


def _extended_panel(report: dict) -> str:
    spo2 = report.get("spo2") or {}
    stress = report.get("stress") or {}
    resilience = report.get("resilience") or {}
    vo2 = report.get("vo2max") or {}
    sleep_time = report.get("sleep_time") or {}
    rest = report.get("rest_mode") or []

    facts = "".join([
        _kv_item("血氧 SpO2", spo2.get("average"), "%"),
        _kv_item("呼吸紊乱指数", spo2.get("bdi")),
        _kv_item("高压力时长", fmt_duration(stress.get("stress_high"))),
        _kv_item("恢复时长", fmt_duration(stress.get("recovery_high"))),
        _kv_item("当日压力总结", stress.get("day_summary")),
        _kv_item("恢复力", resilience.get("level")),
        _kv_item("VO2max", vo2.get("vo2_max")),
        _kv_item("就寝建议", sleep_time.get("status")),
        _kv_item("休息模式", ("%d 段" % len(rest)) if rest else None),
    ])
    return '<h2>扩展指标</h2><div class="panel"><div class="kv">%s</div></div>' % facts


def _contributors_panel(report: dict) -> str:
    scores = report.get("scores") or {}
    blocks = []
    for key, title, color in (("sleep", "睡眠评分构成", SLEEP),
                              ("readiness", "准备度构成", READINESS),
                              ("activity", "活动构成", ACTIVITY)):
        contributors = ((scores.get(key) or {}).get("contributors") or {})
        body = svg_hbars(list(contributors.items())) if contributors else _na()
        blocks.append('<div class="panel"><div class="cap">'
                      '<span style="color:%s">%s</span><span>0–100</span></div>%s</div>'
                      % (color, esc(title), body))
    return "<h2>评分构成</h2>%s" % "".join(blocks)


def _heartrate_panel(report: dict) -> str:
    timeseries = (report.get("timeseries") or {}).get("heartrate") or {}
    curve = timeseries.get("curve") or []
    if not curve:
        return "<h2>心率</h2><div class=\"panel\">%s</div>" % _na()
    facts = "".join([
        _kv_item("最低", timeseries["min"][0] if timeseries.get("min") else None, "bpm"),
        _kv_item("平均", timeseries["avg"][0] if timeseries.get("avg") else None, "bpm"),
        _kv_item("最高", timeseries["max"][0] if timeseries.get("max") else None, "bpm"),
    ])
    return ('<h2>心率</h2><div class="panel"><div class="cap">全天心率曲线</div>%s</div>'
            '<div class="panel"><div class="kv">%s</div></div>'
            % (svg_line(curve, label="心率"), facts))


def _events_panel(report: dict) -> str:
    """Every event domain gets a slot: an empty domain says so, never vanishes."""
    events = report.get("events") or {}
    parts = []

    workouts = events.get("workout") or []
    if workouts:
        from aggregate import _duration_between
        rows = [[esc(w.get("label") or w.get("activity")), esc(w.get("intensity")),
                 esc(w.get("calories")),
                 esc(fmt_duration(_duration_between(w.get("start_datetime"),
                                                    w.get("end_datetime"))))]
                for w in workouts]
        body = _table(["标签", "强度", "千卡", "时长"], rows)
    else:
        body = _na()
    parts.append('<div class="panel"><div class="cap">锻炼</div>%s</div>' % body)

    sessions = events.get("session") or []
    if sessions:
        rows = [[esc(s.get("type")), esc(s.get("mood"))] for s in sessions]
        body = _table(["类型", "心情"], rows, numeric_from=99)
    else:
        body = _na()
    parts.append('<div class="panel"><div class="cap">会话</div>%s</div>' % body)

    tags = events.get("tag") or []
    if tags:
        rows = [[esc(t.get("text")), esc(t.get("timestamp"))] for t in tags]
        body = _table(["内容", "时间"], rows, numeric_from=99)
    else:
        body = _na()
    parts.append('<div class="panel"><div class="cap">标签</div>%s</div>' % body)

    enhanced = events.get("enhanced_tag") or []
    if enhanced:
        rows = [[esc(t.get("custom_name") or t.get("tag_type_code")),
                 esc(t.get("comment")), esc(t.get("start_day"))] for t in enhanced]
        body = _table(["名称", "备注", "起始日"], rows, numeric_from=99)
    else:
        body = _na()
    parts.append('<div class="panel"><div class="cap">增强标签</div>%s</div>' % body)

    missing_domains = report.get("missing_domains") or []
    if missing_domains:
        parts.append('<p class="na">缺失数据域（当日无数据）：%s</p>'
                     % esc("、".join(missing_domains)))
    return "<h2>活动与记录</h2>" + "".join(parts)


def _device_panel(report: dict) -> str:
    """Ring battery and configuration: the two hardware domains."""
    battery = (report.get("timeseries") or {}).get("battery") or {}
    device = report.get("device") or {}
    levels = battery.get("level") or []
    facts = "".join([
        _kv_item("最低电量", battery.get("lowest"), "%"),
        _kv_item("采样次数", len(levels) if levels else None),
        _kv_item("型号", device.get("design")),
        _kv_item("颜色", device.get("color")),
        _kv_item("尺寸", device.get("size"), "号"),
        _kv_item("硬件", device.get("hardware_type")),
    ])
    return '<h2>设备</h2><div class="panel"><div class="kv">%s</div></div>' % facts


def _period_sections(report: dict) -> str:
    trend = report.get("trend") or []
    parts = []

    # 每日评分趋势
    bars = svg_multi_columns(
        trend,
        [("睡眠", "sleep", SLEEP), ("准备度", "readiness", READINESS),
         ("活动", "activity", ACTIVITY)],
        threshold=85, label="每日评分趋势")
    parts.append('<h2>每日评分趋势</h2><div class="panel">%s'
                 '<div class="legend"><span class="sw"><i style="background:%s"></i>睡眠</span>'
                 '<span class="sw"><i style="background:%s"></i>准备度</span>'
                 '<span class="sw"><i style="background:%s"></i>活动</span>'
                 '<span class="sw"><i style="background:%s;opacity:.6"></i>85 优秀线</span>'
                 '</div></div>'
                 % (bars, SLEEP, READINESS, ACTIVITY, MUTED))

    # 每日报表
    rows = []
    for row in trend:
        rows.append([
            esc(str(row.get("day", ""))[5:]),
            _score_pill(row.get("sleep")),
            esc(fmt_duration(row.get("sleep_duration"))) if row.get("missing") is False
            else (esc(fmt_duration(row.get("sleep_duration"))) if row.get("sleep_duration") else "—"),
            esc(fmt_duration(row.get("deep"))) if row.get("deep") else "—",
            esc(fmt_duration(row.get("rem"))) if row.get("rem") else "—",
            ("%s%%" % esc(row.get("efficiency"))) if row.get("efficiency") is not None else "—",
            _score_pill(row.get("readiness")),
            _score_pill(row.get("activity")),
            esc(row.get("steps")) if row.get("steps") is not None else "—",
            esc(row.get("active_calories")) if row.get("active_calories") is not None else "—",
            esc(row.get("lowest_hr")) if row.get("lowest_hr") is not None else "—",
            ("%s%%" % esc(round(float(row["spo2"]), 0))) if row.get("spo2") is not None else "—",
            esc(row.get("vascular_age")) if row.get("vascular_age") is not None else "—",
        ])
    parts.append('<h2>每日报表</h2><div class="panel" style="overflow-x:auto">%s</div>'
                 % _table(["日期", "睡眠", "时长", "深睡", "REM", "效率",
                           "准备度", "活动", "步数", "消耗", "最低心率", "血氧", "心血管"],
                          rows, numeric_from=99))

    # 睡眠结构（多日）
    parts.append('<h2>睡眠结构</h2><div class="panel"><div class="cap">按日拆分睡眠阶段</div>%s'
                 '<div class="legend"><span class="sw"><i style="background:%s"></i>深睡</span>'
                 '<span class="sw"><i style="background:%s"></i>浅睡</span>'
                 '<span class="sw"><i style="background:%s"></i>REM</span>'
                 '<span class="sw"><i style="background:%s"></i>清醒</span></div></div>'
                 % (svg_stacked_day_columns(trend), PHASE_COLORS["1"], PHASE_COLORS["2"],
                    PHASE_COLORS["3"], PHASE_COLORS["4"]))

    # 睡眠时间分布
    gantt_rows = []
    for row in trend:
        start = _minutes_since_midnight(row.get("bedtime_start"))
        end = _minutes_since_midnight(row.get("bedtime_end"))
        if start is None or end is None:
            continue
        gantt_rows.append((str(row.get("day", "")), start, end))
    if gantt_rows:
        parts.append('<h2>睡眠时间分布</h2><div class="panel">%s</div>'
                     % svg_gantt(gantt_rows))

    # 汇总表
    summaries = report.get("summaries") or {}
    if summaries:
        rows = [[esc(render_label(name)),
                 esc(summary_value(name, summary.get("mean"))),
                 esc(summary_value(name, summary.get("min"))),
                 esc(summary_value(name, summary.get("max"))),
                 esc(summary_value(name, summary.get("std"))),
                 esc(summary.get("n"))] for name, summary in summaries.items()]
        parts.append('<h2>周期汇总</h2><div class="panel">%s</div>'
                     % _table(["指标", "均值", "最低", "最高", "标准差", "天数"], rows))

    # 事件统计
    summary_events = report.get("events_summary")
    if summary_events:
        workout = summary_events.get("workout") or {}
        session = summary_events.get("session") or {}
        tag = summary_events.get("tag") or {}
        rows = [
            ["锻炼", esc(workout.get("count", 0)),
             esc(fmt_duration(workout.get("total_duration"))),
             "%s 千卡" % esc(workout.get("total_calories", 0))],
            ["会话", esc(session.get("count", 0)),
             esc(fmt_duration(session.get("total_duration"))), "—"],
            ["标签", esc(tag.get("count", 0)), "—",
             esc("、".join(item.get("text", "") for item in (tag.get("top") or [])) or "—")],
        ]
        parts.append('<h2>事件统计</h2><div class="panel">%s</div>'
                     % _table(["类型", "次数", "总时长", "其他"], rows))

    # 最佳/最差
    best_worst = report.get("best_worst") or {}
    if best_worst.get("best") or best_worst.get("worst"):
        items = []
        if best_worst.get("best"):
            items.append(_kv_item("最佳睡眠日", best_worst["best"]["day"],
                                  str(best_worst["best"]["value"]) + " 分"))
        if best_worst.get("worst"):
            items.append(_kv_item("最差睡眠日", best_worst["worst"]["day"],
                                  str(best_worst["worst"]["value"]) + " 分"))
        parts.append('<div class="panel"><div class="kv">%s</div></div>' % "".join(items))

    # 环比
    comparison = report.get("comparison")
    if comparison:
        parts.append('<h2>环比</h2><p class="note">%s</p>' % esc(comparison.get("note", "")))
        deltas = comparison.get("deltas") or {}
        if deltas:
            rows = [[esc(render_label(key)), "%+.2f" % value] for key, value in deltas.items()]
            parts.append('<div class="panel">%s</div>' % _table(["指标", "较上期"], rows))
        else:
            parts.append('<div class="panel">%s</div>' % _na("上期无可比数据"))

    # 数据覆盖
    missing = report.get("missing_days") or []
    pending = report.get("pending_days") or []
    if missing or pending:
        notes = []
        if missing:
            notes.append('<p class="na">无数据日期：%s（未计入统计）</p>' % esc("、".join(missing)))
        if pending:
            notes.append('<p class="na">尚未到来：%s（不计入统计）</p>' % esc("、".join(pending)))
        parts.append("<h2>数据覆盖</h2>" + "".join(notes))

    return "".join(parts)


DOCS = [
    "数据来源为 Oura Cloud API v2（api.ouraring.com），睡眠/准备度/活动/血氧/压力等分域独立拉取。",
    "睡眠时长在聚合层以秒存储，此处换算为「小时/分」；效率、分数单位为 0–100。",
    "归属日采用 API 返回的 day（睡眠结束当天的本地日期），不做二次归属；心率等时序数据按系统本地时区切日。",
    "主睡眠取 long_sleep，小睡（sleep/late_nap）单列展示，不并入主睡眠时长以免与 Oura 分数重复计算。",
    "已删除（deleted）与误检（rest）的睡眠记录一律排除；缺失日期显式标注，不做插值补齐。",
    "周期报告中的「环比」在周期未结束时按已过天数对齐上一周期的前 N 天，避免周末与工作日混比。",
    "个人数据，仅供参考，不构成医疗建议。",
]


def _docs_panel() -> str:
    items = "".join("<li>%s</li>" % esc(line) for line in DOCS)
    return '<h2>数据说明</h2><div class="panel"><ul class="docs">%s</ul></div>' % items


def render_html(report: dict) -> str:
    """Render a Report as a single self-contained HTML dashboard."""
    period = report.get("period", "daily")
    title = {"daily": "健康日报", "weekly": "健康周报", "monthly": "健康月报"}.get(
        period, "健康报告")
    start, end = report.get("start"), report.get("end")
    span = start if start == end else "%s — %s" % (start, end)

    chips = []
    days_count = len(report.get("days") or [start])
    chips.append("<b>%s</b> 共 %d 天" % (span, days_count))
    context = report.get("context") or {}
    if context.get("age") is not None:
        chips.append("年龄 <b>%s</b>" % context["age"])
    if context.get("height") is not None:
        chips.append("身高 <b>%s</b> cm" % context["height"])
    if context.get("weight") is not None:
        chips.append("体重 <b>%s</b> kg" % context["weight"])
    device = report.get("device") or {}
    if device.get("design"):
        chips.append("Oura <b>%s</b> · %s 号" % (device.get("design"), device.get("size") or "—"))
    if report.get("is_partial"):
        chips.append("周期未结束 · 已过 <b>%s</b> 天" % report.get("elapsed_days"))
    if report.get("missing_days"):
        chips.append("缺失 <b>%d</b> 天" % len(report["missing_days"]))

    subtitle = "%s · 数据来自 Oura Ring" % ("健康概览" if period == "daily" else "周期概览")

    body = [
        '<div class="top"><div class="brand"><span class="dot"></span>OURA'
        '<span class="pill">%s</span></div>'
        '<nav><a href="#scores">概览</a><a href="#sleep">睡眠</a>'
        '<a href="#ext">扩展</a><a href="#hr">心率</a><a href="#docs">说明</a></nav></div>'
        % esc(title),
        '<div class="wrap">',
        "<h1>%s</h1>" % esc(span),
        '<div class="meta">%s</div>' % esc(subtitle),
        '<div class="chips">%s</div>' % "".join(
            '<span class="chip">%s</span>' % chip for chip in chips),
        '<div id="scores"></div>',
        _kpi_grid(report),
    ]

    if period == "daily":
        body.append('<div id="sleep"></div>')
        body.append(_sleep_panel(report))
        body.append('<div id="ext"></div>')
        body.append(_extended_panel(report))
        body.append(_contributors_panel(report))
        body.append('<div id="hr"></div>')
        body.append(_heartrate_panel(report))
        body.append(_events_panel(report))
        body.append(_device_panel(report))
    else:
        body.append(_period_sections(report))

    body.append('<div id="docs"></div>')
    body.append(_docs_panel())
    body.append('<footer><span>由 oura skill 生成 · Oura Cloud API v2</span>'
                '<span>个人数据，仅供参考，不构成医疗建议</span></footer>')
    body.append("</div>")

    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>Oura %s · %s</title><style>%s</style></head><body>%s</body></html>"
            % (esc(title), esc(span), STYLE, "".join(body)))


def _md_cell(value: object) -> str:
    """Escape a value for a Markdown table cell."""
    text = "—" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict) -> str:
    """Render a compact Markdown summary. The agent appends interpretation below."""
    period = report.get("period", "daily")
    title_period = {"daily": "日报", "weekly": "周报", "monthly": "月报"}.get(period, "报告")
    start, end = report.get("start"), report.get("end")
    span = start if start == end else "%s ~ %s" % (start, end)

    scores = report.get("scores") or {}
    lines = ["# Oura %s：%s" % (title_period, span), ""]

    if report.get("is_partial"):
        lines.append("> 周期未结束，已过 %s 天。" % report.get("elapsed_days"))
        lines.append("")

    lines.append("## 核心指标")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append("| 睡眠分数 | %s |" % _md_cell((scores.get("sleep") or {}).get("value")))
    lines.append("| 准备度 | %s |" % _md_cell((scores.get("readiness") or {}).get("value")))
    lines.append("| 活动分数 | %s |" % _md_cell((scores.get("activity") or {}).get("value")))
    if period == "daily":
        lines.append("| 步数 | %s |" % _md_cell((scores.get("activity") or {}).get("steps")))
    else:
        steps_mean = (report.get("summaries") or {}).get("steps_avg", {}).get("mean")
        lines.append("| 步数（日均） | %s |" % _md_cell(steps_mean))

    summaries = report.get("summaries") or {}
    if period == "daily":
        main = report.get("sleep_main") or {}
        if main:
            lines.append("| 总睡眠 | %s |" % fmt_duration(main.get("total_sleep_duration")))
            lines.append("| 睡眠效率 | %s%% |" % _md_cell(main.get("efficiency")))
            lines.append("| 平均 HRV | %s ms |" % _md_cell(main.get("hrv")))
            lines.append("| 最低心率 | %s bpm |" % _md_cell(main.get("lowest_hr")))
    else:
        duration = summaries.get("sleep_duration_avg", {}).get("mean")
        if duration is not None:
            lines.append("| 平均睡眠时长 | %s |" % fmt_duration(duration))
        for field, unit in (("efficiency", "%"), ("hrv", "ms"), ("resting_hr", "bpm")):
            mean = summaries.get(field, {}).get("mean")
            if mean is not None:
                lines.append("| %s（均值） | %s %s |"
                             % (render_label(field), _md_cell(mean), unit))
    lines.append("")

    if summaries and period != "daily":
        lines.append("## 周期汇总")
        lines.append("")
        lines.append("| 指标 | 均值 | 最低 | 最高 | 标准差 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for name, summary in summaries.items():
            lines.append("| %s | %s | %s | %s | %s |" % (
                _md_cell(render_label(name)),
                _md_cell(summary_value(name, summary.get("mean"))),
                _md_cell(summary_value(name, summary.get("min"))),
                _md_cell(summary_value(name, summary.get("max"))),
                _md_cell(summary_value(name, summary.get("std")))))
        lines.append("")

    comparison = report.get("comparison")
    if comparison:
        lines.append("## 环比")
        lines.append("")
        lines.append("> %s" % _md_cell(comparison.get("note")))
        lines.append("")
        for key, value in (comparison.get("deltas") or {}).items():
            lines.append("- %s：%+.2f" % (_md_cell(render_label(key)), value))
        lines.append("")

    missing = report.get("missing_days") or []
    if missing:
        lines.append("## 缺失日期")
        lines.append("")
        lines.append("、".join(missing))
        lines.append("")

    tags = (report.get("events") or {}).get("tag") or []
    if tags:
        lines.append("## 标签")
        lines.append("")
        for tag in tags:
            lines.append("- %s" % _md_cell(tag.get("text")))
        lines.append("")

    lines.append("## 解读")
    lines.append("")
    lines.append("<!-- AGENT-APPEND-BELOW -->")
    lines.append("")
    return "\n".join(lines)