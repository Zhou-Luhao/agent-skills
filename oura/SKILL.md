---
name: oura
license: MIT
compatibility: Requires Python 3.9+ (stdlib only, no third-party packages), outbound HTTPS to api.ouraring.com, and a browser for the one-time OAuth2 authorization; token cache lives in ~/.config/oura-skill/.
description: Use when the user asks about Oura Ring data — 睡眠与睡眠分数 sleep score、准备度 readiness、活动与步数 activity / steps、血氧 SpO2 / blood oxygen、压力 stress、恢复力 resilience、心血管年龄 cardiovascular age、VO2 max、心率 heart rate、戒指电量 ring battery，或要求生成 Oura 日报/周报/月报 daily report / weekly report / monthly report、导出健康报告 export health report、检查授权状态 check authorization status。
---

# Oura 全能助手

查询 Oura Ring 监测数据、生成日/周/月健康报告。所有取数、聚合、渲染都由 `scripts/oura.py` 完成——**不要自己调 HTTP API，也不要手算聚合**。

## 安全红线

- 凭据（`OURA_CLIENT_ID` / `OURA_CLIENT_SECRET` / access_token / refresh_token / 授权码）**只从环境变量或 `~/.config/oura-skill/` 读**，绝不写进报告、缓存、日志或回复。
- 不要请用户把 client_secret 或 token 粘到对话里；需要授权时按下面的两步流程走。
- 展示 token 时只显示掩码（脚本已内置 `mask()`，形如 `_0XB…`）。

## 能力路由

| 用户想要 | 怎么做 |
|---|---|
| 查某个维度最近几天/某个日期段的数据 | `query`（先读 `references/api.md` 了解可用维度与字段） |
| 拉全量数据到本地缓存 | `fetch` |
| 日报 / 周报 / 月报 | 读 `references/reports.md`，再跑对应子命令 |
| 检查授权是否有效 | `check` |
| 首次使用或 token 失效 | 两步授权，见下 |

调用方式（经解释器，不要直接执行脚本路径）：

```bash
python3 scripts/oura.py <子命令>        # macOS / Linux
py -3 scripts/oura.py <子命令>          # Windows
```

## 常用命令

```bash
python3 scripts/oura.py check                      # 凭据 + 令牌状态 + 账号摘要
python3 scripts/oura.py query sleep --last 3       # 近三天睡眠
python3 scripts/oura.py query spo2 --last 5        # 近五天血氧
python3 scripts/oura.py query --list               # 列出 14 个维度
python3 scripts/oura.py fetch --start 2026-11-01 --end 2026-11-30
python3 scripts/oura.py daily   --date 2026-11-12  # 默认今天
python3 scripts/oura.py weekly  --date 2026-11-12  # 周一至周日
python3 scripts/oura.py monthly --date 2026-11-12
```

## 两步授权（首次或 401 后）

1. 跑 `authorize`，把打印出的**授权 URL 原样交给用户**，请其用浏览器打开、批准，
   然后把地址栏里跳转后的**完整回调 URL** 发回；
2. 拿到后跑 `authorize --redirect-url '<回调URL>'`（或 `--paste` 走 stdin）完成交换。

页面显示「无法访问 localhost」属正常——地址栏里已经带着 `code`。
**不要**自己猜测或伪造 `code`；回调 URL 也**不要**写进报告或日志。

## 退出码

`0` 成功 · `2` 参数错 · `3` 未授权（先跑 authorize）· `4` API 错 · `5` 该窗口无数据（可能戒指未同步，需打开 Oura App）

更细的端点契约、字段单位、错误码处理见 `references/api.md`；报告结构与交付契约见 `references/reports.md`。