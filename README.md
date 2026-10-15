# Codex–Claude 协作 · 1.0.1

**简体中文** · [English](README.en.md)

<p align="center">
  <img src="plugins/codex-claude-orchestrator/assets/icon.png" alt="Codex 与 Claude 之间的派发、回传与验收回路" width="128" height="128">
</p>

<p align="center">
  <strong>Claude 来做，Codex 验收。</strong><br>
  你在 Codex 里提需求。Claude 在副本里动手。<br>
  Codex 验收后才交给你。
</p>

<p align="center">
  macOS · Codex 桌面端 · 本机 Claude Code<br>
  <a href="docs/getting-started.zh-CN.md">开始使用</a> ·
  <a href="#怎么用">使用示例</a> ·
  <a href="https://github.com/BryanYue/codex-claude-orchestrator/issues">反馈问题</a>
</p>

## 能做什么

同时用 Codex 和 Claude，常要在两个窗口之间搬需求、代码和审查意见。这个插件让你只待在 Codex 里。你说目标，Codex 把你的原话、相关文件和它的理解分开交给本机 Claude Code。Claude 交回报告和改动，Codex 验收。

**你提需求 → Claude 动手 → Codex 验收 → 交给你**

| 功能 | 说明 |
| --- | --- |
| 写代码 | Claude 在副本里改，自己跑测试 |
| 审查评估 | 先给结论，再给依据 |
| 看进度 | 工作台里随时看做到哪 |
| 接着改 | 在原对话补充，下一轮接着做 |

当前版本：[`v1.0.1`](https://github.com/BryanYue/codex-claude-orchestrator/releases/tag/v1.0.1)（2026-10-15）· [更新记录](CHANGELOG.md) · [验证记录](RELEASE-VERIFICATION.md)

## 开始使用

### 1. 先准备

- **Codex 桌面端**：已登录，版本支持插件。[安装说明](https://developers.openai.com/codex/app)
- **Claude Code**：已装在本机，能登录，有额度。插件直接用你本机这份 Claude Code，不另外下载、更新或切换版本。[安装和登录说明](https://code.claude.com/docs/en/quickstart)
- **uv**：插件用它准备运行环境。[安装说明](https://docs.astral.sh/uv/getting-started/installation/)

面向 macOS。实现任务和默认的分析任务要用 macOS 自带的 `sandbox-exec` 保护原仓库。没有它时，分析任务可以改用只读模式。Intel Mac 尚未单独验证。

### 2. 安装

在终端里依次执行：

```bash
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref v1.0.1
codex plugin add codex-claude-orchestrator@codex-claude-team
```

第一条添加插件来源，第二条安装插件。仓库公开，不需要 GitHub 邀请或登录。旧版本标签保持冻结。固定 ref 不会自动升级，切换版本请按[版本切换步骤](docs/git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip)操作。

**首次安装还要准备依赖，再打开新的 Codex 任务。** 如果你不熟悉终端、遇到 `codex: command not found`，或以前装过本地版本，请按[一步一步的安装指南](docs/getting-started.zh-CN.md)操作。

### 3. 试一次

在 Codex 中打开你的项目，新建任务，发送下面这段话。把文件名换成项目里的实际文件：

> 用 Codex–Claude 协作插件，让 Claude 看看 README.md 的安装步骤，新手最容易卡在哪里？先不要改文件，你核对它的结论后告诉我。

这会真实调用 Claude，用的是你的 Claude 额度。你会看到执行进度、Claude 的报告和 Codex 的验收结论。**Claude 交回报告不等于完成，Codex 验收后才算。**

## 怎么用

**按计划写**

> 按 docs/plan.md 第二阶段，让 Claude 实现导出改异步。完成后你检查改动、跑测试，没问题再应用到项目里。

**问个问题**

> 让 Claude 评估一下把会话存储换成 SQLite 是否可行，重点看并发和迁移成本。先给结论。

**审查改动**

> 让 Claude 审查当前未提交的改动，只看错误处理和测试覆盖，在副本里复现它怀疑的问题。

**纠正方向**

> 这里理解错了：保留原来的登录流程，只修改错误提示。让 Claude 在上一轮的基础上改。

**看进度**

> 打开这次任务的工作台。

文件名和任务内容只是示例，换成你自己的即可。

## 安全边界

- **任务书**：最前面是你的原话，一字不改。后面依次是附件（规格、计划等）、Codex 的说明、约束和完成标准。Codex 的说明会标明“可能有误”。每条约束都标明来自你、附件还是 Codex。任务书写明冲突时谁优先。下一轮会原样追加你的新原话，并带上 Codex 的修正和逐项验收。Claude 实际收到的任务书，可以在工作台里逐字查看。
- **副本模式（默认，Git 项目）**：Claude 在你项目的独立 Git 副本里工作。副本取自项目当前状态，含未提交改动。Claude 可以编辑、运行命令和测试，可以用 Agent/Skill/Workflow。原仓库、它的 `.git` 和插件的运行记录由 macOS 拒写。网络、凭证和其他本机路径不隔离。外部 MCP 关闭。`.env`、`.claude`、`.codex`、`.ssh` 等敏感的未跟踪文件不会复制进副本。
- **只读模式**：Claude 在原目录里只读，只能读取和搜索。经你同意才可联网。报告通过结构化结果交回。适合普通文档目录，或你不希望执行任何命令时。
- **Claude 配置**：Claude 照常加载你的全局和项目 `CLAUDE.md`、rules 与 hooks。插件会列出本次加载了哪些。任务书里写明：与任务书冲突时，以任务书为准。

## 工作台

- 同一任务的每一轮都在一页。顶部一句话说明现状，例如「Codex 验收：改后通过，下一步：再做一轮」或「Claude 正在做 · 已用 3 分钟」。
- Claude 有要你决定的问题时，蓝色框「Claude 有问题问你」排在最上面。
- 下面分三块：**运行情况**（进程怎么结束）、**Claude 自评**（它说做完没有）、**Codex 验收**（通过、改后通过或退回，以及下一步）。
- 提醒用中文写明，例如改动碰了受保护路径。技术细节默认折叠。
- 详细资料有报告、本轮改动和过程记录。过程记录列出 Claude 用了哪些工具、读写了哪些文件。任务书、你的原话等放在「更多」里。
- 页面只用来看。补充要求、纠正方向和继续任务，仍在 Codex 对话里进行。Codex 面板较窄时，任务列表收进左上角「任务」按钮。

## 常见问题

**花谁的额度？**

需要你自己的 Codex 和 Claude Code 使用权限。插件不提供、也不共享账号。Claude 实际执行和在线检查，用的是你的 Claude 额度。只查看本地状态，不会发起模型请求。

**代码会发到哪里？**

交给 Claude 的任务书和它读取的文件，会通过你的 Claude Code 配置发给相应的模型服务。工作台和运行记录保存在你的电脑上（默认 `~/.codex/claude-orchestrator/v1`）。哪些文件可以交给模型，请按项目要求决定。

**会改我的项目吗？**

不会直接改。Claude 只改副本。插件整理好改动，Codex 验收后用 `git apply` 应用到你的项目。插件不自动合并、提交或发布。

**做的时候我能改项目吗？**

可以。Claude 不在你的项目目录里工作。如果期间项目有变化，结果里会提示。Codex 应用改动前，会先检查改动能否无冲突地应用。

**Codex 重启了怎么办？**

正在跑的任务会继续运行。重新连接后，用原来的任务编号查询即可。如果负责监督的进程意外退出，插件会停掉残留的 Claude 进程，把这一轮记为「失联」，并收回已经写下的报告和改动。

**费用怎么算？**

工作台上显示为「约 $X（Claude 估算）」。这是 Claude 客户端的估算，不是实际扣费，也不是订阅剩余额度。它是整个 Claude 会话的累计值，包含子代理。续接的轮次可能把之前轮次也算进去。

**从 0.7 升级要注意什么？**

1.0 不兼容旧任务。旧的运行记录留在原目录供查阅，但不能续接。项目采用、自动选择执行者和协作说明在线管理已移除。详见[更新记录](CHANGELOG.md)。

## 更多资料

| 想看什么 | 去哪里看 |
| --- | --- |
| 安装入门 | [中文指南](docs/getting-started.zh-CN.md) · [English guide](docs/getting-started.en.md) |
| 更新回退 | [Git marketplace 说明（英文）](docs/git-marketplace.md) · [安装与恢复](docs/install-and-recovery.md) |
| 进阶用法 | [进阶说明（中文）](docs/advanced-usage.zh-CN.md) |
| 版本记录 | [更新记录（中文）](CHANGELOG.md) · [验证记录（英文）](RELEASE-VERIFICATION.md) |

## 版本

正式版本：[`v1.0.1`](https://github.com/BryanYue/codex-claude-orchestrator/releases/tag/v1.0.1)（2026-10-15）。

1.0.1 改进了工作台：

- Codex 面板较窄时，任务列表收进「任务」按钮，详情占满宽度。
- 顶部一句话说明现状，文案全部改成日常说法。
- 过程记录列出 Claude 每一步用了什么工具、读写了哪些文件。

1.0.1 能接着做 1.0.0 开始的任务。

[`v1.0.0`](https://github.com/BryanYue/codex-claude-orchestrator/releases/tag/v1.0.0)（2026-10-08）是一次断代重构：

- 任务书改为按角色写的 Markdown，你的原话逐字直达 Claude。
- 实现任务改到独立副本里执行，交回改动。
- 插件只报告事实和警告，不再替 Codex 判定失败。

具体变化见[更新记录](CHANGELOG.md)。已执行的验证和未验证的范围，见[验证记录](RELEASE-VERIFICATION.md)。

由 [BryanYue](https://github.com/BryanYue) 维护的社区项目，与 OpenAI、Anthropic 无官方隶属关系。当前未指定开源许可证。欢迎通过 [Issues](https://github.com/BryanYue/codex-claude-orchestrator/issues) 反馈体验或提出建议。
