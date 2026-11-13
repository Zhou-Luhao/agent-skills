# oura

Oura Ring 数据查询与健康报告技能。通过 Oura Cloud API v2 拉取戒指监测数据，支持按维度查询、按日/周/月生成可视化报告。

- 触发方式见 [`SKILL.md`](SKILL.md)
- 接口与字段口径见 [`references/api.md`](references/api.md)
- 报告结构与交付契约见 [`references/reports.md`](references/reports.md)

## 能力

**查询**——14 个语义化维度，任意时间窗：

```bash
query sleep --last 3          # 近三天睡眠
query spo2 --last 5           # 近五天血氧
query heartrate --start 2026-11-01 --end 2026-11-10
query --list                  # 列出全部维度
```

**报告**——日/周/月三种周期，产物为**自包含 HTML**（内联 SVG，无外链、无脚本，离线可打开）+ Markdown 摘要。默认写入系统「下载」目录。

**数据覆盖**——19 个 collection 端点全覆盖（18 个数据域 + `personal_info`）：

| 族 | 端点 |
|---|---|
| 日键（13） | `daily_sleep` `daily_readiness` `daily_activity` `daily_spo2` `daily_stress` `daily_resilience` `daily_cardiovascular_age` `vO2_max` `sleep` `sleep_time` `tag` `workout` `session` |
| 区间（2） | `rest_mode_period` `enhanced_tag` |
| 时序（2） | `heartrate` `ring_battery_level` |
| 无日期（1） | `ring_configuration` |
| 单文档（1） | `personal_info` |

缺失的数据域在报告中显式标注「无数据」，不会静默消失。

## 前置准备

1. 在 [cloud.ouraring.com/oauth/applications](https://cloud.ouraring.com/oauth/applications) 注册一个 API Application
2. 把回调地址 `http://localhost:8080/callback` 加入 redirect_uri 白名单（端口可改，但必须与白名单逐字一致）
3. 准备 `OURA_CLIENT_ID` 与 `OURA_CLIENT_SECRET`

凭据读取顺序：进程环境变量 → `~/.config/oura-skill/env`（`KEY=VALUE`，权限 600）。

> **为什么需要配置文件**：GUI 启动的进程（含多数 agent harness）不加载 `~/.zshenv`/`~/.zshrc`，只写在 shell 配置里的变量对这类进程不可见。把凭据写进配置文件可保证任何启动方式都能读到。

凭据与 token 只保存在本机 `~/.config/oura-skill/`（权限 600），**不会**进入仓库、报告或日志。

## 安装

```bash
cp -R agent-skills/oura ~/.dsh/skills/oura        # DeepSeek Harness
cp -R agent-skills/oura ~/.claude/skills/oura     # Claude Code
cp -R agent-skills/oura ~/.agents/skills/oura     # Codex / Copilot / Gemini CLI 等
```

## 使用

### 首次授权

OAuth2 授权码流程，采用**两步粘贴式**握手——agent 环境收不到 localhost 回调，因此不起本地服务：

```bash
# 1. 打印授权 URL（同时尽力尝试打开浏览器）
python3 scripts/oura.py authorize

# 2. 浏览器授权后，把地址栏完整的回调 URL 交回
python3 scripts/oura.py authorize --redirect-url 'http://localhost:8080/callback?code=...&state=...'
```

页面显示「无法访问 localhost」属正常——地址栏里已带 `code`。token 有效期 30 天，之后自动刷新。

### 命令

| 子命令 | 作用 |
|---|---|
| `authorize` | 两步 OAuth 授权 |
| `check` | 凭据存在性、token 有效期、scope、账号摘要 |
| `fetch` | 按窗口拉取并合并进本地缓存（`--refresh` 强制重拉） |
| `query <维度>` | 单维度查询，支持 `--last N` / `--start --end` / `--json` |
| `daily` / `weekly` / `monthly` | 生成报告，`--date` 指定锚点，`--out` 指定输出目录 |

```bash
python3 scripts/oura.py check
python3 scripts/oura.py fetch --start 2026-11-01 --end 2026-11-30   # 预拉取进缓存
python3 scripts/oura.py query sleep --last 3
python3 scripts/oura.py daily --date 2026-11-12
python3 scripts/oura.py weekly                    # 周一至周日
python3 scripts/oura.py monthly --date 2026-11    # 自然月
```

退出码：`0` 成功 · `2` 参数错 · `3` 未授权 · `4` API 错误 · `5` 该窗口无数据。

## 报告形态

深色数据看板，单文件自包含：

- **KPI 区**：睡眠 / 准备度 / 活动 / 夜间最低心率 / 睡眠血氧 / 心血管年龄，带「较前一日」涨跌
- **睡眠**：结构堆叠图、分期泳道图（5 分钟粒度，按真实时钟轴）、关键指标网格
- **评分构成**：睡眠 / 准备度 / 活动三组贡献因子，按分数带自动着色
- **心率**：全天曲线 + 极值
- **扩展指标**：血氧、压力、恢复力、VO2max、就寝建议、休息模式
- **活动与记录**：锻炼、会话、标签、增强标签
- **设备**：戒指电量与型号
- **周/月报**：每日评分趋势（含 85 分优秀线）、每日报表、睡眠结构、睡眠时间分布甘特图、周期汇总、事件统计、环比

所有动态文本均转义（含 SVG 文本节点与属性），用户自由文本不会破坏页面结构。

## 开发

纯 Python 3.9+ 标准库实现，无第三方依赖：

| 模块 | 职责 |
|---|---|
| `scripts/oura.py` | CLI 入口与子命令编排 |
| `scripts/oura_auth.py` | 两步授权、token 存储与刷新 |
| `scripts/oura_client.py` | 端点族分发、分页、429 重试、缓存 |
| `scripts/aggregate.py` | 日/周/月聚合（纯函数，无 IO） |
| `scripts/render.py` | SVG 图表与 HTML/Markdown 渲染 |

```bash
python3 -m unittest discover -s scripts/tests -v
```

151 个测试。**不访问网络**：HTTP 层用 `unittest.mock` 替换，数据用 `scripts/fixtures/` 下的固定样例（生产路径不读取该目录）。

## 已知限制

- **数据延迟**：睡眠数据需用户打开 Oura App 才同步，当天报告可能显示「无数据」
- **心率拉取较慢**：5 分钟粒度，长窗口需多页拉取
- **跨时区**：日键族采用 API 返回的 `day`（睡眠结束当天本地日），时序族按系统本地时区切日；旅行时两者可能错位
- **依赖账号历史**：恢复力、VO2max 等域需要 Oura 积累足够历史才会产出数据

## 许可

[MIT](../LICENSE) © 2026 ZhouLuhao