# Codex–Claude 委派操作说明

本说明随插件 1.0 发布，供 Codex 协调者使用。必须遵守的规则在 SKILL.md；这里讲每一步怎么做。

## 1. 先确认环境

- `claude_environment(cwd)`：本机 Claude CLI 路径与版本、所需参数、登录状态，以及 copy 档需要的 macOS `sandbox-exec` 是否可用。不发模型请求。
- 首次使用、换了账号，或用户要求确认时，用 `verify=true` 做一次小额在线验证。网络、额度、模型权限失败不等于登录失效。
- 缺 CLI 或未登录时说明官方处理方式（安装 Claude Code、`claude auth login`、或设置 `CLAUDE_BIN`），不要替用户安装或登录。
- `claude_models(cwd)` 读取 CLI 声明的模型和 effort；用户指定的型号原样传递。

## 2. 选 kind 和 profile

| 任务 | kind | profile |
| --- | --- | --- |
| 改代码、写测试、修 bug | `implement` | `copy`（必须，Git 仓库） |
| 代码审查、方案评估、计划、调研、回答问题 | `analyze` | Git 仓库默认 `copy`；普通目录或用户明确不让执行命令时 `readonly` |

- **copy**：Claude 在原仓库当前状态（含未提交改动）的独立 Git 副本里工作，有 Bash、编辑、Agent、Skill、Workflow 等工具，可以跑测试。原仓库、它的 `.git` 和插件状态目录由 macOS 拒写；凭证、网络和其他本机路径不隔离。改动由插件从受保护的基线算出 patch。
- **readonly**：在原目录只读，只有 Read/Glob/Grep（`web=true` 时加 WebFetch/WebSearch），通过结构化输出交回报告。
- 副本建不起来（子模块、sparse/skip-worktree、未解决冲突、外部硬链接、非 macOS）时，analyze 改用 readonly；implement 先向用户说明。
- 敏感的未跟踪文件（`.env*`、`.claude`、`.codex`、`.ssh` 等）不会复制进副本，结果里会有 `sensitive_inputs_skipped` 警告。
- `web` 默认关闭，只在用户同意联网时打开。关闭时 Claude 会被告知不要联网，但 copy 档的 Bash 本身不受网络限制。

## 3. 写 packet

```json
{
  "task_id": "EXPORT-ASYNC",
  "kind": "implement",
  "cwd": "/abs/repo",
  "user_messages": [{"text": "用户原话，逐字", "source": "human"}],
  "inputs": ["/abs/repo/docs/spec.md", "/abs/task/PLAN.md"],
  "brief": "你的理解与补充（可能有误）",
  "constraints": [{"text": "不改公开 API", "origin": "user"}, {"text": "测试放 tests/", "origin": "coordinator"}],
  "done_when": [{"text": "uv run pytest -q 通过", "origin": "doc"}],
  "write_hint": ["src/export/"], "protected": ["migrations/"], "verify": ["uv run pytest -q"],
  "model": "opus", "effort": "high", "timeout_seconds": 3600
}
```

- `user_messages` 放所有仍然有效的原话，按时间排序；用户后来收回的范围在 `brief` 里说明，不删原话。
- `inputs` 可以是目录，有数量和大小上限；任务消息、批准记录这类没有文件的原话直接放进 `user_messages`。
- `focus`（仅 analyze）只写从原话里摘出的关注点；没有就不写，不要套固定的审查维度。
- `write_hint`、`protected` 只用于事后给 patch 分类，运行中不拦截。
- `timeout_seconds` 必填，按工作量给。`max_budget_usd` 默认不设，只有用户要求限制花费时才填。
- 1.0 之前的字段（role、objective、user_request、requirement_sources、acceptance、owned_files 等）会被拒绝，报错里给出新字段名。

## 4. 启动与等待

- `claude_start(packet)` 立即返回 `run_id` 和 `details_url`。说明任务目标、kind/profile 和真实状态；不要声称 Claude 已开始工作，等 `claude_wait` 里出现执行活动。
- 用 `claude_wait(run_id, after=next_cursor)` 增量等待（每次最多 25 秒），不要高频轮询或重新派单。同一 task_id 同时只能有一个活动 run。
- 用户要求停止或改向：`claude_cancel(run_id, reason)`，再等到 run 结束。取消后已写下的报告和改动仍会收回。
- MCP 重启不影响正在运行的任务：新连接用原 `run_id` 查询即可。bridge 异常退出时，插件会停止残留的 Claude 进程，把 run 记为 `lost`，并收回已有产出。

## 5. 读结果

`claude_status(run_id)` 的 `outcome` 是插件记录的事实：

- `run_outcome`：ok / timeout / cancelled / crashed / not_started / lost，只描述进程怎么结束。
- `claimed_status`：Claude 在 result.json 里的自评（completed / partial / blocked），只是它的说法。
- `warnings`：事实性警告，不改变 run_outcome。重点处理：
  - `questions_for_user`：原样转告用户，由用户决定。
  - `disputes_present`：Claude 认为约束、原件或你的说明与原话冲突，逐条核对。
  - `protected_touched`：patch 改了 protected 路径，裁决时必须在 `protected_confirmed` 里逐条确认。
  - `original_changed_during_run`：Claude 工作期间原仓库被改过，应用 patch 前先 `--check`。
  - `carried_already_in_original`：上一轮尚未记录为已合入的改动其实已经原样在原仓库里；以后合入时记得写 `applied=true`。
  - `undelivered_files_now_ignored`：重新放上的待交付文件被原仓库新的忽略规则匹配；它们仍在 delivery.patch 里，确认是否还应交付。
  - `source_not_read`、`tool_denied`、`workflow_incomplete`、`report_missing`、`result_malformed` 等：核验时据此判断报告的可信度。
- `changes`：两份 patch。
  - `patch`（changes.patch）：只含本轮改动，用来审；`files` 是本轮文件清单（in_scope / outside_hint / protected）和行数。
  - `delivery_patch`（delivery.patch）：所有还没应用到原仓库的改动，包括此前没有合入的轮次，用来合入；`delivery_files` 是它的文件清单。`check_hint`、`apply_hint` 针对的就是这一份；没有待交付的改动时这两个字段为空，不需要合入。

用 `claude_result(run_id, artifact)` 分页读原文（跟着 `next_offset` 读到 `end_of_artifact`，核对 `sha256`）：`report`（第一节是需求对照）、`result`、`patch`、`brief`（Claude 实际收到的任务书）、`final_message`、`inputs`、`instruction_layer`（本次会话加载的用户/项目 CLAUDE.md、rules 和 hooks）、`stream`、`stderr` 等。

## 6. 核验与裁决

- 对照用户原话和 `done_when`，逐条核对报告里的“需求对照”。自己运行必要的测试；区分 Claude 自报、你实际运行的结果和推断。
- 合入改动：执行最新一轮的 `check_hint`（`git -C <repo> apply --check delivery.patch`），通过后执行 `apply_hint`。delivery.patch 以原仓库工作区内容（含未提交改动）为基线，直接作用于工作区，不要加 `--3way` 或 `--index`。检查不通过多半是原仓库在运行期间又改过（见 `original_changed_during_run`），先手工合并冲突，不要用副本里的整文件覆盖新内容。合入之后你可以继续在原仓库里修正。
- `claude_decide(run_id, verdict, next, note, evidence, item_decisions?, protected_confirmed?, applied?)`：
  - `verdict`：accepted / accepted_with_corrections（你修正了部分内容后采纳）/ rejected。
  - `next`：done / next_round（再派一轮）/ codex_finishes（你自己完成剩余部分）。
  - result.json 有 `items` 时，`item_decisions` 要逐条给出 accepted / downgraded / rejected，后两者写原因。
  - `applied=true`：你已把这一轮的 delivery.patch 应用到原仓库（之后再修正也算）。只能用于最新一个产生过改动的轮次。没合入就不要写；如果合入后忘了写，可以重新裁决补上。再次裁决时不写 `applied` 会沿用之前记录的值，只有明确写 `false` 才撤销。
  - 任何结束状态（包括 timeout、crashed、lost）只要进程已停止都可以裁决；再次裁决会追加历史。
- 交付给用户时说清：完成了什么、你实际验证了什么、哪些只是 Claude 自报、还有什么没做、需要用户决定的问题。

## 7. 续接与纠正

- 同一任务的下一轮用 `continue_from=<最新一轮的 run_id>`（只能接最新一轮，不能从更早的轮次分叉）：kind、profile、task_id 不变；`user_messages` 必须原样包含上一轮的全部原话，只能在后面追加新的原话。
- copy 档的代码基线：每一轮都按原仓库的当前状态重新建立副本，再放上最近一份尚未记录为已合入的 delivery.patch（`outcome.carried` 说明来自哪一轮）；这个任务任何一次交付涉及过、现在仍存在于原仓库的文件，即使被忽略规则匹配也会带入（还没合入的删除，也会先带入原文件再由 patch 删除）。副本是一次性的，上一轮中断或副本残缺都不影响下一轮。被忽略的依赖和构建产物（如 `.venv`、`node_modules`）不会跨轮保留，需要时 Claude 会重新安装。
- 已经合入、但没有用 `applied=true` 记录的改动，如果原样还在原仓库里，会被自动识别（`carried_already_in_original`）；如果你合入后又修正过，必须先记录 `applied=true`，否则这一轮以 not_started 结束并说明冲突文件，补记后再续接即可。
- 插件会尽量用 `--resume` 接上原会话；接不上时自动开新会话并给出 `resume_fallback` 警告，任务书仍是完整的。
- 方向变了或需求结构变了，就不用 `continue_from`，开一个新的 task_id。
- 此前各轮的裁决（结论、下一步、说明、逐项结论和理由，以及 decision.json 路径）都会写进下一轮任务书，并注明效力高于原报告，不需要再手抄。

## 8. 清理

任务的最后一轮裁决之后，`claude_cleanup(run_id)` 删除该任务的副本（patch、报告、任务书和裁决都保留）。删除后不能再续接。清理一旦开始，副本就不再可续接；如果清理被中断（进程退出或删除出错），再调用一次 `claude_cleanup` 会接管并完成删除，原清理进程仍在运行时会被拒绝。

## 9. 监督子代理

只有在并行或上下文隔离确有收益时，才按 [supervisor.md](supervisor.md) 派一个原生 Codex 监督子代理。主代理保留裁决和对用户的交付。
