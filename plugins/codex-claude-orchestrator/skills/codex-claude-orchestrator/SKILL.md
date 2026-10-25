---
name: codex-claude-orchestrator
description: Delegate implementation or analysis (code review, planning, research, questions about a codebase or documents) to the local Claude CLI through the claude_* MCP tools, answer Claude's questions while it runs, then verify the result and record a verdict. Use when the user asks Claude to do, review, check or continue something, asks about a Claude run's progress, or corrects an earlier Claude result.
---

# Codex–Claude 委派

Codex 负责理解需求、派单、运行中答疑、核验和最终交付；Claude 在受监督的进程里执行，交回事实、报告和 patch。插件只报告事实和警告，是否通过由 Codex 判定。操作细节见 [references/guide.md](references/guide.md)。

## 派单时必须做到

1. **原话逐字转交。** 把用户仍然有效的每条原话原样放进 `user_messages`（`source=human`；别的代理转来的写 `relayed`）。不改写、不缩窄、不合并。拿不到原话时写 `no_user_words_reason`，不要拿自己的转述冒充原话。
2. **原件优先于转述。** 规格、计划、Jira 导出、用户转发的审查等原件放进 `inputs`（文件或目录），插件按字节冻结给 Claude。`brief` 只写你自己的理解，Claude 会把它当作“可能有误”。
3. **标清来源。** `constraints` 和 `done_when` 每条都标 `origin`：用户说的是 `user`，原件里的是 `doc`，你自己加的一律 `coordinator`。
4. **不替用户加约束。** 用户没要求“保留旧路径、保持兼容、不放宽门禁”，就不要写成约束；确有顾虑时，作为问题交给用户。“不放宽正式判据”只适用于被测项目的验收判据，不是修改本插件或其他工具门禁时的默认立场。
5. **给人看的标题。** `title` 用一句短话说清任务（≤60 字），续接 1.1 的任务时不改就沿用；`round_title` 说明这一轮改了什么（≤80 字）。1.0.x 的标题取自原话开头，不会被继承：第一次续接 1.0.x 的任务时要带上简短的 `title`，否则新一轮显示 `task_id`。标题由你概括，不要截取原话开头；原话仍逐字放在 `user_messages`。
6. **同一对话用同一个 `context`。** 本对话第一次派单前取一个稳定的 `context.chat`（例如 `chat-<日期>-<短随机串>`），记进本任务的进度记录；之后本对话的每次调用都带上它。没有它时工作台只给链接，`task_id` 可能与其他 Codex 对话冲突。

## 运行中沟通

- 用 `claude_wait(run_id, after=next_cursor, context=...)` 等待。`reason=interaction` 时看 `pending`：Claude 的阻塞问题（`question`）或授权请求（`permission`）。
- 在已有授权内能判断的，直接用 `claude_reply(kind=answer, origin=coordinator)` 回答；授权请求按任务授权给 `decision=allow|deny`。不影响主流程的事不要转给用户。
- 只有核心需求冲突、验收冲突、超出授权或必须用户拍板的事才转给用户；先回答不受影响的部分，用户的回答原样用 `origin=human` 转交。
- `note`/`dispute` 是 Claude 的非阻塞说明，已自动登记，Claude 不等回复；需要纠正时用 `claude_reply(kind=update)`。
- 需要改方向用 `kind=interrupt`（可带 `text` 作为改向指令）；要结束整个运行用 `claude_cancel`。回执里的 `delivery.state` 只说明送达阶段，`echoed` 也不等于 Claude 已照做。

## 核验时必须做到

- `reported`/`ok` 只表示进程正常结束，`claimed_status` 是 Claude 的自评；都不是通过。
- 读 `report`、`result`、`patch`，自己运行必要的检查；要合入时执行最新一轮的 `check_hint`，再执行 `apply_hint`（delivery.patch，含此前未合入的轮次），并在裁决里写 `applied=true`。
- 用 `claude_decide` 记录裁决：`verdict`（accepted / accepted_with_corrections / rejected）与 `next`（done / next_round / codex_finishes）分开写，`evidence` 写你实际做过的核验。

## 工作台链接

只有响应里 `workbench.open.action` 为 `open_in_codex` 时才对 `workbench.url` 调用一次 `open_in_codex`；它在插件里先记为已请求，`queued` 也算已请求。其余时候只给可点击链接：换了链接、重连、压缩或新一轮都不再自动打开。用户明确要求时用 `claude_workbench(context, reopen=true)` 再开。MCP 重启后端口和令牌都会换，旧页面会一直重试并提示回 Codex 取新链接；这时给出最新响应里的 `workbench.url`，不要自动再开。原生监督子代理派单时传 `open_workbench=false`，不调用 `open_in_codex`。
