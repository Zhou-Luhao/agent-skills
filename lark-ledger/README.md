# lark-ledger

飞书多维表格个人记账技能：对 agent 说一句“午饭 35 微信”，数据落到你自己的飞书 Base 里，手机/网页随时打开看账、直接改账。管理现金、微信支付宝、银行卡、理财（股票/基金持仓）与负债账户，支持行情回补、资产快照、周报/月报。

> **English summary** — A conversational personal ledger backed by a Feishu Bitable. All writes and
> arithmetic happen in `scripts/ledger.py`; the agent only turns natural language into CLI flags.
> Tracks cash / e-wallet / bank / investment / liability accounts, per-holding shares and cost,
> daily quotes, net worth snapshots, and weekly/monthly self-contained HTML + Markdown reports.
> Requires Python 3.9+ (stdlib only) and `lark-cli` logged in as the user.
>
> **Reports and CLI output are in Chinese.** The rest of this README is in Chinese.

- 触发方式与路由见 [`SKILL.md`](SKILL.md)
- 六张表的字段与净值口径见 [`references/data-model.md`](references/data-model.md)
- 全部子命令、参数与退出码见 [`references/commands.md`](references/commands.md)
- 行情数据源与回补规则见 [`references/quotes.md`](references/quotes.md)
- 预置分类全表见 [`references/categories.md`](references/categories.md)

## 能力

- **一句话记账**：收入、支出、转账、理财申购/赎回、分红、房贷/车贷还款，一条命令完成「明细 → 账户余额 → 持仓份额」联动；金额、份额、本息、盈亏全部由脚本算。
- **账户全貌**：现金、微信支付宝、银行卡、理财、负债五类账户；负债余额为负，净资产 = 在用账户当前余额之和 + 持仓市值（负债自动抵减）。
- **持仓到份额**：每只股票/基金记代码、份额、成本价；市值/浮盈由飞书公式实时算。
- **行情回补**：记账后在 15:05 之后自动抓近 3 天收盘价/净值；基金净值晚公布时先落空值，后续补上；超过 7 天标记「无数据」。
- **周报/月报**：自包含 HTML（内联 CSS/SVG、零外链、离线可开）+ Markdown，含收支分类、净资产曲线、持仓、负债。
- **可修正**：`edit` / `delete` 单条或整组联动；手机端改过账后 `rebuild` 从期初 + 全部明细重算，先 dry-run 给差异再 `--apply`。

## 前置条件

1. **`lark-cli` 已安装，并以“用户身份”登录**（不是 bot）。技能所有飞书读写都经它完成，脚本不接触 token。

   ```bash
   lark-cli auth status          # 确认已登录
   lark-cli auth login --domain base,wiki,drive   # 未登录时执行
   ```

   需要 base（多维表格）、wiki（我的文档库）、drive（可选云盘目录）权限。登录态过期后重新执行 `auth login` 即可。

2. **Python 3.9+**，仅标准库，无需 `pip install`。

3. 能访问 `money.finance.sina.com.cn`（股票）与 `api.fund.eastmoney.com`（基金）。行情失败不影响记账，只影响市值更新。

## 安装

技能就是 `<name>/` 目录，放进 agent 扫描技能的位置即可，无需构建。安装方式与根 README 一致（见 [agent-skills 安装一节](../README.md#安装)）：

```bash
git clone https://github.com/Zhou-Luhao/agent-skills.git

cp -R agent-skills/lark-ledger ~/.dsh/skills/lark-ledger        # DeepSeek Harness
cp -R agent-skills/lark-ledger ~/.claude/skills/lark-ledger     # Claude Code
cp -R agent-skills/lark-ledger ~/.agents/skills/lark-ledger     # Codex / Copilot / Gemini CLI 等
```

也可以把仓库根目录加入 agent 的技能搜索路径（技能发现不支持嵌套递归）。安装后确认 `SKILL.md`、`README.md`、`references/`、`scripts/`、`assets/` 都在同一目录下。

## 首次初始化

在技能根目录执行（Windows 用 `py -3`）：

```bash
python3 scripts/ledger.py init
```

它会：

1. 检查 `lark-cli` 登录；
2. 在飞书「我的文档库」创建 Base《个人账本》（`--folder-token` 可改为指定云盘文件夹，`--base-name` 可改名）；
3. 建 6 张表：账户、持仓、收支明细、分类、行情快照、资产快照；
4. 写入 43 条预置分级分类；
5. 在 `~/.lark-ledger/config.json` 保存 Base 定位、各表 table_id/field_id 与运行标记（不含凭据）。

已存在配置时，`init` 只校验表结构，**不会重建**。想另建一个 Base，先备份并移走 `~/.lark-ledger/config.json` 再 `init`；直接重跑不会覆盖已有账本。

初始化完成后录入期初数据（可只录一部分，之后随时补）：

```bash
# 账户：初始余额即建账时的钱；负债填负数
python3 scripts/ledger.py account add --name 微信零钱 --type 微信支付宝 --opening 1000
python3 scripts/ledger.py account add --name 招行工资卡 --type 银行卡 --opening 20000
python3 scripts/ledger.py account add --name 理财账户 --type 理财 --opening 0
python3 scripts/ledger.py account add --name 房贷 --type 负债 --opening -500000 --rate 3.1 --repay 等额本息

# 持仓：期初份额/成本会同步写成当前值
python3 scripts/ledger.py holding add --name 易方达中小盘 --code 110011 \
  --kind 基金 --account 理财账户 --shares 2447.74 --cost 3.80

# 查一下录入结果
python3 scripts/ledger.py account list
python3 scripts/ledger.py holding list
```

完整参数见 [`references/commands.md`](references/commands.md)。

## 日常使用

对 agent 直接说自然语言即可，例如：

| 你说 | agent 执行（等价命令） |
|---|---|
| 午饭 35 微信 | `add --amount 35 --account 微信零钱 --category "支出·餐饮·午餐" --summary 午饭` |
| 工资 20000 到招行卡 | `add --amount 20000 --account 招行工资卡 --category "收入·固定收入·工资"` |
| 招行卡转 1000 到微信 | `add --amount 1000 --account 招行工资卡 --to-account 微信零钱` |
| 工行卡申购 110011 一万，净值 4.0854 | `add --kind invest_buy --amount 10000 --account 理财账户 --from-account 工行卡 --holding 110011 --price 4.0854` |
| 赎回 110011 全部，净值 4.5000 | `add --kind invest_sell --account 理财账户 --holding 110011 --price 4.5000 --shares 2447.74 --to-account 工行卡` |
| 还房贷 8500 | `add --kind loan_repay --amount 8500 --account 招行工资卡 --loan-account 房贷` |
| 信用卡午饭 300 | `add --amount 300 --account 招行信用卡 --category "支出·餐饮·午餐"` |
| 基金分红 500 | `add --kind dividend --amount 500 --account 理财账户` |

命令里的账户名、分类名必须与飞书表内逐字一致；上表假设这些账户已存在（分类用预置全名）。

记账成功后，脚本会在满足条件时自动更新行情（15:05 后）并补周度资产快照；失败只打警告，不影响已落库的账。

常用查询与报告：

```bash
python3 scripts/ledger.py query balances                     # 各账户余额 + 在用合计
python3 scripts/ledger.py query summary --start 2026-11-01 --end 2026-11-30
python3 scripts/ledger.py query summary --category 餐饮 --json
python3 scripts/ledger.py query holdings                     # 持仓市值/浮盈
python3 scripts/ledger.py report --period month              # 本月月报
python3 scripts/ledger.py report --period week --date 2026-11-17
```

### 改账 / 删账 / 手机改账

- 改一条：`edit <record_id> --amount 36 --note 补差价`。record_id 从飞书《收支明细》对应行复制。
- 删一条：`delete <record_id>`。房贷本息、理财买卖是多行一组，脚本会打印整组并整组删除；确认后加 `--yes`。
- **手机端改分类/备注**：不影响余额，无需重建。
- **手机端改金额/账户/日期，或删了行**：执行 `rebuild` 先看差异，确认后 `rebuild --apply`。它只信任「期初 + 全部明细」，不信任当前余额，因此能把账恢复一致。

## 报告产物

默认输出到系统下载目录：

```text
~/Downloads/lark-ledger/ledger-week-YYYY-MM-DD.html
~/Downloads/lark-ledger/ledger-week-YYYY-MM-DD.md
~/Downloads/lark-ledger/ledger-month-YYYY-MM-DD.html
~/Downloads/lark-ledger/ledger-month-YYYY-MM-DD.md
```

文件名末尾是报告期间的结束日。`--out 目录` 可改输出位置。HTML 是深色看板风格，内联 CSS 与 SVG，无 JS、无外链，可离线打开或直接发给别人；Markdown 是同口径的文字摘要。

报告口径：

- 收支只统计「收入/支出」明细，**转账整条排除**（理财申赎、贷款本金、信用卡还款都不算支出）；
- 贷款利息计入支出、本金不计；赎回已实现盈亏与分红计入收入（亏损为负收入）；
- 净资产曲线来自《资产快照》按日期聚合，报告生成时会强制补一份当天快照，并追加当天实时净值；
- 持仓/负债分别列市值、浮盈、剩余本金与利率。

## 净值口径

- 《账户》的「当前余额」由脚本维护；**理财账户的当前余额只是账户内闲置现金**，不含持仓市值。
- 申购/赎回不改变理财账户的闲置现金净额：钱在「银行卡 ↔ 理财现金 ↔ 持仓」之间转，账户表记现金、持仓表记份额。
- **净资产 = Σ 在用账户当前余额 + Σ 在用理财账户名下持仓市值**；负债余额为负，直接抵减。
- 《资产快照》每个账户一行；理财账户那一行 = 闲置现金 + 名下持仓市值合计，所以按日期求和就是净资产，不会重复计市值。

详见 [`references/data-model.md`](references/data-model.md)。

## 限制（v1）

- **非目标**：预算管理、多币种（仅 CNY）、小票/账单 OCR、微信支付宝账单导入、盘中实时行情/基金估值、港股/美股、飞书消息/文档推送报告、多维表格仪表盘。报告只输出本地 HTML + Markdown。
- **手机改账需要 rebuild**：金额/账户/日期/删行会改变余额或份额，必须 `rebuild`；只改分类/备注不用。多明细组请整组改删，不要只动一行。
- **行情源是第三方公开接口**：新浪日 K 与东财历史净值无 SLA，可能限流、变更或被网络环境拦截；基金净值（尤其 QDII）可能延迟；非交易日最终标记「无数据」。行情失败只影响市值，不影响记账。
- **行情只取已结算价**：没有盘中实时价，也没有基金盘中估值；报告使用最近一次落表的最新价。
- **依赖 `lark-cli`**：这是本技能与 `oura` 的有意差异；未安装或未以用户身份登录时无法读写飞书。token 由 `lark-cli` 管理，技能不保存凭据。
- **分类需手工维护**：脚本不会自动创建分类；新分类先在飞书《分类》表添加，并设置「类型」为收入/支出。
- **周度快照是近似**：`add` 每周首次补落的是“上周日”快照，余额取执行时的当前值；需要精确历史点时用 `snapshot --date` 显式补。
- **单机单写者**：脚本不做并发锁；同一账本请避免多个 agent/进程同时写。

## 开发

纯 Python 3.9+ 标准库，无第三方依赖。测试不访问网络：`lark-cli` 与行情 HTTP 均被 mock，行情解析用 `scripts/fixtures/` 的固定样例。

| 模块 | 职责 |
|---|---|
| `scripts/ledger.py` | CLI 入口、参数校验、退出码映射 |
| `scripts/add_flow.py` | 分类实时解析、记账编排、后置钩子 |
| `scripts/posting.py` | 纯算术：WritePlan、本息拆分、加权成本、重放 |
| `scripts/rebuild_flow.py` | edit / delete / rebuild 与差异报告 |
| `scripts/quotes.py` / `quotes_flow.py` | 行情解析、窗口/过期、抓取与落表 |
| `scripts/snapshot_flow.py` | 资产快照、周触发、净值口径 |
| `scripts/report_flow.py` / `aggregate.py` / `render.py` | 聚合、查询、HTML/Markdown 渲染 |
| `scripts/master_data.py` / `lark.py` / `config.py` / `init_flow.py` | 主数据、lark-cli 封装、配置、初始化 |

```bash
cd scripts/tests && python3 -m unittest discover -v    # 当前 506 个测试
python3 .github/scripts/validate_skills.py             # 在仓库根执行，校验 frontmatter
```

## 许可

[MIT](../LICENSE) © 2026 ZhouLuhao