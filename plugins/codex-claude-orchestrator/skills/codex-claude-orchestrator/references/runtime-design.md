# Runtime 与完整 Workflow 的边界

本页区分 0.4.1 已交付的持久 Runtime、受限保存 Workflow 审查，以及仍未验收的完整 Dynamic Workflow Review；不把候选设计写成当前能力。

## 已交付：持久 Runtime 与受监督执行

当前链路是 Codex 桌面/CLI 会话 + Skill + MCP + Python Runtime + Claude bridge。Runtime 保存每轮 packet、环境预检、公开流事件、结果、回执、运行身份与协调者决定；重启后不会把遗留活动 run 自动当成功或自动重派。它提供 cwd 排他、取消确认、精确 run 身份、局部 correction/resume 与 fresh 改向的边界。项目采用和 `.agents/codex-claude/routing.json` 也已经交付：采用一次后，普通自然请求按任务特点及用户偏好产生可解释选择，不是随机比例或能力断言。

主 Codex 仍拥有目标、正式验收与写入协调；原生 Codex 监督席只在合适时管理 Claude 执行。默认详情是简洁摘要，用户可展开或切到完整证据视图；宿主原生界面与 CLI 样式由 Codex 管理，插件不能直接改写。

未交付的是无人值守 daemon、自动更换协调者、自动合并/发布，以及把状态存储升级为某一特定 TypeScript、Agent SDK 或 SQLite 技术栈。这些是后续候选，不是本版技术契约。

## 0.4.5 补充：报告处置、快照和可见进度

`claude_decide` 的 accepted/returned 仍表示原 Claude 报告的核验决定。returned 默认 resolution=revision_requested；协调者亲自补齐并核验任务后，可显式提供 completed_by_codex、completion_summary、reason、evidence。历史决定追加保存，原 provider result 与 receipt 不改；缺少明确记录不推断闭环，superseded 仍优先显示历史轮。

`workspace_changes` 是前后快照观测，不是写入者归因。Git 路径的状态及内容哈希与原 dirty 列表分开比较；缺快照、HEAD 变化或无法定位的 index 差异返回 unknown/null。非 Git 资料沿用声明内容及目录结构守卫，未声明文件内容不在覆盖范围。

公开 JSONL 保持原始游标；活动索引增加 last_meaningful_event，provider 计数不覆盖简洁页最近活动。旧索引只读重建并有界缓存，不为刷新改写历史证据。新写入继续原子更新索引。

start/status/wait/details/decide 增加可选 compact，默认 false 保留旧接口；Skill 常规调用传 true。摘要省略完整 result/decision_history，保留证据读取入口；wait 摘要省略 provider 活动计数事件并给省略数，next_cursor 仍是原始流游标。完整事件仍可查询。

## 必须验证的机制

- 派单带 task/lane/revision/attempt/要求来源/基线身份；结果绑定同一身份与实际代码/产物。
- 重复交付去重；未知结果先 reconciliation，不把断流等于失败，不因重启自动重做外部动作。
- 改向先撤销相关派单与合入资格，再停止原执行者、核对部分改动。状态中的 lease 不能阻止任意同用户进程写磁盘，须由实际工具/权限边界和终止核对落实。
- 全局单一协调权；写入归属按候选协调。换会话不能重派仍在运行的任务，旧会话的结果不得自动更新新方向。
- 结果错误、缺席、截断与权限拒绝都是明确状态，不能过滤后变成“无问题”。
- 系统语义审查对照需求、职责、正常路径和反例。自动程序只能抓版本/范围/重试等信号，不能机械裁决架构正确。
- 完成后的局部纠正可以复用 session；结构性修复优先重新读源建立理解。保留原生 compaction，不设置固定轮换阈值。

## 保存 Workflow 的只读 `workflow_review`

当前可用的是命名保存 Workflow 的窄入口，而不是完整 Dynamic Workflow Review。协调者必须先从 `claude_saved_workflows` inventory 选择一个有效 `name`/绝对 `path`/`sha256`，再用可选且精确的 JSON `args` 绑定到 `workflow_review` packet。inventory 从 `cwd` 向 Git 根的 `.claude/workflows` 与个人 Claude 配置目录的 `workflows` 得到有效条目；项目优先级与歧义会被明确处理。历史 session 或过去 run 不是安装来源。

该模式只读、fresh-only，不允许 correction 或 resume，不能转而调用内联脚本、其他 Workflow 或普通 review。bridge 需要观察到同一绑定 Workflow 的工具调用、成功结果和完成通知才会允许报告继续进入主 Codex 核验。它既不批准计划，也不等于多席 Workflow 的成功。

受限模式已在 CLI 2.1.276、2.1.277 和 2.1.278 的真实 MCP 夹具重新验证：一名只读 agent，关联启动/完成事件、结构化结果以及文件未变化。支持原生 system/task_notification，拒绝只有模型完成声明的结果。该证据不代表完整 Dynamic Workflow Review、多席位成本与实施型写入边界已验收。

## 尚未验收：Claude Dynamic Workflow Review

官方文档（2026-09-18 核对）：https://code.claude.com/docs/en/workflows

Dynamic Workflows 可用于 CLI、Desktop、`-p` 与 SDK。关键词触发只适用于真正的人类交互输入；程序生成的 prompt 不能冒充人类输入。headless 运行需要明确 Workflow 调用及匹配权限，可对某个已保存 workflow 配置许可。工作流代码需要 Claude 运行时注入的 `agent/parallel/pipeline`，不能直接交给 Node 当普通脚本运行。

若以后接入完整模式，需单独验收“架构独立审查→具体实现→证据复核→合成”的多席策略、并行/成本可见性、失败语义、写入与权限边界、审查输出目录，以及实施型 Workflow 的真实效果。普通单席 review 或当前 `workflow_review` 都不能替代这些证据，也不能作为每个 lane 的默认循环。

## 0.4.1 状态恢复

运行事实与主 Codex 验收分别记录。终态拒绝旧活动快照覆盖；新 bridge 在 run-dir 之外保存输入哈希绑定的启动阶段回执（pre_dispatch → launch_intent → executing → terminal）。launch_intent 尚未持久化 child 身份时不能推断未启动。重连读取可信终态、确认桥/Claude 进程组停止并持有 cwd lane 后同步结果；证据不足仍 unknown。人工 reconcile 只解除已核验占用，不创造 reported 或 accepted。0.4.0 记录通过其旧 packet/receipt/进程绑定核验迁移，不能只靠新字段缺失判死。

2.1.278 隔离负面 Workflow 中，真实子代理 Read 被父 PreToolUse hook 拒绝并在公共事件中留痕；Bash/Write 未被实际调用，子代理报告工具未提供。此证据确认 Read hook 传递，不声称执行过一次实际 Bash 拒绝。
