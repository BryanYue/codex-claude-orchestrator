# 进阶使用与开发说明

[返回中文首页](../README.md) · [English overview](../README.en.md)

这里保留本机 CLI 准入、运行记录、权限边界和开发验证等细节。首次使用请先看[快速开始](getting-started.zh-CN.md)。本页为中文技术参考；[安装与恢复](install-and-recovery.md)及[Git 分发](git-marketplace.md)另有英文说明。

本页按 **v0.7.0** 源码候选维护，完整构建 `0.7.0+codex.20261003090000`；发布与验收状态以验证记录为准，历史机制保留首次引入版本。下面的 ZIP 安装器命令只适用于完整构建包；Git marketplace 用户继续使用所选 Git 来源。

## 版本与文档的对应关系

| 内容 | 身份与更新方式 |
| --- | --- |
| 插件代码与安装包 | 目标版本 `v0.7.0`，完整版本 `0.7.0+codex.20261003090000`；发布后标签、提交和资产固定，文档修订不替换发布包 |
| 旧版本 | `v0.6.1`、`v0.6.2` 等标签保持冻结；旧安装不会自动取得 0.7.0 的独立审查与 bundled-only 说明 |
| README、docs、发布验证 | `main` 保存当前源码说明与验收事实；旧标签和 ZIP 内保留各自发布时的文档快照，不能把新源码能力归给旧安装 |
| `references/` 协调说明 | 随插件发布，由 manifest 的内容版本与哈希标识；新 run 只固定 bundled，旧 run 的不可变绑定保留 |
| 历史变更与设计 | CHANGELOG 的旧版本、RELEASE-VERIFICATION 的历史段和 UI 设计提案保留原时间/基线；不能当作当前功能或新版本验收 |
| 已采用的项目协议与任务输入 | 使用各自批准并钉住的协议、基线和内容 digest；插件或文档更新不替换它们 |

查看“当前安装了什么”用实际安装清单与原生 MCP 诊断；查看“本轮执行了什么”用该轮的插件身份和 provider 回执。插件文件摘要包含随包 Markdown，因此 `main` 上仅改文档也可能改变源码树的整体摘要；不能据此改写已发布 `v0.6.1` 的摘要或声称已安装内容同步改变。

## 使用 ZIP 安装包开始

1. 安装并登录 **Codex 桌面客户端**、**Claude Code CLI**；安装 `uv`（[官方说明](https://docs.astral.sh/uv/getting-started/installation/)，已有 Homebrew 可用 `brew install uv`）。
2. 解压完整安装包，在终端运行 `bash /解压目录/Install.command`；也可双击 **Install.command**。若下载后的 Gatekeeper 阻止双击，使用上述 bash 入口，不必关闭系统安全设置。安装器校验包内哈希并检查环境，使用官方 Codex plugin 命令安装。Claude 尚未安装或登录时也可以先安装插件，再由诊断提示补齐；实际委派仍会被预检阻止。
3. 安装后先按[依赖准备](install-and-recovery.md#first-time-preparation-both-paths)显式预热，再打开一个新的 Codex 任务，让客户端加载插件。项目已经有采用入口时，直接提任务；首次采用一个项目只需说：

   > 这个项目以后采用 Codex–Claude 协作流程。

4. 此后照常说“按计划继续”“实现这个需求”“这里理解错了”“停止这个方向”。**不用每轮点名插件或粘贴预检、派单、详情、验收的长口令。**

项目采用会给现有 `AGENTS.md` 追加独立的插件块，并保存 `.agents/codex-claude/` 中的参考协议与配置。保留原有规则，可随项目代码共享；新同事无需重新采用。不会修改全局规则，也不表示任何 plan、基线或 oracle 已获批准。采用后的项目文件是否提交仍按项目授权处理。

安装源自动保存到 `~/.codex/claude-orchestrator/catalog`（尊重 CODEX_HOME）；下载包可以移走。使用独立 `codex-claude-team` marketplace，不占用同事的 personal。更新仍运行新包的 Install.command；旧安装源保留供回退。`bash Install.command --check` 只检查完整性/环境/冲突。

需要维护时，在解压目录运行：

```bash
bash Install.command --diagnose-json
bash Install.command --configure-claude-bin /绝对路径/claude
bash Install.command --list-backups
bash Install.command --rollback <上一步返回的备份ID>
```

诊断摘要不含账号身份、凭据、任务原文和原始 run/session 标识；不会发送遥测。回退只接受哈希校验通过的本插件备份，并保留当前版本，随后通过官方插件命令重新安装。更新或回退后开一个新 Codex 任务加载相应版本；旧任务已加载的工具不会自动热替换。

每个人使用自己的 Claude 本机安装、认证和额度。安装器只检查并报告本机 Claude CLI，不下载、复制或切换执行版本；不自动登录或切换账号。Skill 会在首次实际需要委派时调用在线凭证检查（bridge 每轮的自动预检仅查本地配置），会使用少量 Claude 额度；近期成功调用可复用。

首发在 macOS Apple Silicon 实测；Intel 尚未单独实测，Windows 不在本版范围。安装器要求在标准 /Applications 目录中检测到 Codex.app 或 ChatGPT.app 内置且支持 plugin 命令的 Codex CLI。仅安装 PATH Codex CLI 的宿主当前不在支持范围，不能作为桌面兼容性证据；--check 不执行安装，但条件不满足会返回非零退出码。

## 本机 Claude CLI

**插件只使用你本机已安装或明确配置的 Claude CLI。** 选择顺序为 `CLAUDE_BIN` → 插件 `settings.json` 的 `claude_bin` → MCP 进程 PATH 中的 `claude`。插件不下载、安装、更新、回退、复制或切换 CLI，不做版本资格验证，也不修改 shell、PATH 持久配置、Claude 自动更新策略、登录或账号。升级 CLI 由你用官方方式自行完成（原生安装的更新行为见 [Claude 官方说明](https://code.claude.com/docs/en/setup#auto-updates)），之后在 Codex 中说“检查 Claude 安装、登录与版本兼容情况”即可。

版本号只作诊断，不作白名单：新版本只要满足本轮任务的实际要求就能使用。每轮任务的准入检查是：

- `--help` 以完整选项形式列出本轮所需 flag（续跑另需 `--resume`）；缺少时报告具体 flag，不派单。
- `claude auth status --json` 报告已登录；未登录、状态读不出分别说明。
- packet 明确要求 `max_turns` / `max_budget_usd` 时，对应 flag 必须存在，否则本轮被阻止，不会忽略预算。

help 只证明 CLI 声明了这些语法，不代表行为已测试。行为由每轮的实际证据确认：启动前的 hook 自检、每个文件工具的 hook 覆盖、实际使用的工具必须在本轮工具集内（strict/implement 按原工具集检查，isolated 可用 Bash 等审查工具）、会话身份一致以及结构化结果校验。`--version` 或 `--help` 失败时，诊断会保留退出码、信号或超时类型和脱敏后的输出摘要。

每次派单固定所选可执行文件的路径和 SHA-256，启动前再次核对；变化即失败，不临时换用其他 CLI。续跑要求本机 CLI 与上一轮是同一文件：你升级或更换 CLI 后，续跑会被明确拒绝并提示改用 fresh 新一轮，不会恢复旧版本。旧记录是否可续跑须同时满足完整的续跑协议身份和本机 CLI 身份检查；早期受管私有 CLI 身份不能恢复使用。缺少完整身份凭据的历史 run，需核对旧结果后另起 fresh 轮次。

**旧版本的受管执行版本已停用。** 早期版本在 `~/.codex/claude-orchestrator/cli`（尊重 CODEX_HOME）保存的私有 CLI、选择记录、资格回执和维护状态会原样保留、不会删除，但不再参与派单、续跑或模型目录，也不会阻断使用本机 CLI。`claude_cli_status` 会把它们列为历史记录，并显示保留文件的字节数（只统计记录大小与实际文件一致的版本）。确认不再需要后可自行清理该目录；运行证据不在其中。

| 需求 | 工具行为 |
| --- | --- |
| 查看 Claude 版本和兼容情况 | `claude_cli_status`，列出本机 CLI、安装、登录、实际调用记录和历史受管记录 |
| 旧的准备/验证/切换/回退/刷新/策略请求 | `claude_cli_update` 已退役，只返回说明，不改变任何状态 |
| 停止旧版本留下的验证任务 | `claude_cli_update(action="cancel", job_id=...)`，等确认进程已停止 |

### 模型升级

`opus`、`sonnet`、`haiku` 等是官方类别别名，不是固定模型版本。`claude_models(cwd)` 查询本机 CLI 公布的模型选择项、`resolvedModel` 和 effort 选项；不再支持指定历史 CLI 身份。新模型类别会自动出现在返回目录中；插件不会自行切到更贵的类别，或把目录声明当作账户调用成功。组织、提供方和官方模型环境覆盖仍由 Claude 处理。

模型升级可能同时要求 CLI 升级；旧 CLI 仍可用不代表已经使用新模型。请求类别别名与实际 provider 回报分别保存；历史任务不会被新目录改写。具体要求以[官方模型配置](https://code.claude.com/docs/en/model-config)为准。

### 桌面找不到 Claude 时指定路径

下面的 `Install.command` 命令仅用于 ZIP 安装。Git marketplace 用户不要执行它或其配置参数，可在启动 Codex 的环境中设置 `CLAUDE_BIN`。

若终端能找到 Claude、桌面找不到，在终端用 `command -v claude` 确认具体路径，再运行：

```bash
bash Install.command --configure-claude-bin /你确认的绝对路径/claude
bash Install.command --diagnose-json
```

该配置只影响插件，不 source `.zshrc`、不切换 Node、不改全局 PATH。npm/nvm 的 `bin/claude` 符号链接会保留原路径，让旧式 Node shim 找到同一 bin 目录的 Node。更新配置后在新 Codex 任务加载插件。

## 检查状态与执行详情

配置检查、环境检查、路由选择都不启动业务 Claude 任务。工具摘要会说明本次操作；配置检查失败时采用状态为 unknown（adopted=null），不能当作确认未采用。停用、未采用、采用但待修复、采用且就绪分别显示。只读检查支持项目目录别名，返回请求目录和真实目录；派单使用经过检查的真实目录。同一仓库的别名共用执行互斥，嵌套 Git 仓库独立检查。配置、协议等受保护路径仍拒绝符号链接；enable/disable 等写入使用真实项目根。

派单后先显示具体任务 ID、目标、范围、请求模型与真实执行状态，并提供可点击工作台链接。同一 Codex 任务内，仅主代理在确认尚未请求打开时自动请求一次，发出前即记账；queued 只表示面板排队，失败不自动重开，上下文不明时只给链接。后续执行通过工作台任务列表查看；用户显式要求时可重新取得同一 run 的链接并打开，这不会新增 Claude 执行。主代理从自己的 MCP 取得 Viewer 链接，不直接依赖监督席的临时服务。详情失败不会重新派单。单页范围是同一任务内存活的 Viewer，跨 MCP/重启的统一入口和宿主深链复用标签页不作保证。主 Codex 用同一 run_id 等待增量并核验结束结果。插件工具标题是否出现在宿主汇总卡片，由 Codex 客户端决定。

工作台详情顶部显示执行、报告、核验三项事实，执行中时附最近公开活动及其时间，顶栏显示页面最后同步时间。页面不断刷新不代表 Claude 有新活动；连接中断也不证明任务停止。可展开的“本轮执行身份”分开显示请求模型与实际响应模型；实际模型来自 assistant.message.model 或 result.modelUsage，可能有多个，只有初始化信息时仍显示待 provider 回报。reported 是模型交回报告，accepted 是 Codex 核验决定；两者保持分开。

## 自动选择和手动指定

| 你说的话 | Codex 的动作 |
| --- | --- |
| 按已确认计划继续 | 读取项目约定/现有进度，按任务适配和偏好选执行者 |
| 提高 Claude 的执行偏好 | 调整该项目的路由权重；不改全局模型 |
| 先改成仅手动调用 Claude | 切换 manual，日常由 Codex 处理 |
| 这一次让 Claude 执行 | 单次覆盖，项目默认保持原样 |
| 这次由 Codex 处理 | 单次指定 Codex |
| 完整审查并复现问题 | 保留原话，在独立副本用工具检查缺陷与质量，核对 OS 写保护、覆盖和报告 |
| 用 Claude 的某个 Workflow 跑这次审查 | isolated 内可选择脚本；兼容 workflow_review 仍精确绑定保存名称/哈希，真实调用与完成分别核对 |
| 这里理解错了，按这个反例修正 | 保留原验收，选择局部续接或停止后重新派单 |
| 打开执行详情 | 在工作台打开同一执行：三项事实与报告直接显示，过程、文件变化、执行身份和技术记录可展开 |

偏好配置在 `.agents/codex-claude/routing.json`：auto/manual，Codex/Claude 各自 0..100。提供“按任务选择”“仅手动”“Claude 偏好”三种预设；高级设置省略的字段保留当前值。默认双方50，加任务适配分；是可调的启发式偏好，不是调用配额、概率或模型能力排名。0表示不自动选择该执行者，手动仍可指定。澄清、小改动和架构裁决默认倾向 Codex；明确方案实现与独立审查默认倾向 Claude。路由同时考虑范围是否明确、独立性、主 Codex 已有上下文、响应时效、所需工具和已知可用性。需要外部连接器或超出当前能力时会解释阻断原因；手动指定保留原选择，不悄悄换执行者。路由建议不产生执行授权。

路由偏好不扩大权限或批准某个多席策略。isolated review 中 Agent/Workflow 是可选方法；兼容命名只读 Workflow 保留单独入口。工具可用、实际执行、完整报告与语义核验分别报告。

## 完整审查：覆盖与执行模式分别选择

新派单保留 `user_request` 用户原话，objective 只是执行摘要；原始来源、指定基线和正式验收不因审查方案改变。`review_scope` 为 defects、quality 或 full，默认 full；完整审查覆盖正确性/安全、架构职责、复杂度/重复/死代码、测试、文档和提示词。报告实际覆盖与未完成项，允许某维度零发现。

| 模式 | 工具与资源边界 |
| --- | --- |
| `review_mode=isolated` | 独立可写 clone 中运行 Bash、Agent、Skill、Workflow、临时脚本与测试；OS 拒绝写原仓、相关 Git 目录、要求源与可信控制记录。带原话的新 MCP Git review 默认此模式 |
| `review_mode=strict` | 只读取/搜索；仓外仅精确 requirement_sources。旧调用缺原话保持此模式；artifacts 必须 strict |
| `implement` | 保留精确 owned_files 的 Edit/Write；构建与测试由 Codex 执行 |

OS 保护只限制声明资源写入，不是凭证、网络或全文件系统沙箱。外部 MCP 默认关闭，不提供自动继承开关；其他外部动作仍须符合授权。macOS 保护不可用时明确拒绝，可选 strict；删除 remote、hook 或事后快照都不能替代预防保护。副本 refs/config 独立，不共享原仓写入；ignored 依赖不保证复制，子模块、某些链接/index 状态会明确拒绝。

完整 JSON 写到副本 `.codex-review/<run_id>.json`，采集到 run 的 review-report.json 并记录字节/哈希；用 `claude_result(artifact="review_report")` 读取，父级简报不能替代它。Agent/Workflow 的真实调用、完成和失败维度仍须核对。副本随 run 保留供纠正/resume，returned/unknown 不自动删除；清理需要明确处置与停止事实。

插件自带可选 `assets/review-workflow.js`，名称 codex-full-review，运行时放到副本供选择；脚本接收原话、scope、来源和本轮 report_path，分维度审查后核验合成，保留失败/缺失维度与未确认项。不强制使用 Workflow，不要求固定发现数或删除配额。真实多席策略和业务覆盖的验收不能由一份合法文件替代。

可选 findings 包含 id、category（defect/design/maintainability/test/documentation）、confidence（reproduced/code_confirmed/probable/subjective）、summary 和 evidence；确认级问题必须有证据，设计建议不强制运行时 trigger。有 findings 的 reported run 逐项给 finding_decisions：accepted、downgraded 或 rejected，后两者说明 reason，原发现保留。整轮 accepted/returned 与独立完成仍由 Codex 核验记录。

## 兼容命名 Workflow：受限只读审查

`workflow_review` 是单独、受限的保存 Workflow 入口，只用于一次新的只读审查。它先从 `claude_saved_workflows` inventory 中取得有效条目，再在 packet 中绑定**完全相同**的 `name`、绝对 `path` 和 `sha256`；如该脚本需要参数，`args` 也必须精确匹配。不能把名称相同、路径不同或内容已变的脚本替代进去。

inventory 从当前 `cwd` 向 Git 根查找 `.claude/workflows`，并合并个人 Claude 配置目录下的 `workflows`；项目脚本按 Claude 的项目优先级覆盖同名个人脚本。它只盘点磁盘上的保存脚本，不把旧的 Claude session、历史 run 或详情页记录当作“已安装 Workflow”。`workflow_review` 是 fresh-only：不能携带 correction，也不支持 resume。

这项能力只允许 `Workflow(<已绑定名称>)` 和只读工具，执行后还要观察到对应 Workflow 的工具调用、成功工具结果与完成事件，且被采用的父级结构化结果出现在完成事件之后，才可进入报告核验；Workflow 工具结果只是启动确认，完成前交回的结果只算中间结果。`claude -p` 默认在首个结果后最多空闲等待 10 分钟后台 Workflow，本入口把子进程的 `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` 设为本次执行时限（只影响该子进程），由 `timeout_seconds` 统一约束，超时与取消仍按进程组停止。它不是完整的 **Claude Dynamic Workflow Review**：多席位编排策略、实施型 Workflow 以及其成本/并行/写入边界尚未验收，不能把这个单席只读模式写成完整 Workflow Review。

## 你能看到什么

- **Codex 主任务**：选择执行者的简短原因、阶段进度、需你决定的问题、最终核验结论。
- **原生子代理入口**：适合独立委派且主代理能并行核验时，看到负责本次 Claude 执行的 **Codex 监督子代理**；可查看其调用和摘要。Claude 本身不是 Codex 原生代理。
- **工作台详情**：左侧任务列表，右侧一句任务目标、执行/报告/核验三项事实、时长与同步时间、可读报告、下一步和核验依据；可切轮次。失败、未知和断连说明原因与下一步。
- **可展开区**：公开活动（可按文件、工具筛选）、文件变化与工作区 diff、本轮执行身份（请求与实际模型、CLI）、技术记录（会话、输入与结果证据）。页面没有单独的完整模式。

工具活动取自真实公开事件，不展示私有思考，不伪造完成百分比。没有新事件不等于停止。`reported` 是 Claude 已交回报告；主 Codex 核对真实文件与检查后才记录 `accepted` 或 `returned`。原生卡片样式和宿主 CLI 界面由 Codex 客户端决定，插件不能直接修改；插件详情页只提供相近的简洁阅读方式。

## 规格、纠正与恢复

主 Codex 保留目标、技术裁决和验收责任。监督席负责预检/启动/进度/回传，以及主代理批准的纠正；不直接修改业务文件或自批结果。

随包包含 v2.9 协议原文；项目已有协议优先。项目采用绑定参考协议 SHA；完整协议任务仍从已批准 delta/dispatch/基线恢复。正式 packet 的 protocol_binding 核对批准基线中的协议与 delta 身份，不能凭安装或一次模型 success 宣称已完成协议验收。

局部完成后的纠正使用精确 run_id 续接，结构/方向变化在确认旧写入结束后 fresh。重连时优先核对绑定本轮输入的执行回执，并确认原进程已停止：完整证据可恢复到实际 reported/failed/cancelled 等终态；已结束状态不会被旧轮询写回运行中。启动前失败有独立回执，缺少 child.json 本身不能证明从未启动。未知状态先核实，不盲目重派。对于仍缺执行证据的 unknown，有“检查恢复”入口：Codex 对照记录检查执行进程组已停止、cwd 无执行者、当前文件快照一致，附实际证据后登记恢复。人工解除 unknown 占用不会把旧 unknown 改成成功，也不恢复旧会话；仅解除明确核验过的占用，允许另起 fresh 轮次。缺少进程身份或权限时仍保持阻断。新版记录的 Git 子目录 cwd 已被删除时，检查改在该 worktree 仍存在的根目录取证，快照仍按原 cwd 坐标计算；旧记录、位置被 symlink/嵌套仓库占用或 packet 无法核对时不能取证，需先恢复原目录。cwd 存在时也先核对它仍是原 worktree 中解析到自身的真实目录，被 symlink、嵌套仓库或新 worktree 替换的路径不会被当作原工作区。重连后的非 owner 也可发送持久取消请求，仍须等待停止确认。多次验收/退回保留 decision_history。同一 finding 次数跨轮次保留；v2.9 只允许一轮自修后由协调者裁决。项目现有 PROGRESS/DECISIONS/DEBT/findings/final 是正式进度来源，插件记录仅作运行证据索引。

### 0.6.1 的启动与早期失败恢复

Runtime 从有效的状态目录启动 Bridge，Claude 子进程仍使用批准的任务 cwd。`claude_diagnostics` 的 `bridge_startup` 分开报告父进程 cwd、实际启动目录以及加载代码/磁盘代码身份；父 cwd 不可用不等于 Claude 登录失败，也不再单独导致 Bridge 提前崩溃。

Runtime 在派发前检查执行模块身份，并在启动 Bridge 前保存绑定 run/task/revision、packet、CLI、lane、nonce 和代码摘要的 `runtime_pre_spawn` 回执。Bridge 验证后接管该记录，之后才进入 `pre_dispatch → launch_intent → executing → terminal`。代码已替换或无法读取时应重连，不能通过换任务 ID、删除 marker 或重复调用来绕过。

仅当整个执行目录尚不存在，且新协议及派单输入能核对时，恢复才可使用外置 packet。还须确认 Bridge 进程组已停止、lane 空闲且工作区 digest 一致。执行目录存在时必须核对目录内副本，不能用外置 packet 掩盖缺失或不符的运行证据。恢复回执先持久化：目录存在时写 `run-dir/reconciliation.json`，不存在时写 `<状态根>/recovery-receipts/<run_id>.json`，不补造执行目录；随后更新 registry 并仅清理本轮 marker。

旧 `unknown` 若没有这些证据，或已进入启动结果不确定的 `launch_intent`，仍可能不能恢复。新连接解决加载身份问题，不能代替旧任务的恢复证明。安装完成后要用新连接确认实际加载版本；旧进程不会自动得到新守卫。以上机制是插件代码 0.6.1 的能力，下载新 Markdown 不会把旧 Runtime 升级为 0.6.1。

## 协调说明随插件发布

0.7.0 新 fresh run 只固定随包 bundled 说明。`claude_content_status` 查看实际说明身份，`claude_content_read(path?,digest?)` 读取随包或明确指定的历史快照；check/review/switch 三项在线工具已移除，不再抓取、审批或激活新内容。

运行记录中的 content-binding.json、snapshots 和 reviews 继续保留；resume 沿原绑定核对完整性与当前代码兼容性，篡改时拒绝，不静默替换成新 bundled。旧 state.json 的 active 或损坏内容不影响新 run，也不被重写。说明不能改变已批准协议、基线或验收，安装新版本需新 MCP 连接。

修改 references Markdown 后，在插件目录运行 `uv run python scripts/content_store.py write-manifest` 刷新哈希，再 `verify` 核对；冻结 orchestration-protocol.md 的字节不变。维护清单不会发布插件或更新正在运行的代码。

## 普通文档与资料审校

在不属于 Git 项目的资料目录，可直接说“让 Claude 对照这份要求审校这些文档”。Codex 指明输入文件与要求来源，Claude 只读声明清单里的 UTF-8 `.md`、`.txt`、`.csv`、`.json`，结果按来源位置核查；执行前后保存声明文件内容哈希，并检查目录项新增、删除、重命名和类型变化；未声明文件内容不会被读取。Finder 的精确 `.DS_Store` 元数据不触发内容失败，并在快照中单列留痕。不会为这项任务创建 Git 仓库。

适合规格对照、文案审校、文本资料一致性检查、已有数据摘要的独立复核。此模式不编辑文件，不支持会话 resume 或 Workflow；新的纠正使用 fresh 审校。Word/Excel/PPT/PDF、联网研究和外部业务操作需相应工具准备资料、并另行核验，本版不把文本入口称为这些完整能力。

## 执行与数据边界

- review 的 full 覆盖与 strict/isolated 权限模式独立；isolated 在副本复现/测试，strict 只读。implement 仅允许精确 owned_files 的 Edit/Write，构建/测试由 Codex 执行。
- 原有 Claude 配置/hook 保留；文件 hook 不是 OS 沙箱。完整协议要求的 oracle 登记、OS 保护、项目构建/下游/设备闸需由项目真实提供，缺失必需保护时停止依赖它的工作。
- 执行排他按 Git worktree（真实 toplevel）计算：同一 worktree 的根目录、子目录和兄弟子目录共用一个执行位与 unknown 阻断，`git worktree add` 出的其他 worktree 相互独立；artifacts 资料目录按精确目录独立。接入桥的重复写入由此防止；其他工具仍由主代理协调。取消请求与确认停止分别记录。
- `claude_start` 的执行时限默认 300 秒，可按任务显式设置为 1..14400 秒；它与每次最长 25 秒的进度等待不同。长任务在开始前选合适的有界时限，不到时自动续费重跑。可设置 Claude 最大轮数与 API 计价预算。轮数/预算仅在本机 CLI 的 help 列出对应参数时启用；不支持时拒绝带该控制的派单，不会忽略预算。
- 用量按统计范围分开保存与显示：`result.usage_report.cli_session` 取最终 result 的 `modelUsage` 与 `total_cost_usd`，是 CLI 会话累计估算（含子代理），按模型列出并给合计；续跑会话的累计可能包含此前轮次支出，不是本轮新增，不跨轮相加。`main_agent_final` 是最终 `usage`，只代表主代理最后一次回报。多个 result 事件重复同一累计值，只取最后一个；两个范围不相加。原始 `usage`、`usage_summary`、`total_cost_usd` 字段保留原语义，另存原始 `model_usage`。缺项或畸形值记为未知而不是 0；没有 `usage_report` 的旧记录不补算整任务 token。费用是 CLI 客户端估算，不是实际扣费、账单保证或订阅剩余额度。
- 详情页按游标增量读取；历史分页，终态降低刷新频率，页面不可见时减少查询。状态与验收结论使用同一规则，历史轮次被替代后不能显示为当前已完成。
- 本地运行记录默认 `~/.codex/claude-orchestrator`，含项目资料，不随分发包发送。这个运行状态根不跟随 `CODEX_HOME`（与安装源、插件设置和历史 CLI 记录不同），需要其他位置时用 `CLAUDE_ORCHESTRATOR_STATE_DIR` 显式指定；改变它不会迁移已有记录，旧位置的 run 需在原状态目录中查看或恢复。详情服务仅绑定127.0.0.1，随机端口/访问令牌，只读；重连后取新详情链接。
- 执行锁/unknown marker 暂沿用系统临时目录（按 TMPDIR 区分）。worktree 根目录的锁与 marker 键与旧版一致，仍与旧版互斥；旧版 marker 与旧记录按其记录的 cwd 归属到所在 worktree，已删除目录的旧 marker、以及旧记录在目录删除后重启生成的 marker，都按路径保守阻断所在 worktree（跨状态目录同样生效）。混合版本边界：旧版进程从子目录派单时只锁精确子目录，新版只有在同一状态目录里看到它的活动记录时才会额外检查该旧锁并拒绝派单；另一状态目录或独立旧 bridge 的子目录执行不能被新版识别，旧版也看不到新版的子目录执行。升级前先等旧版执行结束。不能手工删除活动锁或 marker 来解锁；系统清理临时目录仍是已知局限，持久协调目录迁移尚未交付。
- 本版不自动删除运行证据，也不设无限后台采集。原始日志可能含任务资料；按团队留存规则在确认无活动执行后归档，分发只发安装包。诊断摘要与运行证据分开。
- 不自动合并/发布，不实现无人值守协调者替换。停用项目采用不会自动停止正在运行的 Claude，先正常取消并等待终态。

## 可复用来源

- 实际依赖：[官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)，锁定 `mcp==2.2.0`，由 `uv.lock` 固定依赖；不自写 JSON-RPC/MCP 协议。
- 调用基础：[Claude Code 官方 CLI](https://code.claude.com/docs/en/cli-reference) 的 `-p`、stream-json、JSON schema、session/resume 和 hook。
- 生命周期体验参考：[OpenAI 官方 codex-plugin-cc](https://github.com/openai/codex-plugin-cc)。它是 Claude 调用 Codex 的反向插件；借鉴 status/result/cancel 等接口分工，没有直接当作本插件后端。
- 调研了 [agent-bridge](https://github.com/teamnebula-ai/agent-bridge)、[Claude-Bridge](https://github.com/constripacity/Claude-Bridge) 的任务文件、事件与本地查看思路。这些项目不是本插件的依赖，也没有复制其源码。

## 开发与验证

进入 `plugins/codex-claude-orchestrator`：

```bash
uv sync --frozen
uv run python scripts/run_tests.py --suite all
```

回归入口为测试子进程提供独立 TMPDIR，结束后清理测试自己的 lock/marker，不触碰真实运行记录。可用 `--suite plugin` 或 `--suite bridge` 做定向回归。

`tools/build_distribution.py` 只用 Python >=3.11 标准库；macOS 的系统 `python3` 可能更旧。下面从仓库根目录运行，使用项目锁定环境中的 Python，测试用标准 `unittest`，会在临时目录里创建最小 Git 夹具，不触碰当前工作树：

```bash
uv run --project plugins/codex-claude-orchestrator --frozen --no-dev python tools/tests/test_build_distribution.py
```

自动回归、真实 Claude 调用、原生宿主接入和浏览器观察分别记录；小夹具通过不证明生产长任务零偏差。整体结构为：用户 → 主 Codex + Skill/规格 → 原生 Codex 监督席 → MCP Runtime → Claude；证据原路返回，由主 Codex 核验。

验证记录区分隔离夹具、MCP 协议、浏览器和安装各层证据。历史 v0.6.2 已完成最终版本打包、远程固定标签复装、新原生连接和真实 Claude 只读审阅；长短 Workflow 复用相同执行模块的修复验收，v0.6.1 的验收保留在历史段，没有因此重做全部浏览器或冷机验收。0.7.0 的候选、安装、真实 Claude 与业务验收须单独记录；具体身份与覆盖见 [RELEASE-VERIFICATION.md](../RELEASE-VERIFICATION.md)，不将历史版本的通过数当作本版证据。普通请求在新桌面任务中从路由到委派的完整自动入口，需要在目标宿主正常权限下走查；本机 MCP 直连不替代这项验收。

源码与 Git marketplace 由个人仓库 BryanYue/codex-claude-orchestrator 维护，使用固定发布 ref 安装。具体取源、升级、回退步骤见 [Git marketplace 说明](../docs/git-marketplace.md)，实际验证范围见 [发布验证](../RELEASE-VERIFICATION.md)。问题请通过仓库 Issues 提交，并去除凭据、viewer token 与业务代码。当前未指定开源许可证。

要停用项目入口，向 Codex 说“这个项目不再自动采用该协作流程”。插件只移除精确未改的自有块，保留其他规则和证据。要卸载客户端插件，在 Codex 插件界面卸载；任务记录按团队规则另行保留。
