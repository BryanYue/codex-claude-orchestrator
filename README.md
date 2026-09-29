# Codex–Claude 协作 · 0.5.0

**简体中文** · [English](README.en.md)

<p align="center">
  <img src="plugins/codex-claude-orchestrator/assets/icon.png" alt="Codex 与 Claude 之间的派发、回传与核验回路" width="128" height="128">
</p>

<p align="center">
  <strong>在 Codex 里，让 Claude 一起把事情做好。</strong><br>
  Codex 组织任务与检查结果，Claude 参与实施或审查，你在一个对话里掌握进展。
</p>

<p align="center">
  macOS · Codex 桌面端 · 本机 Claude Code<br>
  <a href="docs/getting-started.zh-CN.md">开始使用</a> ·
  <a href="#可以怎样使用">使用示例</a> ·
  <a href="https://github.com/BryanYue/codex-claude-orchestrator/issues">反馈问题</a>
</p>

## 它能帮你做什么

如果你同时使用 Codex 和 Claude，可能经常在两个窗口之间搬运需求、代码和审查意见。这个插件把协作放回 Codex：你描述目标，Codex 根据任务选择自己处理，或把范围明确的工作交给本机 Claude Code，再检查交回的结果。

**提出任务 → Codex 整理与分工 → Claude 实施或审查 → Codex 核验与反馈**

简单问题仍可以由 Codex 直接完成；你也可以指定这次由谁处理。

| 你想做的事 | 插件带来的帮助 |
| --- | --- |
| 给方案多一轮审查 | 让 Claude 检查遗漏与边界，再由 Codex 对照原要求确认哪些意见成立。 |
| 按已确认的计划开发 | 把明确范围交给 Claude 实施，Codex 检查实际改动并运行所需验证。 |
| 随时知道进展 | 在协作工作台查看任务、执行轮次、报告和核验结果。 |
| 继续或纠正工作 | 在原对话里补充要求，让 Codex 核对已有进度后继续处理。 |

## 开始使用

### 1. 准备好这三样

- **Codex 桌面端**：已登录，所在版本支持插件。[安装说明](https://developers.openai.com/codex/app)
- **Claude Code**：已在本机安装，并有可用的登录与额度。[安装和登录说明](https://code.claude.com/docs/en/quickstart)
- **uv**：插件用它准备运行环境。[安装说明](https://docs.astral.sh/uv/getting-started/installation/)

目前已在 **Apple Silicon Mac** 上验证；Intel Mac 尚未单独验证，Windows/Linux 暂不在本版支持范围。

### 2. 安装插件

在终端中依次执行：

```bash
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref v0.5.0
codex plugin add codex-claude-orchestrator@codex-claude-team
```

第一条添加插件来源，第二条安装插件。仓库公开，无需 GitHub 邀请或登录。

**首次安装还需要准备依赖，再打开新 Codex 任务。** 不熟悉终端、遇到 `codex: command not found`，或以前装过本地版本，都可以按[一步一步的安装指南](docs/getting-started.zh-CN.md)操作；里面也有可以直接交给 Codex 的安装请求。

### 3. 从一次小审查开始

在 Codex 中打开你的项目，新建任务，把下面这段话发给它，并将文件名换成项目里的实际文件：

> 请用 Codex–Claude 协作插件，让 Claude 只读审查 README.md 的安装步骤，找出新手容易卡住的地方。先不要修改文件，你再核对它的建议。

这是一次真实的 Claude 调用，会使用你的 Claude 额度。你应能看到执行进度、Claude 的报告，以及 Codex 对建议的核对结果。**报告交回后，还需要核验，才算这次工作完成。**

想让这个 Git 项目以后自动使用协作流程，再告诉 Codex：

> 这个项目以后采用 Codex–Claude 协作流程。

这是可选的项目设置，会在项目中保存协作规则。之后可以直接说“继续”“修复这个问题”，不用每次重复插件名称。

## 可以怎样使用

**先审查方案，再开始实施**

> 让 Claude 审查 docs/plan.md，重点找遗漏和不清楚的地方。你核对后先给我结论，暂时不要改代码。

**按确认过的范围动手**

> 按刚确认的计划，让 Claude 实施这部分。你检查改动、运行必要测试，并告诉我还剩什么问题。

**调整方向**

> 这里理解错了。我希望保留原来的登录流程，只修改错误提示。请先核对当前进度，再按这个范围继续。

**查看或选择执行者**

> 打开这次协作的工作台。
>
> 这次由 Codex 直接处理。
>
> 这个项目先改成仅手动调用 Claude。

文件名和任务内容只是示例，替换成你自己的即可。

## 协作工作台里有什么

工作台把同一任务的多轮执行放在一起。你可以查看最近活动、切换轮次、阅读报告，分清**执行是否结束、是否交回报告、Codex 是否核验通过**。

它支持明暗主题和窄面板。页面用于查看；补充要求、纠正方向和继续任务，仍在 Codex 对话里进行。同一 Codex 任务会尽量从现有工作台查看后续执行；跨任务或应用重启后可能需要新的链接。

## 你可能关心的问题

**需要两边都登录吗？会使用谁的额度？**

需要你自己的 Codex 和 Claude Code 使用权限。插件不会提供或共享账号；Claude 实际执行、首次在线检查，以及你启用的版本兼容验证，都可能使用 Claude 额度。只查看本地状态不会发起模型请求。

**代码会发到哪里？**

交给 Claude 的任务说明和允许读取的文件，会通过你的 Claude Code 配置发送给相应模型服务。本机工作台与运行记录保存在你的电脑上。请按项目要求决定哪些文件可以交给模型处理。

**会自动改整个项目吗？**

只读审查不修改文件；实施任务限定允许编辑的文件，由 Codex 检查结果。插件不自动合并或发布代码。单次试用不必开启项目长期协作设置。

**装好了却没有开始执行？**

先确认依赖已准备、已打开新任务，以及 Claude Code 的登录和执行环境可用。[查看常见问题](docs/getting-started.zh-CN.md#遇到问题时)

## 进一步了解

| 你需要什么 | 去哪里看 |
| --- | --- |
| 从零安装与第一次使用 | [中文指南](docs/getting-started.zh-CN.md) · [English guide](docs/getting-started.en.md) |
| 更新、迁移与回退 | [Git marketplace 说明（英文）](docs/git-marketplace.md) |
| 版本管理、运行细节与开发 | [进阶说明（中文）](docs/advanced-usage.zh-CN.md) |
| 本版变化与已验证范围 | [更新记录（中文）](CHANGELOG.md) · [验证记录（英文）](RELEASE-VERIFICATION.md) |

由 [BryanYue](https://github.com/BryanYue) 维护的社区项目，与 OpenAI、Anthropic 无官方隶属关系。当前未指定开源许可证。欢迎通过 [Issues](https://github.com/BryanYue/codex-claude-orchestrator/issues)反馈体验或提出建议。
