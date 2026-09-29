# 第一次使用 Codex–Claude 协作

[返回首页](../README.md) · **简体中文** · [English](getting-started.en.md)

这份指南带你完成安装，并做一次小范围的只读审查。你不需要先了解插件内部的工具和协议。

## 开始之前

准备一台 Mac，以及以下工具。当前版本已在 Apple Silicon Mac 上验证。

| 需要什么 | 怎么确认 |
| --- | --- |
| Codex 桌面端 | 能登录、打开项目并创建任务，所在版本支持插件。[官方安装说明](https://developers.openai.com/codex/app) |
| Claude Code | 已安装，并按[官方步骤](https://code.claude.com/docs/en/quickstart)完成自己的登录。Claude 桌面聊天应用不能代替 Claude Code。 |
| uv | 在终端运行 `uv --version` 能看到版本号；缺少时按[官方说明](https://docs.astral.sh/uv/getting-started/installation/)安装。已有 Homebrew 可运行 `brew install uv`。 |

插件需要 Python 3.11 或更新版本；稍后的依赖准备会由 uv 查找或下载所需环境。首次下载可能需要一些时间。

## 安装：可以让 Codex 帮你完成

在已有的 Codex 对话里粘贴下面这段请求：

```text
请帮我安装 Codex–Claude 协作插件：
https://github.com/BryanYue/codex-claude-orchestrator
使用固定版本 v0.5.0，通过 Git marketplace 安装。

先检查本机是否有支持 plugin 命令的 Codex 桌面客户端，以及 uv。
如果 PATH 中找不到 codex，请检查桌面应用内置的 CLI。
如果已有同名插件来源，先核对现状并按仓库的迁移说明处理。

安装后，根据实际插件登记找到对应目录，运行其中的
scripts/launch.sh --prepare-dependencies，准备首次运行需要的依赖。
请使用 bash 执行它，保留 Git 安装来源，不运行 ZIP 的 Install.command。

最后确认插件已启用、依赖已准备，说明还有哪些步骤需要我完成。
这一步只做安装和本地检查，不发起 Claude 模型请求，不替我登录。
```

遇到缺少工具、下载失败或权限问题，先处理 Codex 给出的具体原因。看到插件已登记，还需要确认依赖准备成功。

<details>
<summary>我想自己在终端安装</summary>

先确认你的 CLI 支持插件命令：

```bash
codex plugin --help
```

然后依次执行：

```bash
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref v0.5.0
codex plugin add codex-claude-orchestrator@codex-claude-team
```

这使用 Codex 的 [Git marketplace 机制](https://developers.openai.com/plugins/build/plugins)。仓库公开，读取它不需要 GitHub 登录。

运行 `codex plugin list --json` 查看本插件的实际安装目录；进入该插件目录后执行：

```bash
bash scripts/launch.sh --prepare-dependencies
```

成功时会输出含 `"status":"ready"` 的结果。它只准备 Python 依赖，不调用 Claude。如果列表路径不清楚，可让 Codex 查找，或按[详细安装说明](install-and-recovery.md#first-time-preparation-both-paths)从相同发布版本准备依赖。不要猜缓存版本目录。

`codex: command not found` 表示当前终端找不到这个命令，不一定是桌面应用没有安装。可使用上面的安装请求，让 Codex 定位本机桌面应用内置的 CLI。仅安装一个终端版 CLI 不等于满足本插件的桌面环境要求。

已有同名市场时，按[迁移与回退说明](git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip)操作，保留旧来源以便恢复。

</details>

## 打开新任务，检查是否准备好

安装和依赖准备完成后，**在 Codex 里打开你的项目，并新建一个任务**，让客户端加载插件。安装过程中已经打开的旧任务不会自动替换所有工具。

发给 Codex：

> 请用 Codex–Claude 协作插件检查本机是否已准备好：插件、Claude 执行版本和本地登录状态。先只检查，不发起在线模型请求。如果缺少执行版本，请准备可用版本；需要登录的步骤告诉我，由我完成。

如果提示需要登录，请按它指出的 Claude Code 执行程序完成登录。不要把密码、令牌或认证文件粘贴到对话中。**本地检查就绪后，第一次真实任务才能进一步确认远端模型可用。**

## 做第一次小审查

选项目中一份短文档，例如 `README.md`，发给 Codex：

> 请让 Claude 只读审查 README.md 的安装步骤，找出新手容易卡住的地方。先不要修改文件，你再核对它的建议。

这一步会实际调用 Claude，使用你自己的 Claude 额度；首次调用前的在线检查也可能使用少量额度。

你应当能观察到：

1. **任务开始**：Codex 说明交给 Claude 的范围，并报告实际执行状态。
2. **查看过程**：通过工作台链接查看进展和最近活动。
3. **收到结果**：Claude 交回报告；Codex 核对建议，并告诉你哪些成立、哪些仍有疑问。

如果只看到“环境检查完成”，还不能说明 Claude 已执行了审查。遇到启动失败或中断，继续让 Codex 检查原任务状态，先不要重复派发。

## 以后怎样使用

**偶尔使用**：像上面的例子一样，明确说“让 Claude 审查”或“这次让 Claude 实施”。不必改项目设置。

**在一个 Git 项目里长期使用**：告诉 Codex：

> 这个项目以后采用 Codex–Claude 协作流程。

这会在当前项目的 `AGENTS.md` 中追加协作入口，并保存 `.agents/codex-claude/` 配置，保留已有规则。之后可直接说“按确认的计划继续”。团队成员仍使用各自的 Claude 登录与额度。

想改回按需使用，可以说“这个项目只在我明确要求时调用 Claude”。想移除项目入口，可以说“这个项目不再自动采用该协作流程”。如果还有正在执行的任务，先让 Codex 停止并确认结束，再停用项目流程。

## 遇到问题时

| 现象 | 先做什么 |
| --- | --- |
| 找不到插件或工具 | 确认插件已启用，并在安装后打开一个新任务。 |
| 第一次启动一直等或超时 | 让 Codex 检查具体启动错误，并确认依赖准备已成功。不要反复启动 Claude。 |
| 已安装，但提示没有可用 Claude | 让 Codex 检查执行版本是否准备好，再确认相应 Claude Code 的登录。 |
| 页面没自动打开 | 说“打开这次协作的工作台”，或点击 Codex 给出的链接。打开页面不需要重跑任务。 |
| 页面暂时没有新活动 | 让 Codex 查询执行状态；没有新活动不代表任务已停止。 |
| 想更新或回退插件 | 查看[版本切换步骤](git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip)。固定 `v0.5.0` 不会自动跳到新发布版本。 |
| 手上有 ZIP 包 | 完整安装包与 Git 安装使用不同流程。GitHub 的 “Download ZIP” 是源码归档，不能当作已经构建好的安装包。见[ZIP 安装说明](advanced-usage.zh-CN.md#使用-zip-安装包开始)。 |

仍有问题时，向 [Issues](https://github.com/BryanYue/codex-claude-orchestrator/issues)提供 macOS 版本、插件版本、停在哪一步和去除敏感信息后的报错。请不要提交账号凭据、带访问令牌的工作台链接或业务代码。
