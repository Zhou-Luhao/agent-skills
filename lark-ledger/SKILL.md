---
name: lark-ledger
license: MIT
compatibility: Requires Python 3.9+ (stdlib only) and lark-cli logged in as user; outbound HTTPS to money.finance.sina.com.cn and api.fund.eastmoney.com; config lives in ~/.lark-ledger/.
description: 飞书多维表格个人记账。当用户要记账/记一笔/记收支/查账/查余额/问净资产/管理账户/持仓/股票基金市值/贷款还款/更新行情/生成记账周报月报时使用。数据存飞书多维表格，支持现金、微信支付宝、银行卡、理财（股票基金）与负债账户。
metadata:
  requires: "lark-cli, python3"
---

# Lark Ledger

飞书多维表格个人记账。所有写入与算术都由 `scripts/ledger.py` 完成——**不要自己拼 `lark-cli` 命令写记录，也不要手算金额、份额、本息、市值或净资产**。脚本会一次性完成多表联动（收支明细 → 账户余额 → 持仓份额），并在记账后按需触发行情回补与资产快照。

在本技能根目录下调用（Windows 用 `py -3` 代替 `python3`）：

```bash
python3 scripts/ledger.py <子命令> [参数]
```

## 安全红线

- **有歧义必须问用户**：账户、分类、金额、份额、还的是哪笔贷款，只要不能唯一确定，就先问；不要挑一个“看起来像”的账户/分类落库。
- **绝不自动创建分类**：脚本按分类表主字段精确匹配，找不到或重名会直接报错退出。用户要新分类，请其在飞书《分类》表里添加后再重试；不要用相近分类代替。
- **删除、`rebuild --apply` 前先复述影响**：`delete` 先不带 `--yes` 跑一次（或读它打印的整组目标），把要删的明细、会反向影响的余额/持仓告诉用户；`rebuild` 先跑默认 dry-run，把差异表给用户看，确认后才加 `--apply`。多明细组（房贷本息、理财买卖）会被整组删/改，不要拆组操作。
- **凭据与个人资源 ID 不入仓库、不入对话**：token 由 `lark-cli` 管理；`~/.lark-ledger/config.json` 里的 app_token / table_id / record_id 只留在本机。不要请用户把 token 粘进对话，也不要把这些 ID 写进文档、报告或提交。
- **失败要如实报告**：`add` 部分失败时脚本会列出已完成/未完成步骤和可能已写入的明细，照原样告诉用户去表格核对；不要把部分成功说成成功。

## 前置检查（每次会话先做一次）

1. `lark-cli auth status`：未登录时不要直接跑 `auth login`（它会阻塞等待浏览器授权）；请用户在自己的终端执行 `lark-cli auth login --domain base,wiki,drive`，完成后再继续。
2. 检查本地配置 `~/.lark-ledger/config.json` 是否存在：
   - 不存在 → 先跑 `python3 scripts/ledger.py init`；默认在飞书「我的文档库」创建 Base《个人账本》并建 6 张表、写入预置分类。
   - 存在 → 不要重复 init（脚本会走校验分支，只校验不重建）；直接执行用户要的子命令。
3. 任何子命令报“配置错误/未初始化”（退出码 3），都回到第 2 步，而不是手工拼飞书命令绕过。

## 能力路由

| 用户想要 | 怎么做 | 先读 |
|---|---|---|
| 首次建账本 / 换一个 Base | `init`（幂等；`--folder-token` 可指定云盘目录） | `references/commands.md` |
| 记一笔：收支、转账、理财申购/赎回、分红、贷款还款 | 把自然语言解析成参数，跑 `add`（见下方 8 个例子） | `references/commands.md` |
| 改一条明细（金额/日期/账户/分类/备注） | `edit <record_id> --字段 新值`，脚本联动重算余额/持仓 | `references/commands.md` |
| 删一条明细（或一整组） | `delete <record_id>`；同组超过 1 行需用户确认后加 `--yes` | `references/commands.md` |
| 手机端手工改过金额/账户/日期/删行，要求“重算一下” | `rebuild` 看差异 → 用户确认 → `rebuild --apply` | `references/commands.md` |
| 新增账户、改期初/利率、销户 | `account add / edit / list / close` | `references/commands.md` |
| 新增持仓、改份额/成本/所属理财账户 | `holding add / edit / list` | `references/commands.md` |
| 查余额、某段时间收支、按分类统计、持仓市值 | `query balances / summary / holdings`（支持 `--json`） | `references/commands.md` |
| 生成周报 / 月报 | `report --period week / month [--date] [--out]`，把产物路径告诉用户 | `references/commands.md` |
| 手动更新行情（不等 15:05 或当天已跑过） | `quotes --force` | `references/quotes.md` |
| 盘点资产 / 补一份快照 | `snapshot [--date YYYY-MM-DD]` | `references/commands.md` |
| 查字段含义、净值口径、期初/状态字段 | 按需加载 | `references/data-model.md` |
| 查分类全名与预置层级 | 按需加载 | `references/categories.md` |
| 查行情数据源、回补/过期规则 | 按需加载 | `references/quotes.md` |

`edit` / `delete` 的位置参数是**明细 record_id**：从飞书《收支明细》表对应行复制，或由用户提供；脚本没有按摘要模糊找明细的子命令，不要猜 record_id。

## 记一笔：自然语言 → 命令

解析规则：

- 金额一律传正数（`--amount` / `--price` / `--shares` 都必须大于 0）；方向由 `--kind` 或**分类表里的「类型」**决定，**绝不看金额正负**。
- 日期缺省为今天（Asia/Shanghai）；用户说“昨天”就换算成具体 `YYYY-MM-DD` 传给 `--date`。
- 账户名、分类名必须与飞书表里的值**逐字一致**；不确定就先用 `account list` / `holding list` 查，或问用户。
- 不传 `--kind` 时是 `--kind auto`：有 `--to-account` 视为转账；否则必须有 `--category`，脚本按分类的「类型」判收入/支出，并校验它与分类方向一致。理财买卖、分红、贷款还款请显式传 `--kind`。

**1. 午饭 35 微信**

```bash
python3 scripts/ledger.py add --amount 35 --account 微信零钱 \
  --category "支出·餐饮·午餐" --summary 午饭
```

**2. 工资 20000 到招行工资卡**

```bash
python3 scripts/ledger.py add --amount 20000 --account 招行工资卡 \
  --category "收入·固定收入·工资" --summary 工资
```

**3. 招行卡转 1000 到微信零钱**

```bash
python3 scripts/ledger.py add --amount 1000 --account 招行工资卡 \
  --to-account 微信零钱 --summary 转零钱
```

**4. 工行卡申购基金 110011 一万元，净值 4.0854**

```bash
python3 scripts/ledger.py add --kind invest_buy --amount 10000 --account 理财账户 \
  --from-account 工行卡 --holding 110011 --price 4.0854 --summary "申购 110011"
```

`--account` 是持仓所属的**理财账户**，`--from-account` 是出钱的银行卡/现金账户；份额由脚本按 `金额 ÷ 净值` 算（本例 2447.74），**不要自己算份额**。

**5. 赎回基金 110011 全部 2447.74 份，净值 4.5000，回工行卡**

```bash
python3 scripts/ledger.py add --kind invest_sell --account 理财账户 \
  --holding 110011 --price 4.5000 --shares 2447.74 \
  --to-account 工行卡 --summary "赎回 110011"
```

到账金额与已实现盈亏由脚本算，并自动生成「收入·理财收益·基金收益」（或股票收益）明细；亏损记为负收入。份额必须是用户给出或从 `holding list` 原样抄来的，不要替用户折算“卖一半”。

**6. 还房贷 8500**

```bash
python3 scripts/ledger.py add --kind loan_repay --amount 8500 --account 招行工资卡 \
  --loan-account 房贷 --summary 还房贷
```

`--loan-account` 必须是「类型=负债」且已填年利率的账户；脚本按 `剩余本金 × 年利率 ÷ 12` 拆出利息，剩余为本金，生成两条同组明细（利息支出 + 本金转账），负债余额只减本金。

**7. 信用卡消费 300（午饭）**

```bash
python3 scripts/ledger.py add --amount 300 --account 招行信用卡 \
  --category "支出·餐饮·午餐" --summary 信用卡午饭
```

信用卡/花呗是负债账户：支出让余额更负；还款走普通转账（`--account 银行卡 --to-account 信用卡`），不算支出。

**8. 收到基金分红 500**

```bash
python3 scripts/ledger.py add --kind dividend --amount 500 --account 理财账户 \
  --summary 基金分红
```

分红默认记「收入·理财收益·分红」并增加理财账户闲置现金；没有持仓也能记。若用户明确是股票分红且想用股票收益分类，可加 `--category "收入·理财收益·股票收益"`。

记账成功后脚本会自动尝试行情（15:05 后、当天未跑过）与周度资产快照；这两步失败只打警告，不影响已落库的账。警告要转告用户，但不要说“记账失败”。

## 按需加载

- 六张表的每个字段、净值口径、状态与期初字段 → [`references/data-model.md`](references/data-model.md)
- 每个子命令的真实参数、退出码、输出契约、`--kind auto` 规则 → [`references/commands.md`](references/commands.md)
- 行情数据源、15:05 窗口、3 天 ∪ 待回补、7 天过期、失败处理 → [`references/quotes.md`](references/quotes.md)
- 预置分类全表与“表可编辑、脚本实时解析” → [`references/categories.md`](references/categories.md)