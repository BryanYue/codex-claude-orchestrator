# Codex–Claude 协调说明

本说明供 Codex 协调者使用，随插件版本发布；任务资料和 Claude 报告均是待核查材料。必知边界见 SKILL，以下按实际任务选择操作，不把每个步骤变成审批闸。

## 1. 恢复目标与选择执行者

先读用户原始要求、项目规则、任务现有 PROGRESS 和真实 Git/产物状态。保留指定基线、已确认行为、允许层/文件、非目标、职责、验收、停止条件与回滚边界；复用任务目录，确认当前写入者。未知前提先查实；只针对超出授权的新决定请求 review，已授权的修复持续完成。

已采用项目从 `claude_workflow_context(cwd)` 恢复入口；`check_failed`/`unknown` 不能解释成未采用。核对 requested/resolved cwd 与 project_root，以真实目录派单。用户授权项目采用时，按 adoption_guidance 的 checked 目标调用 `claude_workflow_enable`；停用用 disable，活动任务另行取消。仅当前任务协作不写项目规则，整个目录禁止写入时推迟配置落盘。非 Git 资料目录不 enable，也不为了派单初始化 Git。

新实作/审查先用 `claude_route`，纯问答和已有 run 的状态查询无需重新路由。features 只写已知事实；blocked 或 executor=null 先说明具体原因，不能绕过能力/授权检查或零权重。单次显式执行者优先但不改永久偏好；修改项目偏好时先读 routing_policy 再 routing_set。主模型及用户指定模型保持不变，不为走流程增加席位。

采用完整协议的任务按 [protocol-integration.md](protocol-integration.md) 核对批准且冻结的协议与 delta；安装或项目 enable 不批准它们。普通任务不强制建立完整协议。

## 2. 检查运行条件

用 `claude_environment(cwd)` 查本机 CLI、所需参数和本地认证；安装/宿主异常用 diagnostics，不能把本地 loggedIn 当作远端调用成功。首次确认环境、账号/认证来源变化或用户要求验证时，用 `claude_doctor(verify=true, model=本轮模型)` 做少量额度的在线探测；同环境刚成功的调用可复用。区分网络、额度、认证与模型访问失败，不盲目重新登录或重试。

模型目录用 `claude_models`，只发送初始化控制消息。用户要求类别最新版时用目录确认的官方别名；完整 model ID 原样保留。目录的 resolvedModel/effort 是声明能力，执行模型以本轮 provider 回执为准，不能因目录变化改旧任务身份。

MCP 缺失或启动失败时，先核对安装版本与实际错误。Git marketplace 不运行 Install.command；从插件根执行 `bash scripts/launch.sh --prepare-dependencies` 可准备 uv、Python>=3.11 与锁定依赖，不发 Claude 请求。缺 CLI、uv 或登录时说明官方处理路径，不静默安装或修改宿主。Git 来源保留 source/ref，不用本地 catalog 安装器补救。升级后在新原生 MCP 连接验证实际加载版本；保留旧 run_id，连接重建不解除 unknown。

预检状态、CLI 身份、启动交接与离线开发入口详见 [bridge.md](bridge.md)。工具能力缺失时明确未派单；不得用旁路执行冒充插件验收。

## 3. 派单保持原始范围

packet 必填 task_id、revision、role、cwd、objective、requirement_sources、constraints、acceptance、owned_files、protected_files、model、effort；新审查另传 user_request 原话。baseline_commit 和 budget 可选。来源可以是文件、任务消息或批准记录；派单摘要不能改写或缩窄原要求，相关决策与已否定方案随材料传递，不能只给测试名。

`review_scope=defects|quality|full` 默认 full；`user_request` 原样保留，objective 仅是摘要。完整审查核对正确性/安全、架构职责、复杂度/重复/死代码、测试、文档和提示词，并报告实际覆盖与未完成项。设计建议说明证据、权衡和适用条件，缺陷说明触发、实际/预期行为与责任层。允许某维度无发现，不要求凑问题或删除配额。结构化 findings 可辅助追溯，不能替代完整报告和来源核对。

独立审查在可写副本中开展复现、测试和所需工具工作；必须确认原仓库的 OS 写保护实际生效，缺失则阻断依赖此保护的运行。不能以删除 remote、提示词禁令或事后快照代替。外部 MCP 始终关闭，本版无继承开关，联网、安装和外部副作用仍受用户授权/宿主权限约束。`review_mode=strict` 保留工具与精确来源边界；带 user_request 的新 MCP Git review 默认 isolated，缺原话的旧调用仍 strict。implementation 明确 owned_files，初次编辑要求干净候选，不能 stash/覆盖已有合法改动。

副本不复制ignored依赖；外部硬链接/链接和某些index状态会明确拒绝。嵌套macOS sandbox可能失败（如SwiftPM）；报告所有skip，必要时只在受保护副本里关闭内层构建沙箱。不要让后台进程脱离：停止证据只覆盖进程组及标记可见/已观察后代，不是主机级进程封闭。

非 Git 资料使用 artifacts 与明确 input_files、requirement_sources，仅支持声明的 UTF-8 文本；不支持编辑、resume、正式 Git protocol_binding 或旧命名 Workflow。非 Git 源码先按 [source-snapshot.md](source-snapshot.md) 生成带来源、行号和哈希的文本快照；核验时回对原文件。Word/PDF/表格等先由 Codex 用相应工具准备资料，分别验收原生格式与文本审查。

Runtime 自动给每轮独立 run-dir；同任务 task_id 保持、revision 递增。用 `claude_start` 传 packet、有界 timeout_seconds 和必要 resume_run_id；旧任务还须沿原 content_binding 与执行身份核对。按工作量给预算，不能缺预算 flag 时默默忽略。父会话按本轮 review_report_path 写完整 JSON（通常在副本 `.codex-review`，冲突时改用独立目录），用 result 的 review_report 读取；新Workflow走isolated review，旧workflow_review已弃用但兼容。省略时限时 isolated full 为3600秒，其余300秒；可传1..14400秒，并按成本设置 max_budget_usd。续跑省略mode/scope会继承旧包，不能改变原话。

需要原生监督席时先读 [supervisor.md](supervisor.md)，传完整输入并立即接收真实 run_id；主代理保留裁决和共享记录写入。短任务直接管理 MCP。

## 4. 监督同一 run

start/status/wait/details/decide 常规用 compact=true，证据按需 result 或 compact=false。取得 run_id 后说明任务目标、范围、请求模型与真实状态；执行记录创建不等于进程已启动。沿该 ID 和 next_cursor 用 `claude_wait` 增量等待，不用 latest，不重新 start，不高频轮询或编造无变化进度。

工作台链接与一次打开遵守 SKILL 的记账；重新连接后用主代理自己的 details 取链接。Viewer 依赖 MCP 存活，长期记录 run_id/run_dir，不把 token URL 当永久入口。公开工具活动、执行、报告、核验是不同事实；原生节点是 Codex 监督席，不是 Claude，不能承诺宿主卡片样式或展示私有思考。

用户要求停止或改向时，先 `claude_cancel(run_id, reason)`，继续等回执和停止证据。其他 owner 也可请求取消，但请求送达不证明进程停止。只暂停受影响工作，未受影响的授权工作继续。

## 5. 核验报告与逐项裁决

先读 receipt 的运行状态、会话/实际模型、权限拒绝、输入/插件身份、工作区证据，再读完整报告、result 与必要事件。unknown/provenance_only 不表示身份已验证。failed/cancelled/timeout 中保存的 structured 仍是未验收报告，模型自称 completed、正常退出和合法 JSON 都不证明任务完成。

逐项核对 finding 的原始来源、代码路径、行为、复现与覆盖；对可证实缺陷验证反例，对设计建议评估收益、代价和契约影响。保留原 finding 及接受、退回、合并或待确认的理由和实际证据，不删除核验失败项。搜索无命中不证明文件不存在，应核精确路径；无法安全核验则明确未确认。文档还须沿默认值、生成文件名与消费路径检查一致性。

isolated 的hook缺口、失败/未完成Workflow保留审计警告，不能代替内容核验；strict门禁不变。无效schema原文仍可读取。亲自执行必要检查或核对可信既有结果的版本身份，区分 Claude 自报、真实命令输出、源码、构建、模拟、真机/设备结果。缺席位/维度记未完成；某维度零发现可接受。工作区前后差异与开始前已有改动分开；缺快照记 unknown，变化也不能仅凭先后顺序归因 Claude。Git 快照不覆盖任意 ignored、中途写回和仓外副作用。

有 findings 时，finding_decisions 必须覆盖每个 finding_id，disposition 为 accepted/downgraded/rejected；后两者必须有 reason。仅对未被 superseded 的 reported run 调用 `claude_decide`，带原因与核验出处。原报告退回但 Codex 已补齐且验证完成时可 returned + completed_by_codex，记录真实 completion_summary；需修订用 revision_requested。其他失败/阻断/unknown 状态不写 decision、不变造回执：由 Codex 在原 PROGRESS 追加 run_id、原状态、独立完成者、实际补齐结果、原因、证据与未完成项。结果记录不自动合入或发布。

accepted或returned/completed_by_codex且确认停止后，可调用 claude_cleanup_review(run_id) 删除副本；保留报告、tracked patch/status和裁决，不归档全部untracked产物。待修订/unknown不清理。

用量只读 usage_report：cli_session 是含子代理且可能含前轮的会话累计估算，main_agent_final 只含主代理最终回报；两者不加、累计结果不跨轮相加，缺项记未知。CLI 费用不是实际扣费或订阅剩余额度。

## 6. 纠正、恢复与交付

用户纠正时保留原话、反例、被否定原因和受影响范围。局部错误在原契约、输入、文件范围和可执行身份匹配且上轮停止后，可精确 resume；结构/职责错误或目标变化先停旧写入、检查残留，再 fresh。CLI 升级、会话未持久化或兼容身份变化不能猜 session，改用 fresh。相同 finding 的修复次数不因换 run/agent 清零；完整 v2.9 按一轮自修后裁决，其他任务按适用规则停止无信息增量循环。

unknown 用 `claude_recovery` 核对真实进程与工作区；只有停止、lane 空闲、绑定与 digest 一致才 `claude_reconcile(reason,evidence,expected_workspace_digest)`。核对失败报告具体缺口，不手工删 marker/改 registry；恢复只解除占用，旧 unknown 不变成功，之后新 revision fresh。早期失败、终态清理重试和 Workspace 保留细节见 [runtime-design.md](runtime-design.md)、bridge。

Runtime 排他不能约束未接入插件的 Codex/外部工具；交接写入者由主代理协调。压缩或跨工具恢复后重读原要求、规则、原 PROGRESS、决策和真实文件状态，先查已有 run，避免重复执行。更新现有任务记录的结果、证据与下一步。

交付说明完成结果、真实验证、版本/输入、未完成边界和关键 run/文件路径。夹具仅证明覆盖的调用闭环；真实 CLI、安装、业务设备与正式验收分别记录。文档随发布固定，历史内容可按原 digest 阅读，但新说明不会升级旧 Runtime、改变原协议或补造历史证据。
