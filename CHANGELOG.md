# 更新记录

[返回中文首页](README.md) · [English overview](README.en.md)

以下为版本变更记录，保留实现与验证细节。首次使用见[快速开始](docs/getting-started.zh-CN.md)，实际验证范围见[发布验证](RELEASE-VERIFICATION.md)。

## 0.7.1：审查链路实测修复与隔离边界补齐

版本标签 `v0.7.1`，完整构建 `0.7.1+codex.20261003035940`。本轮核对 0.7.0 审查反馈，并在临时仓库实测本机 Claude CLI；失败记录与实际验收边界见[发布验证](RELEASE-VERIFICATION.md)。

- 修复 Git 审查路由误拒 Bash/Agent/Workflow/Web、续跑丢失模式和范围，以及 full review 默认时限过短；未显式指定时 isolated full 为 3600 秒，其余 300 秒。严格模式继续保留。
- `--tools default` 保留内置工具，`dontAsk` 按真实 CLI 工具名授权；真实测试发现并修复星号许可未放行 Bash/Workflow 的问题。外部 MCP 始终关闭，不增加用户 MCP 继承开关。
- isolated 的 hook 记账缺口、已失败的 Workflow 尝试作为审计警告保留，不丢弃已经交付的完整报告；strict 仍保留守卫证据门禁。无效 JSON/schema 报告也先保存原字节、哈希与诊断，供分页核对，运行仍明确阻塞。
- 内置 Workflow 先做公共检查，再做五维审查、独立核验和合成。兼容 CLI 在子代理 JSON 前附加的安全说明，原说明作为不可信材料保留；多对象或破损 JSON 仍拒绝，缺必需阶段仍 blocked。
- 监督方 Git 调用集中禁用可执行回调并设置超时；保护整个 Runtime 状态、执行源码与 Python 环境；日志拒绝符号链接、硬链接和 FIFO。副本支持 intent-to-add、全局忽略、大小写路径和保护集合内的完整硬链接组，避免保留目录与 Workflow 名称冲突，并覆盖所有关联 worktree。
- 跟踪带本轮标记的脱离进程组后代，退出时核验并清理。新增 `claude_cleanup_review`，仅在明确处置、停止证明及锁内重查通过后删除副本；保留报告、裁决、tracked diff 与状态，unknown 和待返工副本保留。
- findings 补可选级别、位置、建议及结构化简化建议；旧 findings 返回可解释错误，历史记录可带理由交回 Codex 处置。实际 Workflow 调用和实时/最终模型身份使用一致的记录逻辑。
- 清除剩余不可达 CLI 对账链、无效导入兜底与退休接口参数；共用锁观察、Git 和身份路径声明。Ruff 新增按既有基线测得的复杂度/分支/语句上限，局部 strict Mypy 保留；提示词同时补齐必要字段与真实边界。

## 0.7.0：完整审查、副本写保护与随包协调说明

版本标签 `v0.7.0`，完整构建 `0.7.0+codex.20261003090000`。源码、分发与真实 Claude 的验证范围分别记录于 [验证记录](RELEASE-VERIFICATION.md)，旧发布条目、标签与资产保持原身份。

- 审查包保留 `user_request` 原话及来源，`review_scope=defects|quality|full` 默认 full；覆盖与执行权限分别选择。缺原话的旧调用保持 strict 并标明 legacy_unspecified，不把 objective 冒充用户原话。
- 带原话的新 MCP Git review 默认 isolated：在独立可写 clone 中使用 Bash、Agent、Skill、Workflow 与测试工具，OS 限制原仓、Git 元数据、要求源和运行控制文件写入。保护不可用明确拒绝，可选 strict。它不是凭证/网络沙箱，外部 MCP 默认关闭；artifacts 保持 strict，implement 的 owned_files 边界保留。
- 完整报告写到副本 `.codex-review/<run_id>.json`，采集后保留哈希、父级结果、工具和 Workflow 完成事实。可选内置 `codex-full-review` 脚本帮助分维度审查，不强制使用或凑发现。副本随 run 保留供纠正/resume，returned/unknown 不自动清理。
- findings 支持分类和证据置信度；有 findings 时逐项记录 accepted/downgraded/rejected，降级或拒绝保留理由。原发现和失败核验不删除，reported 仍须 Codex 独立核验。
- 协调说明随插件固定发布，在线 content check/review/switch 退役；read/status、不可变历史快照与原任务绑定保留。SKILL/guide 精简去重，legacy 命名只读 Workflow 入口继续兼容。
- 删除退役 CLI 下载、资格验证与切换执行链；按职责拆分启动、进程监督、流解析和结果校验，集中原子写入、文件读取及按用途区分的身份声明。引入固定版本 Ruff 和局部 strict Mypy 检查。
- 执行锁迁移到持久目录，同时继承旧锁与双写 unknown 标记；保留旧进程兼容和按 owner 核验的恢复机制。例行状态查询不再扫描历史 CLI 维护数据。
- 修复非 UTF-8 Git 差异、SIGTERM/SIGHUP 清理、Viewer I/O 错误、未知旧验证任务取消、早期运行目录失败与关闭等待预算；安装 staging 失败清理本次副本，历史备份保留。源码/构建/安装/真实运行验证分别记录，不在本条目补造通过数。

## 0.6.2：Workflow 交付、运行恢复与需求排序

正式版本 `v0.6.2`，完整构建标识 `0.6.2+codex.20261002042337`。发布已完成独立审查与回归的修复，安装命令与版本说明同步更新；旧 `v0.6.1` 标签及资产保持不变。实际验证范围见 [验证记录](RELEASE-VERIFICATION.md)。

- **提示词投递受监督**：Claude 子进程启动后先持久化 PID/PGID，再在同一非阻塞循环中写提示词、读 stdout；执行时限从启动起算。不读 stdin 的子进程也能按时超时或取消；提示词未送完记 `prompt_delivery=incomplete` 并失败。
- **进程清理边界**：SIGTERM 或存活检查遇到 EPERM 时，在原有期限内继续核验；需要时仍进入 SIGKILL，只有独立停止证明才释放，无法证明则保留 unknown 与详细诊断。
- **启动前取消边界**：guard 自检、CLI 描述符和代码身份核对之后、发布 launch intent 前再次检查两处取消标记，命中即“启动前取消”，不启动 Claude。
- **终态 marker 清理可重试**：可信终态 run 清理自己的 lane marker 失败时，终态不变，只记 `admission_cleanup=pending`；下一次同 worktree 派单或 Runtime 重启在原有绑定与进程组停止证明下重试，外来或异常 marker 仍阻断。
- **Workflow 完整报告**：通过只给本轮子进程的 `CLAUDE_CODE_TMPDIR` 私有根，按会话/task 精确快照公开任务输出及 `result` 文本表示（字符串原文或明确标记的对象/数组 JSON；信封保留原始字节，哈希、大小、绑定、收集状态分别记录），与父级摘要分开，经 `claude_result(artifact="workflow_report"|"workflow_envelope")` 与工作台按 UTF-8 字节分页读取并逐页校验哈希。缺失、无效或不匹配时不算交付，只把原本 completed 的 run 记为 `blocked_by=workflow_evidence`，failed/cancelled/timeout 不变。
- **报告选取与证据**：最终报告只取期望会话的父级 `type=result`；文本通知只读开头的头部元素，结构化事件优先；每个 Workflow 调用一条证据并给出稳定原因码。结构化报告要求 summary 与列表项非空白（简短的无发现报告仍合法）。
- **CLI 格式化拒绝证据**：完整的未解析输入包装与唯一同会话父级错误结果证明 StructuredOutput 未执行时，单列证据；不豁免文件操作、已有拒绝或证据不全的调用，最终报告仍必须校验。
- **范围提示与 hook**：提示中给出 cwd 内、cwd 外精确文件与唯一允许的 Workflow 输入；搜索外部文件父目录仍被拒绝并失败，但拒绝原因指出应改用的精确文件。文件 hook 只复核冻结的绑定脚本，无关脚本变化不再把已允许的读取变成拒绝；派单与实际 Workflow 调用仍做完整 inventory。
- **meta 解析**：只解析开头的 `export const meta` 纯字面量，正确处理单行/多行、注释与字符串；拒绝重复、计算、展开等歧义或可执行元数据，不扫描正文。
- **工作台排序**：需求列表按最新一轮执行开始时间倒序，跨状态统一排序；活动、结束和核验更新不移动位置，新一轮执行才会前移。筛选、选中和键盘焦点保持。
- 新增 `workflow_delivery.py`，已同步加入 Bridge 契约、代码身份与分发文件清单；协调内容更新为 `2026.10.02.4`，声明 `workflow_report_v1`、`workflow_report_json_v1` 能力并重校验执行时兼容性；内容哈希已同步。

- **完整再验证与文档对齐**：插件 385、Bridge 176、分发 29 项通过；旧启动 7 场景、恢复对抗 16 场景及独立控制重跑通过。补跑同一旧夹具的真实普通审阅，并核对执行模块与已通过的真实长/短 Workflow 测试完全一致。修正假执行器未读取输入和双崩溃测试过早关闭输出管道的前置条件；不放宽断言、时限或恢复证据要求。README、版本表与验证记录明确区分本次源码和固定标签，失败记录保留。

## 文档维护：2026-10-02（插件仍为 0.6.1）

同步发布验证的实际完成状态、安装/重连/恢复说明，以及 Runtime/Bridge 的 0.6.1 启动协议。工作台当前实现与历史设计分开标注；协调指南移除与稳定 Skill 边界冲突的执行后备说明。动态协调文档使用独立内容版本 `2026.10.02.1` 并更新文件哈希，需经过原有审查流程才生效；不修改插件代码、插件版本、冻结协议、已有任务绑定、旧发布标签或资产。

## 0.6.1：稳定桥接启动与早期失败恢复

- Runtime 从有效状态目录启动 Bridge；子进程仍在任务工作目录执行。已删除父 cwd 不再让参数解析阶段的 `Path.cwd()` 提前崩溃。
- 新启动交接在调用 Claude 前绑定 nonce、代码摘要、任务、工作目录、CLI 和 lane。插件代码已替换时拒绝混用旧 Runtime 与新 Bridge，并保留可检查的诊断。
- 对已证明尚未启动 Claude 的早期失败，支持执行目录尚未生成时的恢复回执；先持久化证据，再提交恢复状态和移除本轮 marker。记录缺失、损坏、绑定不符或启动结果不明时仍保守阻断。
- 新增 29 个回归测试方法。旧版缺少启动证据的 unknown 不会自动解锁；安装升级仍需新 MCP 连接，不能热替换已经加载的旧代码。

## 0.6.0：本机 CLI、动态协调内容与运行证据修复

运行证据：hook 的允许、拒绝与缺少审计分别统计，权限拒绝仍使运行失败；执行失败也保留可解析报告并明确未验收；拒绝列表仅保存拒绝条目。每轮冻结完整插件版本、来源 revision 和实际代码摘要。失败摘要与简洁预览明确标注未验收；Git 探针失败保留真实诊断，缺 Git 元数据的安装缓存不猜提交号。`claude_decide` 只接收未被取代的 reported 轮次，失败运行由 Codex 完成的任务处置记录在既有 PROGRESS 中，原失败事实不改写。

动态协调内容：同仓库 references 目录的 Markdown 可隔离暂存，经 Codex 按 digest 安全评估后启用；新任务固定快照，续跑不随更新变化。停用保持到显式恢复，支持已审查版本回退，原协议正文不变。

用量范围：新增 model_usage/usage_report，分别展示主代理回报、按模型的 CLI 会话累计及客户端估算费用；缺项保持未知，续跑不冒充本轮新增费用。新增内容接口、Bridge 模块和分发契约需一次插件升级，之后正文更新可独立进行。

**本机 CLI 策略。** 新派单、续跑、环境检查、诊断、模型目录、安装器和工作台都只使用用户本机已安装或明确配置的 Claude CLI（`CLAUDE_BIN` → 插件 `claude_bin` → PATH，保留 nvm/npm shim 路径；`launch.sh` 与 `Install.command` 保持调用方 PATH 优先，常见安装目录只作后备）。版本号只作诊断，不再有版本白名单；任务按 `--help` 的精确 flag（与实际任务命令一致，含 `--verbose`；`doctor --verify` 另需 `--no-session-persistence`）、本地登录状态和显式预算 flag 准入，help 明确标为“声明的语法”而非已验证行为。行为继续由 hook 自检、hook 覆盖、会话身份和结构化结果校验确认；普通角色新增运行后工具集核对，出现 `--tools` 以外的工具即失败（`tool_policy_error`）。续跑要求同一可执行文件，用户升级后明确拒绝并提示 fresh；旧版本的私有 CLI 身份不能借续跑、模型目录或资格描述符再次执行。

**版本管理退役。** 移除后台维护循环和派单时的维护/等待；`claude_cli_update` 除取消旧验证任务外只返回说明、不改状态；`claude_models` 不再接受 `identity_id`；安装器不再准备私有 CLI；子进程不再设置 `DISABLE_AUTOUPDATER`；工作台“维护”区改为本机 CLI 状态。`cli_store.py`、`cli_validation.py`、`official_releases.py` 中的下载、捕获、资格和切换实现只作为历史代码保留，用于读取既有记录，生产入口不再调用；`~/.codex/claude-orchestrator/cli` 下的历史身份、回执和选择文件不删除、不参与派单。

2026-09-30 Workflow 补审修复：Bridge 在启动 Claude 前持久记录本轮 lane 占用意图，确认子进程停止且终态证据落盘后只移除自己的标记；Runtime 与 Bridge 同时异常退出仍阻止跨状态目录重派。独立 Bridge 因既有 unknown 被拒绝时会写失败回执并退出 1，仍不启动 Claude。启动结果不确定时保留恢复门禁。

CLI 提示补齐失效配置路径的恢复方式；历史维护锁改为只读共享观察，不可读明确为未知。工作台刷新保持键盘焦点且不拉动滚动位置，复制反馈两秒复位并隔离旧异步结果，预检文案不把未确认启动写成未启动。Viewer 对非 ASCII 鉴权头返回 401，采用项目的来源字段先校验类型。模型目录超时测试先确认夹具进程就绪，生产超时与停止断言不变。本版同步更新完整插件版本、MCP/根包版本与 v0.6.0 安装说明；v0.5.0 标签和历史发布验收记录保留。

审查问题对应：

- F01（capture 失败丢失下载）、F11（latest 漏写 GnuPG）：随生产下载与 latest 通道退役而消除；历史代码中 capture 失败时也会把已验证字节放回断点缓存。
- F02：版本探针失败保留退出码、信号或超时类型，以及限长、脱敏的 stdout/stderr（历史存储代码与桥接环境检查均适用）。
- F03：Git 子目录 cwd 下，status 路径统一换算为 cwd 坐标，内容哈希不再误为 missing；cwd 之外的改动记为 `../` 路径并继续判越界。子目录前缀只去掉 Git 的换行，名称首尾空格保留。
- REMOTE-F1-ISOLATION（F03 同族，锁身份维度）：执行锁、活动/unknown 准入、unknown marker 与恢复改用同一 worktree 身份（`git rev-parse --show-toplevel` 的真实路径），Runtime 与独立 bridge 共用；继承锁 FD 按该身份核对。根目录、子目录和兄弟子目录不能再同时派单，也不能绕过彼此的 unknown；linked worktree 与 artifacts 目录保持独立。cwd、owned/protected 坐标和 task/revision 身份不变。新记录保存 `lane_identity`；marker 带 lane 身份，同一 lane 已有他人 marker 时另存而不覆盖，reconcile 只清本 run 的 marker。旧记录/marker 按记录的 cwd 归属，无法定位时保守阻断；旧记录的 cwd 已删除或无法解析为 worktree 时，重启生成的 marker 只用该路径作键、不声明 lane 身份，所有 Runtime/状态目录仍按其路径归属到所在 worktree；旧子目录活动记录还须拿到旧的精确 cwd 锁才会被判定或恢复。根目录锁/marker 键不变；混合版本边界见[进阶说明](docs/advanced-usage.zh-CN.md#执行与数据边界)。
- F04/F05：本机 CLI 状态读取的任何异常只降级该字段，不遮蔽已创建的 run；MCP 关闭时 Runtime 与 Viewer 的清理相互独立，Viewer 启动失败也会关闭 Runtime。
- F06：保留版本的大小进入 inventory 与诊断，只统计记录大小与实际文件一致的版本，旧记录缺项单独计数。
- F12：首页当前轮候选的详情超过 45 秒会在有界后台预算内补读；保留 60 秒可信新鲜度门禁。
- F13：`unknown` 且已读取结构化报告时展示已记录内容，并注明不能据此验收或重派。
- F14/F20：状态读取失败会被缓存并在打开抽屉时继续显示，同时标出旧数据的读取时间；换用新 token 链接后重新调度状态读取，旧连接代的响应不覆盖新状态。
- F15：技术记录明确提示“展示已截断，完整记录保留在本机”，并区分记录不存在、正在写入和读取失败。
- F16：权限拒绝显示工具、目标与原因，未知结构使用限长 JSON。
- F17：窄面板首次无任务时在列表中显示引导，与搜索无结果区分。
- F18：任务列表与轮次标签支持 roving tabindex、方向键和 Home/End；普通刷新重绘轮次标签时，焦点仍在原轮次，不抢其他控件的焦点。
- F19：执行身份与技术记录显示 `execution_evidence` 来源及说明。
- C01：切换到未载入或无效的执行记录时清空其他记录的报告、身份与技术字段。

Workflow 复审后续修复：

- WF-R1：带已确立 `lane_identity` 的 unknown 记录，其子目录 cwd 被删除后，`inspect_recovery` 改在仍存在的 worktree 根目录取证，快照与摘要仍按原 packet 的 cwd 坐标计算（与该 cwd 自身快照一致），packet 与记录不改写。仅当 cwd 是该 lane 下规范形式的严格子路径、确实不存在（未被文件或 symlink 占位）、最近存活的祖先是真实目录且其 worktree 根仍为该 lane、且（有记录时）派单 packet 哈希一致才允许；旧记录（无 `lane_identity`）、错误 lane、嵌套仓库、symlink 祖先和 packet 缺失/不一致仍无法取证。cwd 仍存在时，带 lane 身份的 Git 记录同样先核对身份：cwd 须为解析到自身的真实目录（本身或祖先都不能是 symlink），其 worktree 根仍为该 lane；被 symlink 指向其他 worktree/仓库/同仓库其他目录、或被嵌套仓库、新 linked worktree 占位的路径不会被当作原工作区取证。无 lane 身份的旧记录在 cwd 存在时沿用原行为。进程已停止、独占 lane、期望摘要与 marker 归属门禁不变。
- WF-R2：找不到 CLI 时的提示不再无条件推荐 `Install.command --configure-claude-bin`；`CLAUDE_BIN` 适用于所有安装方式，安装器持久化只适用于带 `FILE-SHA256.json` 的 ZIP 分发包。CLI 选择与安装行为不变。
- WF-R3：`claude_cli_status`/诊断读取旧验证任务改用只读的 `cli_validation.read_status`：不创建锁文件、不对账、不写报告/事件、不触碰 selection；非终态记录原样返回并标注 `reconciliation: not_performed`，只以只读描述符观察已有 `worker.lock`。显式 `cancel` 与历史 `status` 行为保留。
- WF-R5：`workflow_review` 子进程环境设置 `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` 为本次 run 的超时（毫秒，最小 1000，从不为 0），避免 `claude -p` 在默认 10 分钟空闲等待后停止仍在运行的 Workflow；只作用于该子进程，不改 `os.environ` 或用户设置，外层超时、取消与进程组清理不变。提示词说明 Workflow 工具结果只是启动确认、不要查看私有 journal、等待自动完成通知，完成前的结构化结果只是中间结果。`parse_stream` 只接受在匹配的完成通知（同一 tool_use_id、同一会话、父会话消息、与 `task_started` 绑定的 task_id）之后出现的父级结果；完成前的结果即使随后出现完成通知也判为 blocked（`workflow_final_after_completion`、`workflow_interim_result_count`）。
- WF-R6：Workflow 子代理的工具调用及其 hook 拒绝不一定出现在父级 stream 或 CLI 的 `permission_denials` 中。收口时从本 run 的 `activity.jsonl` 汇总 PreToolUse hook 记录的拒绝，追加到 `permission_denials`（`source: bridge_pretooluse_hook`，含 activity 序号、工具、tool_use_id、原因，以及是否出现在父级 stream / CLI 拒绝中），写入 `hook_denial_error`，并判 run 失败；适用于所有角色。CLI 报告的拒绝、hook 覆盖、工具集、会话、范围与完成通知门禁不变，被拒绝的操作仍被拒绝，读取范围不放宽。
- WF-R4（运行状态根不跟随 `CODEX_HOME`）不作为缺陷处理：状态根默认 `~/.codex/claude-orchestrator`、由 `CLAUDE_ORCHESTRATOR_STATE_DIR` 显式覆盖，行为不变，只在进阶说明中写明。

## 0.5.0：协作工作台与 Git 分发准备

本版提供按任务组织的只读协作工作台，并把源码整理为独立 Git 仓库，新增可复现构建和首次安装说明。实际验证与限制见 [发布验证](RELEASE-VERIFICATION.md)。

- 按任务和项目聚合执行轮次；执行、报告、Codex 核验分别显示。历史轮、详情未载入、过时记录和失败但保留报告内容各有明确提示。
- 同一主任务首次派发时请求打开一次工作台；后续执行通过工作台列表查看。queued 也记为已发出请求，监督子代理不再另外开页。显式重开仍指向原执行；不承诺跨 MCP 进程或跨 Codex 任务自动复用同一浏览器标签。
- 新增响应式明暗主题、窄面板轮次选择、搜索筛选、只读下一步指令、维护抽屉和键盘焦点处理。保留原 HTTP 权限与执行核心。

- 新增 `tools/build_distribution.py`：从干净、已提交的 Git commit 构建分发包；校验根包版本（`.codex-plugin/plugin.json` 基础版本号与 `pyproject.toml`、`uv.lock` 中的 `codex-claude-orchestrator` 版本一致，第三方依赖锁定内容不受影响）；拒绝覆盖已有输出、拒绝 symlink 和未登记的文件类型；生成确定性排序/时间戳/权限的 ZIP，使同一 Git 提交、同一输出文件名的重复构建可核对哈希；随包写出 `FILE-SHA256.json` 与 `RELEASE-MANIFEST.json`（版本、源提交、执行契约摘要）。也校验 `launch.sh`、`Install.command` 在 Git 中记录为可执行模式（100755），因为 Git 安装路径依赖这个位，ZIP 安装路径依赖打包时写入的权限位。用法（Python >=3.11）：`uv run --project plugins/codex-claude-orchestrator --frozen python tools/build_distribution.py --source-root <repo> --output <目标目录>`（默认输出到 `<repo>/dist/codex-claude-orchestrator-macos-<version>`）。
- 新增 [Git marketplace 说明](docs/git-marketplace.md) 与 [安装与恢复说明](docs/install-and-recovery.md)：区分 ZIP 与 Git 两条首次安装路径；uv/Python/依赖/CLI/认证的分步准备；本机同名市场迁移的具体步骤与失败回退；并明确区分"本地 Git-ready"（可克隆、可复现构建）与"真实远端已验证"（实际执行过 Git 取源与安装）两种不同结论，不把前者当后者宣称。
- `plugins/codex-claude-orchestrator/scripts/launch.sh` 新增 `--prepare-dependencies` 显式模式：只执行 `uv sync --frozen --no-dev` 做冷启动依赖预热，不启动 MCP server、不发起任何模型调用、不修改 global/PATH/profile/认证；`.mcp.json` 的默认启动路径（不带该参数）行为不变。
- 插件与根包版本号同步为 0.5.0；`uv.lock` 中第三方依赖的锁定内容未改动。

## 0.4.7：审查后的稳定性修复

- 自动资格验证启动与用户暂停策略同步；仅在能够证明未启动时延后重试，启动结果不明保留原任务，不重复消耗额度。同一维护故障确认后不反复提醒；新版发现失败不再被“已是最新”遮盖。
- 分开显示配置的 active CLI 与基础任务预计使用的版本。具体任务仍以执行记录为准；模型初始化声明、实际响应及一轮中的多个模型分别记录。
- 自然语言让 Claude 审查时，经插件建立执行和详情。自动打开为 queued 时说明排队并给出可点击链接；重连继续查询同一 run。非 Git 源码可先生成带原始路径、行号、哈希的明确文本快照，再经现有只读资料入口审查。
- 修复终态刷新/取消竞态、已证明未启动的恢复出口及受监督进程清理；守卫处理异常和歧义路径更明确。已发现 hooks 被关闭时阻止派单并解释，不改写用户或组织设置。
- 安装注册失败时先核实宿主实际登记，必要时恢复已验证旧 catalog；只有宿主也核对成功才说完全恢复。运行中的 MCP 使用独立版本缓存。

本机验收及企业策略、Workflow 等尚未确认的边界见[发布验证](RELEASE-VERIFICATION.md)。

## 0.4.6：官方最新版与模型发现

- 可一次开启“自动跟进 Claude 官方最新版”。后台发现官方发布、验证来源和文件完整性，在私有目录准备候选；兼容验证通过后才切换新任务。下载和资格失败保留可用版本，进度、原因与未通过能力可查。
- 模型信息直接从实际 CLI 读取，包括新出现的类别、解析后的模型 ID 和 effort 选项。按类别选择时使用 Claude 官方别名，不维护固定模型版本表；显式模型 ID 保留。目录信息和实际模型调用结果分别展示。
- 部分能力通过时明确列出缺失能力；已有按能力选择机制可使用此前保留的有效版本。旧任务与续跑不随全局升级更换执行文件。
- 官方最新频道需要本机 GnuPG（`gpg`）核验发布签名；已有 Homebrew 的同事可运行 `brew install gnupg`。缺少时会保留可用执行版本并给出提示，插件不会跳过验签。
- 旧配置仍按随包清单维护。想启用新模式，告诉 Codex：**“自动跟进 Claude 官方最新版，允许每个新候选做一次有界兼容验证。”** 这会使用本人 Claude 额度；不希望自动验证时可只自动获取候选。

## 0.4.5：实际使用细节修复

- 项目检查返回明确的绑定目标和下一步。多仓库工作区先保留工作区约定，进入实际 Git 仓库时按已有授权完成绑定；检查失败、尚未采用、明确停用分别处理。
- Claude 报告退回后，如果 Codex 已独立补齐并核验，可以显示“原报告未采纳 · Codex 已完成”。尚未补齐时仍显示待修正；不从旧记录的自由文本推测完成。原报告、退回原因、补齐摘要和历史均可查看。
- 文件影响比较前后快照，分开展示开始前已有改动与执行期间观察到的变化。缺少证据显示未确认，不能把整个工作区脏文件列表归给 Claude；忽略文件和中途改动不在快照统计内。
- 简洁页显示最近有意义的活动及其真实时间，原始公开活动在完整页保留；工具许可显示“已允许读取”，不冒充读取成功。
- Skill 的常规进度调用使用 compact 摘要；完整结果与核验历史按需读取。旧 MCP 调用省略 compact 参数仍返回完整结构。
- 审查要求明确：搜索无命中不能证明文件不存在；命令说明须核对实际默认值、产物与消费路径。该规则帮助减少错误，结果仍必须由 Codex 核验。

上述 0.4.5 修复当时没有扩展定制 plan 或完整 Workflow Review，使用隔离回归与历史证据副本，没有为页面和状态修复重复调用付费 Claude。0.4.6 的 CLI 资格验证涉及真实调用，当前覆盖与未验证项见 [发布验证](RELEASE-VERIFICATION.md)。
