# Oura API 参考（实测校准版）

本文件记录 `oura` 技能依赖的 Oura API v2 事实。**所有条目经 2026-11-13 Gate 0 实测或权威元数据核对**；与公开 OpenAPI 文件冲突处以本文件为准。

## 权威来源

| 用途 | 来源 |
|---|---|
| OAuth 端点与 scope | `https://api.ouraring.com/.well-known/oauth-authorization-server`（授权服务器元数据） |
| 端点清单与字段 | 用户提供的 OpenAPI 3.1.0（`openapi-1.43.json`）+ 逐端点实测 |

**⚠️ 公开 OpenAPI 文件中的 OAuth 章节已过时**，不要照抄：

| 项 | OpenAPI 文件（错） | 实际（对） |
|---|---|---|
| authorization_endpoint | `https://cloud.ouraring.com/oauth/authorize` | `https://api.ouraring.com/v2/oauth/authorize` |
| token_endpoint | `https://api.ouraring.com/oauth/token` | `https://api.ouraring.com/v2/oauth/token` |
| scope 名 | `daily`、`spo2`、`heart_health` … | `extapi:daily`、`extapi:spo2`、`extapi:heart_health` … |
| PAT | 文件未提 | **已于 2025-12 弃用**，OAuth2 是唯一路径 |

## 鉴权：OAuth2 授权码 + 两步粘贴

### 端点与参数

- `response_type=code`、`client_id`、`redirect_uri`、`scope`（空格分隔）、`state`
- **redirect_uri 必须与 Oura API Application 白名单逐字一致**；本技能默认 `http://localhost:8080/callback`
- 回调 URL 形如：
  ```
  http://localhost:8080/callback?iss=<issuer>&code=<code>&state=<state>
  ```
  `iss` 参数可忽略；`code` 单次使用、短时效（约 10 分钟）
- 令牌请求：`grant_type=authorization_code`、`code`、`redirect_uri`、`client_id`、`client_secret`
  （`token_endpoint_auth_methods_supported` 含 `client_secret_post`，表单方式可用）
- 刷新：`grant_type=refresh_token`、`refresh_token`、`client_id`、`client_secret`
- 元数据还支持 PKCE（`S256`/`plain`）——本技能使用机密客户端方式，未启用 PKCE

### 为什么用「两步粘贴」而不是本地回调服务

agent 环境里 `http.server` 起的监听随进程结束即消失，且沙箱内的 localhost 未必是用户浏览器所在机器。重定向发生在**浏览器侧**，无论是否有服务监听，地址栏都会带上 `code`：因此第一步打印授权 URL，第二步由用户把地址栏 URL 交回，脚本解析并换 token。零服务器依赖，跨环境成立。

### scope 清单（实测：10 个全授予，无 403）

`extapi:personal` `extapi:daily` `extapi:heartrate` `extapi:workout` `extapi:tag`
`extapi:session` `extapi:spo2` `extapi:heart_health` `extapi:stress` `extapi:ring_configuration`

- **不请求 `extapi:email`**：技能不需要邮箱，且 `personal_info.email` 在 PII 黑名单内
- 实测 19 个 collection 端点在该 scope 组合下**全部 200**，无缺口
- 注意 `extapi:stress` 与 `extapi:ring_configuration` 在公开 OpenAPI 的 scope 列表里没有，只在元数据中存在

### token 生命周期（实测）

- `expires_in` = **2592000 秒（30 天）**
- 刷新会轮换 `refresh_token`：以响应值为准
- 触发刷新：`expires_at - now <= 60s` 时主动刷新；数据请求遇 401 刷新一次后重试

### token 存储

`~/.config/oura-skill/tokens.json`（Windows `%APPDATA%\oura-skill\tokens.json`），权限 600：

```json
{"access_token": "...", "refresh_token": "...", "expires_at": 1234567890.0,
 "scope": "...", "token_type": "Bearer"}
```

`pending_auth.json` 同目录，TTL 600 秒，成功授权后删除。两者都**不得**进入仓库、报告、缓存或日志。

## 端点契约矩阵（19 个 collection 端点）

| 族 | 端点 | 日期参数 | 归属键 | 去重键 | 响应包装 |
|---|---|---|---|---|---|
| daily | daily_sleep, daily_readiness, daily_activity, daily_spo2, daily_stress, daily_resilience, daily_cardiovascular_age, vO2_max, sleep, sleep_time, tag, workout, session | `start_date`/`end_date` | `day` | `id` | `{data, next_token}`（next_token 必填） |
| range | rest_mode_period, enhanced_tag | `start_date`/`end_date` | `start_day`/`end_day` 区间 | `id` | 同上 |
| timeseries | heartrate, ring_battery_level | **`start_datetime`/`end_datetime`**（仅日期时间）+ `latest` | 无 `day`；由 `timestamp` 转本地日 | **`timestamp`** | `{data, next_token?}`（next_token 可缺省） |
| nodate | ring_configuration | 仅 `fields`/`next_token` | — | `id` | 同 daily |
| single | personal_info | 无参数 | — | — | 单对象 |

- 所有多文档端点共有 `next_token`（分页游标）与 `fields`（逗号分隔字段裁剪）
- **时序端点行内没有 `id`**：用 `id` 去重会 `KeyError` 或把整批折叠成 1 行
- **参数族不可混用**：服务端静默忽略未声明参数，混用会无声拉错窗口
- 过取对冲：请求窗口两端各外扩 1 天，本地按归属键过滤

## Gate 0 实测结论（2026-11-13）

| 项 | 结论 |
|---|---|
| 两步粘贴授权 | ✅ 走通，`state` 校验有效 |
| token 有效期 | ✅ 30 天；刷新可用，refresh_token 轮换 |
| scope→端点 | ✅ 10 个 scope 覆盖全部 19 端点，无 403 |
| `day` 归属 | ✅ **= 睡眠结束当天的本地日期**（醒来日）。例：`day=2026-11-11` 的 long_sleep 为 `11-10T23:26+08:00 → 11-11T07:15+08:00` |
| `start_date`/`end_date` 开闭 | ✅ **双端闭区间**。`start=end=2026-11-10` 返回 1 条；`11-10..11-11` 返回 2 条 |
| `type` 实际取值 | 观测到 `long_sleep` 与 `sleep`；**未观测到 `late_nap`/`deleted`/`rest` 样本** |
| `sleep` 类型的小睡 | 存在同日多文档：11-11 有 1 条 long_sleep（24750s）+ 2 条 sleep（870s、720s）。**nap 不得并入主睡眠时长** |
| `late_nap` 判定 | ⚠️ 22:46–23:23（结束于 6pm 之后）仍被标为 `sleep`，说明 `late_nap` 不单由时钟决定。不影响统计：nap 一律只展示 |
| 时序时间戳格式 | 形如 `2026-11-09T12:18:05.500Z`（**带毫秒 + Z**）；解析需兼容 `Z` 与毫秒 |
| 账户数据起点 | 2026-11-09 前后（更早窗口返回空数组而非错误） |
| sandbox 镜像 | 存在但需独立验证；本技能不使用 |

## 字段口径

### sleep（PublicModifiedSleepModel）

| 字段 | 说明 |
|---|---|
| `day` | 睡眠结束当天的本地日期（实测确认），直接采用，不做二次归属 |
| `bedtime_start` / `bedtime_end` | ISO 8601 **带偏移**（如 `+08:00`） |
| `type` | `long_sleep`（>3h 主睡眠）/ `sleep`（用户确认小睡 15min–3h）/ `late_nap`（6pm 后结束的小睡）/ `deleted`（用户删除）/ `rest`（误检被拒） |
| `total_sleep_duration` `time_in_bed` `awake_time` `deep_sleep_duration` `light_sleep_duration` `rem_sleep_duration` `latency` | **秒** |
| `efficiency` | 1–100 |
| `average_hrv` | ms |
| `average_heart_rate` `lowest_heart_rate` | bpm |
| `average_breath` | 次/分 |
| `sleep_phase_5_min` | 每字符 5 分钟：`1`=深睡 `2`=浅睡 `3`=REM `4`=清醒 |

**过滤规则**：排除 `deleted`/`rest`；主睡眠取 `long_sleep`；nap 取 `sleep`+`late_nap`，仅展示、不并入主睡眠时长。

### 分数类

- **daily_sleep**：`score`、`day`、`contributors`（deep_sleep/efficiency/latency/rem_sleep/restfulness/timing/total_sleep，各 1–100）
- **daily_readiness**：`score`、`day`、`temperature_deviation`(℃)、`temperature_trend_deviation`、`contributors`（activity_balance/body_temperature/hrv_balance/previous_day_activity/previous_night/recovery_index/resting_heart_rate/sleep_balance/sleep_regularity）
- **daily_activity**：`score`、`steps`、`active_calories`/`total_calories`/`target_calories`、`high/medium/low_activity_time`（秒）、`sedentary_time`、`non_wear_time`、`inactivity_alerts`、`equivalent_walking_distance`（米）、`contributors`（meet_daily_targets/move_every_hour/recovery_time/stay_active/training_frequency/training_volume）
- **daily_spo2**：`spo2_percentage`、`breathing_disturbance_index`
- **daily_stress**：`stress_high`、`recovery_high`（**秒**）、`day_summary`（`restored`/`normal`/`stressful`）——**没有「正常时长」字段**

### 事件与上下文

- **workout**：`day`、`activity`、`label`（用户文本，需转义）、`intensity`、`calories`、`distance`、`start/end_datetime`、`source`
- **session**：`day`、`type`、`mood`、`start/end_datetime`、`heart_rate`、`heart_rate_variability`、`motion_count`
- **tag**：`day`、`text`（用户文本，需转义）、`timestamp`、`tags`
- **enhanced_tag**：`start_day`/`end_day` 区间、`comment`/`custom_name`（用户文本，需转义）、`tag_type_code`
- **rest_mode_period**：`start_day`/`end_day` 区间（`end_day` 为空 = 进行中）、`episodes`
- **heartrate**：`timestamp`、`timestamp_unix`、`bpm`、`source`
- **ring_battery_level**：`timestamp`、`timestamp_unix`、`level`、`charging`、`in_charger`
- **personal_info**：`id`、`age`、`height`、`weight`、`biological_sex`、`email` — **`email` 与 `biological_sex` 不得进入任何输出**

## 错误码与重试

| 码 | 含义 | 处理 |
|---|---|---|
| 400 | 参数校验失败 | `ApiError`，退出码 4 |
| 401 | token 无效/过期 | 刷新一次后重试；仍失败则提示重新 `authorize`（退出码 3） |
| 403 | **权限不足或订阅过期**（双语义） | `ApiError`，提示两种可能 |
| 429 | 限流 | 优先 `Retry-After`，其次 `X-RateLimit-Reset`，都缺失才指数退避（0.5/1/2 秒），最多 3 次 |
| 422 | 校验错误 | `ApiError` |

规范明示：429 时应**优先使用响应头**而非固定间隔退避。限流分两层（per-token 与 per-application），`X-RateLimit-Tier` 标明哪层触发。

## 缓存

- 目录：`~/.cache/oura-skill/`（Windows `%LOCALAPPDATA%\oura-skill\`），`OURA_CACHE_DIR` 可覆盖
- 每端点一个 JSON：`{"fetched_at": ts, "fetched_ranges": [{start, end, fetched_at}], "documents": [...]}`
- 去重键按族：daily/range/nodate 用 `id`；**timeseries 用 `timestamp`**
- 新鲜度：窗口含今天或昨天 → 强制补拉（睡眠数据需用户打开 App 才同步）；更早窗口缓存有效（24 小时 TTL），`--refresh` 强制重拉

## 凭据配置

| 变量 | 用途 |
|---|---|
| `OURA_CLIENT_ID` | OAuth 应用 ID（公开信息，但统一按凭据对待） |
| `OURA_CLIENT_SECRET` | OAuth 应用密钥 |

加载顺序：进程环境变量 → `~/.config/oura-skill/env`（`KEY=VALUE`，权限 600；Windows `%APPDATA%\oura-skill\env`）。

**为什么需要配置文件**：GUI 启动的进程（含 agent harness 的 bash 工具）不加载 `~/.zshenv`/`~/.zshrc`，只写在 shell 配置里的变量对这类进程不可见。

- **macOS**：写 `~/.zshenv`（所有 zsh 调用都会加载；`.zshrc` 仅交互会话、`.zprofile` 仅登录 shell）。改完 `source ~/.zshenv`；GUI 应用需重启才继承，或直接用配置文件
- **Windows**：`setx OURA_CLIENT_ID "..."`（用户级）或 GUI「编辑账户的环境变量」。注意 1024 字符截断、且只对**之后新启动**的进程生效
- **WSL**：回到 Linux 语义（配在 WSL 内的 `~/.zshenv`）；Windows 侧变量要透传需 `WSLENV` 桥接

## 已知限制

1. **跨时区旅行**：日键族直接用 API 的 `day`，时序族按系统本地时区切日。系统时区与戒指归属日可能错位
2. **数据延迟**：睡眠数据需用户打开 Oura App 才同步；当天日报可能显示「无数据」
3. **API Application 限 10 用户**，超出需 Oura 审批（个人自用无影响）
4. **`late_nap` 语义**：规范描述与实测行为不完全一致（6pm 后结束的小睡未必标 `late_nap`）；本技能对所有 nap 一视同仁，因此无影响
5. **PAT 已死**：不要尝试个人访问令牌，2025-12 已停用