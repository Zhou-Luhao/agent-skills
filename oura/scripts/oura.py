"""`oura` skill CLI.

Subcommands: authorize / check / fetch / query / daily / weekly / monthly.

Exit codes: 0 success, 2 bad arguments, 3 missing credentials or token,
4 API error, 5 no data in the requested window.
"""
from __future__ import annotations

import argparse
import datetime
import sys
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional

import aggregate
import oura_auth
import oura_client
import render
from oura_auth import (
    CredentialError,
    PendingExpired,
    RefreshFailed,
    StateMismatch,
    TokenExchangeError,
    complete_authorization,
    ensure_valid_token,
    mask,
    start_authorization,
)
from oura_client import (
    ENDPOINTS,
    ApiError,
    fetch_endpoint,
    load_cache,
    merge_documents,
    needs_fetch,
    record_range,
    save_cache,
)

EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_NO_CREDENTIALS = 3
EXIT_API = 4
EXIT_NO_DATA = 5

AUTHORIZE_HINT = "python3 scripts/oura.py authorize --redirect-url '<回调URL>'"

# 14 user-facing dataset names -> the endpoints behind them.
DATASET_TO_ENDPOINTS: Dict[str, List[str]] = {
    "sleep": ["daily_sleep", "sleep"],
    "spo2": ["daily_spo2"],
    "activity": ["daily_activity"],
    "readiness": ["daily_readiness"],
    "stress": ["daily_stress"],
    "resilience": ["daily_resilience"],
    "cardio-age": ["daily_cardiovascular_age"],
    "vo2max": ["vO2_max"],
    "sleep-time": ["sleep_time"],
    "workout": ["workout"],
    "session": ["session"],
    "tag": ["tag", "enhanced_tag"],
    "heartrate": ["heartrate"],
    "battery": ["ring_battery_level"],
}

ALL_ENDPOINTS: List[str] = list(ENDPOINTS.keys())

# Columns worth printing per endpoint in `query`.
TABLE_FIELDS: Dict[str, List[str]] = {
    "daily_sleep": ["score"],
    "daily_readiness": ["score", "temperature_deviation"],
    "daily_activity": ["score", "steps", "active_calories"],
    "daily_spo2": ["spo2_percentage", "breathing_disturbance_index"],
    "daily_stress": ["day_summary", "stress_high", "recovery_high"],
    "daily_resilience": ["level"],
    "daily_cardiovascular_age": ["vascular_age", "pulse_wave_velocity"],
    "vO2_max": ["vo2_max"],
    "sleep": ["type", "total_sleep_duration", "efficiency", "average_hrv"],
    "sleep_time": ["status", "recommendation"],
    "workout": ["activity", "label", "calories", "distance"],
    "session": ["type", "mood"],
    "tag": ["text"],
    "enhanced_tag": ["custom_name", "comment"],
    "rest_mode_period": ["end_day"],
    "heartrate": ["bpm"],
    "ring_battery_level": ["level", "charging"],
    "ring_configuration": ["design", "color", "size"],
    # email / biological_sex deliberately absent: PII blacklist.
    "personal_info": ["age", "height", "weight"],
}


# --------------------------------------------------------------------- helpers


def resolve_report_dir(override: Optional[str]) -> Path:
    """`--out` > OURA_REPORT_DIR > platform downloads > ./reports."""
    if override:
        return Path(override)
    import os

    from_env = os.environ.get("OURA_REPORT_DIR")
    if from_env:
        return Path(from_env)

    home = Path.home()
    if os.name == "nt":
        candidate = home / "Downloads"
    elif sys.platform == "darwin":
        candidate = home / "Downloads"
    else:
        candidate = home / "Downloads"
        xdg = home / ".config" / "user-dirs.dirs"
        if xdg.is_file():
            for line in xdg.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("XDG_DOWNLOAD_DIR="):
                    raw = line.split("=", 1)[1].strip().strip('"')
                    candidate = Path(raw.replace("$HOME", str(home)))
                    break
    if candidate.is_dir():
        return candidate
    fallback = Path.cwd() / "reports"
    print("提示：找不到系统下载目录，报告将写入 %s（若在仓库内请改用 --out）" % fallback,
          file=sys.stderr)
    return fallback


def _parse_date(text: Optional[str]) -> datetime.date:
    if not text:
        return datetime.date.today()
    return datetime.date.fromisoformat(text)


def _load_docs(endpoints: List[str]) -> Dict[str, List[dict]]:
    return {ep: list(load_cache(ep).get("documents") or []) for ep in endpoints}


def _ensure_data(endpoints: List[str], start: datetime.date, end: datetime.date,
                 refresh: bool = False) -> None:
    """Fetch and merge anything the cache cannot answer."""
    for endpoint in endpoints:
        if not refresh and not needs_fetch(endpoint, start, end):
            continue
        fresh = fetch_endpoint(endpoint, start, end)
        cache = load_cache(endpoint)
        cache["documents"] = merge_documents(endpoint, cache.get("documents") or [], fresh)
        save_cache(endpoint, cache)
        record_range(endpoint, start, end)


def _probe_personal_info() -> dict:
    """Verify the token against a cheap endpoint."""
    docs = fetch_endpoint("personal_info", datetime.date.today(), datetime.date.today())
    return docs[0] if docs else {}


def _write_report(report: dict, out_dir: Path, label: str,
                  anchor: str) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "oura-%s-%s" % (anchor, label)
    html_path = out_dir / (stem + ".html")
    md_path = out_dir / (stem + ".md")
    html_path.write_text(render.render_html(report), encoding="utf-8")
    md_path.write_text(render.render_markdown(report), encoding="utf-8")
    return [html_path.resolve(), md_path.resolve()]


def _report_is_empty(report: dict) -> bool:
    days = report.get("days") or [report.get("start")]
    return len(report.get("missing_days") or []) >= len(days)


def _no_data_message(span: str) -> None:
    print("所选窗口无数据：%s" % span, file=sys.stderr)
    print("可能原因：戒指尚未同步（睡眠数据需要打开 Oura App 才会同步）、"
          "当时未佩戴，或该日期早于账户数据起点。", file=sys.stderr)


def _format_cell(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, dict):
        return str(value.get("average", value))
    return str(value)


def _query_table(endpoint: str, documents: List[dict]) -> str:
    fields = TABLE_FIELDS.get(endpoint, [])
    columns = ["date"] + fields
    rows = []
    for doc in documents:
        stamp = doc.get("day") or doc.get("start_day") or doc.get("timestamp") or ""
        cells = [str(stamp)]
        cells.extend(_format_cell(doc.get(field)) for field in fields)
        rows.append(cells)
    widths = [max(len(columns[i]), *(len(r[i]) for r in rows)) if rows
              else len(columns[i]) for i in range(len(columns))]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(columns)),
             "  ".join("-" * widths[i] for i in range(len(columns)))]
    for row in rows:
        lines.append("  ".join(row[i].ljust(widths[i]) for i in range(len(columns))))
    return "\n".join(lines)


def _filter_window(endpoint: str, documents: List[dict],
                   start: datetime.date, end: datetime.date) -> List[dict]:
    family = ENDPOINTS[endpoint]["family"]
    kept = []
    for doc in documents:
        if family == "single":
            kept.append(doc)
        elif family == "daily":
            try:
                day = datetime.date.fromisoformat(str(doc.get("day")))
            except (TypeError, ValueError):
                continue
            if start <= day <= end:
                kept.append(doc)
        elif family == "range":
            try:
                span_start = datetime.date.fromisoformat(str(doc.get("start_day")))
                span_end = datetime.date.fromisoformat(
                    str(doc.get("end_day") or doc.get("start_day")))
            except (TypeError, ValueError):
                continue
            if span_start <= end and span_end >= start:
                kept.append(doc)
        elif family == "timeseries":
            day = aggregate.local_day_of(doc.get("timestamp"))
            if day and start <= day <= end:
                kept.append(doc)
        else:
            kept.append(doc)
    kept.sort(key=lambda d: str(d.get("day") or d.get("start_day")
                                or d.get("timestamp") or ""))
    return kept


# ------------------------------------------------------------------ subcommands


def _cmd_authorize(args: argparse.Namespace) -> int:
    redirect = args.redirect_url
    if redirect is None and args.paste:
        print("请粘贴回调 URL 后回车：", file=sys.stderr)
        redirect = sys.stdin.readline().strip()
    if redirect:
        tokens = complete_authorization(redirect)
        print("授权成功，access_token: %s" % mask(tokens.access_token))
        return EXIT_OK

    url = start_authorization(args.port)
    print(url)
    print("")
    # Best effort only: in a headless or remote session there is no browser
    # to open, and the printed URL is the real deliverable.
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - a failed launch must not abort authorize
        pass
    print("已尝试自动打开浏览器；若没有弹出，请手动复制上面的 URL 打开。")
    print("请用浏览器打开上面的 URL，用 Oura 账号批准授权。")
    print("跳转后浏览器可能显示无法访问——这是正常的：")
    print("请复制地址栏里的完整 URL，粘贴回来，然后运行：")
    print("  " + AUTHORIZE_HINT)
    print("（也可以直接说一声，把回调 URL 发给我。）")
    return EXIT_OK


def _cmd_check(args: argparse.Namespace) -> int:
    print("凭据检查：")
    for name in ("OURA_CLIENT_ID", "OURA_CLIENT_SECRET"):
        present = "已设置" if _credential_present(name) else "未设置"
        print("  %s：%s" % (name, present))

    try:
        token = ensure_valid_token()
    except CredentialError:
        print("授权状态：未授权（请运行 authorize）", file=sys.stderr)
        return EXIT_NO_CREDENTIALS

    tokens = oura_auth.load_tokens()
    if tokens is not None:
        remaining = max(0.0, tokens.expires_at - __import__("time").time())
        print("授权状态：已授权，access_token %s，剩余 %.1f 天"
              % (mask(tokens.access_token), remaining / 86400.0))
        print("授予 scope：%s" % (tokens.scope or "（未知）"))
    else:
        print("授权状态：已就绪，access_token %s" % mask(token))

    try:
        info = _probe_personal_info()
    except ApiError as exc:
        if exc.status == 0:
            print("网络错误：%s。请检查网络后重试。" % exc, file=sys.stderr)
            return EXIT_API
        if exc.status == -1:
            print("Oura API 响应异常：%s（可能是网关或代理返回了错误页）" % exc,
                  file=sys.stderr)
            return EXIT_API
        if exc.status == 401:
            print("令牌校验失败：401，请重新运行 authorize", file=sys.stderr)
            return EXIT_NO_CREDENTIALS
        print("令牌校验未通过：HTTP %s（授权本身可能仍有效）" % exc.status, file=sys.stderr)
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - offline check should still print state
        print("令牌校验跳过：%s" % exc, file=sys.stderr)
        return EXIT_OK

    shown = {k: info.get(k) for k in ("age", "height", "weight") if info.get(k) is not None}
    print("账号信息（已排除 email / biological_sex）：%s" % shown)
    return EXIT_OK


def _credential_present(name: str) -> bool:
    import os

    if os.environ.get(name):
        return True
    env_file = oura_auth.config_dir() / "env"
    if not env_file.is_file():
        return False
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith(name + "="):
            return True
    return False


def _cmd_fetch(args: argparse.Namespace) -> int:
    end = _parse_date(args.end)
    start = _parse_date(args.start) if args.start else end - datetime.timedelta(days=89)
    if start > end:
        print("起始日期晚于结束日期", file=sys.stderr)
        return EXIT_BAD_ARGS

    if args.datasets:
        requested = [d.strip() for d in args.datasets.split(",") if d.strip()]
        endpoints: List[str] = []
        for name in requested:
            if name in DATASET_TO_ENDPOINTS:
                endpoints.extend(DATASET_TO_ENDPOINTS[name])
            elif name in ENDPOINTS:
                endpoints.append(name)
            else:
                print("未知数据集：%s" % name, file=sys.stderr)
                return EXIT_BAD_ARGS
        endpoints = sorted(set(endpoints))
    else:
        endpoints = ALL_ENDPOINTS

    ensure_valid_token()
    for endpoint in endpoints:
        fresh = fetch_endpoint(endpoint, start, end)
        cache = load_cache(endpoint)
        merged = merge_documents(endpoint, cache.get("documents") or [], fresh)
        cache["documents"] = merged
        save_cache(endpoint, cache)
        record_range(endpoint, start, end)
        print("%-28s 新增 %d 条，缓存共 %d 条" % (endpoint, len(fresh), len(merged)))
    print("完成：%s ~ %s" % (start, end))
    return EXIT_OK


def _cmd_query(args: argparse.Namespace) -> int:
    if args.list:
        for name in sorted(DATASET_TO_ENDPOINTS):
            print("%-12s -> %s" % (name, ", ".join(DATASET_TO_ENDPOINTS[name])))
        return EXIT_OK

    if not args.dataset:
        print("请指定数据集，或使用 --list 查看可用维度", file=sys.stderr)
        return EXIT_BAD_ARGS

    if args.dataset in DATASET_TO_ENDPOINTS:
        endpoints = DATASET_TO_ENDPOINTS[args.dataset]
    elif args.dataset in ENDPOINTS:
        endpoints = [args.dataset]
    else:
        print("未知数据集：%s" % args.dataset, file=sys.stderr)
        return EXIT_BAD_ARGS

    if args.last is not None:
        if args.last <= 0:
            print("--last 必须是正整数", file=sys.stderr)
            return EXIT_BAD_ARGS
        end = datetime.date.today()
        start = end - datetime.timedelta(days=args.last - 1)
    else:
        end = _parse_date(args.end)
        start = _parse_date(args.start) if args.start else end
    if start > end:
        print("起始日期晚于结束日期", file=sys.stderr)
        return EXIT_BAD_ARGS

    ensure_valid_token()
    _ensure_data(endpoints, start, end)

    payload: Dict[str, List[dict]] = {}
    for endpoint in endpoints:
        documents = _filter_window(endpoint, list(load_cache(endpoint).get("documents") or []),
                                   start, end)
        payload[endpoint] = documents

    total = sum(len(v) for v in payload.values())
    if total == 0:
        print("该窗口无数据：%s ~ %s（%s）" % (start, end, args.dataset), file=sys.stderr)
        return EXIT_NO_DATA

    if args.json:
        import json
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    for endpoint, documents in payload.items():
        print("== %s（%d 条）" % (endpoint, len(documents)))
        print(_query_table(endpoint, documents))
        print("")
    return EXIT_OK


def _cmd_report(args: argparse.Namespace) -> int:
    anchor = _parse_date(args.date)
    period = args.command

    if period == "daily":
        start = end = anchor
        prev = None
    elif period == "weekly":
        start, end = aggregate.week_bounds(anchor)
        prev = (start - datetime.timedelta(days=7), end - datetime.timedelta(days=7))
    else:
        start, end = aggregate.month_bounds(anchor)
        prev_end = start - datetime.timedelta(days=1)
        prev = (aggregate.month_bounds(prev_end)[0], prev_end)

    ensure_valid_token()
    _ensure_data(ALL_ENDPOINTS, start, end)
    docs = _load_docs(ALL_ENDPOINTS)

    if period == "daily":
        report = aggregate.build_daily(docs, anchor)
    else:
        prev_docs = None
        if prev:
            try:
                _ensure_data(ALL_ENDPOINTS, prev[0], prev[1])
                prev_docs = _load_docs(ALL_ENDPOINTS)
            except ApiError:
                prev_docs = None
            except Exception:  # noqa: BLE001 - comparison is optional
                prev_docs = None
        report = aggregate.build_period(docs, start, end, prev_docs=prev_docs,
                                        today=datetime.date.today())

    if _report_is_empty(report):
        _no_data_message(str(start) if start == end else "%s ~ %s" % (start, end))
        return EXIT_NO_DATA

    out_dir = resolve_report_dir(args.out)
    paths = _write_report(report, out_dir, period, str(start))
    for path in paths:
        print(str(path))
    return EXIT_OK


# ---------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oura",
        description="Oura Ring 数据查询与报告生成（OAuth2 授权）",
    )
    sub = parser.add_subparsers(dest="command")

    p_auth = sub.add_parser("authorize", help="OAuth2 两步授权")
    p_auth.add_argument("--port", type=int, default=oura_auth.DEFAULT_PORT,
                        help="回调端口（需与 Oura 应用白名单一致），默认 8080")
    p_auth.add_argument("--redirect-url", default=None,
                        help="浏览器跳转后的完整回调 URL")
    p_auth.add_argument("--paste", action="store_true",
                        help="从标准输入读取回调 URL")

    sub.add_parser("check", help="检查凭据与令牌状态")

    p_fetch = sub.add_parser("fetch", help="拉取并缓存数据")
    p_fetch.add_argument("--start")
    p_fetch.add_argument("--end")
    p_fetch.add_argument("--datasets", help="逗号分隔的维度名或端点名")
    p_fetch.add_argument("--refresh", action="store_true",
                         help="强制重拉，忽略缓存新鲜度")

    p_query = sub.add_parser("query", help="单维度查询")
    p_query.add_argument("dataset", nargs="?")
    p_query.add_argument("--list", action="store_true", help="列出可用维度")
    p_query.add_argument("--last", type=int, help="最近 N 天（含今天）")
    p_query.add_argument("--start")
    p_query.add_argument("--end")
    p_query.add_argument("--json", action="store_true", help="输出原始文档 JSON")

    for name, help_text in (("daily", "生成日报"), ("weekly", "生成周报"),
                            ("monthly", "生成月报")):
        p_rep = sub.add_parser(name, help=help_text)
        p_rep.add_argument("--date")
        p_rep.add_argument("--out")

    return parser


HANDLERS = {
    "authorize": _cmd_authorize,
    "check": _cmd_check,
    "fetch": _cmd_fetch,
    "query": _cmd_query,
    "daily": _cmd_report,
    "weekly": _cmd_report,
    "monthly": _cmd_report,
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else EXIT_BAD_ARGS

    command = getattr(args, "command", None)
    if not command:
        parser.print_help()
        return EXIT_BAD_ARGS

    handler = HANDLERS.get(command)
    if handler is None:
        parser.print_help()
        return EXIT_BAD_ARGS

    try:
        return handler(args)
    except ValueError as exc:
        print("参数错误：%s" % exc, file=sys.stderr)
        return EXIT_BAD_ARGS
    except (CredentialError, PendingExpired) as exc:
        print("需要先完成授权：%s" % exc, file=sys.stderr)
        return EXIT_NO_CREDENTIALS
    except StateMismatch as exc:
        print("授权校验失败：%s" % exc, file=sys.stderr)
        return EXIT_BAD_ARGS
    except RefreshFailed as exc:
        print("授权已失效：%s" % exc, file=sys.stderr)
        print("请重新运行 authorize 完成授权。", file=sys.stderr)
        return EXIT_NO_CREDENTIALS
    except TokenExchangeError as exc:
        print("Oura 授权服务返回错误：%s" % exc, file=sys.stderr)
        return EXIT_API
    except ApiError as exc:
        if exc.status == 0:
            print("网络错误：%s。请检查网络后重试。" % exc, file=sys.stderr)
            return EXIT_API
        if exc.status == -1:
            print("Oura API 响应异常：%s（可能是网关或代理返回了错误页）" % exc,
                  file=sys.stderr)
            return EXIT_API
        if exc.status == 401:
            print("令牌无效或已过期，请重新运行 authorize", file=sys.stderr)
            return EXIT_NO_CREDENTIALS
        if exc.status == 403:
            print("权限不足或 Oura 订阅已过期（HTTP 403）", file=sys.stderr)
            return EXIT_API
        print("Oura API 错误：%s" % exc, file=sys.stderr)
        return EXIT_API


if __name__ == "__main__":
    sys.exit(main())