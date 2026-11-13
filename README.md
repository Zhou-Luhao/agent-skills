# agent-skills

[![Tests](https://github.com/Zhou-Luhao/agent-skills/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/Zhou-Luhao/agent-skills/actions/workflows/tests.yml)

Reusable [Agent Skills](https://agentskills.io) (`SKILL.md`) for Claude Code, ChatGPT, DeepSeek Harness and other AI coding agents.

Docs are in Chinese; each skill's `README.md` states its output language.

一个可复用的 AI agent 技能合集。每个技能是一个自包含目录：`SKILL.md` 负责触发与路由，`README.md` 面向人类读者，`references/` 存放按需加载的细节，`scripts/` 存放可执行逻辑。

## 技能目录

| 技能 | 说明 | 依赖 |
|---|---|---|
| [`oura`](oura/README.md) | Oura Ring data query & health reports — all 19 API endpoints, day/week/month visual reports (self-contained HTML + Markdown). 拉取睡眠、准备度、活动、血氧、压力等全部 19 个端点，按维度 × 时间窗查询 | Python 3.9+ · stdlib only |

## 仓库结构

```
agent-skills/
├── README.md              # 本文件：技能索引
└── <skill-name>/
    ├── SKILL.md           # 必需：YAML frontmatter（name / description）+ 指令正文
    ├── README.md          # 面向人类的完整说明（能力、安装、用法、限制）
    ├── references/        # 可选：按需加载的详细文档
    ├── scripts/           # 可选：可执行脚本（经解释器调用，不依赖可执行位）
    └── assets/            # 可选：模板与静态资源
```

`SKILL.md` 的 `description` 只描述**何时使用**，不摘要工作流——避免 agent 读了摘要就跳过正文。详细内容放在 `references/`，由 `SKILL.md` 按需引导加载。

## 安装

技能本质是目录包 `<name>/SKILL.md`，放进 agent 会扫描的位置即可，无需构建：

```bash
git clone https://github.com/Zhou-Luhao/agent-skills.git

# 用户级（所有项目可用）
cp -R agent-skills/oura ~/.dsh/skills/oura        # DeepSeek Harness
cp -R agent-skills/oura ~/.claude/skills/oura     # Claude Code
cp -R agent-skills/oura ~/.agents/skills/oura     # Codex / Copilot / Gemini CLI 等

# 项目级（仅该项目会话可用）
cp -R agent-skills/oura <你的项目>/.dsh/skills/oura
```

也可以把本仓库根目录加入 agent 的技能搜索路径（DeepSeek Harness 的 `customSkillDirs`），新增技能无需重复拷贝。注意技能发现**不支持嵌套递归**（`**/SKILL.md`）——必须让 `agent-skills` 本身成为搜索根，`oura/SKILL.md` 才会被识别。

各技能的具体前置条件（凭据、外部账号等）见对应技能的 README。

## 新增技能

在本仓库根目录建 `<skill-name>/`，写入 `SKILL.md`（frontmatter 需含 `name` 与 `description`，`name` 为小写字母数字连字符）与一份面向人类的 `README.md`，然后在本文件的「技能目录」表格里加一行。

## 许可

[MIT](LICENSE) © 2026 ZhouLuhao