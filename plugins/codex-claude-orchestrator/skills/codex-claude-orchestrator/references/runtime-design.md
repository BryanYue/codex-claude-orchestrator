# Runtime 与完整 Workflow 的边界

本页以已发布 **0.6.1** 的 Runtime/Bridge 为当前实现基线，区分已交付的监督执行、受限保存 Workflow 审查和仍未验收的完整 Dynamic Workflow Review。标明历史版本的实测仅证明当时覆盖；本文的协调内容版本独立于插件版本，更新本文不升级旧 Runtime。

## 已交付：持久 Runtime 与受监督执行

当前链路是 Codex 桌面/CLI 会话 + Skill + MCP + Python Runtime + Claude bridge。Runtime 保存每轮 packet、环境预检、公开流事件、结果、回执、运行身份与协调者决定；重启后不会把遗留活动 run 自动当成功或自动重派。它提供按 Git worktree 的执行排他、取消确认、精确 run 身份、局部 correction/resume 与 fresh 改向的边界。项目采用和 `.agents/codex-claude/routing.json` 也已经交付：采用一次后，普通自然请求按任务特点及用户偏好产生可解释选择，不是随机比例或能力断言。

主 Codex 仍拥有目标、正式验收与写入协调；原生 Codex 监督席只在合适时管理 Claude 执行。工作台详情显示执行、报告、核验三项事实，用户可展开公开活动、文件变化、执行身份和技术记录；宿主原生界面与 CLI 样式由 Codex 管理，插件不能直接改写。

未交付的是无人值守 daemon、自动更换协调者、自动合并/发布，以及把状态存储升级为某一特定 TypeScript、Agent SDK 或 SQLite 技术栈。这些是后续候选，不是本版技术契约。

## 报告处置、快照和可见进度（当前行为）

`claude_decide` 的 accepted/returned 仍表示原 Claude 报告的核验决定。returned 默认 resolution=revision_requested；协调者亲自补齐并核验任务后，可显式提供 completed_by_codex、completion_summary、reason、evidence。历史决定追加保存，原 provider result 与 receipt 不改；缺少明确记录不推断闭环，superseded 仍优先显示历史轮。

`claude_decide` 的接口前提是未被 superseded 的 `reported` run。failed / cancelled / timeout / blocked / unknown 不调用它，也不改变这些 run 的状态或回执；Codex 独立补齐这类任务时不新增状态、不放宽接口，在既有任务 PROGRESS 记录独立处置（run_id、原状态、完成者 Codex、实际 completion_summary、reason、evidence、未完成事项）。这些 run 保存的最终报告是未验收证据（`result.report_evidence.accepted=false`），不代表运行成功。

每轮开始 bridge 把插件身份冻结为 run-dir 内的 `plugin-identity.json`，receipt 与 result 直接带完整身份：插件版本、来源 revision（Git checkout 或分发 RELEASE-MANIFEST 可用时）与 dirty 状态、含路径与字节的实际代码 SHA-256（标明范围）和 bridge_contract_id。ZIP 分发的 manifest 只是来源说明，不表示当前代码已核对；无 Git 也无 manifest 时明确 unknown，不编造。插件根由模块自身位置决定，不使用被审仓的 HEAD。builder 在 RELEASE-MANIFEST 中同样记录 `plugin_code_digest`，与运行时算法一致。

`workspace_changes` 是前后快照观测，不是写入者归因。Git 路径的状态及内容哈希与原 dirty 列表分开比较；缺快照、HEAD 变化或无法定位的 index 差异返回 unknown/null。非 Git 资料沿用声明内容及目录结构守卫，未声明文件内容不在覆盖范围。

公开 JSONL 保持原始游标；活动索引增加 last_meaningful_event，provider 计数不覆盖工作台最近活动。旧索引只读重建并有界缓存，不为刷新改写历史证据。新写入继续原子更新索引。

start/status/wait/details/decide 增加可选 compact，默认 false 保留旧接口；Skill 常规调用传 true。摘要省略完整 result/decision_history，保留证据读取入口；wait 摘要省略 provider 活动计数事件并给省略数，next_cursor 仍是原始流游标。完整事件仍可查询。

## 任务级核验要求（由协调者落实）

以下是协调与验收要求，不表示每个普通 packet 都具备完整协议字段，或 Runtime 已自动执行所有项目闸。

- 派单带 task/lane/revision/attempt/要求来源/基线身份；结果绑定同一身份与实际代码/产物。
- 重复交付去重；未知结果先 reconciliation，不把断流等于失败，不因重启自动重做外部动作。
- 改向先撤销相关派单与合入资格，再停止原执行者、核对部分改动。状态中的 lease 不能阻止任意同用户进程写磁盘，须由实际工具/权限边界和终止核对落实。
- 全局单一协调权；写入归属按候选协调。换会话不能重派仍在运行的任务，旧会话的结果不得自动更新新方向。
- 结果错误、缺席、截断与权限拒绝都是明确状态，不能过滤后变成“无问题”。
- 系统语义审查对照需求、职责、正常路径和反例。自动程序只能抓版本/范围/重试等信号，不能机械裁决架构正确。
- 完成后的局部纠正可以复用 session；结构性修复优先重新读源建立理解。保留原生 compaction，不设置固定轮换阈值。

## 保存 Workflow 的只读 `workflow_review`

当前可用的是命名保存 Workflow 的窄入口，而不是完整 Dynamic Workflow Review。协调者必须先从 `claude_saved_workflows` inventory 选择一个有效 `name`/绝对 `path`/`sha256`，再用可选且精确的 JSON `args` 绑定到 `workflow_review` packet。inventory 从 `cwd` 向 Git 根的 `.claude/workflows` 与个人 Claude 配置目录的 `workflows` 得到有效条目；项目优先级与歧义会被明确处理。历史 session 或过去 run 不是安装来源。

该模式只读、fresh-only，不允许 correction 或 resume，不能转而调用内联脚本、其他 Workflow 或普通 review。bridge 需要观察到同一绑定 Workflow 的工具调用、成功结果和完成通知，且所采用的父级结果在完成通知之后，才会允许报告继续进入主 Codex 核验；完成前的结果只是中间结果。它既不批准计划，也不等于多席 Workflow 的成功。

历史记录：早期版本曾在 CLI 2.1.276、2.1.277 和 2.1.278 的真实 MCP 夹具观察该受限模式：一名只读 agent，关联启动/完成事件、结构化结果以及文件未变化。支持原生 system/task_notification，拒绝只有模型完成声明的结果。当前版本不再按 CLI 版本准入，任何本机 CLI 都必须在该轮产生同样的完成证据。该证据不代表完整 Dynamic Workflow Review、多席位成本与实施型写入边界已验收。

## 尚未验收：Claude Dynamic Workflow Review

官方文档（2026-09-18 核对）：https://code.claude.com/docs/en/workflows

Dynamic Workflows 可用于 CLI、Desktop、`-p` 与 SDK。关键词触发只适用于真正的人类交互输入；程序生成的 prompt 不能冒充人类输入。headless 运行需要明确 Workflow 调用及匹配权限，可对某个已保存 workflow 配置许可。工作流代码需要 Claude 运行时注入的 `agent/parallel/pipeline`，不能直接交给 Node 当普通脚本运行。

若以后接入完整模式，需单独验收“架构独立审查→具体实现→证据复核→合成”的多席策略、并行/成本可见性、失败语义、写入与权限边界、审查输出目录，以及实施型 Workflow 的真实效果。普通单席 review 或当前 `workflow_review` 都不能替代这些证据，也不能作为每个 lane 的默认循环。

## 状态恢复与 0.6.1 启动交接

0.6.1 的 Runtime 从自己的有效状态目录启动 Bridge，显式设置 `Popen(cwd=state_root)`；Claude 子进程继续使用 packet 中批准的任务 cwd。解析其他 Bridge 子命令时不再提前调用 `Path.cwd()`；隐式 doctor cwd 不可用时返回有解释的诊断错误，显式 `--cwd` 不依赖已删除的父 cwd。

在创建新运行记录前，Runtime 检查加载时捕获的执行模块身份与磁盘一致；代码已更换或无法读取时拒绝派单。启动 Bridge 前在执行目录之外写 `runtime_pre_spawn`，绑定 nonce、协议版本、代码摘要、run/task/revision、packet、CLI 描述符和 lane。该阶段 `child_started=null`，不能被旧恢复 reader 当成已证明未启动。Bridge 通过 `--startup-nonce` 核对交接，接管后才进入后续阶段；旧 Bridge 不识别这个参数时会拒绝启动，不退回无绑定执行。

运行记录中的 `bridge_spawn_cwd` 与任务 `cwd` 分开保留。`claude_diagnostics.bridge_startup` 将启动目录和代码身份与 CLI 安装/认证结果分开：旧父 cwd 不可用不等于登录失败；只有新加载的 Runtime 才具有这些守卫，旧进程不会因安装动作自动更新。

运行事实与主 Codex 验收分别记录。终态拒绝旧活动快照覆盖；新 bridge 在 run-dir 之外保存输入哈希绑定的启动阶段回执（0.6.1 受监督派发为 runtime_pre_spawn → pre_dispatch → launch_intent → executing → terminal）。launch_intent 尚未持久化 child 身份时不能推断未启动。重连读取可信终态、确认桥/Claude 进程组停止并持有该 run 的 worktree lane（旧版子目录记录另需其旧的精确 cwd 锁）后同步结果；证据不足仍 unknown。人工 reconcile 只解除已核验占用，不创造 reported 或 accepted。带 `lane_identity` 的记录其 Git 子目录 cwd 已删除时，inspect 在仍解析为该 lane 的 worktree 根目录取证，快照保持原 cwd 坐标，不改写 packet；无 lane 身份的旧记录、symlink/嵌套仓库/越界位置或 packet 绑定不符时仍不能取证。cwd 仍存在时同样须是解析到自身、worktree 根仍为该 lane 的真实目录，否则不取证。历史记录继续按自身的 packet/receipt/进程绑定核验，不能只靠新字段缺失推断已停止或从未启动。

对 0.6.1 新绑定的 run，只有整个执行目录不存在时，恢复才可使用外置派单 packet；目录存在时要求完整且一致的目录内 packet/CLI 副本。仍须在 lane 锁内证明 Bridge 进程组已停止、无其他执行者、工作区与 expected_workspace_digest 一致，并验证生命周期的 nonce/代码/lane 绑定。损坏绑定不能借 `child.json` 绕过，启动结果不确定时保持 unknown。

恢复先持久化审计事实，再提交 registry，最后只移除匹配本轮的 marker。执行目录存在时沿用 `reconciliation.json`；整个目录尚未生成时写 `<state_root>/recovery-receipts/<run_id>.json`，不补造目录或活动日志。后一路径即使回执已写、registry 提交失败，重试也只能采纳同一份绑定事实；不覆写原因/证据。恢复后旧 unknown 状态和历史证据保留，只允许新的 fresh revision。旧版缺少启动绑定的事故记录不因此获得恢复资格。

Claude 子进程在独立进程组中运行，不继承 lane 锁。因此 bridge 在 lane 锁内复查无 marker 后，于 Claude Popen 之前发布本 run 的 lane 级 launch intent marker，记录原 cwd、规范 lane、run_id、run_dir 与 lifecycle 文件位置。Runtime 与 bridge 都崩溃而子进程仍存活时，其他状态目录的 Runtime 与独立 bridge 仍会被这个 marker 拒绝，同一 worktree 的任何 cwd 都不能派发；linked worktree 不受影响。bridge 只在以下条件都满足后，才按 nonce 删除自己未被改写的 marker：直接子进程已回收、进程组确认不存在（或 Popen 明确失败、从未启动），且回执与终态 lifecycle 已落盘。清理未确认、启动被中断、以及任何崩溃窗口都保留 marker。Runtime 判定 unknown 时，会用带 `state_root` 的同 run marker 覆盖它。之后只能由原状态目录采纳可信终态，或经 inspect/reconcile 核实进程组已停止后清除，不凭 PID 自动清理。

历史记录：2.1.278 隔离负面 Workflow 中，真实子代理 Read 被父 PreToolUse hook 拒绝并在公共事件中留痕；Bash/Write 未被实际调用，子代理报告工具未提供。此证据确认 Read hook 传递，不声称执行过一次实际 Bash 拒绝。
