# 第一次使用 Codex–Claude 协作

[返回首页](../README.md) · **简体中文** · [English](getting-started.en.md)

这份指南带你完成安装，并做一次不改文件的小分析任务。你不需要先了解插件内部的工具和参数。

## 开始之前

准备一台 Mac，以及以下工具：

| 需要什么 | 怎么确认 |
| --- | --- |
| Codex 桌面端 | 能登录、打开项目并创建任务，所在版本支持插件。[官方安装说明](https://developers.openai.com/codex/app) |
| Claude Code | 已安装，并按[官方步骤](https://code.claude.com/docs/en/quickstart)完成自己的登录。插件直接使用本机这份 Claude Code，不另外下载、更新或切换版本。Claude 桌面聊天应用不能代替 Claude Code。 |
| uv | 在终端运行 `uv --version` 能看到版本号；缺少时按[官方说明](https://docs.astral.sh/uv/getting-started/installation/)安装。已有 Homebrew 可运行 `brew install uv`。 |

插件需要 Python 3.11 或更新版本；稍后的依赖准备会由 uv 查找或下载所需环境，首次下载可能需要一些时间。实现任务和 Git 项目里的默认分析任务还要用到 macOS 自带的 `/usr/bin/sandbox-exec` 来保护原仓库，一般无需另装。Intel Mac 尚未单独验证。

> **关于版本：** 本页面向 `v1.0.0`，完整构建 `1.0.0+codex.20261007165220`；发布状态和已验证范围见[验证记录](../RELEASE-VERIFICATION.md)。旧版本标签保持冻结，固定 ref 不会自动跨标签升级，切换按[版本切换步骤](git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip)操作。从 0.7.x 升级先看[升级说明（英文）](install-and-recovery.md#upgrading-from-07x)：1.0 不能续接旧任务。

## 安装：可以让 Codex 帮你完成

在已有的 Codex 对话里粘贴下面这段请求：

```text
请帮我安装 Codex–Claude 协作插件：
https://github.com/BryanYue/codex-claude-orchestrator
使用固定版本 v1.0.0，通过 Git marketplace 安装。

先检查本机是否有支持 plugin 命令的 Codex 桌面客户端，以及 uv。
如果 PATH 中找不到 codex，请检查桌面应用内置的 CLI。
如果已有同名插件来源，先核对现状并按仓库的迁移说明处理。

安装后，根据实际插件登记找到对应目录，运行其中的
scripts/launch.sh --prepare-dependencies，准备首次运行需要的依赖。
请使用 bash 执行它，保留 Git 安装来源，不运行 ZIP 的 Install.command。

最后确认插件已启用、依赖已准备，说明还有哪些步骤需要我完成。
这一步只做安装和本地检查，不发起 Claude 模型请求，不替我登录。
```

遇到缺少工具、下载失败或权限问题，先处理 Codex 给出的具体原因。插件已登记之外，还需要确认依赖准备成功。

<details>
<summary>我想自己在终端安装</summary>

先确认你的 CLI 支持插件命令：

```bash
codex plugin --help
```

然后依次执行：

```bash
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref v1.0.0
codex plugin add codex-claude-orchestrator@codex-claude-team
```

这使用 Codex 的 [Git marketplace 机制](https://developers.openai.com/plugins/build/plugins)。仓库公开，读取它不需要 GitHub 登录。

运行 `codex plugin list --json` 查看本插件的实际安装目录；进入该插件目录后执行：

```bash
bash scripts/launch.sh --prepare-dependencies
```

成功时会输出含 `"status":"ready"` 的结果。它只准备 Python 依赖，不启动插件服务，也不调用 Claude。如果列表里的路径不清楚，可让 Codex 查找，或按[详细安装说明](install-and-recovery.md#first-time-preparation-both-paths)从相同版本准备依赖。不要猜缓存版本目录。

`codex: command not found` 表示当前终端找不到这个命令，不一定是桌面应用没有安装。可使用上面的安装请求，让 Codex 定位本机桌面应用内置的 CLI。仅安装一个终端版 CLI 不等于满足本插件的桌面环境要求。

已有同名市场时，按[迁移与回退说明](git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip)操作，保留旧来源以便恢复。

</details>

## 打开新任务，检查是否准备好

安装和依赖准备完成后，**在 Codex 里打开你的项目，并新建一个任务**，让客户端加载插件。安装过程中已经打开的旧任务不会自动换成新工具。

发给 Codex：

> 请用 Codex–Claude 协作插件检查本机是否已准备好：Claude Code 能否找到、是否支持所需参数、本地登录状态，以及 macOS 写保护是否可用。先只检查，不发起在线模型请求。如果找不到 Claude Code 或缺少所需功能，告诉我需要自己安装或升级什么；需要登录的步骤也告诉我，由我完成。

如果提示需要登录，在终端运行 `claude auth login`（或按你的 API/提供方方式配置认证），使用检查结果里指出的那个 Claude Code 程序。不要把密码、令牌或认证文件粘贴到对话中。

本地检查只读取本机状态。想确认登录在线可用，可以再说“做一次在线验证”：它会发一个很小的模型请求，使用少量 Claude 额度。网络、额度或模型权限问题导致的失败，不等于登录失效。

## 做第一次小任务

选项目中一份短文档，例如 `README.md`，发给 Codex：

> 用 Codex–Claude 协作插件，让 Claude 看看 README.md 的安装步骤，新手最容易卡在哪里？先不要改文件，你核对它的结论后告诉我。

这一步会实际调用 Claude，使用你自己的 Claude 额度。在 Git 项目里，Claude 默认在项目的独立副本中分析，可以运行命令复现问题；在普通目录里，它只读取和搜索。两种方式下，Claude 都不会写你的项目目录。

你应当能观察到：

1. **任务开始**：Codex 说明交给 Claude 的内容、任务类型（实现或分析）和执行方式，并报告真实状态。
2. **查看过程**：通过工作台链接查看进展和最近活动。
3. **收到结果**：Claude 先回答你的问题，再给依据；Codex 核对后告诉你哪些成立、哪些仍有疑问。

**报告交回不等于完成，Codex 核验后才算。** 如果只看到“环境检查完成”，还不能说明 Claude 已经执行了任务。遇到启动失败或中断，让 Codex 用原来的任务编号查询，先不要重复派发。

## 以后怎样使用

不需要任何项目设置。需要 Claude 时，在请求里明确说出来即可：

- **实现**：“让 Claude 按 docs/plan.md 第二阶段实现，完成后你检查 patch、跑测试，没问题再应用。”Claude 在副本里改代码、自己跑测试，交回 patch；Codex 核验后才应用到你的项目。插件不提交、不推送。
- **分析**：“让 Claude 评估……先给结论”“让 Claude 审查当前未提交的改动，只看错误处理”。Claude 先回答问题，再给依据。
- **纠正或补充**：直接在原对话里说，例如“这里理解错了，保留原来的登录流程，让 Claude 在上一轮的基础上改”。下一轮接着上一轮的结果做，你的新原话原样追加。方向完全变了时，Codex 会另开一个新任务。
- **停止**：“停止这个 Claude 任务”。已经写下的报告和改动仍会收回。
- **收尾**：任务做完后可以说“这个任务结束了，删掉 Claude 的副本”。patch、报告和裁决都会保留，但之后不能再续接这个任务。

Claude 提出需要你决定的问题时，Codex 会原样转告你，不替你拍板。

## 遇到问题时

| 现象 | 先做什么 |
| --- | --- |
| 找不到插件或工具 | 确认插件已启用，并在安装后打开一个新任务。 |
| 第一次启动一直等或超时 | 让 Codex 检查具体启动错误，并确认依赖准备已成功。不要反复启动 Claude。 |
| 已安装，但提示没有可用的 Claude | 让 Codex 检查本机 Claude Code 能否被找到、是否缺少所需参数，再确认登录。通过 nvm 等安装、桌面端找不到时，见[指定 Claude 路径](advanced-usage.zh-CN.md#桌面找不到-claude-时指定路径)。 |
| 提示副本建不起来（子模块、sparse checkout、未解决的冲突等） | 分析任务可以改用只读方式；实现任务需要先处理这些仓库状态，Codex 会说明原因。 |
| 提示需要 `sandbox-exec` | 这台机器上没有 macOS 写保护。分析任务可以改用只读方式；实现任务暂时不能执行。 |
| Claude 在副本里跑测试缺依赖 | 被 `.gitignore` 忽略的文件（如 `node_modules`、`.venv`）不会复制进副本。如果允许 Claude 联网安装依赖，请在请求里明确说明。 |
| 页面没自动打开 | 说“打开这次协作的工作台”，或点击 Codex 给出的链接。打开页面不需要重跑任务。 |
| 页面暂时没有新活动 | 让 Codex 查询执行状态；没有新活动不代表任务已停止。 |
| 想更新或回退插件 | 查看[版本切换步骤](git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip)。固定 ref（例如 `v0.7.1`）不会自动跳到新发布版本。 |
| 手上有 ZIP 包 | 完整安装包与 Git 安装使用不同流程。GitHub 的 “Download ZIP” 是源码归档，不能当作已经构建好的安装包。见 [ZIP 安装说明](advanced-usage.zh-CN.md#使用-zip-安装包)。 |

仍有问题时，向 [Issues](https://github.com/BryanYue/codex-claude-orchestrator/issues)提供 macOS 版本、插件版本、停在哪一步和去除敏感信息后的报错。请不要提交账号凭据、带访问令牌的工作台链接或业务代码。
