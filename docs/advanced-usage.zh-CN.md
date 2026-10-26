# 进阶使用与开发说明

[返回中文首页](../README.md) · [English overview](../README.en.md)

这里写给想了解运行细节的用户和开发者：本机 CLI 准入、一次运行怎样进行、运行中怎样沟通、结果里有什么、数据放在哪里，以及怎样开发和验证。首次使用请先看[快速开始](getting-started.zh-CN.md)；安装、升级与恢复另见英文的[安装与恢复](install-and-recovery.md)和 [Git 分发](git-marketplace.md)。

本页按 **v1.1.0** 维护，完整构建 `1.1.0+codex.20261025234436`；发布状态与已验证范围以[验证记录](../RELEASE-VERIFICATION.md)为准。Codex 侧必须遵守的派单、答疑与核验规则在插件的 [SKILL.md](../plugins/codex-claude-orchestrator/skills/codex-claude-orchestrator/SKILL.md) 和 [guide.md](../plugins/codex-claude-orchestrator/skills/codex-claude-orchestrator/references/guide.md)。

## 版本与文档的对应关系

| 内容 | 身份与更新方式 |
| --- | --- |
| 插件代码与安装包 | 版本 `v1.1.0`，完整版本 `1.1.0+codex.20261025234436`；每轮运行在 `plan.json` 里记录实际执行的完整版本 |
| 旧版本 | `v1.0.1`、`v0.7.1` 等标签保持冻结；1.1 能续接 1.0.x 的任务（1.0.x 的轮次没有运行中沟通）；1.0 起不能续接 0.7.x 的任务 |
| README、docs、验证记录 | `main` 保存当前说明；旧标签和 ZIP 内保留各自发布时的文档，不能把新能力归给旧安装 |
| Codex 侧规则 | `SKILL.md` 与 `references/` 随插件发布；安装新版本后需要新的 Codex 任务才会加载 |

## 使用 ZIP 安装包

完整安装包与 Git 安装是两条路径；Git 安装不要运行 `Install.command`。GitHub 的 “Download ZIP” 是源码归档，没有 `FILE-SHA256.json`，安装器会拒绝。

1. 安装并登录 **Codex 桌面客户端**、**Claude Code**；安装 `uv`。
2. 解压完整安装包，在终端运行 `bash /解压目录/Install.command`（也可双击；Gatekeeper 阻止双击时用 bash 入口，不必关闭系统安全设置）。安装器先按 `FILE-SHA256.json` 校验包内文件，再检查环境，然后用官方 `codex plugin` 命令安装。Claude 尚未安装或登录时也可以先装插件；实际派单会在启动前的检查中停下。
3. 按[依赖准备](install-and-recovery.md#first-time-preparation-both-paths)预热依赖，再打开一个新的 Codex 任务。

安装源保存在 `$CODEX_HOME/claude-orchestrator/catalog`（默认 `~/.codex/...`），下载包可以移走；使用独立的 `codex-claude-team` marketplace。更新时运行新包的 `Install.command`，旧安装源保留为 `catalog-previous-*` 备份。维护命令在解压目录运行：

```bash
bash Install.command --check            # 只检查完整性、环境与冲突，不安装
bash Install.command --diagnose-json    # 本机就绪摘要，不发模型请求
bash Install.command --configure-claude-bin /绝对路径/claude
bash Install.command --list-backups
bash Install.command --rollback <上一步返回的备份ID>
```

诊断摘要不含账号凭据、任务原文和运行产物，不发送遥测。回退只接受哈希校验通过的本插件备份。安装器要求 `/Applications` 下的 Codex.app 或 ChatGPT.app 内置支持 plugin 命令的 CLI；只有 PATH 里的 Codex CLI 不算满足桌面环境要求。更新或回退后开一个新 Codex 任务；旧任务已加载的代码不会热替换。

## 本机 Claude CLI

**插件只使用你本机已安装或明确配置的 Claude CLI。** 选择顺序为 `CLAUDE_BIN` → 插件设置文件 `$CODEX_HOME/claude-orchestrator/settings.json` 的 `claude_bin` → MCP 进程 PATH 中的 `claude`。插件不下载、安装、更新或切换 CLI，也不修改 shell 配置、持久 PATH、Claude 自动更新策略、登录或账号。

`claude_environment` 做本地检查，不发模型请求：

- 版本号只作诊断，没有版本白名单。
- `--help` 必须以完整选项形式列出派单所需的参数：`-p`、`--model`、`--effort`、`--input-format`、`--output-format`、`--verbose`、`--replay-user-messages`、`--json-schema`、`--session-id`、`--resume`、`--permission-mode`、`--tools`。缺少时状态为 `cli_incompatible`，并列出 `missing_flags`。help 只证明 CLI 声明了这些语法，不代表行为已测试。
- 派单还用到 `--permission-prompt-tool stdio`，它不在 `--help` 里列出。每次运行在 CLI 的初始化事件里核对：原生工具非空、有 `AskUserQuestion`（`interactive=true` 时）、`permissionMode` 为 `auto`。不满足时这一轮以 `not_started` 结束并说明原因，不降级到 `dontAsk` 或缩减的工具集。
- `claude auth status --json` 报告已登录；未登录为 `not_logged_in`，读不出为 `auth_check_failed`。
- 报告 `/usr/bin/sandbox-exec` 是否存在（`sandbox.available`），以及用一个最小规则实际探测能否生效（`sandbox.works`，失败时给出 `reason`）。副本档和只读档都需要它；装着但在另一层沙箱里被拒绝时，`works` 为 false。

`verify=true` 额外发一个很小的模型请求（不给工具、不保存会话），确认登录在线可用，会使用少量额度。失败按 `authentication_failed`、`quota_or_rate_limited`、`access_denied`、`network_error` 等分类；网络、额度或模型权限失败不等于登录失效。

`sandbox-exec` 探测不通过时，`claude_start` 直接报错并说明原因，不建立运行，也不会只靠提示词保护原目录。每次派单在启动 Claude 前重做本地检查，不通过就记为 `not_started`。packet 设置了 `max_budget_usd` 时，CLI 还必须支持 `--max-budget-usd`，否则同样不启动，不会忽略预算。

插件不钉住 CLI 身份。你用官方方式升级 Claude Code 后，新的运行直接使用新版本；续接时插件先尝试 `--resume` 原会话，接不上就开新会话并给出 `resume_fallback` 警告，任务书本身是完整的。

### 模型

`claude_models(cwd)` 读取本机 CLI 声明的模型选择项和 effort 选项，不发送用户消息，也不证明账户有权调用。`opus`、`sonnet` 等是官方类别别名，具体解析由 Claude 处理，插件不维护模型版本表。packet 必须给出 `model` 和 `effort`（low / medium / high / xhigh / max）；用户指定的型号原样传递。实际响应的模型记录在结果的 `models` 里，与请求的别名分开。具体要求以[官方模型配置](https://code.claude.com/docs/en/model-config)为准。

### 桌面找不到 Claude 时指定路径

MCP 进程不读取 `.zshrc` 等 shell 初始化文件，也不选择 nvm 版本。终端能找到 Claude、桌面端找不到时，先在终端用 `command -v claude` 确认绝对路径，然后：

- **Git 安装**：在启动 Codex 的环境里设置 `CLAUDE_BIN=/你确认的绝对路径/claude`，再重启 Codex。
- **ZIP 安装**：也可以运行 `bash Install.command --configure-claude-bin /你确认的绝对路径/claude`，把路径写进插件设置文件。

启动器会把 `CLAUDE_BIN` 所在目录放到插件自己的 PATH 前面，npm/nvm 的 `bin/claude` 因此能找到同目录的 `node`；选中的路径按原样保留，不解析到 `node_modules` 里。该配置只影响插件，不改全局 PATH。设置文件里的 `claude_bin` 失效时，插件不会退回 PATH 中的 `claude`，检查结果会说明恢复方法。

## MCP 工具

共 12 个工具。`claude_start` 从 packet 里读 `context`（`{chat, namespace?}`），其后的工具都以参数接受它；Codex 在同一对话的每次调用里带上同一个值，插件据此核对归属：属于某个对话的运行，省略或给错 `context` 都会被拒绝。

| 工具 | 作用 |
| --- | --- |
| `claude_environment(cwd, verify?, model?)` | 本机 CLI、参数、登录与 `sandbox-exec` 检查；`verify=true` 做一次小额在线验证 |
| `claude_models(cwd)` | 读取 CLI 声明的模型与 effort |
| `claude_start(packet, open_workbench?)` | 派发一轮运行，立即返回 `run_id` 和 `workbench`（链接 `url` 与打开决定 `open`） |
| `claude_status(run_id, context?)` | 运行状态、`outcome`、待处理的问答和 Codex 裁决 |
| `claude_wait(run_id, after, context?)` | 增量等待，每次最多 25 秒；返回 `reason`（interaction / activity / terminal / timeout）和 `pending` |
| `claude_reply(run_id, command_id, kind, …)` | 运行中回答问题或授权请求（`answer`）、补充信息（`update`）、打断当前这一步（`interrupt`），返回持久回执 |
| `claude_workbench(context, reopen?, outcome?)` | 本对话的工作台链接与打开决定；`reopen=true` 只在用户要求时用；`outcome` 记下宿主报告的打开结果 |
| `claude_result(run_id, artifact, context?)` | 分页读取运行产物原文 |
| `claude_cancel(run_id, reason, context?)` | 请求停止活动运行 |
| `claude_decide(run_id, verdict, next, note, evidence, …)` | 记录 Codex 裁决 |
| `claude_runs(limit?, offset?, task_id?, context?)` | 按开始时间倒序列出运行；带 `context` 时只列本对话的运行 |
| `claude_cleanup(run_id, context?)` | 删除副本档任务的独立副本 |

## 一次运行怎样进行

### packet 与任务书

packet 的字段见 [guide.md](../plugins/codex-claude-orchestrator/skills/codex-claude-orchestrator/references/guide.md#3-写-packet)。要点：

- `user_messages` 逐字放用户仍然有效的原话（`source=human` 或 `relayed`，最多 50 条）；确实没有原话时改写 `no_user_words_reason`，两者不能同时给。
- `inputs` 是规格、计划等原件，可以是文件或目录。开工时按字节复制进运行目录并设为只读；上限 2000 个文件、合计 64 MiB，符号链接、`.git` 和敏感文件名（`.env*`、`.claude`、`.codex`、`.ssh`、`.aws`、`.gnupg`、`.netrc`、`.npmrc`、`.pypirc`）跳过并列出。
- `constraints`、`done_when` 每条标 `origin=user|doc|coordinator`；`focus` 只用于 analyze。
- `timeout_seconds` 必填，范围 1–14400 秒；`max_budget_usd` 可选。
- `title`（≤60 字，单行）是给人看的任务名，续接时不传就沿用上一轮的标题，都没有时显示 `task_id`；`round_title`（≤80 字）说明这一轮做什么。超长会被截断，换行合并为空格。它们只用于显示，不参与路由和权限；用户原话仍逐字放在 `user_messages`。1.0.x 的记录保留原来的标题；1.0.x 的标题取自原话开头，不会被继承，从 1.0.x 续接且不传 `title` 时，新一轮显示 `task_id`。所以第一次续接 1.0.x 的任务时应带上简短的 `title`，之后的轮次会沿用它。插件不会从原话里截取标题。
- `context.chat` 是 Codex 为本对话取的稳定键（1–128 个字母、数字或 `. _ : -`）。没有它时工作台只给链接，`task_id` 也可能与其他对话冲突。插件启动环境设置了 `CODEX_CLAUDE_NAMESPACE` 时，它优先作为命名空间。
- `interactive` 默认 `true`：Claude 运行中可以提问和留言。设为 `false` 时，Claude 的提问会被立即拒绝并提示它写进 `questions`。
- 同一对话（`context`）里，同一个 `task_id` 同时只能有一个活动运行；不同对话用相同的 `task_id` 互不干扰。
- 1.0 之前的字段（`role`、`objective`、`user_request`、`review_mode`、`review_scope`、`owned_files` 等）直接拒绝，报错里给出新字段名。

插件把 packet 渲染成 UTF-8 Markdown 任务书，原样作为 Claude 的输入，并落盘为 `brief.md`、记录 sha256。任务书依次包含：优先级说明（原话与原件 > 约束 > 协调者说明 > 方法建议；冲突写进 `disputes`，只有用户能决定的事写进 `questions`）、用户原话（只出现一次、不转义）、原件清单、协调者说明（标明可能有误）、约束、完成标准、续接信息、任务指令、与 Codex 的沟通方式、环境说明和交付格式。任务指令写明按任务复杂度决定是否先列计划，不需要开工确认。实现任务的指令要求 Claude 自己运行测试、不提交不推送；分析任务要求先回答原问题再给依据，不套固定审查清单。

### 执行方式

| | copy（副本档） | readonly（只读档） |
| --- | --- | --- |
| 何时使用 | Git 仓库的默认值；`implement` 只能用它 | 普通目录、副本建不起来的分析任务，或需要直接在原目录分析时 |
| 工作目录 | 原仓库当前状态的独立 Git 副本 | 原目录 |
| 工具 | Claude Code 原生的全部工具（`--tools default`），权限模式 `auto` | 同左 |
| 写保护 | 原仓库、它的 `.git` 和插件状态由 macOS 拒写 | 原目录（Git 仓库时含 `.git`）和插件状态由 macOS 拒写，写入会失败；可以读、搜索、运行只读命令 |
| 交付 | Claude 把 `report.md`、`result.json` 写进副本里的交付目录 | 用结构化输出一次交回报告和结果 |

两档共同的规则：

- **工具不分档**：插件不维护工具白名单或黑名单，不按 analyze/implement 移除能力，不传 MCP 配置，也不禁用斜杠命令。用户和组织的设置、MCP 服务器、技能和 hooks 照常加载。隔离靠操作系统写保护和任务授权，而不是藏起工具。
- **写保护是前提**：两档都用 macOS `sandbox-exec` 拒绝 Claude 及其子进程写入受保护的路径，另外还保护插件代码、插件的 Python 环境和 Claude CLI 本身。凭证、网络和其他本机路径不隔离。`sandbox-exec` 是 Apple 已标记弃用的本机机制；不可用或探测失败时插件拒绝开工，不降级执行。
- **授权请求**：需要越权的操作会成为 `permission` 请求，由 Codex 按任务授权给出 `allow` 或 `deny`，插件不按工具名一律放行或拒绝。
- **联网**：`web` 默认关闭，只在用户同意时打开。它是写在任务书里的授权：关闭时任务书告诉 Claude 不要联网，但联网工具和 Bash 本身不受网络限制。

副本档的细节：

- **副本内容**：已跟踪文件和未被忽略的未跟踪文件，含未提交和已暂存的改动。被 `.gitignore` 忽略的文件（如 `node_modules`、`.venv`、构建产物）不复制；测试需要时由 Claude 在副本里重新安装，需要联网的应先取得用户同意。敏感的未跟踪文件跳过并给出 `sensitive_inputs_skipped` 警告。副本没有 remote，不与原仓库共享对象。
- **写保护范围**：原仓库的所有 worktree、Git 元数据和插件状态目录。
- **建不起来的情况**：子模块、sparse/skip-worktree 或 assume-unchanged 条目、未解决的合并冲突、Git alternates、指向仓库外的符号链接或硬链接、边界扫描超过 60 秒。此时运行记为 `not_started`；分析任务可以改用 readonly，实现任务需要先处理仓库状态。
- **Workflow**：Git 仓库中的分析任务会在副本里放一个可选的参数化模板 `codex-analyze`（参数 `brief_path`、`focus`），按关注点分别审查、独立核验，最后先回答原问题；是否使用由 Claude 判断。每轮运行都把 `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` 设为本轮时限，避免 `claude -p` 提前停止等待后台 Workflow；运行中后台代理和 Workflow 在跑时插件不关闭输入。
- **嵌套沙箱**：在 `sandbox-exec` 里运行的部分工具链（例如 SwiftPM）可能失败或跳过测试；跳过不等于通过。

### 宿主指令层

Claude 会话照常加载用户与项目的 `CLAUDE.md`、`CLAUDE.local.md`、rules、settings 中的 hooks、MCP 服务器、技能以及受管设置。插件不隔离也不修改它们，只把本次会加载的文件和 hook 事件列进 `instruction-layer.json`，并在任务书里写明：与任务书的原话、原件或约束冲突时以任务书为准，冲突写进 `disputes`。

### 运行中沟通

`interactive=true`（默认）时，Codex 可以在运行中和 Claude 交换信息：插件在运行期间保持 Claude 的输入打开，把回答和补充写进同一个会话。

- **阻塞的问题和授权请求**：Claude 用原生的 `AskUserQuestion` 提严重问题（不先确认就会做错、与原话或验收冲突、超出授权），插件记为待处理的 `question`；需要越权的操作成为 `permission` 请求。两者都会阻塞。CLI 本身停在请求上，插件同时挂起这次执行的其余子进程，包括后台命令（`hold.mode=family_hold`）。后台 Workflow 和后台代理在 CLI 进程内部，插件用 CLI 原生的 `stop_task` 停止它们（`hold.stopped_tasks`），回答仍直接交给等待中的工具。CLI 没有确认停止时，插件停止整个执行，等所有待回答的问题都有回答后，在原会话里一次送达全部回答（`stop_for_answer`），并提醒 Claude 先检查工作目录和后台任务的状态。Codex 打断时，CLI 撤回正在等待的问题，状态记为 `cancelled`。
- **不阻塞的说明和异议**：Claude 把 `{"type": "note", ...}` 或 `{"type": "dispute", ...}` 追加到任务书指定的 `notes.jsonl`，插件立刻登记并让 `claude_wait` 返回，Claude 不等回复。
- **等待**：`claude_wait` 在出现问答（`reason=interaction`）、可见进展（`activity`）、运行结束（`terminal`）或超时（`timeout`）时返回；`pending` 列出待回答的问题和授权请求。模型和工具的计数事件只推进游标，不会提前唤醒。
- **回复**：`claude_reply(run_id, command_id, kind, text?, interaction_id?, decision?, answers?, origin, expected_revision?, context)`。

| kind | 用途 | 说明 |
| --- | --- | --- |
| `answer` | 回答 `pending` 里的问题或授权请求 | 必须带 `interaction_id`；授权请求还要 `decision=allow|deny`；一次问了多个问题时可用 `answers` 按问题给答案，每个问题都要有答案。同一问题只接受一个回答 |
| `update` | 运行中补充信息，不打断 | Claude 做完当前这一步后读到，消息带【Codex 补充】标记 |
| `interrupt` | 打断当前这一步 | 可带 `text`，紧接着作为改向指令送达。打断请求不证明进程已停；结束整个运行用 `claude_cancel` |

- **来源**：`origin=coordinator` 是 Codex 自己的话；`origin=human` 只用于逐字转交的用户原话。Claude 看到的回答标明来自“Codex 协调者”还是“用户原话”，任务书写明用户原话的效力高于协调者的回答。
- **谁来回答**：Codex 在已有授权内能判断的直接回答；只有核心需求、验收冲突、超出授权或必须由用户拍板的事才转给用户，先回答不受影响的部分。
- **回执**：命令、对应问题的状态和修订号在同一个事务里落盘。`delivery.state` 依次为 `queued`（已记录）→ `written`（整条已写入 CLI 的输入管道）→ `echoed`（CLI 回显，或把回答作为工具结果交给了会话）；`failed` 表示 run 在送达前结束。`echoed` 只说明送达，不说明 Claude 已理解或照做。
- **幂等与并发**：`command_id` 由 Codex 生成且每条唯一。同一 `command_id` 同样内容重发，返回第一次的回执，run 结束后也能取回；内容不同会被拒绝。`expected_revision` 填读到的 `revision`；两个回答同时到达时只有一个生效，另一个收到“已回答”或“修订号过期”的错误。
- **超时与结束**：等用户期间 Claude 一直暂停；到截止时间仍未回答就按超时结束，未回答的问题在结果里列为 `questions_unanswered`。插件让 CLI 报告会话状态（`CLAUDE_CODE_SDK_READS_SESSION_STATE=1`），在 CLI 报告 `idle`、没有排队或未送达的消息、没有待回答的问题时关闭输入，run 随之结束。CLI 在后台代理和 Workflow 结束、它们引出的后续回复结束之后才报告 `idle`。不报告会话状态的 CLI 则以本轮结果和插件记录的后台任务为准。CLI 在后台命令（Bash 后台、Monitor）还在跑时就报告 `idle`，它们结束后再起一轮回复读取结果；所以插件记录的后台命令未结束时 `idle` 不算结束，输入保持打开，直到它们结束、后续回复结束并再次 `idle`，或取消、截止。Claude 只剩不再需要的后台命令（如常驻服务）时，在 notes 文件写一行 `{"type": "final", "after": "<编号>"}`（after 是它读到的最新一条 Codex 补充/打断标题里的编号，恢复消息末尾写明的编号，或一条都没有时的 run_id），插件在下一次 `idle` 用 `stop_task` 停掉它们（`background_commands_stopped` 警告）再关闭输入；Codex 之后再写入任何消息，这个信号作废。之后再要补充，用 `continue_from` 开下一轮。
- **非交互运行**：`interactive=false` 时没有实时通道，Claude 的提问被立即拒绝并提示写进 `questions`，授权请求被拒绝。1.0.x 开始的轮次同样没有实时通道，`claude_reply` 对它们报错。

### 结果

运行结束后插件写出 `outcome.json`，`claude_status` 返回其中的事实：

- **`run_outcome`**：`ok`（进程交回了最终结果）、`timeout`、`cancelled`、`crashed`（退出但没有最终结果，或在已经开始的后续回复里退出、没有它的结果；此前的结果只作历史保留）、`not_started`（启动前的检查或副本准备失败）、`lost`（负责监督的 bridge 意外退出）。它只描述进程怎么结束，不代表通过。
- **`process_stopped`**：进程是否已确认停止；只有已停止的运行才能裁决或续接。
- **`claimed_status`**：Claude 在 `result.json` 里的自评（completed / partial / blocked）。
- **`warnings`**：事实性警告，不改变 `run_outcome`。常见的有 `questions_for_user`、`disputes_present`、`not_verified_present`、`protected_touched`、`outside_hint`、`original_changed_during_run`、`source_not_read`、`tool_denied`、`workflow_incomplete`、`report_missing`、`result_missing`、`result_malformed`、`patch_unavailable`、`provider_error`、`resume_fallback`、`sensitive_inputs_skipped`、`residual_processes_stopped`、`process_stop_unconfirmed`，以及运行中沟通相关的 `questions_unanswered`（run 结束时 Claude 的阻塞问题仍没有回答）和 `turn_interrupted`（之前某一轮回复以错误或打断结束，最终结果取最后一轮）。
- **`interactions`**：问答和说明的计数、未回答的问题和暂停记录；逐条内容在 `claude_status` 的 `interactions` 里。
- **`changes`**（副本档）：插件在受保护的运行目录里另建基线对象库，记录起止树，生成 `changes.patch`（本轮改动，含新增、删除和二进制）和 `delivery.patch`（所有尚未应用到原仓库的改动，含此前未合入的轮次）。基线对象库和索引每轮一份，放在该轮的运行目录里。文件按 `write_hint`、`protected` 分为 in_scope / outside_hint / protected，这两个字段只用于事后分类，运行中不拦截。`check_hint` 和 `apply_hint` 针对 delivery.patch，给出 `git apply --check` 与 `git apply` 命令：patch 以原仓库的工作区内容（含未提交改动）为基线，直接作用于工作区，不用 `--3way`（它要求索引一致，原仓库有未暂存改动时会被拒绝）。分析任务在副本里写出的文件也会出现在 patch 里，它们是证据或交付物，不一定要应用到项目。
- 另有 `report`、`result`、`brief` 的路径与哈希，`session_id`、实际模型、用量、耗时、所用 CLI 和插件版本。

超时或取消时，副本档里已经写下的报告和改动仍会收回；只读档只有交回结构化结果后才有报告。CLI 最终回复的文本（如有）保存在 `final_message`。

`claude_result(run_id, artifact)` 按 UTF-8 安全的字节页读取原文（默认每页 200,000 字节，最多 1,000,000），跟着 `next_offset` 读到 `end_of_artifact` 并核对 `sha256`。可读的 artifact：`report`、`result`、`brief`、`patch`、`delivery_patch`、`carried_patch`、`outcome`、`packet`、`plan`、`inputs`、`instruction_layer`、`final_message`、`decision`、`decision_history`、`execution`、`environment`、`command`、`stream`、`stderr`、`bridge_log`。

### 裁决、续接与清理

- **裁决**：`claude_decide` 的 `verdict`（accepted / accepted_with_corrections / rejected）与 `next`（done / next_round / codex_finishes）分开写，`note` 说明理由，`evidence` 列出 Codex 实际做过的核验。`result.json` 有 `items` 时，`item_decisions` 要逐条给出 accepted / downgraded / rejected，后两者写原因；有 `protected_touched` 警告时，每个路径都要列进 `protected_confirmed`。任何结束状态只要进程已停止都可以裁决，再次裁决追加历史。
- **续接**：下一轮用 `continue_from=<上一轮 run_id>`，`task_id`、`kind`、`profile` 不变（只读档还要求同一 `cwd`）；`user_messages` 必须原样包含上一轮的全部原话，只能在后面追加。上一轮必须已结束且进程已停止；上一轮属于某个对话时，续接必须带同一个 `context`。副本档每一轮都按原仓库当前状态重新建立副本，再放上最近一份尚未记录为已合入的 delivery.patch，已交付过的任务文件即使被忽略也会带入；被忽略的依赖目录不跨轮保留。合入后又修正过的，要先用 `claude_decide(..., applied=true)` 记录，否则这一轮以 not_started 结束并说明冲突。上一轮的报告路径和此前各轮的完整裁决会写进新任务书。方向或需求结构变了，就换一个新的 `task_id`。
- **清理**：`claude_cleanup` 只能对副本档任务的最新一轮调用，要求该轮已裁决、同一副本的所有运行都已停止。它删除副本，保留 patch、报告、任务书、stream 和裁决；之后不能再续接。

### 重启与中断

运行由独立会话里的 bridge 进程监督，MCP 服务关闭时不会停止它们。新的 MCP 连接启动后，用原来的 `run_id` 和同一个 `context` 继续查询；仍在运行的 bridge 会继续被观察。问答、命令和回执存在控制库里，不随 MCP 连接消失。bridge 没写出结果就退出时，插件停止残留的 Claude 进程组，把这一轮记为 `lost`，并收回已经写下的报告和改动。bridge 收到 SIGTERM/SIGHUP 时按取消处理。

## 工作台

工作台是只读页面，只绑定 `127.0.0.1` 的随机端口，访问令牌放在链接的 `#` 片段里；页面拒绝写请求，回答 Claude、补充要求和纠正都在 Codex 对话里进行。每个 MCP 进程有自己的随机端口和令牌，重启后用 `claude_status` 或 `claude_workbench` 取新链接。已经打开的旧页面连不上新进程，会一直显示“暂时连不上，正在重试”，并提示在 Codex 里取新的工作台链接；它不能自己换成新链接，插件也不会因此自动再开页面。短暂断线时旧页面会继续重试并自己恢复。

**每个 Codex 对话一个页面。** 页面按 `context` 显示本对话的任务，任务和轮次在页面里切换；没有 `context` 时显示全部任务。打开规则：

- 打开请求按命名空间和对话（`context`）记录，不按连接或 run 记录。`claude_start` 第一次为这个对话请求时，响应的 `workbench.open.action` 为 `open_in_codex`，插件先把请求记为已发出，再由 Codex 调用一次 `open_in_codex`；宿主返回 `queued` 也算已请求。
- 之后的响应都是 `link_only`：链接变了、MCP 重连、对话压缩或新一轮都不会再自动打开。宿主报告的结果可以用 `claude_workbench(context, outcome=queued|failed|unknown)` 记下，但任何结果都不会触发自动重试。
- 用户要求再打开时用 `claude_workbench(context, reopen=true)`。监督子代理派单时传 `open_workbench=false`，不打开页面。
- 没有 `context.chat` 时永远只给链接。插件保证的是“自动请求最多一次”，宿主实际是否只开了一个标签页，插件无法确认。

页面按对话分组任务，分开显示项目、任务标题、轮次（含本轮说明）和状态；同一条续接链（`continue_from` 连起来的各轮）放在同一张任务卡里，包括 1.0.x 或不带 `context` 开始、后来在本对话续接的早先轮次，它们只是一起显示，归属和授权不变；同名但不在同一条链上的任务分开显示。详情里分开显示运行事实、Claude 自称和 Codex 裁决，并提供警告、改动清单、报告和 Claude 实际收到的任务书原文。Claude 在等回答时，页面顶部显示「Claude 在等回答」（授权请求显示「请求授权」），任务卡片标「等回答」；已处理的问答折叠在「已处理的问答」里。过程记录显示 Codex 的补充和打断正文（最多 480 字）及来源（Codex 协调者或用户原话），完整原文以命令记录和原始流为准。没有新活动不等于任务停止。

## 用量与费用

用量取自 CLI 最终 result 事件，分两个范围保存，互不相加：

- **CLI 会话累计估算**（工作台“CLI 会话累计估算”）：`modelUsage` 与 `total_cost_usd`，按模型列出，包含子代理；续接会话的累计可能包含之前轮次，不是本轮新增，也不能跨轮相加。
- **主代理最终回报**：最终 `usage`，只代表主代理最后一次回报，不是整任务总量。

缺项或畸形值记为未知而不是 0。费用是 Claude CLI 的客户端估算，不是实际扣费或订阅剩余额度。

## 监督子代理

需要并行或隔离上下文时，主 Codex 可以按 [supervisor.md](../plugins/codex-claude-orchestrator/skills/codex-claude-orchestrator/references/supervisor.md) 派一个原生 Codex 监督子代理，负责启动、等待、按要求取消和回传事实。它使用主代理给的 `context`，派单时传 `open_workbench=false`。主代理要写明哪些问题它可以在已有授权内直接 `claude_reply`，没写明时一律原样回传。它不改业务文件、不裁决、不打开工作台；核验、`claude_decide` 和对用户的交付仍由主 Codex 负责。短任务由主代理直接管理即可。

## 执行与数据边界

- **运行记录**：默认在 `~/.codex/claude-orchestrator/v1/`，每轮一个 `runs/<run_id>/` 目录，副本档的副本放在该任务第一轮的运行目录里。这个状态根不跟随 `CODEX_HOME`；需要其他位置时用 `CLAUDE_ORCHESTRATOR_STATE_DIR` 显式指定，改动不会迁移已有记录。0.7.x 及更早的记录留在原位置，1.0 起不读取也不修改。
- **控制库**：1.1 新增 `~/.codex/claude-orchestrator/v2/control.sqlite3`（与 `v1` 同在状态根下），只记录需要一起变化的事实：对话归属、命令与回执、问答，以及工作台打开请求。每次修改是一个事务。它使用 SQLite 回滚日志（`journal_mode=DELETE`），不启用 WAL，打开时核对实际模式。大日志和产物仍是 `v1/runs` 下的文件。1.0.x 的运行在控制库里没有记录，照常通过 `v1` 读取和续接，只是没有实时通道；插件不把它们迁入控制库。
- **跟随 `CODEX_HOME` 的位置**：插件设置 `settings.json`、按完整版本区分的依赖环境 `venvs/<版本>`（可用 `CLAUDE_ORCHESTRATOR_ENV_DIR` 覆盖）和 ZIP 安装源 `catalog`，都在 `$CODEX_HOME/claude-orchestrator/` 下。
- **记录里有项目资料**：任务书、原件副本、项目副本和完整 stream 都在本机，不随分发包发送。除 `claude_cleanup` 删除副本外，插件不自动删除运行记录；按团队留存规则在确认没有活动运行后自行归档或清理。
- **不自动合入**：插件不合并、提交、推送或发布；patch 由 Codex 核验后应用。
- **原仓库可以继续改**：运行期间原仓库有变化时只给 `original_changed_during_run` 警告，应用 patch 前用 `--check` 确认。

## 可复用来源

- 实际依赖：[官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)，锁定 `mcp==2.2.0`，由 `uv.lock` 固定；不自写 JSON-RPC/MCP 协议。
- 调用基础：[Claude Code 官方 CLI](https://code.claude.com/docs/en/cli-reference) 的 `-p`、stream-json、JSON schema、session/resume 与权限参数。
- 生命周期体验参考：[OpenAI 官方 codex-plugin-cc](https://github.com/openai/codex-plugin-cc)。它是 Claude 调用 Codex 的反向插件；借鉴 status/result/cancel 等接口分工，没有当作本插件后端。
- 调研过 [agent-bridge](https://github.com/teamnebula-ai/agent-bridge)、[Claude-Bridge](https://github.com/constripacity/Claude-Bridge) 的任务文件、事件与本地查看思路；它们不是依赖，也没有复制其源码。

## 开发与验证

进入 `plugins/codex-claude-orchestrator`：

```bash
uv sync --frozen
uv run python scripts/run_tests.py --suite all
```

测试入口为子进程提供独立的 `TMPDIR` 和 `CLAUDE_ORCHESTRATOR_STATE_DIR`，结束后删除，不触碰真实运行记录。可用 `--suite plugin` 或 `--suite bridge` 做定向回归。

在本机查看已有运行记录、但不经过 Codex 时，可以在同一目录前台启动只读工作台，它会打印 `details_url`，Ctrl-C 结束：

```bash
uv run python scripts/server.py --dashboard
```

以下命令从仓库根目录运行。代码检查（固定版本的 Ruff、部分模块的严格 Mypy 与复杂度上限）：

```bash
uv run --project plugins/codex-claude-orchestrator --group dev --frozen python tools/check_quality.py
```

`tools/build_distribution.py` 从干净、已提交的 Git commit 构建可复现的 ZIP 分发包，只用 Python ≥3.11 标准库（macOS 系统自带的 `python3` 可能更旧，因此用项目锁定的环境运行）：

```bash
uv run --project plugins/codex-claude-orchestrator --frozen --no-dev python tools/tests/test_build_distribution.py
uv run --project plugins/codex-claude-orchestrator --frozen --no-dev python tools/build_distribution.py --source-root . --output <目标目录>
```

省略 `--output` 时输出到仓库内已被 Git 忽略的 `dist/codex-claude-orchestrator-macos-<版本>`。自动回归、真实 Claude 调用、原生宿主接入和浏览器观察分别记录在[验证记录](../RELEASE-VERIFICATION.md)；小夹具通过不证明生产长任务没有偏差，历史版本的通过数也不能当作本版证据。

源码与 Git marketplace 由个人仓库 BryanYue/codex-claude-orchestrator 维护。问题请通过仓库 Issues 提交，并去除凭据、工作台令牌与业务代码。当前未指定开源许可证。要卸载插件，在 Codex 插件界面卸载；运行记录按团队规则另行保留或清理。
