# 工作台当前实现与历史设计参考

## 当前状态：v0.6.2

本节按 v0.6.2 的 `assets/dashboard.html` 与 `scripts/viewer.py` 核对。后面的 0.5.0 实现修订和 0.4.7 基线 v2 设计保留其历史用途，不能据此声明所有提案已经实现。冲突时以本节和当前源码为准；实际验收覆盖见 [RELEASE-VERIFICATION.md](../RELEASE-VERIFICATION.md)。

| 项目 | 当前行为与边界 |
| --- | --- |
| 页面与接口 | 单页只读工作台；保留 GET 的 runs/snapshot/events/artifact/cli-maintenance，POST 仍为 405，没有停止、重派或验收按钮 |
| 本机 CLI 状态 | CLI 版本管理已经退役。`/api/cli-maintenance` 保留为只读本机 CLI/历史记录状态接口；抽屉展示本轮 CLI 与本机路径/来源，不提供下载或切换 CLI |
| 本轮身份 | 记录本轮 CLI、请求模型/effort、实际 provider 模型、环境与执行证据；实际模型缺失时显示等待回报。旧设计的 `cli_selection_reason`、全局版本差异标签不作为已实现字段 |
| 执行、报告、核验 | 分开显示三项事实；失败/unknown 的已有报告仍是未验收证据。报告内容使用中性色，绿色用于当前核验通过结论 |
| B1/B2/B3 | `gate` 仍全部为 false；截断完整性、待收集、报告契约的拟新增字段不能描述成当前后端能力，模拟 gate 测试不等于已启用 |
| 需求默认排序 | 跨状态按最新一轮执行开始时间倒序；轮询、结束、核验更新不移动需求，新的执行轮次才可能前移 |
| Workflow 报告 | 完整报告与父级摘要分开保存并分页读取，校验字节数和哈希；这不启用 B1/B2/B3，也不替代 Codex 语义验收 |
| 公开活动 | 允许事件是权限检查，CLI/Workflow 完成消息也不能替代最终报告与 Codex 验收；不展示私有推理 |
| 打开与重连 | 由主代理按既有记账最多自动请求一次；queued 只表示排队。跨 MCP 的统一入口和宿主标签页复用未交付 |

下面提及的设计任务文件和原型是历史来源，不是本仓库附带的可执行输入。历史验收清单表示当时的要求，不自动构成本次版本的实测记录。

## 0.5.0 实现修订（历史）

以下修订在当时优先于 v2 参考伪代码。Stage A 保留只读 Viewer 和执行契约；未实现的后端字段保持关闭。

## 4. A 阶段的具体实现规则

### 4.1 数据与当前轮（关闭 V2-A / R1 / R2）

1. 建立按 run_id 的摘要/详情分层缓存。摘要更新不删除 detail；涉及 status/decision/superseded_by 的变化使旧详情失效并安排补读。只在详情中存在的字段未加载时不作“空值”解释。
2. 统一记录摘要和 snapshot 对相应事实的观察时间；snapshot 补读成功也推进该 run 的新鲜度。旧 connGen/epoch/请求序号的响应不得覆盖新数据。
3. 按 task_id + cwd 分组。最高已知 revision 只是查询候选，不能直接等同当前轮；先检查更高轮证据、后续指向及缓存矛盾。指向未知 run、多个 tips 冲突或候选过时时，展示“当前轮待确认/待加载”，隐藏下一步。
4. 补取必须验证 task key、revision 方向，设置 visited 去重，最多 5 跳；当前候选的可信新快照无 superseded_by、且不与更高轮证据冲突后，才能标记当前。失败或达到跳数上限保持待确认，不循环补取。
5. 深链始终尊重指定 run：旧 run 仍显示历史详情。刷新/发现新轮不抢走正在查看的报告或焦点。
6. 首页可见时有未收口任务每 5 秒、否则每 15 秒刷新；隐藏时每 60 秒。选中 run 保留现有 pollingDelay、事件尾部窗口和积压追赶行为。
7. 背景请求保留每 5 秒最多 2 个、观察集最多 40 条的上限。采用公平轮转：有观察任务时至少保留一个槽给最久未刷新的候选，另一个处理补取/预读；空闲槽可借用。补取失败退避，不让坏指向挤占全部刷新预算。
8. **保留 60 秒新鲜度并明确降级，不承诺 40 条都实时。** 超时或超出观察上限的卡片显示最后同步时间/过时说明，不能提供基于“已确认当前轮”的下一步。观察对象包含需处理的 returned/revision_requested、failed、blocked、timeout 等候选，不只包含无 decision 的 reported。历史轮不全量预读。
9. 分页按 run_id 去重；新首页插入造成 offset 重叠时有界继续取页，连续无新行最多追加 3 页，累计 500 行封顶。计数/搜索明确仅已载入，不保证从未请求过的历史已全部覆盖。

### 4.2 执行、报告、核验三项事实（关闭 V2-C / V2-D）

| 真实输入 | 显示与行为 |
| --- | --- |
| 摘要缺 result/receipt | 详情未载入；status=reported 可粗略显示已回报，不推断完整性 |
| reported，无 decision | 报告已交回、待 Codex 核验；中性色 |
| reported，已有 accepted/returned/Codex 补齐 | 报告事实不再写待核验；核验区保留对应结论 |
| failed/timeout/blocked/cancelled + structured | 有内容、未正常交回；只作过程/失败证据，不提供对本轮 accepted/returned 的建议 |
| 无 result 文件或 structured 为空 | 未取得正式结构化报告；保留允许读取的公开活动、回执和工作区证据，不推断没有任何输出 |
| 历史 accepted/returned/Codex 补齐 | 分别保留历史结论与补齐摘要；历史视觉，不关闭新轮 |
| claude_started=false | 可结合具体阶段说未启动或启动前取消 |
| claude_started=null/字段缺失 | 未确认；不得通过 `!== true` 推成未启动 |
| unknown + 任意回执文件 | 仍按可信进程/核对事实显示；回执存在不证明退出 |
| 当前轮真实 accepted/Codex 补齐 | 才使用核验通过绿色；执行结束/报告完整保持中性 |

B1/B2/B3 第一阶段不产生新字段，gate 关闭。模拟 gate 测试仍覆盖 unknown 回执、未退出但 pending、不同契约失败类别，避免以后打开开关时回归。通过当前三项事实如实显示已有 Workflow 缺口，不修改后端 reported 的准入规则。

### 4.3 连接与恢复（关闭 V2-B）

- 关键请求保留：首页 runs，及有效选中 run 的 snapshot/events。网络错误、超时或关键 5xx 都使对应关键数据进入过时/重试状态；服务返回错误时文案使用“同步失败”，不无条件声称网络已断。
- maintenance、单项 artifact、观察/预读/补取失败只影响对应区域；400 无效 run 清除有效详情选择条件，显示记录不可读，列表仍可用。
- token 改变时递增 connGen，中止旧代请求；切轮更新 epoch。AbortError 与过期响应不改变连接状态。
- 进入故障时重置恢复成功标记。没有有效选择时，故障之后的当前 connGen 首页成功即可恢复；有有效选择时，首页、该轮 snapshot 和 events 都成功后恢复，记录各自同步时间。
- 关键请求故障立即使已选详情的当前性降级，即使最后快照距今不足 60 秒。保留报告阅读和历史证据复制，但隐藏或禁用依赖“已确认当前轮”的下一步及“复制给 Codex”行动指令；动作在点击时也重新检查当前性。关键读取按上述恢复条件成功后才重新启用。局部 artifact/维护失败不触发全局行动禁用。
- 401 保留数据并停止自动重试，等待新链接。关键请求失败使用 15/30/60 秒退避。恢复只说已恢复读取，不声称活动连续无遗漏。
- 不因连接恢复创建 run；文字保持不透明，断线/过时采用横幅、虚线和停止动画。

### 4.4 页面与宿主交互

- 按 v2 实现宽布局/窄面板、项目筛选、三组任务、历史轮切换、报告证据、环境抽屉、复制给 Codex、键盘/焦点与明暗主题。
- 单文件 HTML 内按缓存/状态推导/调度/渲染分区组织代码，保留可单独验证的纯状态函数；不引入编译前端或更改 CSP。
- 新增任务提示默认“执行已创建”；真实详情 claude_started=true 时才称已开始。加载旧页、补取和深链不弹新增提示。
- 单次自动打开由主代理持有 requested 状态：调用时即记为已请求，queued 也计入；明确失败不给自动重试；后续 start 只给深链。监督席不操作主窗口。用户显式要求重开时取同 run 新链接并打开，绝不重派。
- 监督席回传 run_id 后，主代理先通过**自己的 MCP 连接**调用 `claude_details(run_id)` 取得本方 Viewer 链接，再负责唯一自动打开；不直接依赖监督席可能短命的 Viewer URL。主代理取链接失败时保留 run_id 和可用说明，不通过再 start 或反复开页补偿。验收记录 Viewer 是否共享，并覆盖监督席结束后主代理入口仍能读取同一 run。
- 同步检查 Skill 中其他“立即打开”措辞，尤其原生监督席段，不只修改第 88 行；server presentation、claude_details、supervisor reference 和使用文档必须一致。
- 承诺范围为同一 Codex 任务/存活 Viewer 的一次自动请求。上下文恢复时保留该事实；无法确认已请求与否时优先给可点击链接，不用多开页补偿。跨 MCP 的统一入口仍不属于 A。


---

## Design v2 reference

# Codex–Claude 协作工作台 · UI 与交互规格（供实现）· v2

- 日期：2026-09-28 · 版本：**v2**（按 `CODEX-REVIEW.md` 的 R1–R8 修订；v1 原文保留为 `UI-SPEC.v1.md`）
- 基线：codex-claude-orchestrator `0.4.7+codex.20260923044503`，路径均相对插件根 `plugins/codex-claude-orchestrator/`
- 设计来源：`ui-redesign-20260928/DESIGN-BRIEF.md`；可交互原型见设计画布（02 宽布局、03 窄面板、04 状态映射、05 交接）
- 性质：设计规格，**不是**已批准的实现或验收。原型数据全部为模拟，不证明后端已修复。深色值是配色方案，尚未做渲染验收。

## v2 修订摘要

| 审查项 | 修订 | 位置 |
|---|---|---|
| R1 列表缺字段 ≠ 无报告 | 区分“列表摘要”和“详情数据”两层；字段未载入显示“详情未载入”；`status=reported` 只给粗粒度“已回报 · 待核验” | §2.1、§5.0、§5.2 |
| R2 分页外过期 / 历史轮冒充当前轮 | 当前轮三态（已知 / 未载入 / 已确认历史）；按 `superseded_by` 有界补取；已载入未收口任务的轮转刷新；分页去重 | §2.2–2.4 |
| R3 完整 / 正式交回 / 通过混淆 | 绿色只表示核验通过；执行结束、报告完整用中性“已定”色；失败但有结构化内容单独显示；B1/B2/B3 条件与降级统一 | §0.8、§4.5、§5 |
| R4 历史轮丢失 Codex 补齐事实 | 新函数把“该轮曾由 Codex 补齐”与“能否作为当前收口”分开；历史轮用灰色历史色 | §5.3 |
| R5 单次打开对 queued 不明确 | 按“是否已发出自动打开请求”记账，queued 计入；只有主代理处理窗口 | §1.2 |
| R6 局部失败升级成断线 | 关键同步请求与局部请求分开；AbortError 不算失败；连接代次防串；删除“记录与断线前连续”措辞 | §6.3 |
| R7 创建 ≠ 已启动 | 提示改为“新增任务 · 执行已创建”；按 `claude_started` 与回执区分未启动、启动前取消、预检阻止；补页记录不弹“新任务” | §2.5、§5.1、§6.1 |
| R8 整卡 78% 透明度对比不足 | 删除整卡透明度，文字保持不透明；用横幅、虚线边框、文案和停止动画表达过时 | §4.2、§6.3、§7 |
| 测试 | 保留现有行为测试覆盖（切轮取消、过期响应丢弃、事件分页、维护失败隔离等），不只改 ID 和文案 | §10 |
| title 字段 | 暂缓；第一阶段用 objective 首句 | §1.3 |

---

## 0. 给实现者的硬约束

1. **页面只读。** 只调用现有 GET 接口：`/api/runs`、`/api/snapshot`、`/api/events`、`/api/artifact`、`/api/cli-maintenance`。不新增写入口；`do_POST` 维持 405。页面上**不出现**停止、重试、重派、通过、验收之类的按钮。
2. **单文件、自包含。** 继续在 `assets/dashboard.html` 内联 CSS/JS，遵守 `scripts/viewer.py` 现有的 Host/Origin/token/CSP。不引入 CDN、外部字体、遥测。字体用系统栈。
3. **不推断新终态。** 标 **[拟新增]** 的分支，在后端提供对应字段前**一律关闭**（字段缺失 → 该分支不生效，回落到 §5 中基于现有 status/result/decision 的结果；不是一律回落成“已回报”）。
4. **持久事实不改名。** `reported`、`accepted`、`returned`、`resolution=completed_by_codex` 等存储值保持原样，只改面向用户的文案。
5. **单页不等于单执行。** UI 聚合展示，但每个 `run_id` 的输入、进程、会话、事件、报告、核验保持独立；不合并执行身份。
6. **不改权限守卫。** 越界读取被拒是正常行为，不得通过开放临时目录来“修复”。
7. 只展示公开活动事件，不展示私有推理，不制造完成百分比。
8. **颜色语义（v2）。** 绿色（`ok`）**只**用于当前轮的核验通过与 Codex 已补齐完成。“执行已结束”“已回报 · 完整”“已回报 · 待核验”用中性的 `settled` 色；历史轮的核验结论用灰色 `history` 色。
9. **缺字段 ≠ 空值。** 某字段只在详情（snapshot）里才有时，列表摘要中它缺失只能表示“未载入”，不能推导为“没有”。

---

## 1. 实现分期与改动清单

### 1.1 阶段 1a · UI（只改 `assets/dashboard.html`）

| # | 内容 | 依据 |
|---|---|---|
| 1 | 布局改为工作台：顶栏 + 左侧任务列表 + 右侧任务详情；去掉“完整模式/简洁模式” | §3、§4 |
| 2 | 两层数据缓存（列表摘要 / 详情），按任务归并，当前轮三态 | §2 |
| 3 | 已载入未收口任务的轮转刷新；分页去重 | §2.3–2.4 |
| 4 | 三项事实：执行 / 报告 / 核验，按 §5 推导 | §5 |
| 5 | 列表三组 + 原因行 + 三个状态标志 | §5.4 |
| 6 | 下一步卡片 + “复制给 Codex”（沿用现有 `action()` 思路与剪贴板逻辑） | §4.4、§8.2 |
| 7 | 报告区按类型渲染（含失败但有内容、无结构化报告）；证据、过程、文件变化、本轮执行身份、技术记录放进可展开区 | §4.5–4.6 |
| 8 | 环境与维护抽屉；全局 CLI 信息不覆盖任务区 | §4.7 |
| 9 | 关键 / 局部请求分开的错误处理，断线与恢复横幅 | §6.3 |
| 10 | 新增任务的非打断提示 | §2.5、§6.1 |
| 11 | 窄布局（< 900px）两步式 | §3.2 |
| 12 | 键盘与无障碍 | §7 |
| 13 | 计数、搜索只针对已载入记录并写明 | §6.5 |
| 14 | 迁移并保留现有行为测试 | §10.1 |

### 1.2 阶段 1b · Skill / 宿主（U1：减少页面数量）

**承诺范围：同一 Codex 任务、同一存活 Viewer 内，默认只发出一次自动打开请求。** 点击深链是否在原标签页定位，要在 Codex 宿主实测后才能对用户这样说。

**打开请求记账**（由主代理在本 Codex 任务的对话上下文中维护，**不**写入 Runtime 或执行状态）：

| 状态 | 进入条件 | 后续 start 的行为 |
|---|---|---|
| `not_requested` | 本 Codex 任务尚未发出过自动打开请求 | 调用一次 `open_in_codex`，进入下一状态 |
| `requested` | 已调用 `open_in_codex`，结果为成功**或 queued** | **不再**自动打开；只在回复中给该 run 的深链和一句状态 |
| `failed` | `open_in_codex` 明确返回失败 | 不自动重试；回复中给链接并说明“未能自动打开，可点击链接查看” |
| 用户显式重开 | 用户说“重新打开 / 另开查看 / 打不开了” | 调用 `claude_details(run_id)` 取新链接并打开一次；状态回到 `requested`；**不新增 Claude run** |

- 监督席（`references/supervisor.md:14`）**永不**调用 `open_in_codex`，只把 run_id 与 details_url 回传主代理。现有措辞已如此，保留并在 SKILL.md 中重申。
- Viewer 重启（MCP 进程换了、token 变了）时，旧页面会断线；主代理在用户要求或下一次需要查看时按“用户显式重开”处理，不因此自动开新页。

| 文件 | 改动 |
|---|---|
| `skills/codex-claude-orchestrator/SKILL.md:88` | 把“首次返回即用 `open_in_codex` 打开一次”改为上表的记账规则：只有 `not_requested` 时才自动打开；`queued` 视为已请求；失败不自动重试；之后的 start 只给深链与一句状态 |
| `scripts/server.py:129` `presentation.instruction` | 改为：“若本 Codex 任务尚未请求打开工作台，打开一次；已请求（含 queued）只提供可点击链接。queued 只表示排队，不能说已显示。” `host_open_state` 保持 `unobserved`，不在执行状态里混入页面状态 |
| `scripts/server.py:442` `claude_details` 说明 | 改为：“给用户可点击链接；仅在用户要求重新打开或本任务尚未请求打开时调用 open_in_codex。” |
| 需宿主实测 | ① 首个打开 queued 后连续三次 start，主代理只调用一次 open_in_codex；② 监督席零次；③ 深链点击是否复用标签页、hashchange 是否定位。③ 不通过时的后备：保留已开工作台，用户在列表中选择 |

### 1.3 阶段 1c · 可选小后端改动（不阻塞 1a/1b）

| 字段 | 说明 |
|---|---|
| runs 列表行 `title` | **暂缓。** 第一阶段标题 = `objective` 首句（registry 已有 `objective`，runtime.py:432）。以后新增时再定义类型、来源与兼容 |

### 1.4 阶段 2 · Runtime / 后端 **[拟新增]**（不在本阶段实现；UI 预留 gate）

| 编号 | 字段 / 能力 | 取值约定 | UI 用途 |
|---|---|---|---|
| B1 | `report.integrity` | `complete` / `truncated` / 缺失（=未知）；`truncated` 带 `reason` | 报告“已回报 · 完整”“报告被截断”。只读明确值，缺失不猜 |
| B1 | 受控报告产物收集 | 长报告不靠通知文本 | 截断时可给“正式报告产物”入口 |
| B2 | `exit_reason`、`completion_matched`、`report.collection_state` | `collection_state`: `pending` / `collected` | “待收集”；**只有**同时满足 §5.1 的 `isExited` 才写“外层已退出” |
| B3 | `report.contract_check` | `{passed: bool, failures: [{kind, path, detail}]}`，`kind` ∈ `placeholder` / `missing_field` / `identity_mismatch` / `artifact_missing` / … | 按 `failures` 的真实类别显示；只有 `kind=placeholder` 才标“占位” |
| U4 | `cli_selection_reason` | 如 `workflow_capability` | 本轮 CLI 旁的选择原因 |
| — | 任务聚合 / 全量计数 / 后端搜索与过滤 | 如 `GET /api/tasks` | 全量统计；现在只能写“已载入” |
| — | Workflow 子阶段事件结构化 | — | 子阶段列表（不做百分比） |
| P2 | 跨 Codex 任务、MCP 重启后的稳定入口 | — | 另行设计 |
| P1 | 队列、任务组、同目录并发 | — | 另行设计；在此之前 UI 不出现相关状态或按钮 |

---

## 2. 数据模型（前端）

### 2.1 两层数据：列表摘要 与 详情

| 层 | 来源 | 有 | 没有 |
|---|---|---|---|
| 摘要 `summary` | `/api/runs` 行 | run_id、task_id、cwd、revision、status、decision、superseded_by、previous_run_id、objective、started_at、ended_at、elapsed_seconds、workspace_changes（占位）等 registry 字段 | **result、receipt**（viewer.py:136 明确剔除）；claude_started、execution_evidence、last_meaningful_event 等 snapshot 才计算的字段 |
| 详情 `detail` | `/api/snapshot?run_id=` | 摘要全部 + receipt、result、claude_started、execution_evidence、events_count、last_meaningful_event、last_activity_at、workspace_changes（真实） | — |

**缓存**：`cache[run_id] = { summary, summaryAt, detail, detailAt, detailStale }`

合并规则：

1. 摘要刷新只更新 `summary`，**不擦除** `detail`。
2. 新摘要的 `status` / `decision` / `superseded_by` 与 `detail` 中的不同 → `detailStale = true`，并加入 §2.3 的补读队列。
3. 推导时使用 `view = detail && !detailStale ? detail : { ...detail, ...summary }`，同时带 `hasDetail = !!detail && !detailStale`。
4. 所有只在详情中存在的字段，在 `hasDetail=false` 时视为**未载入**（§5.0）。

### 2.2 任务归并与当前轮三态

```js
taskKey(r) = r.task_id ? r.task_id + '\u0000' + r.cwd : 'run:' + r.run_id
task.runs  = 已载入的同 key 记录，按 revision 升序
task.title = firstSentence(objective) || task_id || run_id   // title 字段暂缓
task.project = basename(cwd)   // 重名时附上级目录
```

**当前轮判定（不得用“本地最大 revision”兜底）：**

```js
tips = task.runs.filter(r => !r.superseded_by)
if (tips.length === 1)                         → current = { state:'known', run: tips[0] }
else if (tips.length === 0)                    → current = { state:'not_loaded', pointer: 最大 revision 那条的 superseded_by }
else /* 多个无 superseded_by（数据不一致） */  → current = { state:'known', run: tips 中 revision 最大者 }，技术记录中注明“轮次链不完整”
```

- `not_loaded`：立即沿 `superseded_by` 链补取 snapshot，**每一跳一个请求，最多 5 跳**；补到的记录并入缓存。补取期间与失败时：
  - 任务卡原因行：“当前轮待加载”（`unknown` 色），归入“需要处理”组的最末。
  - 详情：显示已选的历史轮 + 横幅“本轮已有后续轮（第 N 轮），正在读取…”；失败时“后续轮未能载入；以下为历史轮，不代表当前状态。”
  - 这期间**不显示任何下一步卡片**。
- **深链指向旧 run**：选中该旧 run，作为历史轮展示（历史横幅）；同时按上面的规则确定当前轮。**深链不能把旧 run 变成当前轮。**
- 一条记录只有在自身 `superseded_by` 为空、且其摘要在 §2.3 的新鲜度内，才可能是当前轮。

### 2.3 刷新规则

| 对象 | 方式 | 频率 / 上限 |
|---|---|---|
| 首页 `offset=0&limit=50` | `/api/runs` | 页面可见：有未收口任务时 5 秒，否则 15 秒；隐藏时 60 秒 |
| 选中的 run | `/api/snapshot` + `/api/events` | 沿用现有 `pollingDelay()` |
| **观察集**：已载入、不在最近一次首页结果中、且未收口的 run（status ∈ ACTIVE ∪ {unknown} ∪ {reported 且无 decision}，或 `detailStale`） | `/api/snapshot` 轮转 | 每 5 秒一轮，**每轮最多 2 个请求**；观察集上限 40 个 run，超出部分不实时刷新，列表底部注明“另有 N 个较早任务未实时刷新” |
| 当前轮补取（§2.2） | `/api/snapshot` | 优先于观察集，占用同一请求预算 |
| 需要处理组中可见、但 `hasDetail=false` 的卡片 | `/api/snapshot` 预读 | 每次首页刷新后最多预读 10 个，同样占用每轮 2 个请求的预算 |

- 页面隐藏时暂停观察集与预读，只保留首页 60 秒刷新。
- 每张卡片记录 `summaryAt`：超过 60 秒未刷新的卡片，元信息改为“同步于 HH:MM”，**不**显示相对活动时间。
- 不为全部已载入行读取完整报告；只有选中、观察集、补取、预读四类会请求 snapshot。

### 2.4 分页与去重

- 所有分页结果按 `run_id` 去重后并入缓存。
- “加载更早记录”请求 `offset = 上次 next_offset`。首页新增行会使 offset 下移，导致下一页出现已见行：去重后若本页没有新行且 `has_more=true`，自动再取下一页，最多连续 3 页。
- 已载入上限 500 行；达到后按钮改为“已达到载入上限”。
- 计数、搜索、筛选只作用于已载入集合（§6.5）。

### 2.5 “新增任务”判定（R7）

- **基线**：页面首次成功载入首页时的 run_id 集合，记为 `baseline`；同时记录 `baselineAt`。
- **候选**：之后的首页刷新中出现、且不在 `baseline` 与已见集合中的 run。
- 通过“加载更早记录”、深链补取、当前轮补取得到的记录，**永远不**算新增。
- 候选的任务 key 也是新的 → 该任务标“新”，触发 §6.1 提示；key 已存在 → 只是已有任务的新一轮，不弹提示，只更新卡片。
- 用户打开过该任务后清除“新”标记。仅存在内存中。

---

## 3. 布局

### 3.1 宽布局（视口 ≥ 900px）

```
┌ 顶栏 56px ───────────────────────────────────────────────────────────┐
│ 工作台名 · 项目选择 ········ 只读说明 · 连接状态 · [环境与维护]         │
├ 横幅区（断线 / 恢复 / 凭证失效，按需）──────────────────────────────┤
├ 列表 392px ────────────┬ 详情（最大内容宽 900px，居中，左右 40px）────┤
│ 搜索                    │ 项目 / cwd ········· 执行编号 [复制]         │
│ 状态筛选 chips          │ 任务标题 24px                                │
│ 新增任务提示（按需）    │ 轮次标签 ············ 时间信息               │
│ 分组列表（可滚动）      │ 历史轮 / 后续轮待加载 横幅（按需）           │
│                          │ 三项事实（3 列）                             │
│                          │ 下一步（按需）                               │
│ 已载入说明 [加载更早]   │ 报告 → 可展开区                              │
└─────────────────────────┴──────────────────────────────────────────────┘
```

- 列表与详情各自独立滚动；顶栏固定。
- 900–1100px：列表宽降到 340px；< 1000px 时事实卡的问题提示（“Claude 在跑吗”）隐藏。

### 3.2 窄布局（视口 < 900px，Codex 内嵌面板约 360–480px）

- 顶栏 48px：名称 · 连接点与时间 · 右侧“在独立窗口打开”图标按钮（`aria-label`），用户显式点击才打开同一 URL。
- 两个视图：**列表** / **详情**，同一时间只显示一个。
  - 列表：筛选 chips + 分组卡片（紧凑版：标题两行、三个 16px 标志 + 原因行 + 时间）。底部说明：“三个图标依次为 执行 · 报告 · 核验。计数仅含已载入记录。”
  - 详情：顶部“‹ 任务列表”按钮；标题 18px；轮次用 `<select>`；三项事实改为纵向三行；下一步按钮全宽 36px 高；报告默认展开；其余可展开区同宽布局。
- 返回列表时恢复滚动位置和焦点到原卡片。从 hash 进入时直接打开详情视图。
- 环境与维护抽屉在窄布局下为全屏覆盖层。

---

## 4. 组件规格

### 4.1 顶栏

| 元素 | 规格 |
|---|---|
| 名称 | “Claude 协作工作台”，15px/600 |
| 项目选择 | `<select>`：“全部项目” + 已载入记录中出现的每个项目。只筛选列表 |
| 只读说明 | “只读视图 · 执行、停止与核验在 Codex 中进行”，12px muted；窄布局省略 |
| 连接状态 | 胶囊：绿点“已连接 · 同步于 HH:MM:SS”；断线“连接中断 · 显示 HH:MM:SS 的记录”；凭证失效“需要重新打开”。`role=status`。同步时间 = 最近一次**关键请求**成功时间，不是活动时间 |
| 环境与维护 | 按钮；`/api/cli-maintenance` 有 `notice_pending`、维护进行中或 `active_unqualified_for_contract` 时右侧 7px 橙点（`aria-label="有说明"`）；该接口失败时显示灰点（`aria-label="维护状态暂不可用"`）。`aria-expanded` |

### 4.2 列表

**搜索**：`type=search`，占位“搜索任务标题（仅已载入记录）”，匹配 title、task_id、run_id、项目名（不区分大小写）。

**筛选 chips**：全部 / 需处理 / 进行中 / 已收口，各带计数（已载入任务数）。`aria-pressed`。

**排序**（0.6.2 起）：列表不再按状态分组，所有筛选下都是同一个顺序——按每个需求最新一轮执行的 `started_at` 倒序。新一轮真实执行（更高 revision 的新 run）会让需求前移；轮询、活动、结束、验收/退回等更新不改变位置。开始时间相同按任务键升序，缺少开始时间的排在最后。筛选 chips 只缩小成员范围（需处理/进行中/已收口的判定见 §5.4），三项状态标志照常显示；选中项与键盘焦点按任务键保留。

**任务卡**（`<button>`，整卡可点击）：
- 第 1 行：标题 14px/600，最多两行截断；新增任务右侧“新”标签。
- 第 2 行：原因行 12px/500，颜色见 §5.4。
- 第 3 行：三个 16px 标志 + 文字“执行 / 报告 / 核验”（标志 `aria-hidden`，附视觉隐藏文本）；右侧元信息：`[项目名 · ]`（仅“全部项目”）`[第 N 轮 · ]`（仅 N>1）时间。
- 时间：卡片新鲜（§2.3）时显示最近活动的相对时间（`last_activity_at`，没有则 `ended_at`/`started_at`）；不新鲜时显示“同步于 HH:MM”；断线时显示“断线前”。
- 选中态：白底 + 1px `--text` 边框；`aria-current=true`。悬停：白底。
- 断线时（R8）：**不降低透明度**，文字颜色不变；卡片边框改为 1px 虚线 `--line-strong`，停止“执行中”脉冲。
- “执行中”标志脉冲 1.6s；`prefers-reduced-motion` 或断线时关闭。

**列表底部**：“已载入 N 个任务 · M 轮执行 · 计数不含未载入历史” + [加载更早记录]（`has_more=false` 时禁用并改为“没有更早记录”）。观察集超上限时追加一行“另有 N 个较早任务未实时刷新”。

**空态**：
- 从未有任务：“还没有执行任务 / 在 Codex 描述要做的事；这里会显示真实活动和核验结果。”
- 搜索/筛选无结果：“已载入的记录中没有匹配的任务。更早的记录需先‘加载更早记录’。”

### 4.3 详情头部

- 面包屑行（12px）：项目名 / cwd（等宽）······ “执行编号 run_xxx” + [复制]。
- 标题 24px/600；objective 与标题不同时，标题下 13px 显示目标首句。
- 轮次标签（`role=tablist`）：“第 N 轮” + 小字状态；当前轮加“· 当前”；当前轮未载入时最后一个标签为“第 N 轮 · 待加载”（禁用，`aria-disabled`）。
- 时间信息：“HH:MM 开始 · 用时 X 分 · 已结束/已运行”。
- **历史轮横幅**：“正在查看第 N 轮（历史记录）。当前是第 M 轮；历史轮的结论不作为当前验收依据。” + [回到当前轮]。
- **后续轮待加载横幅**：见 §2.2。
- 以上两种横幅出现时**隐藏下一步卡片**。

### 4.4 三项事实 + 下一步

**事实卡**（3 列 grid，gap 12）：标志 20px + 维度名 + 问题提示 / 状态文案 16px/600 / 一句说明 13px。
- 断线时追加 11px“断线前记录 · 可能已过时”。
- 详情未载入的字段在说明中写“详情未载入”，并在后台补读（§2.3）。
- 卡片底色/边框：`none`、`settled` 为白底 + `--line`；其余用对应 tone 的 bg/border。

**归因说明**（事实卡下方 12px，仅在有依据字段时显示）：
- `receipt.blocked_by === 'preflight'`：“预检未通过，Claude 未启动。”
- 守卫拒绝（`result.permission_denials` 非空）：“守卫按设计拒绝越界读取，属于正常行为。”
- 调用参数错误：仅当记录中有可依据的错误字段（如 `result_validation_error` 指向输入）时写“归因：调用参数不符合要求。”，否则不写。

**下一步卡片**（当前轮已知且 §8.2 有对应文案时显示）：
- 标题“下一步” + 标签“在 Codex 中处理”；正文 14px；有建议指令时，等宽框 + 主按钮“复制给 Codex”。复制内容 = 指令 + 换行 + `执行编号：<run_id>`。
- 固定脚注：“本页只读：停止、重派、改派和验收都经 Codex 对话完成，页面不直接执行。”
- 对执行未正常交回的 run（failed / timeout / blocked / cancelled），下一步**只**建议核对原因或按新方向开启新一轮，**不**建议对该 run 记录 accepted / returned（runtime.py:1133 只允许对未被替代的 `reported` 记录结论）。

### 4.5 报告区

标题“报告” + 类型标签 + 右侧来源说明。`reportKind` 由 §5.2 的结果决定：

| reportKind | 条件（概要，精确见 §5.2） | 渲染 |
|---|---|---|
| `not_loaded` | 需要详情字段但未载入 | 灰框：“报告详情未载入，正在读取…” |
| `none` | 进行中无结果 / 终态且 result 与 structured 都没有 | 虚线框：进行中“执行中，尚无报告。”；收集中“正在收集结果。”；停止中“已请求停止，本轮不再期待报告。”；终态“本轮没有任何报告输出。” |
| `formal` | `status=reported`，无 decision | 白框：summary + evidence；标签“已回报 · 待核验”（`settled` 中性色） |
| `accepted` | decision=accepted（当前轮） | 同上，标签“已通过核验” |
| `returned` | decision=returned，非 Codex 补齐 | 顶部“原 Claude 报告（已退回）” + 正文 |
| `codex` | returned + completed_by_codex | 当前轮：绿框“Codex 补齐后的结果” + completion_summary；历史轮：灰框同内容，标题加“（历史轮）”。两者都有折叠区“原 Claude 报告（已退回，未采纳）” |
| `failed_with_content` | status ∈ {failed, timeout, blocked, cancelled} 且 `result.structured` 存在 | 橙框：顶部“以下内容来自未正常交回的执行，仅作失败证据，不能对本轮记录验收结论。” + 失败原因（按 `invariant_error`、`scope_error`、`result_validation_error`、`workflow_error`、`session_error`、`permission_denials` 顺序列出非空项）+ 折叠区中的 structured.summary 与 evidence |
| `failed_no_structured` | 同上状态，`result` 存在但 `structured` 为空 | 虚线框：“没有结构化报告。” + 失败原因列表 + 入口“查看执行回执 / 原始结果 / 工作区 Diff”（artifact 查看器） |
| `truncated` **[B1]** | `report.integrity === 'truncated'` | 橙框：“以下是通知中收到的部分内容，不是正式报告全文” + 已收到内容 + “在此截断” + 说明“正式报告产物：未取得……截断的内容不能用来判断通过或失败。” + `reason` |
| `pending` **[B2]** | `report.collection_state === 'pending'` | 橙框：满足 `isExited` 时“外层会话已退出，正式报告还没有收齐。”，否则“正式报告待收集。” + 已观察到的内部完成事件（如有）+ 脚注“这些事件不等于最终报告。” |
| `contract_failed` **[B3]** | `report.contract_check.passed === false` | 红框：“报告未通过交付契约校验，不能作为交付结果。” + `failures` 逐条（类别文案 + path + detail）；只有 `kind=placeholder` 的条目后加“占位”标签 + 脚注“语义是否正确仍由 Codex 核验。” |

- 任何历史轮（`superseded_by` 存在）：正文前加“本轮已被后续轮替代，仅供回看。”，标签后缀“· 历史”。
- `/api/artifact` 的 `truncated=true` 只表示**展示截断**，在技术记录中注明“展示已截断；完整记录保留在本机”，**不得**据此显示“报告被截断”。
- **Workflow 完整报告**（0.6.2 起）：`result.workflow_delivery` 存在时，在上述正文之后另起“Workflow 完整报告（与父级摘要分开保存）”。说明交付状态与原因码；每个已收集调用显示字节数与 SHA-256 前缀，并提供“加载完整报告 / 继续加载”，按 `/api/artifact?name=workflow_report&index=&offset=&limit=` 的 UTF-8 字节分页拼接。只有偏移、总字节数和 SHA-256 与已载入部分一致的页才会拼接，否则停止并提示重新打开；载入内容按 run 缓存，刷新不丢失。未收集的调用只显示原因码。它与 B1/B2/B3 gate 无关，gate 仍保持关闭。

**核验依据与未确认项**（可展开，默认收起）：依次列出报告引用、Claude 自报检查、未确认项、Codex 核验依据、Codex 核验原因；失败类 run 的标题改为“失败证据与未确认项”。

### 4.6 可展开区（`<details>`，默认收起）

| 区块 | 内容 | 数据 |
|---|---|---|
| 过程 · 公开活动 | 摘要右侧“已载入 N 条 / 共 M 条”。每行：时间 + `activityDescription()` + 工具/文件。顶部筛选输入。脚注“只显示公开工具事件，不含私有推理；一段时间没有新活动不等于执行已停止。” | `/api/events`，保留现有分页、尾部窗口与积压追赶行为 |
| 文件变化 | `workspaceChangeText()`；按钮打开工作区 Diff / 输入快照 / 结果快照 | detail.workspace_changes、artifact |
| 本轮执行身份 | 本轮 CLI（+ 选择原因，仅有 `cli_selection_reason` 时）、全局配置 CLI（+“仅供参考”）、请求模型、请求 effort、实际模型、执行证据（`execution_evidence`：provider_event / bridge_lifecycle / unconfirmed）、协调内容（固定的内容来源、版本与 digest 前 12 位）、用量。本轮与全局都已知且不同 → 摘要加标签“本轮 CLI 与全局配置不同” | `runCliEvidence()`、maintenance.current_version、`actualModelText()`、`contentBindingText()`、`usageLines()` |
| 技术记录 | run_id、task_id、Claude 会话、cwd、持久状态（`status · decision · resolution · claude_started`）、上一轮 / 后续轮 run_id；原文按钮：packet / result / receipt / decision / decision_history / reconciliation / environment / content_binding | detail + artifact |

- 实际模型：只有 `actual_models` 或（`actual_model` 且 `actual_model_source`）时显示值，否则“待 provider 回报”，不用初始化模型或请求模型代替。
- 协调内容：`content_binding.mode=active` 显示“已审查的动态内容”，`bundled` 显示“插件内置内容”；无记录显示“未固定（本轮记录未提供）”。
- 用量（`usageLines(result)`，逐行显示）：有 `usage_report` 时先列“CLI 会话累计估算（含子代理，按模型）”，每个模型一行“未缓存输入 / 输出 / 缓存读取 / 缓存写入 / 估算 $”，再给“合计”行（token 为各模型之和，费用取 CLI 的 `total_cost_usd`，不自算价格）；没有按模型统计时只显示 CLI 累计估算费用并注明整任务 token 合计未知。续跑会话追加“CLI 会话累计可能包含此前轮次的支出，不是本轮新增，也不能与其他轮相加”。最后一行前显示“主代理最终回报（不含子代理，不是整任务总量）”。缺项或畸形值显示“未知”，0 照常显示。末行“费用为 Claude CLI 客户端估算，不是实际扣费或订阅剩余额度。”
- 旧记录（无 `usage_report`）：token 标为“主代理最终回报（旧记录，统计范围未确认，不是整任务总量）”，费用标为“CLI 会话累计估算（旧记录：可能含子代理，与上面的 token 不是同一统计范围；未记录按模型分项）”，不推算合计。

### 4.7 环境与维护抽屉

- 宽布局：右侧覆盖层 440px；窄布局全屏。`role=dialog`，打开时焦点到关闭按钮，Esc 关闭并还焦点。
- 分节：本机连接 / Claude CLI / 维护 / 用量说明（内容同 v1）。
- `/api/cli-maintenance` 失败：**只**在抽屉内显示“版本维护状态暂不可用；本轮执行记录仍可查询。”，不影响任务区，不触发断线（§6.3）。
- 移除现在页面顶部的 `maintenanceNotice` 横幅。

---

## 5. 状态推导（核心）

### 5.0 标志 tone

状态永远是“符号 + 文字”，不只靠颜色。

| tone | 符号 | 用于 |
|---|---|---|
| `active` | ● | 进行中 |
| `settled` | ■ | 中性已定：执行已结束、已回报（待核验 / 完整） |
| `ok` | ✓ | **只**用于当前轮的核验通过、Codex 已补齐完成 |
| `attention` | ! | 需处理 |
| `bad` | ✕ | 不通过 / 失败 |
| `unknown` | ? | 未知、详情未载入、当前轮待加载（虚线边） |
| `history` | 沿用原符号（✓ / ✕ / ■） | 历史轮的核验结论（灰色） |
| `none` | – | 尚无 |

常量（来自 runtime.py:26–27）：

```js
ACTIVE   = {'preflight','starting','executing','collecting','cancelling','running'}  // running 兼容旧记录
FAIL_END = {'failed','timeout','blocked','cancelled'}
FORMAL   = 'reported'           // 唯一的“正式交回”状态
```

### 5.1 执行 execFact(v, hasDetail)

```js
st = v.status
started = hasDetail ? v.claude_started : undefined     // true / false / null / undefined(未载入)
by = hasDetail ? (v.receipt?.blocked_by ?? v.result?.blocked_by ?? null) : undefined
isExited = FAIL_END.has(st) || st === FORMAL || (hasDetail && !!v.receipt)
           || (gate.B2 && v.exit_reason != null)

if (gate.B2 && v.completion_matched === false && isExited)
                   → attention '已退出 · 报告未收齐'
switch (st):
 'preflight'  → none      '已创建 · 预检中'
 'starting'   → started === true ? active '执行中' : active '正在启动'
                 detail: started === true ? '' : 'Claude 尚未确认启动'
 'executing' | 'running'
              → active '执行中'  detail: 最近公开活动相对时间 + 描述；hasDetail=false 时 '活动详情未载入'
 'collecting' → active    '收集结果'
 'cancelling' → attention '停止待确认'   detail: '已请求停止，尚未收到进程停止回执。'
 'cancelled'  → started === false ? none '启动前已取消'
                                   : none '已停止'   detail: '已按回执确认停止；工作区外副作用仍需核对。'
 'blocked'    → by === 'preflight' && started !== true ? bad '预检阻止 · Claude 未启动'
              : by === 'executor'                      ? bad '执行者无法继续'
              : by === 'workflow_evidence'             ? bad 'Workflow 报告未完整交回'
              : by === undefined                       ? bad '被阻止'  detail: '原因详情未载入'
              :                                          bad '被阻止'
 'failed'     → started === false ? bad '启动失败 · Claude 未启动' : bad '执行失败'
 'timeout'    → bad '已超时'
 'unknown'    → v.reconciliation?.outcome === 'confirmed_stopped'
                  ? none '已核对停止' : unknown '状态未知'  detail: '无法确认进程状态；不代表已失败。'
 'reported'   → settled   '执行已结束'
 default      → unknown   '状态待确认'（原值写入技术记录）
```

`started === null`（有详情但未能确认）时，一律不写“已启动”也不写“未启动”。

### 5.2 报告 reportFact(v, hasDetail)

```js
st = v.status; hist = !!v.superseded_by
r = hasDetail ? v.result : undefined
// [拟新增] 分支：字段缺失时整条不生效
if (gate.B3 && v.report?.contract_check?.passed === false)
      → bad (全部 failures.kind==='placeholder' ? '缺乏实质内容（占位值）' : '未通过报告契约')   kind: contract_failed
if (gate.B1 && v.report?.integrity === 'truncated')  → attention '报告被截断'   kind: truncated
if (gate.B2 && v.report?.collection_state === 'pending')
      → attention '待收集'   kind: pending
// 现有字段
if (st === 'unknown')                    → unknown '不确定'
if (st === FORMAL)
   gate.B1 && v.report?.integrity === 'complete'
                                          → settled '已回报 · 完整'
   else                                   → settled '已回报 · 待核验'      // 不需要详情即可给出
   kind: 视 decision → formal / accepted / returned / codex（hasDetail=false 时正文区为 not_loaded）
if (FAIL_END.has(st))
   r === undefined                        → unknown '详情未载入'           kind: not_loaded
   r?.structured                          → attention '有内容 · 未正常交回' kind: failed_with_content
   r                                      → none '无结构化报告'            kind: failed_no_structured
   else                                   → none '无报告'                  kind: none
if (st === 'collecting')                  → none '收集中'                  kind: none
if (ACTIVE.has(st))                       → none '尚无报告'                kind: none
// 历史轮：以上结果的标签后缀 '· 历史'，tone 改为 history（bad/attention 除外，保持原色以免掩盖问题）
```

gate 关闭时的降级即上面“现有字段”部分：例如 B2 未落地时，早退的 run 只能按其 status 显示（可能是 `reported` 或 `failed`），**不**显示“待收集”。

### 5.3 核验 verifyFact(v)

```js
d   = decisionValue(v)                         // 沿用现有 decision()
res = typeof v.decision === 'object' ? v.decision.resolution : undefined
hist = !!v.superseded_by
codexDone = d === 'returned' && res === 'completed_by_codex'    // 不看 superseded_by（替代现有 completedByCodex 的历史判断）

if (codexDone) → hist ? history 'Codex 曾补齐完成 · 已有后续轮'  (符号 ✓，灰)
                      : ok      'Codex 已补齐完成'
if (d === 'accepted') → hist ? history '已验收 · 已有后续轮' (✓，灰) : ok '通过'
if (d === 'returned') → hist ? history '已退回 · 已有后续轮' (✕，灰) : bad '已退回'
if (FAIL_END.has(v.status)) → none '不适用'   detail: '本轮未正常交回，不能记录验收结论。'
else → none '未核验'   detail 按报告事实：formal '等待你在 Codex 核验。'；待收集/截断 '报告未收齐，暂时无法核验。'；进行中 '报告交回后由 Codex 核验。'
```

- 历史轮的 completion_summary 与“原报告未采纳”事实照常展示（§4.5 `codex`），但**不**关闭当前轮，也不为历史轮给出下一步指令。
- 现有 `completedByCodex()` 仍可用于“当前任务是否收口”，但报告区与核验事实改用 `codexDone`。

### 5.4 分组、原因行与排序（取当前轮）

```js
if (current.state === 'not_loaded')  → attention，原因“当前轮待加载”（unknown 色）
c = current.run; ex = execFact(c); rp = reportFact(c); vf = verifyFact(c)
closed    if vf ∈ {ok:'通过', ok:'Codex 已补齐完成'} || c.status === 'cancelled'
active    if ACTIVE.has(c.status)
attention otherwise
```

- **原因行** = 三项事实中最需关注的一项（优先级 bad > unknown > attention > 其余），写法：
  - 执行中：“执行中 · ” + 最近活动简述（无详情时只写“执行中”）
  - 正式交回未核验：“已交回报告 · 待你在 Codex 核验”
  - 状态未知：“状态未知 · 暂不要重派”
  - 已退回：“已退回 · 待决定下一步”
  - 有内容未正常交回：“执行失败 · 有部分报告内容”
  - 预检阻止：“预检阻止 · Claude 未启动”
  - Codex 补齐：“Codex 已补齐完成 · 原报告未采纳”
- **原因行颜色**：取该事实的 tone fg；`settled` / `history` / `none` 用 `--text-2`。
- **排序**：分组只决定筛选 chips 的成员，不决定顺序；顺序统一按最新一轮执行的开始时间倒序（见 §4.2）。此前“需要处理按关注度、进行中按最近活动、已收口按结束时间”的分组排序已移除。

---

## 6. 交互规格

### 6.1 新增任务（R7）

- 触发：§2.5 判定出新增任务。
- 未选中任何任务或列表为空：直接选中。
- 否则**不切换、不移焦点、不滚动**：列表顶部提示条（`role=status`，`aria-live=polite`）：“新增任务：<标题> · 执行已创建。不会打断你当前查看的报告。” + [查看]。多个时：“N 个新增任务” + [查看最新]。
- 只有该 run 的详情已载入且 `claude_started === true` 时，提示条可改写为“… · Claude 已开始执行”。
- 点击[查看]或选中任意新增任务后，提示消失。

### 6.2 深链与 hash

- `hashchange`：选中该 run；若不在缓存，先请求 snapshot 再定位。该 run 若有 `superseded_by`，按历史轮展示（§2.2）。
- token 变化：`connGen += 1`，中止旧连接代的所有请求，用新 token 重新同步；旧代的响应一律丢弃（§6.3）。
- 选中任务/切轮时 `history.replaceState` 更新 `run`。

### 6.3 错误边界、断线与恢复（R6、R8）

**请求分类：**

| 类别 | 请求 | 失败时 |
|---|---|---|
| 关键 | 首页 `/api/runs`；选中 run 的 `/api/snapshot`、`/api/events` | 网络错误 / 超时 → 进入 `offline`；401 → 进入 `token_invalid` |
| 局部 | `/api/cli-maintenance` | 只在抽屉内提示，按钮灰点 |
| 局部 | `/api/artifact`（单个产物） | 只在对应区块显示“该项记录暂不可读” |
| 局部 | 观察集 / 预读 / 补取的 snapshot | 只把那张卡的 `summaryAt` 保持旧值，卡片显示“同步于 HH:MM” |
| 局部 | 选中 run 返回 400（run 不存在 / 无效） | 详情显示“该执行记录无法读取”，不影响列表和连接状态 |
| 忽略 | 切轮、切任务、换 token 造成的 `AbortError`，或 epoch / connGen 已过期的响应 | 不改任何状态 |

保留现有的 `AbortController` + `epoch` 机制，并新增 `connGen`：每个请求记录发出时的 `(epoch, connGen)`，回来时任一不匹配即丢弃。

**offline（关键请求网络失败）：**
- 顶栏胶囊变橙；顶部横幅（`role=alert`）：“与本机服务的连接已断开，HH:MM:SS 之后未同步。下方是断线前的记录，**不代表执行已停止**，也不要据此重派。恢复：在 Codex 中说‘重新打开 Claude 协作工作台’。” + [复制恢复指令]。
- 数据全部保留；事实卡标“断线前记录 · 可能已过时”；卡片时间显示“断线前”；卡片虚线边框、停止脉冲；**文字不降透明度**。
- 后台按 15s → 30s → 60s（封顶）退避重试关键请求。

**token_invalid（401）：** 横幅“访问凭证已失效，请在 Codex 中重新打开工作台以获得新链接。” 停止重试，数据保留并标过时。

**恢复**：只有在**当前 connGen** 下，首页 runs 与选中 run 的 snapshot 都成功后，才从 offline / token_invalid 切回在线。随后显示绿色横幅（`role=status`）：“已恢复读取，HH:MM:SS 完成同步。以下为最近同步的记录；断线期间的活动以各任务的公开活动记录为准。页面只读取记录，没有发起任何执行。” + [知道了]。
- **不得**写“记录与断线前连续”“没有遗漏”等无依据的完整性声明。

页面不因断线或恢复触发任何动作。

### 6.4 切轮

- 点轮次标签 → 中止旧请求（AbortController），`epoch += 1`，加载该 run；非当前轮显示历史横幅并隐藏下一步。
- `<details>` 的展开状态在切轮时保留。

### 6.5 计数与搜索的诚实性

- 计数、筛选、搜索只针对已载入记录；文案必须出现“已载入”。不显示全量数字，除非后端提供全量字段。

### 6.6 复制

- “复制给 Codex”：指令 + `执行编号：<run_id>`；执行编号[复制]：仅 run_id；[复制恢复指令]：`重新打开 Claude 协作工作台`。成功反馈 2 秒，失败提示手动复制。

---

## 7. 键盘与无障碍

| 项 | 规格 |
|---|---|
| Tab 顺序 | 顶栏 → 搜索 → 状态筛选 → 任务列表 → 详情（轮次 → 下一步 → 报告 → 展开区）→ 抽屉（打开时焦点困于抽屉） |
| 列表 | roving tabindex；↑/↓ 移动，Enter/Space 打开，Home/End；监听挂在列表容器上 |
| 轮次 | `role=tablist`；←/→、Home/End；“待加载”标签 `aria-disabled` 并跳过 |
| Esc | 关闭抽屉；窄布局详情返回列表，焦点回原卡片 |
| `/` | 焦点不在输入框时聚焦搜索 |
| 焦点环 | `:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px }` |
| 播报 | 新增任务 `aria-live=polite`；断线与凭证失效 `role=alert`；恢复与同步 `role=status` |
| 不只靠颜色 | 每个状态 = 符号 + 文字；卡片标志 `aria-hidden` + 视觉隐藏文本 |
| 对比度 | 正文与次要文字 ≥ 4.5:1（两套主题）。**禁止对含文字的容器使用 opacity < 1** 来表达状态；需要弱化时只改背景或装饰 |
| 动态 | `prefers-reduced-motion: reduce` 关闭脉冲 |
| 长文本 | 列表两行截断、详情完整；路径与 ID 等宽并 `overflow-wrap:anywhere`；报告正文 `white-space:pre-wrap` |
| 控件尺寸 | 宽布局按钮 ≥ 28px 高，主按钮 ≥ 36px；窄布局主要按钮 ≥ 36px |

---

## 8. 文案表

### 8.1 旧 → 新

| 0.4.7 | 新 |
|---|---|
| 页面标题“Claude 执行详情” | “Claude 协作工作台” |
| 执行结束 · 待核验 | 执行“执行已结束”（中性）+ 报告“已回报 · 待核验”（中性）+ 核验“未核验” |
| 已验收 | 核验“通过”（历史轮：“已验收 · 已有后续轮”） |
| 原报告未采纳 · Codex 已完成 | 核验“Codex 已补齐完成”（历史轮：“Codex 曾补齐完成 · 已有后续轮”） |
| 本轮未验收 · 保留部分报告与失败证据 | 报告“有内容 · 未正常交回”或“无结构化报告”；核验“不适用” |
| 状态待核对 | 执行“状态未知”；下一步“不代表失败，暂不要重派” |
| 正在停止 | “停止待确认” |
| 被阻止（预检） | “预检阻止 · Claude 未启动” |
| 已被后续轮次替代 | 轮次标签 + 历史轮横幅 |
| 完整模式 / 简洁模式 | 删除；技术内容进可展开区 |
| 连接中断 · 保留最后记录 | 顶部横幅 + “断线前记录 · 可能已过时” |
| 需要处理（卡片） | “下一步” + “在 Codex 中处理” |
| 报告与验收 | 拆成“报告”“核验”两项事实 |
| （新）新任务已开始 | “新增任务 · 执行已创建” |

### 8.2 下一步文案

| 状态 | 正文 | 建议指令（复制给 Codex） |
|---|---|---|
| 正式交回未核验 | 报告已交回，请在 Codex 核验后记录结论。 | 核验执行 <run_id> 的报告，并记录 accepted 或 returned 及依据。 |
| 已退回（当前轮） | 原报告已退回。可在 Codex 带着退回原因开启新一轮，或让 Codex 直接完成。 | 按退回原因修正这项任务。 |
| 预检阻止 | Claude 未启动。先处理预检问题，再开启新一轮。 | 检查执行 <run_id> 的预检阻止原因，并给出处理步骤。 |
| 执行者无法继续 | 执行者需要补充材料或澄清规格（沿用现有 action() 的 summary / unresolved 拼接）。 | 根据执行者说明补充材料或澄清规格后，再决定下一步。 |
| Workflow 报告未完整交回 | Workflow 已执行，但完整报告未按证据收齐（附 `workflow_delivery.reason_codes`）。父级摘要不能代替完整报告。 | 核对执行 <run_id> 的 workflow_delivery 原因码与已保留证据，决定是否开启新一轮；不要对本轮记录验收结论。 |
| 失败 / 超时（有内容） | 执行未正常交回，报告内容仅作失败证据。先核对失败原因和文件状态。 | 核对执行 <run_id> 的失败原因与文件状态；不要对本轮记录验收结论。 |
| 失败 / 超时（无内容） | 执行未正常结束，需核对错误和文件状态。 | 核对这项执行失败的原因和文件状态。 |
| 状态未知 | 状态无法确认，不代表已经失败。先在 Codex 核对实际进程和文件；确认之前不要重派，避免同一目录出现两个执行。 | 检查执行 <run_id> 是否仍在运行；核对进程与工作区文件后给出结论，暂不重派。 |
| 停止待确认 | 停止请求不等于已经停止。等待停止回执；确认前不要开启新一轮。 | （无） |
| 已停止 / 启动前已取消 | 本轮已停止。工作区外副作用仍需按任务核对。 | 核对残留文件后，按新的任务方向继续。 |
| 已核对停止 | 旧轮已核对停止，历史结果仍未验收；可在 Codex 指定新的任务方向。 | 核对恢复记录后，按新方向开启新一轮。 |
| 已退出未收齐 [B2] | 执行已结束，但报告不完整。先在 Codex 核对子阶段产物，再决定补收还是开启新一轮。 | 核对执行 <run_id> 的子阶段产物与正式报告：列出已生成和缺失的部分；报告未齐前不要重派。 |
| 报告被截断 [B1] | 只收到了报告前半部分。请在 Codex 读取正式报告产物；不要根据截断内容判断通过或失败。 | 读取执行 <run_id> 的正式报告产物（不是通知文本）；如果产物不存在，说明缺失原因。 |
| 契约未过 [B3] | 报告未通过交付契约校验（按 failures 列出原因）。请在 Codex 退回或重开。 | 按契约校验失败项处理执行 <run_id> 的报告，并决定是否重开。 |

历史轮、当前轮待加载时不显示下一步。

---

## 9. 视觉 Token

### 9.1 中性色

| Token | Light | Dark | 用途 |
|---|---|---|---|
| `--bg-detail` | #FBFAF7 | #1B1C1F | 详情底、顶栏 |
| `--bg-list` | #F4F2EE | #17181B | 列表底、次级面、代码框 |
| `--panel` | #FFFFFF | #232529 | 卡片、输入框 |
| `--line` | #E2DFD8 | #34373C | 分隔线、卡片边 |
| `--line-strong` | #D9D6CE | #43464C | 控件边框、断线虚线边 |
| `--text` | #1C1B19 | #F3F4F6 | 正文、选中 |
| `--text-2` | #3F3D39 | #D5D7DB | 次级正文 |
| `--muted` | #56544F | #AFB3BA | 说明文字 |
| `--muted-2` | #6B6964 | #9A9EA6 | 时间、标签名 |
| `--focus` | #1F4FB8 | #8FB0FF | 焦点环 |
| `--primary-bg / fg` | #1C1B19 / #FFFFFF | #F3F4F6 / #1B1C1F | 主按钮、选中 chip/标签 |

### 9.2 状态色（fg / bg / border）

| tone | Light | Dark |
|---|---|---|
| active | #1F4FB8 / #EAF0FC / #BFD0F3 | #9DB8FF / #1E2A45 / #3A5184 |
| settled | #3F3D39 / #FFFFFF / #CFCBC2 | #D5D7DB / #232529 / #55585E |
| ok | #17704B / #E6F3EC / #B7DBC7 | #7FD3A8 / #16302A / #2F6B50 |
| attention | #9A4B00 / #FCF1E3 / #EDCB9E | #FFC870 / #3A2A10 / #8A6424 |
| bad | #A8271B / #FBEAE8 / #EDBDB7 | #FFB0A6 / #3D1D1A / #8C4A42 |
| unknown | #5E5C57 / #F2F1ED / #A9A59C（dashed） | #C9C6BF / #2A2B2E / #6E6B64（dashed） |
| history | #56544F / #F2F1ED / #CFCBC2 | #AFB3BA / #2A2B2E / #4A4D52 |
| none | #6B6964 / #FFFFFF / #D9D6CE | #AFB3BA / transparent / #43464C |

横幅：断线用 attention，文字 #6E3800（dark #FFE6A6）；恢复用 ok，文字 #0F5537（dark #BFF0D4）；凭证失效同断线。

颜色对仅按数值核对过基础 fg/bg；实际组合（尤其深色、断线态、420px 窄面板）需浏览器渲染检查（§10.2 #24）。

### 9.3 字体与尺寸

- 字体：`-apple-system, BlinkMacSystemFont, "PingFang SC", "Segoe UI", sans-serif`；等宽 `ui-monospace, Menlo, monospace`。设计画布中的 IBM Plex 仅作展示。
- 字号：页面标题 24/600 · 分区 16/600 · 事实状态 16/600 · 正文 14 · 次要 13 · 说明 12 · 标签 11。
- 间距：4 / 8 / 12 / 16 / 20 / 24 / 40。圆角：卡片 10、控件 7、标签 4、chip 15、标志 50%。
- 标志：列表 16px、事实卡 20px、窄布局 22px；符号字号 = 尺寸 × 0.6，700。
- 阴影：仅抽屉 `-12px 0 32px rgba(28,27,25,.10)`（dark `rgba(0,0,0,.4)`）。

---

## 10. 测试与验收

### 10.1 现有测试迁移（保留行为覆盖）

- `tests/test_product_viewer.py` 在 Node VM 中执行真实页面脚本。重写后**允许**改 DOM 选择器与相应文案，**不得**删除以下行为覆盖，需在新页面上重写对应断言：
  - 切轮立即中止旧请求（AbortController）、丢弃过期 epoch 的响应；
  - 事件分页、积压时继续追赶、大量事件从尾部窗口开始读取；
  - maintenance 接口失败或变慢不影响任务详情；
  - 同步时间不冒充活动时间；“没有新活动不等于执行停止”等安全语义。
- `tests/test_mcp_viewer.py`（只读、token、Host/Origin、CSP、POST 405）：**保持原样通过**，不弱化断言。
- 1b 的 SKILL/server 文案改动：同步更新对 presentation/instruction 文本的断言（如有）。
- 当前基线（Codex 已运行）：`test_product_viewer.py` 1 个 unittest 通过，仅证明现有页面基线。

### 10.2 验收场景（回放 / 模拟 fixture，标注来源）

| # | 场景 | 期望 |
|---|---|---|
| 1 | 三个不同 cwd 同时执行 | 一个工作台；“进行中”三项 |
| 2 | 8 个任务 / 14 次尝试 | 8 张卡；多轮卡显示“第 N 轮”；底部“8 个任务 · 14 轮执行（已载入）”；各轮结论不串 |
| 3 | 长报告截断（B1 gate 开 / 关） | 开：“报告被截断”；关：按 status 显示，不出现“截断” |
| 4 | 外层早退（B2 gate 开 / 关；isExited 真 / 假） | 开且已退出：“已退出 · 报告未收齐”；开但未退出：只写“待收集”；关：按 status 显示 |
| 5 | 契约未过（B3 gate 开）：failures 全为 placeholder / 含 identity_mismatch | 前者“缺乏实质内容（占位值）”；后者“未通过报告契约”，只有 placeholder 条目标“占位”；任何情况不显示绿色 |
| 6 | Codex 补齐完成（当前轮） | 核验绿色“Codex 已补齐完成”；原报告折叠“未采纳”；任务进“已收口” |
| 7 | 全局 CLI 2.1.283、本轮 2.1.278 | 本轮执行身份 2.1.278 + “不同”标签；抽屉显示全局；互不覆盖 |
| 8 | cancelling / unknown | “停止待确认”在进行中；“状态未知”在需处理，下一步含“暂不要重派” |
| 9 | 断线 → 恢复 | 断线横幅、数据保留、虚线边、文字不透明；恢复横幅**不含**“连续 / 无遗漏”措辞；页面未发起任何写请求 |
| 10 | 已载入 50 条、`has_more=true` | 计数带“已载入”；无全量数字 |
| 11 | 新增任务在查看其它报告时到达 | 当前详情与焦点不变；提示“新增任务 · 执行已创建”；点[查看]才切换 |
| 12 | 窄布局 420px | 两步式；返回后滚动与焦点恢复；抽屉全屏 |
| 13 | 键盘 | 仅用键盘完成：搜索 → 选任务 → 切轮 → 展开证据 → 复制 → 抽屉开关 |
| 14 | 深色模式 | 文字组合对比 ≥ 4.5:1 |
| 15 | **R1** 列表 `reported` 行无 result/receipt，未点击 | 报告显示“已回报 · 待核验”，不显示“无报告”；首页刷新后已载入的详情不被擦除 |
| 16 | **R1** 列表 `failed` 行未载入详情 | 报告显示“详情未载入”（并预读），不显示“无报告” |
| 17 | **R2** 第 51 行以后的执行中任务 | 进入观察集，终态与后续轮更新可见；超出上限时底部注明 |
| 18 | **R2** 深链旧 run，其 superseded_by 未载入 | 旧 run 以历史轮展示；卡片“当前轮待加载”；补取后指向正确当前轮；期间无下一步 |
| 19 | **R2** 首页新增 3 条后点“加载更早记录” | 去重，无重复卡片，无遗漏 |
| 20 | **R3** failed + structured / failed + structured=null / reported + integrity=complete 未核验 | 分别为“有内容 · 未正常交回”（下一步不建议验收）、“无结构化报告”（有错误入口）、“已回报 · 完整”（中性色） |
| 21 | **R4** 历史 accepted / 历史 returned / 历史 returned+completed_by_codex | 三者各自显示“已验收 / 已退回 / Codex 曾补齐完成 · 已有后续轮”，灰色，不影响当前轮 |
| 22 | **R6** maintenance 失败、单个 artifact 失败、选中 run 400、切轮 AbortError、换 token 后旧响应到达 | 均不进入断线；旧响应不把新连接标成已恢复 |
| 23 | **R7** 新 run 仍在 preflight；预检阻止（claude_started=false）；启动前取消；加载更早记录带来的旧 run | 分别为“已创建 · 预检中”、“预检阻止 · Claude 未启动”、“启动前已取消”、不弹新增提示 |
| 24 | **R8** 浏览器渲染检查 | 明暗主题、断线态、420px 布局、焦点环实际截图核对；屏幕阅读器体验单独注明验证范围 |
| 25 | **R5 宿主集成**（不在页面测试中） | 首个打开 queued 后连续三次 start：主代理只调用一次 open_in_codex；监督席零次；用户显式重开一次且不新增 Claude run；深链是否复用标签页如实记录结果 |

---

## 11. 非目标（不要做）

- 页面内的停止 / 重试 / 重派 / 验收 / 改派按钮或任何写请求。
- 排队、任务组、组取消、同目录并发的状态或控件。
- 完成百分比、估计费用冒充账单、订阅余额。
- 固定端口、删除 token、拼接多个 MCP 实例的数据源、常驻服务。
- 用字符串黑名单判定报告完整性。
- 把 `reported`、“执行已结束”或“报告完整”显示为绿色通过；把内部阶段完成显示为报告已收齐。
- 用透明度弱化含文字的卡片。
- 在原型或文案中宣称宿主标签页复用已验证。
