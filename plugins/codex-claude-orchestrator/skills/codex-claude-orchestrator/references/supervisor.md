# 原生 Codex 监督子代理

这是 Skill 对原生子代理的派单约定，不是另一个 Claude 协调者，也不是插件注册的自定义模型。满足独立性、并行工作或上下文隔离需要，且客户端支持原生子代理时，使用实际 spawn/send/wait 工具；短任务可由主代理直接管理 MCP；不要用 create_thread 创建用户侧任务。

## 派单

主 Codex 发给一个监督席：本 Skill 绝对路径；任务目录与现有 PROGRESS；明确的 task/lane、cwd、revision；原始要求/批准 delta/适用协议；文件范围；约束与验收；运行上限；模型/effort；本轮 packet 或构造它所需的完整材料；主代理保留的并行核验工作。规则明确的监督默认采用宿主适用的轻量模型路由，不静默更换用户指定型号。

说明唯一权限归属：监督席可启动/观察/按要求取消 Claude 和回传证据；不直接改业务文件，不改项目 PROGRESS，不批准 accepted，不改变目标、oracle 或 file scope。Claude 是本轮唯一获派业务写入者。主代理负责共享记录和验收。

## 执行

1. 读取本 Skill、packet 和实际来源。包括审查插件自身在内，本轮委派通过插件建立；不要把禁止 Claude 递归派单解释为禁止 Codex 管理本轮。直接发现 `claude_*` MCP 工具；不可见时回报工具不可用，不自行反复安装、另起协调者或把 shell 后备伪称为原生 MCP 调用。
2. 本地/必要在线预检后只启动一次，立即用父代理通信工具回传 run_id、cwd、task/revision、执行者、（本监督席的）details_url 与 run_dir，仅供参考。主代理不直接复用该 details_url；改用自己的 MCP 连接调用 `claude_details(run_id)` 取得本方链接，再按打开请求记账决定是否打开一次（`queued` 只表示排队）。监督席不负责操作主窗口，也不自行调用 `open_in_codex`。
3. 用 claude_wait(compact=true) 与游标等待真实活动；start/status/details 也使用 compact=true，需要完整报告时按需 claude_result。阶段变化、重要工具/文件活动、故障或终态时发简短消息；普通无变化等待不刷屏。无需伪造总进度百分比。记录 `reported` 与接受结论的区别。
4. 收到主代理停止消息，先 claude_cancel 再等终态回执；回传停止证据与残留变更。不要先结束自己的会话，让取消无法送达。非 owner 的 MCP Runtime 可能拒绝 cancel，应发消息给原 owner；不能猜 PID 或盲目重启。
5. Claude 结束后读取 receipt/result、权限拒绝、Git 前后证据，回传摘要、问题/不确定性、原始记录路径。主代理亲自看文件和执行所需检查，仅对未被 superseded 的 reported 轮次调用 claude_decide，并更新任务记录。failed/cancelled/timeout/blocked/unknown 不调用该接口、不改原状态；主代理独立完成任务时，在既有 PROGRESS 记录 run_id、原状态、完成者 Codex、completion_summary、reason、evidence 和未完成事项。

MCP 连接/监督任务结束会影响本地详情服务；主代理需要继续查看时用自己的 claude_details 重新获取入口。不要把一次 session 的 token URL 写成永久文档入口，持久记录使用 run_id/run_dir。

## 纠正

主代理保持该监督席作为明确运行 owner，给出本轮修正的原要求、反例、finding_id 与次数。局部已完成纠正可 resume；结构/方向变化须停止受影响写入、核查部分产物、fresh。监督席不自行“再试一轮”。完整 v2.9 的同一 finding 一轮自修后由主代理裁决，换执行者不清零。

## 必须回传

- 实际路径：原生 Codex 监督席 → MCP → Claude，或明确说明后备路径。
- run_id / task / revision / cwd / 实际 session 和模型。
- 本轮输入来源、文件证据、真实检查与未运行检查。
- 当前状态、必要纠正/需主代理决定的问题、详情入口。

主代理仍是唯一面对用户交付、接受结果和调整工作方向的协调者。
