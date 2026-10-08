# 原生 Codex 监督子代理

监督子代理是主 Codex 派出的原生子代理，负责启动和看护一个 Claude run；它不是另一个协调者。短任务由主代理直接用 MCP 管理即可。

## 主代理派单时给它

- 本 Skill 的绝对路径，以及完整的 packet（或构造 packet 所需的全部原件：用户原话逐字、原件路径、约束及其 origin、完成标准）。
- 运行上限、模型与 effort、需要回传的时机。
- 明确权限：它可以启动、等待、按要求取消 Claude run，并回传事实；它不改业务文件、不调用 `claude_decide`、不改变目标或范围、不再派嵌套子代理，也不调用 `open_in_codex`。

## 它的执行步骤

1. 直接使用 `claude_*` MCP 工具；工具不可用就如实回报，不改用直接调用 CLI 或临时脚本冒充。
2. `claude_start` 只调用一次，立即把 `run_id` 回传给主代理。
3. 用 `claude_wait` 和游标等待；阶段变化、故障或结束时简短回传，无变化时不刷屏。
4. 收到停止指令先 `claude_cancel`，等 run 结束后再回传。
5. 结束后回传：run_id、`run_outcome`、`claimed_status`、警告清单（尤其 `questions_for_user` 原文）、报告和 patch 的位置、它自己未能核实的内容。

主代理用自己的 `claude_status(run_id)` 取得工作台链接，亲自核验后调用 `claude_decide`，并负责对用户交付。
