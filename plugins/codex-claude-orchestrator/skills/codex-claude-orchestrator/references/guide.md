# Codex–Claude 委派操作说明

本说明随插件 1.1 发布，供 Codex 协调者使用。必须遵守的规则在 SKILL.md；这里讲每一步怎么做。

## 1. 先确认环境

- `claude_environment(cwd)`：本机 Claude CLI 路径与版本、所需参数、登录状态，以及 macOS `sandbox-exec` 写保护是否真的能启用（`sandbox.works`）。不发模型请求。
- 首次使用、换了账号，或用户要求确认时，用 `verify=true` 做一次小额在线验证。网络、额度、模型权限失败不等于登录失效。
- 缺 CLI 或未登录时说明官方处理方式（安装 Claude Code、`claude auth login`、或设置 `CLAUDE_BIN`），不要替用户安装或登录。
- `claude_models(cwd)` 读取 CLI 声明的模型和 effort；用户指定的型号原样传递。

## 2. 选 kind 和 profile

| 任务 | kind | profile |
| --- | --- | --- |
| 改代码、写测试、修 bug | `implement` | `copy`（必须，Git 仓库） |
| 代码审查、方案评估、计划、调研、回答问题 | `analyze` | Git 仓库默认 `copy`；普通目录或需要直接在原目录分析时 `readonly` |

- 两档都给 Claude 原生的全部工具，权限模式 `auto`；插件不维护工具白名单或黑名单，也不改用户或组织的设置、MCP、技能和 hooks。隔离靠操作系统写保护和任务授权，而不是藏起工具。
- **copy**：Claude 在原仓库当前状态（含未提交改动）的独立 Git 副本里工作，可以编辑、跑测试、用 Agent/Skill/Workflow。原仓库、它的 `.git` 和插件状态目录由 macOS 拒写；凭证、网络和其他本机路径不隔离。改动由插件从受保护的基线算出 patch。
- **readonly**：在原目录工作，原目录（Git 仓库时含 `.git`）和插件状态由 macOS 拒写，写入会失败；可以读、搜索、运行只读命令，通过结构化输出交回报告。分析的目录不能包含插件状态目录（默认 `~/.codex/claude-orchestrator`，例如直接分析 `~`）：这种布局在启动前就以 not_started 结束并说明原因；改为分析更小的目录，或用 `CLAUDE_ORCHESTRATOR_STATE_DIR` 把状态目录放到分析目录之外。插件不会自动搬动状态目录或放宽写边界。
- 两档都需要能真正生效的 `sandbox-exec`。不可用（非 macOS、或在另一层沙箱里被拒绝）时插件拒绝开工并说明原因，不会只靠提示词保护原目录。
- 副本建不起来（子模块、sparse/skip-worktree、未解决冲突、外部硬链接）时，analyze 改用 readonly；implement 先向用户说明。
- 敏感的未跟踪文件（`.env*`、`.claude`、`.codex`、`.ssh` 等）不会复制进副本，结果里会有 `sensitive_inputs_skipped` 警告。
- `web` 默认关闭，只在用户同意联网时打开。它是任务授权：关闭时任务书告诉 Claude 不要联网，但联网工具和 Bash 本身不受网络限制。

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
  "model": "opus", "effort": "high", "timeout_seconds": 3600,
  "title": "导出改成异步", "round_title": "第一版：后台任务和失败提示",
  "context": {"chat": "chat-20261025-k3f9"}
}
```

- `title`（≤60 字，单行）是给人看的任务名，由你概括，不截取原话开头。续接 1.1 的任务时不传就沿用上一轮的标题。1.0.x 的标题取自原话开头，不会被继承：第一次续接 1.0.x 的任务时要带上简短的 `title`，不带时新一轮显示 `task_id`，之后的轮次沿用你给的标题。都没有时显示 `task_id`。`round_title`（≤80 字）说明这一轮做什么。超长会被截断，换行会被合并成空格；它们不参与路由和权限。
- `context.chat` 是你为本对话取的稳定键，所有调用都带上。同一台电脑上的两个 Codex 对话即使用了相同的 `task_id` 也互不干扰：查询、取消、裁决和回复都会核对 `context`：不匹配或省略都会被拒绝（不带 `context` 启动的运行和 1.0.x 的运行不受此限）。插件启动环境设置了 `CODEX_CLAUDE_NAMESPACE` 时，它优先作为命名空间。
- `interactive` 默认 `true`：Claude 运行中可以提问和留言。设为 `false` 时，Claude 的提问会被立即拒绝并提示它写进 `questions`。

- `user_messages` 放所有仍然有效的原话，按时间排序；用户后来收回的范围在 `brief` 里说明，不删原话。
- `inputs` 可以是目录，有数量和大小上限；任务消息、批准记录这类没有文件的原话直接放进 `user_messages`。
- `focus`（仅 analyze）只写从原话里摘出的关注点；没有就不写，不要套固定的审查维度。
- `write_hint`、`protected` 只用于事后给 patch 分类，运行中不拦截。
- `timeout_seconds` 必填，按工作量给。`max_budget_usd` 默认不设，只有用户要求限制花费时才填。
- 1.0 之前的字段（role、objective、user_request、requirement_sources、acceptance、owned_files 等）会被拒绝，报错里给出新字段名。

## 4. 启动与等待

- `claude_start(packet)` 立即返回 `run_id` 和 `workbench`。说明任务目标、kind/profile 和真实状态；不要声称 Claude 已开始工作，等 `claude_wait` 里出现执行活动。
- 用 `claude_wait(run_id, after=next_cursor, context=...)` 增量等待（每次最多 25 秒），不要高频轮询或重新派单。它在 Claude 提问或留言（`reason=interaction`）、出现可见进展（`activity`）、run 结束（`terminal`）或超时（`timeout`）时返回；模型和工具的计数事件只推进游标，不会提前唤醒。同一对话里同一 task_id 同时只能有一个活动 run。
- 用户要求停止：`claude_cancel(run_id, reason)`，再等到 run 结束。取消后已写下的报告和改动仍会收回。改方向不必取消，见下一节。
- MCP 重启不影响正在运行的任务：新连接用原 `run_id` 查询即可。bridge 异常退出时，插件会停止残留的 Claude 进程，包括 bridge 已经观察到、离开了 Claude 进程组的后台命令，把 run 记为 `lost`，并收回已有产出。这些后台进程仍在运行时，这一轮不算已停止，不能续接或清理。

## 5. 运行中沟通

Claude 用原生的 AskUserQuestion 提严重问题，插件把它记成待处理的 `question`；需要越权的操作会成为 `permission` 请求。两者都会阻塞：Claude 在等，插件同时暂停这次执行——其余子进程（包括后台命令）被挂起（`hold.mode=family_hold`）；后台 Workflow 和后台代理在 CLI 进程内部，挂起子进程停不住，插件用 CLI 原生的 `stop_task` 停止它们并在 `hold.stopped_tasks` 里记下，Claude 收到回答后可以重新启动。CLI 没有确认停止时，插件停止整个执行，等所有待回答的问题都有了回答，再在原会话里一次送达全部回答（`stop_for_answer`）；停止前已写入但没有回显的补充和打断改向、只写入一部分的回答，以及等待期间新发的补充，也按发送顺序放进恢复后的第一条消息，已回显的不重发。停止执行时，被挂起的后台进程在挂起状态下直接结束，不会先恢复运行。Codex 打断（`interrupt`）时，CLI 会撤回正在等待的问题，它的状态变为 `cancelled`；执行已为等回答而停止时，插件代 CLI 撤回打断之前的待答问题，再带着打断（有 `text` 时连同改向内容）恢复会话，没有 `text` 时 Claude 停下当前这一步并结束这一轮。打断不算回答，也不取消整个运行；同时到达的 `claude_cancel` 或截止时间优先，之后不会再恢复执行。不严重的说明或异议，Claude 写成 `note`/`dispute`，插件立刻登记，Claude 不等待。

`claude_reply(run_id, command_id, kind, text?, interaction_id?, decision?, answers?, origin, expected_revision?, context)`：

| kind | 用途 | 说明 |
| --- | --- | --- |
| `answer` | 回答 `pending` 里的问题或授权请求 | 必须带 `interaction_id`；授权请求还要 `decision=allow|deny`。一次问了多个问题时用 `answers` 按问题给答案，或用 `text` 统一回答；每个问题都要有答案。同一问题只接受一个回答，后到的被拒绝 |
| `update` | 运行中补充信息，不打断 | Claude 做完当前这一步后读到，带【Codex 补充】标记 |
| `interrupt` | 打断当前这一步 | 可带 `text` 作为紧接着的改向指令。打断请求不证明进程已停；要结束整个运行用 `claude_cancel` |

- `command_id` 由你生成且每条唯一。同一 `command_id` 同样内容重发，返回第一次的回执（run 已结束也可以取回）；内容不同会被拒绝。网络或连接不确定时，用同一 `command_id` 重发即可。
- 回执是持久的：命令、对应问题的状态和修订号在同一个事务里落盘。`delivery.state` 依次为 `queued`（已记录）→ `written`（整条已写入 CLI 的输入管道）→ `echoed`（CLI 回显或把回答作为工具结果交给了会话）；`failed` 表示这次送达没有完成：run 在送达前结束，或 CLI 的输入在这一条只写入一部分时关闭（备注里记下写出的字节数）。只写入一部分的回答、补充或打断，如果执行随后为等回答停止并恢复，会放进恢复后的第一条消息再送一次，状态再前进到 `written`/`echoed`，原来的失败备注保留。`echoed` 只说明送达，不说明 Claude 已理解或照做。
- `origin=coordinator` 是你自己的话；`origin=human` 只用于逐字转交的用户原话。Claude 看到的回答会标明来源。
- `expected_revision` 填你读到的 `revision`。两个回答同时到达时只有一个生效，另一个收到“已回答”或“修订号过期”的错误。
- 能在授权内判断的直接回答；只有核心需求、验收冲突、超出授权或必须用户拍板的事才转给用户。等用户期间 Claude 一直暂停，到截止时间仍未回答就按超时结束，未回答的问题在结果里列为 `questions_unanswered`。
- 插件按 CLI 自己报告的会话状态关闭输入：CLI 报告 `idle`、没有排队或未送达的消息、没有待回答的问题时，关闭输入，run 随之结束。CLI 在后台代理或 Workflow 结束、它们引出的后续回复也结束之后才报告 `idle`，所以这些结果都会等到。后台命令不让 run 一直开着：Claude 收尾时仍在跑的后台命令，插件先用 `stop_task` 停掉再关闭输入，并给出 `background_commands_stopped` 警告。之后再要补充，用 `continue_from` 开下一轮。

## 6. 读结果

`claude_status(run_id)` 的 `outcome` 是插件记录的事实：

- `run_outcome`：ok / timeout / cancelled / crashed / not_started / lost，只描述进程怎么结束。
  CLI 已报告结果但随后以非零退出码结束，也记为 `crashed`；结果保留作证据。
- `claimed_status`：Claude 在 result.json 里的自评（completed / partial / blocked），只是它的说法。
- `warnings`：事实性警告，不改变 run_outcome。重点处理：
  - `questions_for_user`：原样转告用户，由用户决定。
  - `disputes_present`：Claude 认为约束、原件或你的说明与原话冲突，逐条核对。
  - `protected_touched`：patch 改了 protected 路径，裁决时必须在 `protected_confirmed` 里逐条确认。
  - `original_changed_during_run`：Claude 工作期间原仓库被改过，应用 patch 前先 `--check`。
  - `carried_already_in_original`：上一轮尚未记录为已合入的改动其实已经原样在原仓库里；以后合入时记得写 `applied=true`。
  - `undelivered_files_now_ignored`：重新放上的待交付文件被原仓库新的忽略规则匹配；它们仍在 delivery.patch 里，确认是否还应交付。
  - `questions_unanswered`：run 结束时 Claude 的阻塞问题仍没有回答。
  - `background_commands_stopped`：Claude 收尾时还有后台命令在跑，插件停掉了它们；核对它们的结果是否被需要。
  - `turn_interrupted`：之前某一轮回复以错误或打断结束，最终结果取最后一轮。
  - `source_not_read`、`tool_denied`、`workflow_incomplete`、`report_missing`、`result_malformed` 等：核验时据此判断报告的可信度。
- `interactions`：问答和说明的计数、未回答的问题、暂停记录；逐条内容在 `claude_status` 的 `interactions` 里。
- `changes`：两份 patch。
  - `patch`（changes.patch）：只含本轮改动，用来审；`files` 是本轮文件清单（in_scope / outside_hint / protected）和行数。
  - `delivery_patch`（delivery.patch）：所有还没应用到原仓库的改动，包括此前没有合入的轮次，用来合入；`delivery_files` 是它的文件清单。`check_hint`、`apply_hint` 针对的就是这一份；没有待交付的改动时这两个字段为空，不需要合入。

用 `claude_result(run_id, artifact)` 分页读原文（跟着 `next_offset` 读到 `end_of_artifact`，核对 `sha256`）：`report`（第一节是需求对照）、`result`、`patch`、`brief`（Claude 实际收到的任务书）、`final_message`、`inputs`、`instruction_layer`（本次会话加载的用户/项目 CLAUDE.md、rules 和 hooks）、`stream`、`stderr` 等。

## 7. 核验与裁决

- 对照用户原话和 `done_when`，逐条核对报告里的“需求对照”。自己运行必要的测试；区分 Claude 自报、你实际运行的结果和推断。
- 合入改动：执行最新一轮的 `check_hint`（`git -C <repo> apply --check delivery.patch`），通过后执行 `apply_hint`。delivery.patch 以原仓库工作区内容（含未提交改动）为基线，直接作用于工作区，不要加 `--3way` 或 `--index`。检查不通过多半是原仓库在运行期间又改过（见 `original_changed_during_run`），先手工合并冲突，不要用副本里的整文件覆盖新内容。合入之后你可以继续在原仓库里修正。
- `claude_decide(run_id, verdict, next, note, evidence, item_decisions?, protected_confirmed?, applied?)`：
  - `verdict`：accepted / accepted_with_corrections（你修正了部分内容后采纳）/ rejected。
  - `next`：done / next_round（再派一轮）/ codex_finishes（你自己完成剩余部分）。
  - result.json 有 `items` 时，`item_decisions` 要逐条给出 accepted / downgraded / rejected，后两者写原因。
  - `applied=true`：你已把这一轮的 delivery.patch 应用到原仓库（之后再修正也算）。只能用于最新一个记录了副本改动的轮次（包括保留此前未合入改动的 `not_started` 轮次）。没合入就不要写；如果合入后忘了写，可以重新裁决补上。再次裁决时不写 `applied` 会沿用之前记录的值，只有明确写 `false` 才撤销。
  - 任何结束状态（包括 timeout、crashed、lost）只要进程已停止都可以裁决；再次裁决会追加历史。
- 交付给用户时说清：完成了什么、你实际验证了什么、哪些只是 Claude 自报、还有什么没做、需要用户决定的问题。

## 8. 续接与纠正

- 同一任务的下一轮用 `continue_from=<最新一轮的 run_id>`（只能接最新一轮，不能从更早的轮次分叉）：kind、profile、task_id 不变；`user_messages` 必须原样包含上一轮的全部原话，只能在后面追加新的原话。
- copy 档的代码基线：每一轮都按原仓库的当前状态重新建立副本，再放上最近一份尚未记录为已合入的 delivery.patch（`outcome.carried` 说明来自哪一轮）；这个任务任何一次交付涉及过、现在仍存在于原仓库的文件，即使被忽略规则匹配也会带入（还没合入的删除，也会先带入原文件再由 patch 删除）。副本是一次性的，上一轮中断或副本残缺都不影响下一轮。被忽略的依赖和构建产物（如 `.venv`、`node_modules`）不会跨轮保留，需要时 Claude 会重新安装。
- 已经合入、但没有用 `applied=true` 记录的改动，如果原样还在原仓库里，会被自动识别（`carried_already_in_original`）；如果你合入后又修正过，必须先记录 `applied=true`，否则这一轮以 not_started 结束并说明冲突文件，补记后再续接即可。
- 插件会尽量用 `--resume` 接上原会话；CLI 运行后拒绝了这个会话时，自动开新会话并给出 `resume_fallback` 警告，任务书仍是完整的。CLI 根本启动不了（例如找不到可执行文件）时不换新会话重试，这一轮以 not_started 结束，`note` 里是原始错误。
- 某一轮以 not_started 结束（例如 CLI 初始化时没有 AskUserQuestion 或不是 auto 模式）时，它的副本改动照常记录；修好 CLI 后从这一轮续接即可，尚未合入的改动和更早一轮的会话都会接上，已完成的工作不重做。 如果此时才合入更早一轮的 patch，先核对它与最新轮记录的改动一致，再给最新轮记录 `applied=true`；不要回到旧轮补记，以免下一轮重复携带已合入的改动。
- 方向变了或需求结构变了，就不用 `continue_from`，开一个新的 task_id。
- 此前各轮的裁决（结论、下一步、说明、逐项结论和理由，以及 decision.json 路径）都会写进下一轮任务书，并注明效力高于原报告，不需要再手抄。

## 9. 清理

任务的最后一轮裁决之后，`claude_cleanup(run_id)` 删除该任务的副本（patch、报告、任务书和裁决都保留）。删除后不能再续接。清理一旦开始，副本就不再可续接；如果清理被中断（进程退出或删除出错），再调用一次 `claude_cleanup` 会接管并完成删除，原清理进程仍在运行时会被拒绝。

## 10. 工作台

每个 Codex 对话（`context`）只有一个工作台页面，任务和轮次在页面里切换。`claude_start` 第一次为这个对话请求打开时，响应的 `workbench.open.action` 是 `open_in_codex`，插件先把请求记为已发出，再由你调用一次 `open_in_codex`；之后的响应都是 `link_only`，链接变了、MCP 重连、压缩或新一轮都不会再自动打开。宿主报告的结果可以用 `claude_workbench(context, outcome=queued|failed|unknown)` 记下，但任何结果都不会触发自动重试。用户要求再打开时用 `claude_workbench(context, reopen=true)`。没有 `context.chat` 时永远只给链接。工作台端口随机、令牌每个 MCP 进程新生成：MCP 重启后旧页面连不上，会一直重试并提示用户回 Codex 取新链接，它不能自己换链接。用户问起时给出最新响应里的 `workbench.url`；用户要求打开时再用 `reopen=true`。插件保证的是“自动请求最多一次”，宿主是否真的只开了一个标签页它无法确认。

## 11. 监督子代理

只有在并行或上下文隔离确有收益时，才按 [supervisor.md](supervisor.md) 派一个原生 Codex 监督子代理。主代理保留裁决和对用户的交付。
