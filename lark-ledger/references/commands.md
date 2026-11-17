# 命令参考

所有子命令都经由解释器调用，在技能根目录执行：

```bash
python3 scripts/ledger.py <子命令> [参数]      # macOS / Linux
py -3 scripts/ledger.py <子命令> [参数]        # Windows
```

脚本只依赖 Python 3.9+ 标准库；飞书读写全部委托给 `lark-cli`，因此运行前需 `lark-cli auth status` 为已登录。配置在 `~/.lark-ledger/config.json`，由 `init` 生成，不含凭据。

## 退出码

| 码 | 含义 | 典型场景 |
|---|---|---|
| 0 | 成功 | 含“dry-run 有差异但未写入”“行情源单个失败但已告警”“快照零行但有警告” |
| 2 | 参数/用法错误 | 缺必填参数、金额 ≤ 0、日期非法、`delete` 多行组缺 `--yes`、分类不存在/重名/方向不符、与 kind 无关的开关 |
| 3 | 未配置或未登录 | 配置文件缺失/损坏、`lark-cli` 未登录 |
| 4 | `lark-cli` 调用失败 | 飞书写入失败、部分写入失败、行情落表失败 |
| 5 | 数据错误 | 账户/持仓找不到或重名、Base 缺「当前余额」/「年利率」单元格、快照数据缺失、报告聚合失败、重放不可信（`edit/delete` 或 `rebuild --apply`） |

参数解析错误（argparse）统一返回 2；错误信息走 stderr，正常结果与差异表走 stdout。**2 与 5 的分工**：2 是用法/参数形状错误（缺必填参数、参数类型或取值不合法）；5 是参数形状正确但 Base 里的数据不可用（记录不存在、单元格为空、结构不一致）。

## 通用约定

- **日期**：`--date YYYY-MM-DD`；缺省为今天（Asia/Shanghai）。不存在的日期（如 `2026-02-30`）按参数错误处理。
- **金额**：`--amount` 必须 > 0；方向由 `--kind` 或分类「类型」决定，**不看金额正负**。`--amount=-35` 会直接报错。
- **账户/持仓引用**：可传名称/代码，或 record_id；必须**精确唯一**匹配，找不到或重名都报错，不会猜。
- **分类引用**：主字段全名，如 `支出·餐饮·午餐`；脚本每次实时查《分类》表，找不到/重名报错，**绝不自动创建**。
- **record_id**：`edit`/`delete` 的位置参数；从飞书《收支明细》表对应行复制，或由用户提供。
- 所有写入都按「先写明细，再更新账户余额/持仓」的顺序；中途失败会报告已完成/未完成步骤，不伪装成整体成功。

## `init`

```bash
python3 scripts/ledger.py init [--folder-token TOKEN] [--base-name NAME]
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `--folder-token` | 否 | 云盘文件夹 token；缺省创建到「我的文档库」（wiki） |
| `--base-name` | 否 | Base 名称，默认 `个人账本` |

首次执行：检查登录 → 创建 Base → 按 `assets/schema.json` 建 6 张表及全部字段 → 写入 `assets/categories.json` 的预置分类 → 保存配置（app_token / node_token / 各表 table_id 与 field_id）。

已存在配置时：只校验 Base 可访问、表与字段齐全，**不重建、不改写**；结构不一致时列差异并以退出码 5 结束，由用户决定迁移。

输出：

```text
初始化完成：个人账本，6 张表，预置分类已写入。
配置文件：/Users/<你>/.lark-ledger/config.json
```

已存在配置时输出（不会声称写入分类）：

```text
账本已存在，校验通过：个人账本，6 张表；未做任何改动。
配置文件：/Users/<你>/.lark-ledger/config.json
```

（旧配置未记录 Base 名称时，第一行省略名称，只报表数。）

退出码：0 成功/校验通过 · 3 未登录或配置错误 · 4 飞书调用失败 · 5 表结构不一致或资源异常。中途失败会说明已创建的 Base 与表；此时直接重跑会再建一个 Base，需先删除半成品或手工补配置。

## `add`

```bash
python3 scripts/ledger.py add --account ACCOUNT [--kind KIND] [--amount F]
    [--date YYYY-MM-DD] [--summary TEXT] [--note TEXT]
    [--to-account REF] [--from-account REF] [--loan-account REF]
    [--category NAME] [--holding REF] [--price F] [--shares F]
```

### `--kind auto` 规则

`--kind` 缺省是 `auto`：

1. 传了 `--to-account` → 视为 `transfer`；此时再传 `--category` 会被拒绝（转账不分分类）。
2. 没传 `--to-account` → 必须传 `--category`。脚本按分类主字段实时查表，读它的「类型」：`收入 → income`，`支出 → expense`。
3. 分类不存在/重名 → 退出码 2，提示先在《分类》表添加该分类或消除重名（脚本不会自动创建）。分类缺失或分类「类型」为空 → 退出码 2，提示补 `--category`，或用 `--kind income|expense` 显式指定方向。
4. **绝不按金额正负猜方向**：金额本身必须 > 0，符号不参与判断。
5. 理财申购/赎回、分红、贷款还款不会由 `auto` 推断，请显式传对应的 `--kind`（赎回/还贷还要传 `--holding`/`--loan-account` 等）。

显式 `--kind income|expense` 时，脚本仍会校验分类的「类型」与方向一致，不一致拒绝写入。

### 各 kind 的必填参数

| `--kind` | 必填 | 说明 |
|---|---|---|
| `income` | `--amount --account --category` | 收入，账户余额增加 |
| `expense` | `--amount --account --category` | 支出，账户余额减少 |
| `transfer` | `--amount --account --to-account` | 普通转账；两个账户一减一增，不产生收支 |
| `invest_buy` | `--account(理财) --from-account --holding --price`，且 `--shares` / `--amount` 二选一 | 申购；脚本按 `价格×份额` 重算转账金额，买入后更新份额与加权成本 |
| `invest_sell` | `--account(理财) --holding --price --shares --to-account` | 赎回；`--category` 可选但必须属于 `收入·理财收益`，缺省按品种路由 |
| `dividend` | `--amount --account(理财)` | 分红；分类缺省 `收入·理财收益·分红`，可显式覆盖 |
| `loan_repay` | `--amount --account --loan-account` | 贷款还款；`--category` 缺省 `支出·金融·利息支出` |

`--account` 永远填「这笔钱记账所在的账户」：理财买卖填持仓所属的**理财账户**，买入的资金来源用 `--from-account`，卖出的去向用 `--to-account`。`--date`、`--summary`、`--note` 对所有 kind 可选。

脚本会拒绝与 kind 无关的开关（例如给 `income` 传 `--to-account`、给 `transfer` 传 `--category`），避免静默丢参数。

### 联动结果

- 收入/支出：1 条明细，更新 1 个账户余额。
- 转账：1 条明细，更新来源与转入两个账户。
- 申购：2 条同组明细（银行卡→理财转账 + 理财现金↔持仓份额），更新银行卡余额与持仓份额/成本；理财账户闲置现金净变化 0。
- 赎回：3 条同组明细（理财→银行卡转账 + 已实现盈亏收入 + 份额行），更新银行卡余额、持仓份额；盈亏 = 到账金额 − 成本价×卖出份额，亏损为负收入。
- 分红：1 条收入明细，理财账户余额 +金额。
- 贷款还款：2 条同组明细（利息支出 + 本金转账），付款账户 −总额、负债余额 +本金；利息 = 剩余本金 × 年利率 ÷ 12，本金 = 总额 − 利息。

### 输出契约

```text
已记账：支出 35.00 元 · 账户 微信零钱（日期 2026-11-17，明细 <record_id>，更新 1 项）
```

理财买卖展示的金额是 `成交价 × 份额`。记账成功后依次尝试行情钩子与周度快照；失败只在 stderr 打「警告：…（明细已落库，不影响记账）」，退出码仍为 0。

部分写入失败：退出码 4，stderr 打印失败步骤、可能已写入的明细 record_id、已完成/未完成步骤。此时不要去重试整笔，先让用户到飞书表格核对。

## `edit`

```bash
python3 scripts/ledger.py edit RECORD_ID [--amount F] [--date YYYY-MM-DD]
    [--account REF] [--category NAME] [--note TEXT]
```

至少给一个修改项，否则退出码 2。可改金额、日期、账户、分类、备注；**不能**改类型、成交价、份额、持仓（理财交易请整组删除后重记）。改备注会保留同组 `#G` tag。改分类只允许收入/支出行，且新分类方向必须与行类型一致；转账行不接受分类。

执行前先把「改完后的全部明细」在内存里重放；若重放不可信（缺账户/持仓、组缺方向行等），**一行都不写**，退出码 5 并打印诊断。写入顺序：先改明细，再更新受影响的账户余额/持仓。

输出：

```text
已修改明细 recXXXX：变更 金额、备注；重算更新 1 项
```

有诊断时先逐行打印 `诊断：…`。退出码：0 · 2 参数/分类方向/无修改项、明细 record_id 不存在 · 3 未配置 · 4 部分写入失败 · 5 重放不可信、账户/持仓不存在。

## `delete`

```bash
python3 scripts/ledger.py delete RECORD_ID [--yes]
```

先按备注里的 `#G` tag 找出整组并**逐行打印将删除的目标**（record_id、日期、类型、金额、摘要、备注）。单行直接删除；组内超过 1 行时必须加 `--yes`，否则退出码 2 且不删任何数据。

删除前同样先对「删完后的明细」重放；不可信则退出码 5，一行不删。删除后反向更新账户余额与持仓（份额/成本/状态）。

输出：

```text
将删除 2 条明细（组 #G1a2b3c4d）：
  1. recA  2026-11-17  支出  2300.00  摘要：还房贷  备注：…本金6200.00+利息2300.00 #G1a2b3c4d
  2. recB  2026-11-17  转账  6200.00  摘要：还房贷  备注：…
已删除 2 条明细：recA、recB；重算更新 2 项
```

退出码：0 · 2 多行组缺 `--yes`、明细 record_id 不存在或参数错误 · 3 未配置 · 4 部分写入失败 · 5 重放不可信。

## `rebuild`

```bash
python3 scripts/ledger.py rebuild [--apply]
```

从「各账户初始余额 + 各持仓期初份额/期初成本 + 全部明细」重放，算出所有账户当前余额与持仓份额/成本/状态，与表内现值对照。

- 缺省 **dry-run**：只打印差异，不写任何数据。
- `--apply`：写入账户「当前余额」与持仓「当前份额/成本价/状态」；不碰明细，也不写公式字段。
- 重放不可信（明细缺账户/持仓、组缺方向行、tag 组疑似丢行、期初字段不可用等）：dry-run 打印诊断后退出 0；`--apply` 拒绝写入并退出 5。

输出（有差异）：

```text
对象                字段      现值       应为
账户「微信零钱」（rec…）  当前余额  1000      965
dry-run：未写入任何数据；确认无误后加 --apply。
```

无差异时输出 `账实一致，无需修正。`。`--apply` 完成时输出 `已修正 N 项余额/持仓。`

退出码：0 成功/一致/dry-run 有差异或诊断 · 3 未配置 · 4 写入失败 · 5 `--apply` 时重放不可信。

## `quotes`

```bash
python3 scripts/ledger.py quotes [--force]
```

拉取持有持仓的行情并回补待回补行。缺省只在 Asia/Shanghai **15:05 之后**且**今天尚未成功跑过**时执行；不满足时输出：

```text
行情未运行：未到 15:05 或今天已经更新过；可用 --force 强制更新。
```

`--force` 同时忽略时间窗与今日标记。单个代码/数据源失败只打 `警告：…`，其余代码照常落表；全部待抓代码都失败时不写表、不记“今天已跑”，下次触发会重试。写入成功：

```text
行情更新完成：更新 4 项，窗口内待回补 1 条，告警 0 个。
```

退出码：0 成功/跳过（行情失败默认不改变退出码）· 3 配置错误 · 4 落表失败。回补窗口、过期与数据源细节见 [`quotes.md`](quotes.md)。

## `snapshot`

```bash
python3 scripts/ledger.py snapshot [--date YYYY-MM-DD]
```

为指定日期（缺省今天，UTC+8）给每个「在用」账户落一行资产快照；同一「日期_账户」重复执行只更新、不重复创建。理财账户值为「闲置现金 + 名下持仓市值」。

```text
资产快照完成：2026-11-17，写入 5 行。
```

没有可写账户时打警告、退出 0，且不记 `last_snapshot_date`（下周仍会重试）。账户缺当前余额、重名、缺 record_id 等数据问题退出 5。

## `report`

```bash
python3 scripts/ledger.py report --period week|month [--date YYYY-MM-DD] [--out DIR]
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `--period` | 是 | `week` = 锚点所在 ISO 周一至周日；`month` = 锚点所在自然月 |
| `--date` | 否 | 锚点日期，缺省今天（UTC+8） |
| `--out` | 否 | 输出目录，缺省 `~/Downloads/lark-ledger` |

生成前会强制落一份当天快照（失败只警告，不阻塞报告），读取明细/分类/账户/持仓/快照五张表，输出同名 HTML + Markdown：

```text
报告已生成：
  HTML：/Users/<你>/Downloads/lark-ledger/ledger-month-2026-11-30.html
  Markdown：/Users/<你>/Downloads/lark-ledger/ledger-month-2026-11-30.md
```

文件名是 `ledger-<week|month>-<期间结束日>`。报告内容：收支总览（**排除转账**，含贷款利息、赎回已实现盈亏与分红）、支出/收入按分类、净资产曲线（来自资产快照；报告期间包含今天时补当天实时末点）、持仓市值/浮盈、负债概览。HTML 自包含（内联 CSS/SVG、零外链、无 JS），离线可开。

退出码：0 · 2 日期/期间参数错误 · 3 未配置 · 4 飞书读取失败 · 5 聚合/写文件失败。

## `query`

```bash
python3 scripts/ledger.py query {balances,summary,holdings}
    [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--category NAME] [--json]
```

| `what` | 内容 | 相关参数 |
|---|---|---|
| `balances` | 所有账户（含已销户行）的名称、类型、当前余额、状态；合计只算「在用」 | — |
| `summary` | 闭区间收入/支出/结余与分类明细 | `--start/--end`，缺省本月；`--category` 只保留一个分类并重算合计 |
| `holdings` | 未清仓持仓的份额/最新价/市值/浮盈与合计 | — |

`summary` 的区间规则：两端都不给 = 本月；只给 `--start` = 开始日到今天；只给 `--end` = 该月 1 日到结束日；开始晚于结束退出 5。`--category` 接受一级、二级、`一级·二级` 或分类全名，未知分类退出 5 并列出可用分类。

`--json` 输出原始 dict（只在 `query` 提供）：

```json
{"what": "summary", "start": "2026-11-01", "end": "2026-11-30", "category": null,
 "income": 0.0, "expense": 0.0, "net": 0.0,
 "by_category": [{"一级": "餐饮", "二级": "午餐", "金额": 35.0}],
 "income_by_category": []}
```

`balances` 的行键是 `名称/类型/余额/状态` 加 `total`；`holdings` 的行键是 `名称/代码/份额/最新价/市值/浮盈` 加 `total`。

退出码：0 · 2 日期参数错误 · 3 未配置 · 4 飞书读取失败 · 5 分类/区间/聚合错误。

## `account`

```bash
python3 scripts/ledger.py account add --name NAME --type TYPE --opening F
    [--rate PERCENT] [--repay 等额本息|等额本金|随借随还]
python3 scripts/ledger.py account edit REF [--name NAME] [--type TYPE]
    [--opening F] [--rate PERCENT] [--repay METHOD] [--status 在用|已销户] [--note TEXT]
python3 scripts/ledger.py account list
python3 scripts/ledger.py account close REF
```

- `--type`：现金 / 微信支付宝 / 银行卡 / 理财 / 负债。
- `--opening`：负债填负数（如房贷 `-500000`）；新增时初始余额与当前余额都写成该值。
- `--rate`：**百分数**（`4.2` 表示 4.2%），脚本按小数存表；只对负债有意义。
- `account edit` 至少给一个字段，否则退出码 2；`--opening` 只改期初，改后建议 `rebuild`。
- `account close` 只把状态改为「已销户」，不删记录。
- `REF` 可传账户名称或 record_id；重名/找不到退出 5。

输出示例：`已添加账户：招行工资卡（recXXXX）`、`已更新账户：招行工资卡（recXXXX）`、`已销户：招行工资卡（recXXXX）`。`account list` 打印 8 列表格（年利率按百分数展示）。

退出码：0 · 2 参数/无修改项 · 3 未配置 · 4 飞书写入失败 · 5 账户不存在/重名/字段非法。

## `holding`

```bash
python3 scripts/ledger.py holding add --name NAME --code CODE --kind 股票|基金
    --account REF --shares F --cost F
python3 scripts/ledger.py holding edit REF [--name NAME] [--code CODE]
    [--kind 股票|基金] [--account REF] [--shares F] [--cost F]
    [--opening-shares F] [--opening-cost F] [--status 持有|已清仓]
python3 scripts/ledger.py holding list
```

- `--code`：股票 `sh600519`/`sz000001`；基金 6 位数字。品种决定行情源与赎回盈亏分类。
- `holding add` 会把 `期初份额/期初成本` 同步设为当前值，状态默认「持有」。
- `holding edit --shares/--cost` 只改**当前值**；`--opening-shares/--opening-cost` 只改 `rebuild` 的**基线**。改基线后建议 `rebuild`。
- 没有 `holding close`；清仓通过卖出，或 `holding edit REF --status 已清仓`。
- `REF` 可传持仓名称、代码或 record_id；重名/找不到退出 5。

输出示例：`已添加持仓：易方达中小盘（recXXXX）`、`已更新持仓：110011（recXXXX）`。`holding list` 打印 13 列表格（含期初份额/期初成本）。

退出码：0 · 2 参数/无修改项 · 3 未配置 · 4 飞书写入失败 · 5 持仓/账户不存在、重名、字段非法。