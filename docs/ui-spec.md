# 工作台规格（1.0）

本文描述 `plugins/codex-claude-orchestrator/assets/dashboard.html` 的当前行为，与源码冲突时以源码为准。0.7 及更早的设计（gate、reconciliation、协调内容绑定等）已随数据模型一起删除，不再适用。

## 定位

- 只读页面，名称是「Claude 工作台」。插件只报告事实，Codex 是唯一裁决者；页面没有停止、重派、验收按钮，服务端对 POST 返回 405。
- 运行情况、Claude 自评、Codex 验收分三块显示，提醒单独列出，互不推断。顶部的一句话状态只是三块的摘要：有 Codex 裁决时显示裁决，没有时显示运行状态加“待验收”。没有 Codex 裁决时，详情区不出现“通过”字样。
- 文案按“一眼看懂、日常说法”写：名称 2–4 字，说明不超过 20 字。后端字段名、warning code、英文 detail 只出现在“技术细节”折叠区里。

## 打开与鉴权

- `details_url` 形如 `http://127.0.0.1:<port>/#token=…&run=…`。页面从 `location.hash` 读取 token 和 run，所有 API 请求带 `Authorization: Bearer <token>`。
- 选中其他轮次时用 `history.replaceState` 更新 hash 中的 `run`；hash 中的 token 变化时重新加载页面。
- 401 或缺少 token：横幅显示“链接已过期，请回 Codex 重新打开”，连接状态为“链接过期”，停止全部轮询。
- 网络失败或 5xx：横幅显示“暂时连不上，正在重试：<原因>”，连接状态为“已断开”，继续轮询；下一次成功后横幅消失。
- CSP 为 `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:`。页面只有内联 JS/CSS，不加载外部资源，不用 eval。

## 接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/runs?limit&offset` | 任务列表，最新的在前；`next_offset`、`has_more` 用于分页 |
| `GET /api/snapshot?run_id` | 选中轮次的记录、`outcome`（运行中为 null）、`live`、`decision` |
| `GET /api/events?run_id&after&limit` | 公开活动，按 `next_cursor` 增量读取 |
| `GET /api/artifact?run_id&name&offset&limit` | 原始材料分页读取，返回 `size_bytes`、`sha256`、`next_offset`、`end_of_artifact` |

## 布局

- 宽度 ≥ 900px：左栏是任务列表（300px，随页面固定），右栏是选中轮次的详情。
- 宽度 < 900px（例如 Codex 的侧边面板）：详情占满宽度。任务列表收进顶栏左侧的“任务 N”按钮，点开后以抽屉形式盖在详情上方（最高 72vh）。点任务卡片、点遮罩或按 Esc 都会收起抽屉。没有选中任务时，空状态里有“打开任务列表”按钮。
- 详情区内，运行情况和 Claude 自评两列并排；宽度 < 520px 时改为单列。Codex 验收块占满一行：有裁决时排在最前，没有裁决时排在最后。
- 浅色和深色由 `prefers-color-scheme` 切换。

### 任务列表

- 按 `task_id` 分组；组内按 `round`、`started_at` 排序，`started_at` 最大的一轮为“最新一轮”。
- 组的排序依据是组内各轮 `started_at / updated_at / ended_at / decision.recorded_at` 的最大值，倒序。新一轮、运行结束、Codex 裁决都会让任务前移。
- 卡片内容：标题；`任务类型 · 第 N 轮 · 月/日 时:分`；一个状态标签（规则见下）。
- 状态标签：运行中为“进行中”；有裁决时为“<结论> · <下一步>”；正常结束未裁决为“待验收”（虚线）；其他结束状态未裁决为“<运行状态> · 待验收”。
- 首页读 100 条；`has_more` 时显示“加载更多”，按 `next_offset` 追加。定时刷新只重读首页（最多 200 条）。计数在全部载入时为“共 N 个”，否则为“已显示 N 个”。
- 链接未指定 run 时，自动打开排在最前的任务的最新一轮。

### 详情头部

- 轮次标签：同一 `task_id` 下已载入的各轮，文案为“第 N 轮 · 进行中 / <结论> / 待验收”；同轮有多条记录时附 run_id 末 4 位。
- 事实行（以 · 分隔）：任务类型、执行方式、模型（实际模型取 `outcome.models`，运行中取 `live.models` 或 `live.initialized_model`；实际模型不包含请求的 model 时附“（指定 <model>）”）、思考强度（effort）、用时（运行中附上限）、开始时间、“查看上一轮”（`continue_from`，点击跳转）。
- 任务号、运行号（以及上一轮的运行号）放在页面底部的“编号”折叠区，各带“复制”按钮。

### 一句话状态

| 情况 | 标题 | 副标题 |
| --- | --- | --- |
| 运行中 | Claude 正在做 | 最近一条有意义的活动 · 已用 <时长> |
| 有裁决 | Codex 验收：<结论> | 下一步：<下一步> |
| 已结束、未裁决 | Claude 已交回，等 Codex 验收（非正常结束时用运行状态代替“Claude 已交回”） | Claude 自评：<自评>，或“Claude 没给自评” |

### Claude 有问题问你

`questions_for_user` 提醒不进提醒列表，而是放在一句话状态下方的蓝色框里：标题“Claude 有问题问你”，说明“需要你来定，Codex 不代答，在 Codex 对话里回答即可”，下面逐字列出 `items`。

### 三块

| 块 | 内容 |
| --- | --- |
| 运行情况 | 运行状态；运行中显示“正在做”（最近一条有意义的活动）；进程是否停止、用时、费用（“约 $X（Claude 估算）”，运行中为“结束后统计”）、备注（`note`） |
| Claude 自评 | `claimed_status` 与 `summary`，并注明“Claude 自己说的，不算验收” |
| Codex 验收 | 无 `decision` 时显示“待验收”，运行中注明“做完后 Codex 会来验收”，结束后注明“Codex 还没验收，可在对话里提醒它”；有裁决时显示结论、下一步、备注、验收依据、逐条意见、放行文件、验收时间，有多次裁决时注明“验收过 N 次，这是最新的” |

配色约定：绿色只用于 `verdict=accepted`；改后通过用橙色，退回用红色，未裁决用虚线边框。运行结果 `ok` 用中性色，`timeout / crashed / lost` 用红色，`cancelled / not_started` 用橙色，运行中用蓝色。

### 文案

| 字段 | 取值 → 文案 |
| --- | --- |
| status / run_outcome | running 进行中 · ok 正常结束 · timeout 超时 · cancelled 已取消 · crashed 意外中断 · not_started 未启动 · lost 失联 |
| verdict | accepted 通过 · accepted_with_corrections 改后通过 · rejected 退回 |
| next | done 已完成 · next_round 再做一轮 · codex_finishes Codex 收尾 |
| claimed_status | completed 做完了 · partial 没做完 · blocked 卡住了 |
| process_stopped | confirmed 已确认停止 · stopped 已停止 · not_launched 进程未启动 · unknown 未能确认停止 |
| kind | implement 动手改 · analyze 只分析 |
| profile | copy 副本模式 · readonly 只读模式 |
| 改动范围 class | in_scope 预期内 · outside_hint 超出预期 · protected 受保护 |
| item disposition | accepted 采纳 · downgraded 降级 · rejected 不采纳 |
| 原话来源 source | human 你说的 · relayed 别人转述 |

未知取值一律显示原始值，不猜测含义。

### 提醒

- 已结束时读 `outcome.warnings`；`outcome` 为空时退回记录里的 code 列表。
- 每条显示中文标题、一句中文说明、`paths`（等宽）和 `items`（逐字，保留换行）。原始 code 和英文 `detail` 收进“技术细节”折叠区。
- 标题｜说明：original_changed_during_run 期间你的项目有改动｜合入前确认改动还能用上 · protected_touched 动了受保护的文件｜需要 Codex 逐个放行 · outside_hint 改了预期外的文件｜确认这些改动是否需要 · tool_denied 有操作被拦下｜Claude 的部分操作没获准 · source_not_read 有附件没读｜Claude 可能漏看了这些文件 · workflow_incomplete 子流程没跑完｜部分 Workflow 未完成或失败 · report_missing 没交报告｜可以看“最后回复” · result_missing 没交结果数据｜所以没有 Claude 自评 · result_malformed 结果数据格式不对｜自评可能不完整 · residual_processes_stopped 已停掉遗留进程｜Claude 退出后还有进程在跑 · process_stop_unconfirmed 不确定进程已停｜请检查是否还有进程在跑 · sensitive_inputs_skipped 部分附件没给 Claude｜敏感、链接或过大的文件被跳过 · resume_fallback 没能接上上一轮｜这一轮换了新会话 · questions_for_user Claude 有问题问你｜需要你来定，Codex 不代答 · disputes_present Claude 有不同意见｜看看它反对的是哪条 · not_verified_present 有内容没验证｜Claude 自己说这些没核实 · provider_error 模型服务出错｜Claude 那边返回了错误 · patch_unavailable 没能整理出改动｜改动清单暂不可用 · carried_already_in_original 之前的改动已合入｜上一轮的改动已在项目里 · finalize_error 结果没汇总全｜请看下方详细资料 · undelivered_files_now_ignored 有改动被忽略规则挡住｜这些文件现在被 Git 忽略。
- 未知 code 的标题为“其他提醒”，说明为原始 code。

### 改动

- 显示 `outcome.changes` 的文件数与增删行数，以及文件清单（路径、变化类型与行数、范围标签：受保护为红色，超出预期为橙色）。
- 有 `apply_hint` 时显示“手动合入”折叠区，里面是命令（有 `check_hint` 时先 check 再 apply）和“复制命令”按钮（`navigator.clipboard`，失败时提示手动选中）。有 `delivery_patch` 时说明“含之前没合入的改动，共 N 个文件”。是否应用由用户或 Codex 决定。
- 只读模式显示“只读模式，没有改动”。

### 详细资料

| 标签 | artifact | 展示 |
| --- | --- | --- |
| 报告 | report | 纯文本，不渲染 Markdown |
| 本轮改动 | patch | 按行着色（超过 6000 行不着色）；`outcome.changes` 为空时不显示此标签 |
| 过程记录 | （events 接口） | 时间、类型、中文摘要；最多保留 5000 条，显示最近 500 条 |
| 任务书 | brief | 纯文本，注明“这是 Claude 实际收到的原文”；sha256 与 `outcome.brief.sha256` 对比 |
| 你的原话 | packet | 逐条逐字显示 `user_messages`（来源、时间），没有时显示 `no_user_words_reason` 原文 |
| 附件 | inputs | 路径、类型、大小、校验码，以及没交给 Claude 的路径 |
| Claude 配置 | instruction_layer | 规则文件、Hooks，以及 `outcome.instruction_layer.plugins` |
| 结果数据 | result | 格式化 JSON |
| 最后回复 | final_message | 纯文本 |
| 累计改动 | delivery_patch | 同本轮改动；只在 `delivery_patch` 存在时显示 |

- 前三个标签常驻；其余收在“更多”里，点开后另起一行显示，按钮变为“收起”。当前打开的是“更多”里的标签时保持展开；此时点“收起”会切回报告。
- 每页 200000 字节。未读完时显示“加载更多（已载入 / 总大小）”，按 `next_offset` 续读，各页按顺序拼接。JSON 材料跨页时，读到最后一页后再解析。
- 元信息显示大小（未读完时为“已加载 X / 共 Y”）；报告和任务书与运行记录里的 sha256 比对，显示“校验一致”或“校验不一致，内容和记录对不上”；完整 sha256 在“校验码”折叠区里。
- `available=false` 时显示“这一轮没有这份资料”（运行中显示“还在进行，这份还没生成”）。
- 默认标签：运行中为“过程记录”，其余为“报告”；用户选过的标签在切换轮次后保留。

### 过程记录

- 后端为主会话的每次工具调用记一条 `kind=tool` 活动：`tool` 为工具名，`file` 为目标路径（在工作目录内时改为相对路径），`text` 只取模型为人写的标签（Bash/Agent/Task 的 `description`、Skill 名、Workflow 名、Glob 的 pattern）。命令行、URL、搜索内容和文件内容不记录。子代理内部的工具调用不记录。
- 页面把工具名译成动作：Read 读取、Write 写入、Edit/MultiEdit/NotebookEdit 修改、Bash 运行命令、Grep 搜索、Glob 查找文件、Agent/Task 派子代理、Skill 使用技能、WebFetch 打开网页、WebSearch 联网搜索、TodoWrite 更新待办、Workflow 启动子流程；其他工具显示“使用 <工具名>”。例如“修改 src/export.py”“运行命令：Run tests”。
- 固定的英文摘要译成中文：检查本机 Claude、准备工作目录、Claude 开始工作、模型服务状态、已请求取消、运行结束：<状态>、Codex 验收：<结论> · <下一步>。其他摘要原样显示。
- 连续的 `provider` 活动合并为一行“模型服务状态 · 共 N 条”。
- `claude_wait` 的 compact 输出不含 `provider` 和 `tool` 活动；最新一步仍通过 snapshot 的 `last_meaningful_event` 传给 Codex。

## 刷新与并发

- 列表每 5 秒刷新一次，页面隐藏时改为 30 秒。
- 选中的轮次：运行中每 2 秒（隐藏时 15 秒），已结束每 10 秒（隐藏时 60 秒）。页面回到前台时立即刷新。
- 活动按 `next_cursor` 增量读取，每轮最多 10 页 × 200 条，按 `seq` 去重。
- 轮次从运行中变为结束时，重读当前打开的材料。
- 切换轮次会递增 generation，旧轮次的 snapshot / events / artifact 响应一律丢弃；列表响应按请求序号丢弃旧的，合并记录时 `updated_at` 更旧的不会覆盖更新的。
- 列表、轮次标签、一句话状态和详情块都先比较签名，没有变化就不重绘，以免打断文本选择和焦点。

## 安全

- 所有数据都经 `textContent` / `createTextNode` 写入，不用 `innerHTML`，也不拼接 HTML。
- token 只出现在 URL hash 和 `Authorization` 请求头里，页面不写 console。

## 测试

`plugins/codex-claude-orchestrator/tests/test_dashboard.py` 把页面脚本放进 Node 的 vm，配一个最小化的假 DOM 和假 fetch 来运行（没有 node 时跳过）。覆盖范围：任务分组与排序、每张卡片只有一个状态标签、窄屏抽屉的打开与收起、状态和裁决文案（改后通过 + 再做一轮；裁决前详情区不出现“通过”）、一句话状态、全部提醒的标题和说明与未知 code、“Claude 有问题问你”置顶、过程记录的翻译与合并、三个后端样例的渲染、“更多”标签的展开与收起、切换轮次后丢弃过期响应、材料分页拼接（含跨页 JSON 和用户原话逐字显示）、轮询节奏与隐藏降速、401 和断网横幅、静态约束（≤ 900 行、无 innerHTML 和外部资源）。

后端的工具步骤由 `skills/codex-claude-orchestrator/scripts/tests/test_stream_and_result.py`（不记录命令、URL 和搜索内容）和 `test_run_end_to_end.py`（真实 bridge 下工具调用变成活动）覆盖。

```bash
cd plugins/codex-claude-orchestrator
.venv/bin/python -m unittest tests.test_dashboard
```

## 已知边界

- 报告按纯文本显示，不渲染 Markdown。
- 不展示 token 级用量（`outcome.usage`），只展示 `cost_usd`。
- 列表和轮次标签只覆盖已载入的记录；深链打开的旧轮次如果不在已载入页里，轮次标签可能不全。
- 没有搜索和筛选。
- 过程记录只覆盖主会话的工具调用；Claude 的文字回复和子代理内部步骤不进入过程记录。
