---
name: codex-claude-orchestrator
description: Coordinate implementation, specification or document reviews, continued tasks, corrections and recovery through Codex and Claude. Use automatically in projects that adopted this workflow for ordinary requests such as 按计划继续, 修复这个问题, 这里理解错了, or 查看进度; users need not mention tools. Also use when asked to delegate to Claude, review local documents with Claude, configure task preferences, adopt or disable the project workflow, or check Claude login. Outside adopted projects use for requested orchestration or Claude collaboration; unrelated questions do not need it.
---

# Codex–Claude 任务编排

Codex 拥有任务目标、计划、技术裁决和验收。用本技能把明确任务交给 Claude，再核查真实结果。保持用户原始要求与适用项目规则，主代理也可直接实现，不强制派席。

## 能力与边界

本版是**macOS 监督式插件**：主 Codex 协调目标与验收，MCP Runtime 管理本机 Claude CLI。保存每轮输入、公开活动、结构化结果和回执。Git 项目支持只读 review、精确文件编辑、已保存命名 Workflow、指定会话纠正；普通资料文件夹支持明确文本清单的只读审校。支持取消、核对异常状态后 fresh 改向，以及默认简洁的结果/进度详情。

未实现：无人值守 daemon、自动更换 Codex 协调者、自动合入、完整 Claude Dynamic Workflow Review。用户请求这些能力时明确说明缺口；单席 review 不能标成 Workflow Review。完整 Runtime 设计见 [runtime-design.md](references/runtime-design.md)，仅在设计升级或恢复机制时读。

## 首次安装与依赖恢复

Git marketplace 安装不会运行根 `Install.command`。MCP 工具缺失或启动超时时，先检查实际安装版本与启动错误；不要把注册成功当作依赖或 Claude 已就绪。MCP 启动器需要 uv、Python >=3.11 和锁定依赖，第一次可能下载。可从本 Skill 所属插件根（本文件目录向上两层）执行 `bash scripts/launch.sh --prepare-dependencies` 显式预热；此命令只准备该完整 manifest 版本的 venv，不发 Claude 请求。无 uv 时说明缺项并按官方安装方式处理，不静默安装 Homebrew、改 shell 配置或登录。

预热成功后重新加载插件 MCP，再用 `claude_cli_status(cwd)` 或 `claude_diagnostics(cwd)` 区分插件/宿主、执行器与本地认证；它们不证明冷下载或远端模型调用成功。执行器未准备时复用 `claude_cli_update(action="prepare")`，认证由用户完成。Git 来源不能用 `Install.command` 或其配置参数补救，以免切回本地 catalog；保留原 source/ref 和维护策略。预热后仍失败则核对实际错误，不无限延长120秒启动窗口或重派模型任务。

## 自然入口与项目采用

用户不需要逐次点名插件、预检、派单、打开详情或验收。已采用项目中的普通需求、已批准计划、“继续”和纠正都属于入口；从项目 AGENTS、`claude_workflow_context(cwd)` 和当前任务记录恢复，而不是让用户复述流程。只在实际需要 Claude 时检查并派发，不把小改动强制做成多代理任务。配置检查先看 `check_status` 和 `adoption_status`：`check_failed` / `unknown` 表示尚不能确认，不能概括成未采用；多项目逐个保留结论。只读检查可解析目录别名，核对 `requested_cwd`、`resolved_cwd`、`project_root` 后用真实目录派单。配置和协议文件的符号链接限制不因此放开。

用户要求“这个项目以后采用该协作流程”时，调用 `claude_workflow_enable(cwd=<明确项目根>)` 一次；它保存项目级入口及参考协议，保留既有 AGENTS。用户要停止自动采用时用 `claude_workflow_disable`，它不负责停止正在执行的 Claude，须先按取消流程收口。仅安装不修改任意项目/全局规则；只采用当前任务时沿用对话授权，不写项目规则。用户已授权采用则直接执行，不追加确认。

启用前先看 context 的 `adoption_guidance`。常规采用在 `can_enable=true` 时使用其 `target_cwd`；用户明确要求重新启用已停用项目时，可在 checked 且配置无问题的 target_cwd 执行 enable；非 Git 工作区不尝试 enable，不把工作区约定落盘说成仓库已绑定。用户已授权该工作区后续项目采用时，进入实际仓库要完成检查及允许的绑定，不只再次承诺“以后绑定”。已有 disabled / 不完整配置须保留并按返回原因处理。区分“业务文件只读”和“整个目录禁止任何写入”：前者不撤销已有项目采用授权，后者须推迟配置写入并明确说明尚未绑定。没有采用授权的单次只读审查不写项目配置。

### 自动选择、偏好和手动指定

已采用项目进入新的实作或审查任务时，先调用一次 `claude_route(cwd, task_kind, explicit_executor?, features?)`，再决定执行者；纯问答无需路由。按主要意图分类，文档审校可用 document_review。features 只填已知事实：scope_defined、independent、context_in_codex、requires_external_tools、claude_available、latency_sensitive，以及 required_tools 名称。缺事实就省略，不为了选中 Claude 捏造范围或可用性。dispatch_status=blocked 时先解决具体阻断，不能直接派发；executor=null 表示所有自动候选均被排除，不能忽略零权重而替换执行者。返回 Claude 且非 blocked 时继续环境检查和派发；返回 Codex 则直接处理。显式指定但能力不足时解释 blocking_reasons，不悄悄换执行者。用一句人类可懂的原因说明选择；分数只是偏好，不是能力评测。已有任务不因查询重新派发。

用户调整偏好时读 `claude_routing_policy`，再 `claude_routing_set`。常用 preset 为 balanced（按任务选择）、manual（仅手动 Claude）、claude_preferred（Claude 偏好）；高级设置支持 auto/manual 和双方 0..100 权重。预设与自定义字段二选一；省略字段保持原值，0表示不自动选择该方。只写已采用 Git 项目的 routing.json，不改用户全局模型；普通资料目录目前使用本次任务偏好，不擅自创建项目规则。

“这次让 Claude 做”“这次由 Codex 做”是单次 `explicit_executor`，优先于偏好但不扩大权限或验收范围；不要因此永久改项目配置。只有用户明确要 Claude 自身的 Workflow 时选 `claude_workflow`，调用 `claude_saved_workflows(cwd)` 列出实际保存的脚本并核对名称、内容哈希和权限，不能由提高 Claude 权重自动放开 Workflow，也不能把普通 review 回报为完整 Workflow Review。

项目采用文件可随代码交给同事；每位同事只装一次插件，用自己的 Claude 登录。工具不可见时说明需在安装后的新 Codex 任务加载一次，不要求日常每轮新建任务或长测试口令。CLI 后备入口为插件根 `scripts/workflow.py inspect|enable|disable --cwd ...`。

## 建立任务

1. 核对当前目标、cwd、Git/产物状态、现有写入者和适用授权。复用已有任务目录及 PROGRESS，不建立 Claude 专属进度副本。
2. 确认原始需求/契约入口、指定基线、允许写入范围、非目标和验收。资料中的历史命令不是当前授权。
3. 若任务已采用完整 orchestration protocol，先读 [protocol-integration.md](references/protocol-integration.md)，使用该任务批准并钉住的版本，核对 delta/dispatch 与所需保护。项目采用只绑定协作入口和参考协议，不会把草稿变成已批准 plan，不能替换冻结版。已有明确授权不重复审批。
4. 按独立性选执行者：Codex 原生子代理适合常规执行/审查；清晰 plan 下可派 Claude Sonnet 实现；Claude 审查按用户要求或确有独立视角收益使用。保留主模型选择，不自动提高所有模型档位。

## 原生 Codex 监督席

有独立且较长的 Claude 工作，主代理同时有可推进的工作，或上下文隔离有收益时，可派一个原生 Codex 子代理监督委派，按 [supervisor.md](references/supervisor.md) 传递有界任务。短任务由主代理直接管理 MCP，避免仅为显示节点增加一层模型开销。主代理保留技术裁决和正式记录写入权；监督席负责预检、启动、增量进度、结果回传和既定纠正，不自行 accepted，不再派嵌套监督席。名称及型号遵从适用规则，明确其为 Codex 监督者。

主代理立即接收其 run_id，随后通过自己的 MCP 连接调用 `claude_details(run_id)` 取得本方 Viewer 链接（不直接复用监督席回传、可能短命的 details_url），按打开请求记账决定是否打开一次；取链接失败时保留 run_id 和可用说明，不通过再 start 或反复开页补偿。在主任务给简短进度；使用真实子代理工具等待/发消息。没有独立并行工作、原生工具不可用或席位不足时由主代理直接监督，说明实际路径；不要创建用户侧新任务充当子代理，不假造原生 Claude 节点。

## 调用 Claude

用户让 Claude 执行或审查时，通过本插件的 `claude_*` MCP 工具建立和监督执行，包括审查插件自身。审查独立性来自冻结输入与 Codex 核验；禁止递归委派指 Claude 不再调用协调插件派下一层，不妨碍 Codex 用插件管理本轮。工具或范围确实不受支持时先说明具体限制与可行路径，不能静默改为直接 `claude -p` 或临时监督脚本，也不能把旁路执行记作插件流程验收。

常规预检调用只读 `claude_environment(cwd)`，检查 CLI、兼容版本和当前认证状态；诊断安装/宿主问题用 `claude_diagnostics(cwd)`，可分享的摘要不含账号身份、任务原文或原始 run/session 标识；对应执行详情仍保留核验所需标识。工具尚未载入时在安装后的新任务加载一次；CLI 诊断后备用 `python3 <skill>/scripts/bridge.py doctor --cwd ...`。每轮 run 自动保存 environment.json；未安装、未登录、未验证的 CLI 版本分别说明下一步，不盲目重试。ZIP 安装且桌面 PATH 缺失时，才使用安装器的 `--configure-claude-bin` 指定经确认的绝对执行路径；不 source shell 配置或静默选择其他 nvm 版本。版本固定与更新说明见随包 README，不自行改 Claude 全局更新策略。不得读取、复制或打印 token/keychain/凭证文件，不自动登录、登出或切换账号。

### CLI 更新、验证与回退

日常任务不要求用户背命令、手动改 profile 或把一次升级变成阻断。需要查看或升级 Claude CLI 时，先用 `claude_cli_status(cwd)` 区分：系统发现的 CLI、配置的 active CLI、基础只读任务的派单预览、候选验证阶段，以及安装、登录和最近真实调用。编辑或 Workflow 所需资格可能不同；当前任务版本以 run 的固定执行证据为准。资格缺失时选择器可能回退保留版本，不能把 active 配置或通用预览说成本轮实际版本。

保留已有更新策略；旧配置默认 `bundled` 随包支持清单，不声称官方最新。用户要求自动跟随 Claude 官方最新版时，说明隔离验证会使用 Claude 额度，调用 `claude_cli_update(action="policy", policy="automatic", channel="latest", auto_qualify=true)` 一次开启；已有明确授权不重复询问。只要自动下载、暂不授权付费验证时用 `auto_qualify=false`。`latest` 在后台查询官方发布源、校验并保留独立执行文件；资格通过前不切换。每个执行身份和当前桥接契约只自动验证一次；未确认或失败保留现用版本，不能循环付费重试。`cli_maintenance.channel` 标明来源、范围与检查时间；普通 `up_to_date` 不重复打扰用户。

若 `cli_maintenance.notice_pending=true`，先简明告诉用户**当前版本、目标版本、保留或切换/失败原因，以及对本任务的影响**；随后调用 `claude_cli_update(action='acknowledge', notice_id=<返回值>)` 记录已说明。已有 run 继续使用启动时固定的 CLI identity，不能把全局更新回填成旧 run 已升级。用户询问版本维护时可调用 `claude_cli_update(action='refresh')`；可用 `policy='automatic'` 或 `policy='manual'` 设置维护方式。手动 rollback 会暂停自动维护，直到用户显式重新启用 automatic。

用户明确要求准备、验证、切换、回退或停止验证时，使用 `claude_cli_update`：

- `prepare` 自动复用健康的已记录 native CLI；自动发现到 unknown 或 non-native 候选时保留该 candidate，并从具备来源和完整性证明的官方分发 bootstrap 已测基线。它不发起模型请求，也不改全局 launcher、登录或 profile；无法证明来源或签名时报告 bootstrap 错误。
- `validate` 是用户明确触发的真实兼容验证；已授权的 `latest + auto_qualify` 也可在后台启动同样的验证。最多 10 个场景、总时限 900 秒，并在有限 cleanup 后报告终态；它没有 USD 硬上限。它保留 run/报告路径和阶段结果，验证失败继续保留旧 active CLI。
- `activate` 只在验证结果和所需能力满足时原子切换；普通任务会继续由选择器使用仍 eligible 的 active 版本。
- `rollback` 恢复上一条已记录的 active CLI；它是 Claude executable 回退，和插件包安装器的 catalog rollback 不同。
- `cancel` 只请求停止仍在进行的验证，随后查看终态；取消请求本身不代表验证或进程已停止。

未知 candidate、仅有 `--help` flag、未登录、登录状态无法读取、最近调用缺失，以及某个可选预算能力不可用，分别显示各自事实和下一步。不要把 unknown profile 说成登出，也不要从历史 fake fixture 推断当前账号或认证有效。缺少可选 capability 只限制明确请求该 capability 的任务，不降低不需要它的 review/implement 任务。详情页中的 CLI 版本和环境记录是该 run 固定下来的实际证据；全局候选验证进度、结果和 `report_path` 从 `claude_cli_status` 获取，不回填进旧 run 的历史事实。

首次使用此环境、切换账号/认证来源，或用户要求确认凭证有效时，调用 `claude_doctor(..., verify=true, model=<本轮模型>)`（CLI 等价为 `doctor --verify`）：通过当前 CLI 做一次无工具、单轮、不保存会话的最小在线请求。它会使用少量模型额度，并保留原有认证与 hook。仅本地 `loggedIn` 不能记作远端有效；网络、额度、模型权限、认证失败分别报告。刚完成同环境的成功调用可复用，不为每轮任务重复探测。实际派单前读 [bridge.md](references/bridge.md) 的 packet 字段。

### 模型类别与最新版本

首次为项目选择模型、用户询问最新模型、CLI 已切换或账号/提供方配置变化时，调用 `claude_models(cwd)` 获取实际执行 CLI 的模型目录；检查保留候选或按能力回退版本时传其 `identity_id`。工具仅发 initialize 控制消息，无用户提示词或模型生成请求；目录失败不能编造映射，也不阻断已有明确模型配置。

用户要求某类别最新版时传官方别名（如 `opus`、`sonnet`、`haiku`），让 Claude 按当前 CLI/提供方/组织和环境配置解析；不要在插件中维护具体模型版本表或把目录展示值强制改写为版本号。新类别以目录返回的 `value` 为准，不自动选择更贵的类别；用户指定完整模型 ID 时原样保留。目录提供的 `resolvedModel`、effort 列表是声明能力，账号实际可调用性及执行模型只认在线回执。若旧 CLI 的目录仍是旧模型，先按已授权更新策略准备新版；不能承诺改 alias 就能绕过最低 CLI 版本要求。

每轮显示请求模型与实际模型。缺能力回退到旧 CLI 时说明本轮执行版本及模型可能不同；已有任务和 resume 保留原执行身份与请求参数，不能因全局目录更新偷偷换任务契约。资格报告的 `outcome` 只针对基础组；同时核对 `requested_groups_outcome` 和 `missing_groups`，不能把部分通过说成全部兼容。

### MCP 执行与可见进度

检查、路由和派单是不同操作。配置/环境/路由结果中的 `operation_type` 与摘要说明本次做了什么；只做检查时明确说“本次未启动 Claude”，并说明接下来由 Codex 处理还是继续派单。安装成功、版本维护完成、路由选择 Claude 都不证明 Claude 已参与本任务。

常规 start/status/wait/details/decide 调用传 `compact=true`，避免重复展开整份报告和核验历史；旧调用不传该参数仍返回完整结构。需要证据时通过 `claude_result` 或 `compact=false` 按需读取，摘要不代替验收。

1. 按本次工作量选择有界 `timeout_seconds`（默认 300 秒，最大 14400 秒；不是进度轮询时限），调用 `claude_start(packet, timeout_seconds, resume_run_id?)`。取得具体 `run_id` 后说明任务目标、范围、请求模型和记录中的实际状态；“已创建执行记录”不等于进程已启动，`claude_started` 未确认时如实说明。
2. 主代理在本 Codex 任务内按"打开请求记账"维护 `not_requested / requested / failed` 状态（只存在对话上下文，不写入 Runtime 或执行状态）。只有能确认 `not_requested` 且取得有效链接时才自动打开：给出 `[查看 Claude 协作工作台](details_url)`，在发出 `open_in_codex` 调用前先记为 `requested`，再用 browser target 请求一次；成功与 `queued` 都保持 `requested`（`queued` 只表示排队，不能说已显示），明确失败改为 `failed`、不自动重试。已处于 `requested` 或 `failed` 时，同任务后续 start/details 只给可点击链接和一句状态，不再自动调用 `open_in_codex`。上下文恢复时沿用已有记账；无法确认是否请求过时只给链接，不能按 `not_requested` 猜测重开。工具调用中的 URL 不代替用户可点击回复。详情不可用时保留原 `run_id` 继续查询，绝不因此重新派单。重连后用主代理自己的 `claude_details(run_id)` 取新链接；页面依赖 MCP 进程存活。用户显式说"重新打开/另开查看/打不开了"时，取新链接、先记 `requested` 再请求打开一次；这不新增 Claude run。不能保证宿主深链复用原标签页时，使用原工作台的任务列表定位；单页承诺限定为同一 Codex 任务内存活的 Viewer。原生监督席永不调用 `open_in_codex`，只回传 run_id 与 details_url。
3. 用 `claude_wait(run_id, after=<上次 next_cursor>, timeout_seconds=25)` 获取增量，围绕同一 ID 跟踪到结束。报告新出现的有意义公开活动；无新事件不编造进展，也不重复相同状态。不用 latest 代替原任务，不高频轮询。默认详情区分“最近公开活动”和“页面同步”；请求模型/effort 是配置，实际模型只认 provider 证据。
4. 执行结束读 `claude_result` 的 receipt/result/diff，按下节核验并给最终结论。`reported` 只表示 Claude 已交回结果；`claude_decide` 记录 Codex 有证据的 accepted/returned，不能省略验证。公开活动、简洁/完整详情、核验依据复用现有入口，不另外创建监控任务或许诺无人值守通知。
5. `claude_cancel(run_id, reason)` 发出取消；继续等待终态回执。`unknown` 表示无法确认，先核实进程和残留变更，不能直接重派。

重连后先查询原 run；Runtime 会核对输入身份、回执和进程停止事实，证据完整可回到实际终态。非 owner 可通过 `claude_cancel` 请求原 bridge 停止，不能凭取消已发送认定已停止。仍出现 unknown 时调用 `claude_recovery(run_id)`，对照实际文件与返回的进程/工作区证据。只有系统确认进程已停止、lane 空闲且本轮 workspace digest 与核对一致，才用 `claude_reconcile(run_id, reason, evidence, expected_workspace_digest)` 记录恢复。证据须指向实际检查，不填“已核对”占位字符串。核对失败就报告具体缺口，不手动删除 marker/改 registry；人工 reconcile 核对成功也只允许新的 fresh 轮次，不能把缺少执行证据的旧 unknown 改为成功或继续旧 session。正常运行的例行状态查询不需要恢复操作。

### 普通资料文件夹

用户授权 Claude 审校本地资料、且 cwd 真正不在 Git 中时，packet 使用 `workspace_kind="artifacts"`、`role="review"`、显式 `input_files` 文本文件清单和明确 `requirement_sources`。原文件保持只读，按来源位置/原句核对报告；用 workspace_before/after 清单哈希证明输入保持一致。不要为方便而初始化 Git，也不能用 artifacts 模式绕过现有 Git 项目的协议。此模式不支持修改、resume、命名 Workflow 或正式 Git protocol_binding。

仅处理声明支持的纯文本格式。Word/Excel/PPT/PDF 的原生编辑、联网采集和外部应用动作需相应工具与领域验收；可由主 Codex 完成已授权资料准备，再委派明确文本分析，不能声称 Claude 已使用它没有的工具。

非 Git 代码目录中的源码审查可先使用 [source-snapshot.md](references/source-snapshot.md) 生成有来源、行号和哈希的只读文本快照，再经 artifacts 派发。明确告知审查的是该冻结快照；核验 findings 时对照原文件及哈希。已有 Git 仓直接用 Git review，不以快照绕过项目协议。

工具活动只展示实际公开输出、工具名称/路径和生命周期事件，不展示私有思考块。原生树中的节点是 Codex 监督子代理，Claude 本身不是原生代理；插件也不能改变原生卡片样式。工具标题是否显示取决于宿主，不能承诺替换其汇总文案。详情页的工作区 diff 可能包含已有改动，不能直接归因为当前轮。

- 用版本化 packet 传目标、原始来源、约束、验收、cwd、model/effort 和明确文件范围。有关决策和已否定方案必须随相关任务传递，不只给文件名/测试名。
- 每轮用新 run-dir；同一任务保持 task_id，revision 递增。MCP 记录默认在 `~/.codex/claude-orchestrator`，在现有 PROGRESS 中链接对应 run，不污染实现仓或改正式判据。
- review 只有读文件与搜索工具；implement 增加 Write/Edit，精确列出 owned_files。普通角色不向 Claude 提供 shell、代理、Workflow 或 MCP 工具，构建/测试由 Codex 在用户授权的环境中执行。
- 明确请求已保存 Workflow 时，使用 `role=workflow_review`、空 owned_files 与 inventory 返回的 `workflow={name,path,sha256,args?}`，只允许该名称和精确参数。它是只读、fresh-only，不能 resume；缺失真实工具调用或关联完成通知就不能记成功。开始前阅读脚本确认只读意图；失败则报告，不擅自替换为普通 review。完整协议的审查席与验收仍须单独满足。
- MCP Runtime 托管 bridge；需要 CLI fallback 时在托管终端启动，保留运行 session id。不要用 `nohup` 或丢弃进程管理把本版伪装成持久服务。
- 默认保留 Claude 的配置和 hook；附加本轮文件约束。宿主沙箱仍生效。若命令因为沙箱不能写 Claude 自身会话目录而失败，按宿主的权限升级流程请求具体命令权限，不使用 bypass/bare 或搬移凭证绕过。

## 接收、审查和验收

`blocked_by=preflight` 表示未通过预检，先按 environment 的具体原因修正；`blocked_by=executor` 表示 Claude 已执行但缺材料/条件，先读 summary/unresolved。权限拒绝仍使本轮 failed；保留已取得的证据给 Codex 判断下一步，不自动原样付费重跑。

先看 receipt 的执行状态、实际会话/模型、权限拒绝及输入/仓库身份，再读 result 与必要原始事件。进程退出零、模型返回 completed、JSON 合法，均不构成验收通过。

对照实际文件/diff 核查：原需求是否完成、契约与职责是否保持、变更是否超范围、已有正式判据是否变化。亲自执行适用检查或验证可信的既有结果身份；区分模型自报与实际执行证据。缺失的审查席/维度记未完成。

搜索、Glob 没有命中不等于文件不存在，尤其是点文件或忽略文件。对“缺少某文件”的 finding，用获准工具核对精确路径；无法安全核对则写未确认，不为证明存在读取凭据或扩大权限。文档审查还要沿实际默认值、生成文件名、消费路径核查，不能仅凭参数声明相同就断言命令一致。

`workspace_changes` 区分前后快照观察到的变化、开始前已有改动和结束时改动；缺快照或无法定位的变化记 unknown，不能说“零修改”。观察差异不单独证明由 Claude 造成，ignored 文件与中途改动存在覆盖边界。

Claude 原报告有错但 Codex 已亲自补齐并完成必要核验时，调用 `claude_decide(decision="returned", resolution="completed_by_codex", completion_summary=<实际补齐结果>, reason=<原报告退回原因>, evidence=<真实核验出处>, compact=true)`。这保留原报告未采纳事实，同时收口本轮任务；不能仅因不想重跑就标完成。尚待修正时省略 resolution 或用 revision_requested。历史记录缺少明确完成信息时不推测补写；后续轮的验收仍须单独满足。

由协调者在现有 PROGRESS 中记录 run-dir、接受/退回原因及证据。bridge 回执是运行索引，不能取代需求、DECISIONS 或验收记录。不开自动合并/发布。

## 纠正与改向

用户说“这里理解错了”“改成这个方向”“停一下”时，由主 Codex 判断影响并给监督席明确纠正/停止消息。不要要求用户提供 run_id、revision 或 resume/fresh 术语。先保留新要求与被否定原因，停止受影响的旧写入，再处理下表；只暂停真正依赖新决定的部分。

| 情况 | 操作 |
|---|---|
| 局部遗漏/可证实的局部错误 | 给原要求、反例、根因或待验证假设；上轮完成、任务契约与续跑协议兼容且当前能力资格有效时用 `resume_run_id` 精确续跑（桥接器使用 CLI 的 `--resume <session_id>`） |
| 结构性错误/错误职责划分 | 停止受影响写入，核对残留变更，重定义 repair packet 并 fresh 执行；不靠旧会话继续堆补丁 |
| 用户改变目标 | 留存用户原话及影响；仅使相关工作失效；更新获准要求/执行安排，然后 fresh 执行 |
| 网络/认证/权限失败 | 查真实状态与改动，先解决运行问题；结果未知时不盲目重派 |
| 用户要求停止或旧方向仍运行 | 请求 cancel，核对最终回执及进程；确认写入者停止后再交接。取消请求不等于停止确认 |

同一 finding 的修复次数跟随问题，换 agent、run-dir 或 revision 不清零。采用 v2.9 的任务严格遵守一轮自修后裁决；其他任务按适用规则停止无信息增量的循环。纠正中不得删失败样本、缩小覆盖、迎合实现修改正式验收。

记录受影响旧 run 的失效原因；旧结果可留作证据，不能作为新方向验收依据。Runtime 维护受监督任务的 cwd 排他，协调者仍须确认 Codex 或其他工具的写入者及跨任务依赖；文件锁不能约束未接入插件的执行者。

## 长任务与交付

压缩或交接后回读原要求、适用规则、当前 PROGRESS、决策理由与真实 Git 状态。先核查已有 run，避免重派仍在执行的工作；必要时使用 fresh reviewer 对整体职责、不变量和正常流程作独立核对。

交付先讲结果、实际验证和未完成边界，附关键 run/文件路径。若用户说“测试这个 Skill”但没给项目，使用隔离夹具做真实 CLI 调用与纠正，报告这些只证明调用闭环，不能证明生产长任务已降低偏差。
