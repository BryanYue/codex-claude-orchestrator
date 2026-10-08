# 工作台规格（1.0）

本文描述 `plugins/codex-claude-orchestrator/assets/dashboard.html` 的当前行为，与源码冲突时以源码为准。0.7 及更早的设计（gate、reconciliation、协调内容绑定等）已随数据模型一起删除，不再适用。

## 定位

- 只读页面。插件只报告事实，Codex 是唯一裁决者；页面没有停止、重派、验收按钮，服务端对 POST 返回 405。
- 运行事实、Claude 自称、Codex 裁决分三块显示，提醒单独列出，互不推断。没有 Codex 裁决时只显示“待核验 / 尚未核验”，页面不出现“通过”字样。

## 打开与鉴权

- `details_url` 形如 `http://127.0.0.1:<port>/#token=…&run=…`。页面从 `location.hash` 读取 token 和 run，所有 API 请求带 `Authorization: Bearer <token>`。
- 选中其他轮次时用 `history.replaceState` 更新 hash 中的 `run`；hash 中的 token 变化时重新加载页面。
- 401 或缺少 token：横幅显示“链接失效，请从 Codex 重新获取”，停止全部轮询。
- 网络失败或 5xx：横幅显示“连接中断，正在重试：<原因>”，继续轮询；下一次成功后横幅消失。
- CSP 为 `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:`。页面只有内联 JS/CSS，不加载外部资源，不用 eval。

## 接口

| 接口 | 用途 |
| --- | --- |
| `GET /api/runs?limit&offset` | 任务列表，最新的在前；`next_offset`、`has_more` 用于分页 |
| `GET /api/snapshot?run_id` | 选中轮次的记录、`outcome`（运行中为 null）、`live`、`decision` |
| `GET /api/events?run_id&after&limit` | 公开活动，按 `next_cursor` 增量读取 |
| `GET /api/artifact?run_id&name&offset&limit` | 原始材料分页读取，返回 `size_bytes`、`sha256`、`next_offset`、`end_of_artifact` |

## 布局

左栏是任务列表，右栏是选中轮次的详情。宽度 ≤ 820px 时改为单列，列表在上（最高 42vh，可滚动）。浅色和深色由 `prefers-color-scheme` 切换。

### 任务列表

- 按 `task_id` 分组；组内按 `round`、`started_at` 排序，`started_at` 最大的一轮为“最新一轮”。
- 组的排序依据是组内各轮 `started_at / updated_at / ended_at / decision.recorded_at` 的最大值，倒序。新一轮、运行结束、Codex 裁决都会让任务前移。
- 卡片内容：标题；`task_id · 类型 / 档案 · 第 N 轮`（已载入多轮时注明）；最新一轮的状态徽章；裁决徽章（`裁决 · 下一步`，无裁决时为“待核验”）。
- 首页读 100 条；`has_more` 时显示“加载更早的记录”，按 `next_offset` 追加。定时刷新只重读首页（最多 200 条），计数注明“仅已载入”。
- 链接未指定 run 时，自动打开排在最前的任务的最新一轮。

### 详情头部

- 轮次标签：同一 `task_id` 下已载入的各轮，文案为“第 N 轮 · 运行中 / 裁决 / 待核验”；同轮有多条记录时附 run_id 末 4 位。
- 事实行：标题、任务、运行（run_id）、类型 / 档案 / 轮次、模型（请求的 model 与 effort；实际模型取 `outcome.models`，运行中取 `live.models` 或 `live.initialized_model`，缺失时显示“待回报”）、用时（运行中附超时上限）、开始时间、接续自（`continue_from`，可点击跳转）。

### 三块事实

| 块 | 内容 |
| --- | --- |
| 运行事实 | `run_outcome`（运行中显示“运行中”和最近一条有意义的活动）、进程是否停止、`duration_seconds`、`cost_usd`（注明是 CLI 估算）、`note` |
| Claude 自称 | `claimed_status` 与 `summary`，并注明“这是 Claude 自己的说法，不是核验结论” |
| Codex 裁决 | 无 `decision` 时显示“尚未核验”；有裁决时显示“报告：<verdict>”、下一步、说明、核验依据、逐项处置、已确认的受保护路径、记录时间，有多次裁决时注明次数 |

配色约定：绿色只用于 `verdict=accepted`；修正后采纳用橙色，不采纳用红色，未裁决用虚线边框。运行结果 `ok` 用中性色，`timeout / crashed / lost` 用红色，`cancelled / not_started` 用橙色，运行中用蓝色。

### 文案

| 字段 | 取值 → 文案 |
| --- | --- |
| status / run_outcome | running 运行中 · ok 正常结束 · timeout 超时 · cancelled 已取消 · crashed 异常退出 · not_started 未启动 · lost 失联 |
| verdict | accepted 采纳 · accepted_with_corrections 修正后采纳 · rejected 不采纳 |
| next | done 任务完成 · next_round 需要下一轮 · codex_finishes 由 Codex 收尾 |
| claimed_status | completed 已完成 · partial 部分完成 · blocked 受阻 |
| process_stopped | confirmed 已确认停止 · stopped 已停止 · not_launched 进程未启动 · unknown 未能确认停止 |
| 改动归类 class | in_scope 范围内 · outside_hint 超出预期范围 · protected 受保护 |
| item disposition | accepted 采纳 · downgraded 降级 · rejected 不采纳 |

未知取值一律显示原始值，不猜测含义。

### 提醒

- 已结束时读 `outcome.warnings`；`outcome` 为空时退回记录里的 code 列表。
- 每条显示中文标签、原始 code、`detail`、`paths`（等宽）和 `items`（逐字，保留换行）。
- 中文标签：original_changed_during_run 运行期间原工作区有变动 · protected_touched 改动了受保护路径 · outside_hint 改动超出预期范围 · tool_denied 有工具调用被拒绝 · source_not_read 有原件未被读取 · workflow_incomplete 工作流未完成 · report_missing 缺少报告 · result_missing 缺少结果头 · result_malformed 结果头格式不合规 · residual_processes_stopped 已清理残留进程 · process_stop_unconfirmed 无法确认进程已停止 · sensitive_inputs_skipped 部分原件未复制给 Claude · resume_fallback 未能续接上一轮会话 · questions_for_user Claude 有需要用户决定的问题 · disputes_present Claude 提出异议 · not_verified_present Claude 列出未验证的内容 · provider_error 模型服务返回错误 · patch_unavailable 无法生成补丁。未知 code 直接显示 code。

### 改动

- 显示 `outcome.changes` 的文件数与增删行数，以及文件表（路径、变化类型、行数、归类徽章：受保护为红色，超出预期范围为橙色）。
- 有 `apply_hint` 时显示命令和“复制命令”按钮（`navigator.clipboard`，失败时提示手动选中）。是否应用补丁由用户或 Codex 决定。
- `delivery_patch` 存在时，原始材料里多出“待交付 Patch”，复制命令针对它（先 check 再 apply）。只读档案显示“只读运行，没有改动”。

### 原始材料

| 按钮 | artifact | 展示 |
| --- | --- | --- |
| 报告 | report | 纯文本，不渲染 Markdown |
| Claude 收到的任务书 | brief | 纯文本；sha256 与 `outcome.brief.sha256` 对比 |
| 结果头 | result | 格式化 JSON |
| Patch / 待交付 Patch | patch / delivery_patch | 按行着色（超过 6000 行不着色） |
| 用户原话 | packet | 逐条逐字显示 `user_messages`（来源、时间），没有时显示 `no_user_words_reason` 原文 |
| 原件 | inputs | 原路径、类型、大小、sha256，以及未复制的路径 |
| 指令层 | instruction_layer | 指令文件、hooks，以及 `outcome.instruction_layer.plugins` |
| 最终回复 | final_message | 纯文本 |
| 活动 | （events 接口） | 时间、kind、summary、status/tool/file、text；最多保留 5000 条，显示最近 500 条 |

- 每页 200000 字节。未读完时显示“加载更多（已载入 / 总大小）”，按 `next_offset` 续读，各页按顺序拼接。JSON 材料跨页时，读到最后一页后再解析。
- 元信息显示大小、已载入字节数和完整 sha256；报告和任务书还会与运行记录里的 sha256 比对，结果显示“与运行记录一致”或“不一致”。
- `available=false` 时显示“本轮没有这份材料”（运行中显示“还没有生成”）。
- 默认标签：运行中为“活动”，其余为“报告”；用户选过的标签在切换轮次后保留。

## 刷新与并发

- 列表每 5 秒刷新一次，页面隐藏时改为 30 秒。
- 选中的轮次：运行中每 2 秒（隐藏时 15 秒），已结束每 10 秒（隐藏时 60 秒）。页面回到前台时立即刷新。
- 活动按 `next_cursor` 增量读取，每轮最多 10 页 × 200 条，按 `seq` 去重。
- 轮次从运行中变为结束时，重读当前打开的材料。
- 切换轮次会递增 generation，旧轮次的 snapshot / events / artifact 响应一律丢弃；列表响应按请求序号丢弃旧的，合并记录时 `updated_at` 更旧的不会覆盖更新的。
- 列表、轮次标签和详情块都先比较签名，没有变化就不重绘，以免打断文本选择和焦点。

## 安全

- 所有数据都经 `textContent` / `createTextNode` 写入，不用 `innerHTML`，也不拼接 HTML。
- token 只出现在 URL hash 和 `Authorization` 请求头里，页面不写 console。

## 测试

`plugins/codex-claude-orchestrator/tests/test_dashboard.py` 把页面脚本放进 Node 的 vm，配一个最小化的假 DOM 和假 fetch 来运行（没有 node 时跳过）。覆盖范围：任务分组与排序、状态和裁决文案（修正后采纳 + 需要下一轮；裁决前不出现“通过”）、全部提醒标签和未知 code、三个后端样例的渲染、切换轮次后丢弃过期响应、材料分页拼接（含跨页 JSON 和用户原话逐字显示）、轮询节奏与隐藏降速、401 和断网横幅、静态约束（≤ 900 行、无 innerHTML 和外部资源）。

```bash
cd plugins/codex-claude-orchestrator
.venv/bin/python -m unittest tests.test_dashboard
```

## 已知边界

- 报告按纯文本显示，不渲染 Markdown。
- 不展示 token 级用量（`outcome.usage`），只展示 `cost_usd`。
- 列表和轮次标签只覆盖已载入的记录；深链打开的旧轮次如果不在已载入页里，轮次标签可能不全。
- 没有搜索和筛选。
