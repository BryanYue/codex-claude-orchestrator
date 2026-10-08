---
name: codex-claude-orchestrator
description: Delegate implementation or analysis (code review, planning, research, questions about a codebase or documents) to the local Claude CLI through the claude_* MCP tools, then verify the result and record a verdict. Use when the user asks Claude to do, review, check or continue something, asks about a Claude run's progress, or corrects an earlier Claude result.
---

# Codex–Claude 委派

Codex 负责理解需求、派单、核验和最终交付；Claude 在受监督的进程里执行，交回事实、报告和 patch。插件只报告事实和警告，是否通过由 Codex 判定。操作细节见 [references/guide.md](references/guide.md)。

## 派单时必须做到

1. **原话逐字转交。** 把用户仍然有效的每条原话原样放进 `user_messages`（`source=human`；别的代理转来的写 `relayed`）。不改写、不缩窄、不合并。拿不到原话时写 `no_user_words_reason`，不要拿自己的转述冒充原话。
2. **原件优先于转述。** 规格、计划、Jira 导出、用户转发的审查等原件放进 `inputs`（文件或目录），插件按字节冻结给 Claude。`brief` 只写你自己的理解，Claude 会把它当作“可能有误”。
3. **标清来源。** `constraints` 和 `done_when` 每条都标 `origin`：用户说的是 `user`，原件里的是 `doc`，你自己加的一律 `coordinator`。
4. **不替用户加约束。** 用户没要求“保留旧路径、保持兼容、不放宽门禁”，就不要写成约束；确有顾虑时，作为问题交给用户。“不放宽正式判据”只适用于被测项目的验收判据，不是修改本插件或其他工具门禁时的默认立场。
5. **用户的决定交给用户。** 结果里出现 `questions_for_user` 警告，或 Claude 把某事标为需要用户决定时，原样转告用户，不要自己拍板后写进下一轮的约束。

## 核验时必须做到

- `reported`/`ok` 只表示进程正常结束，`claimed_status` 是 Claude 的自评；都不是通过。
- 读 `report`、`result`、`patch`，自己运行必要的检查；要合入时执行最新一轮的 `check_hint`，再执行 `apply_hint`（delivery.patch，含此前未合入的轮次），并在裁决里写 `applied=true`。
- 用 `claude_decide` 记录裁决：`verdict`（accepted / accepted_with_corrections / rejected）与 `next`（done / next_round / codex_finishes）分开写，`evidence` 写你实际做过的核验。

## 工作台链接

每个 Codex 任务只在确认尚未请求过时，对 `details_url` 调用一次 `open_in_codex`（先记为已请求；`queued` 也算已请求），之后只给可点击链接。用户明确要求时才重新打开。原生监督子代理不调用 `open_in_codex`。
