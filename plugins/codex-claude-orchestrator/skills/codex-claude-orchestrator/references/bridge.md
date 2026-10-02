# Claude bridge 使用

路径中的 `<skill>` 是本技能目录；由 Codex 展开为实际绝对路径。本页描述随插件发布的 Bridge；它要求 Python >=3.11 与本机已安装且可正常认证的 `claude`，沿用本机认证，不读取或复制 token。用户任务经原生 `claude_*` MCP 执行，本页底层 CLI 参数是开发参考，不能当作 MCP 不可用时的真实委派后备。仅更新 Markdown 不会升级已加载的 Bridge。

## 任务包

每轮由协调者写一个 JSON 文件，示意：

```json
{
  "task_id": "example-L1",
  "revision": 1,
  "role": "review",
  "cwd": "/absolute/project",
  "user_request": "请完整审查目标实现与 SPEC.md 的一致性和可维护性。",
  "objective": "按原始要求核对行为、职责与质量，给出来源与核验方法。",
  "review_scope": "full",
  "review_mode": "isolated",
  "requirement_sources": ["/absolute/project/SPEC.md"],
  "constraints": ["只在独立副本开展复现；保持原需求与正式验收；不要把测试名当成行为证据"],
  "acceptance": ["报告缺陷与质量的实际覆盖、问题证据、权衡和未完成项；允许无发现"],
  "owned_files": [],
  "protected_files": ["SPEC.md", "test_example.py"],
  "model": "sonnet",
  "effort": "medium"
}
```

`workspace_kind` 默认为 `git`，保持原 packet 兼容。`baseline_commit` 可选；传入时为明确的完整 commit，当前 HEAD 必须一致，不能猜分支。此字段标识本轮执行输入，不能替代正式 oracle 的批准登记。所有 run 保存真实 HEAD/status/diff 身份。

- `review_scope` 为 `defects`、`quality` 或 `full`，默认 full；它只定义覆盖，不改变权限。新协调派单必须传 `user_request` 保留原话，objective 是执行摘要。旧包缺原话可保留兼容，但不能把 objective 冒充用户原话。
- `review_mode="isolated"` 在独立可写 clone 中运行，支持 Bash、Agent、Skill、Workflow 等审查工具；OS 写保护覆盖原仓库/相关 Git 目录及外部 requirement_sources。它不是网络、凭证或全文件系统隔离，外部 MCP 默认关闭。不支持所需 OS 保护的宿主会明确拒绝，可改用 strict。
- `review_mode="strict"` 只提供读取/搜索，cwd 外只能精确读取/搜索 requirement_sources。旧原始 packet 省略模式保持 strict；带 user_request 的新 MCP Git review 默认 isolated；旧调用缺 user_request 时保持 strict 并记录 legacy_unspecified，显式 isolated 要求非空原话。范围 full 与模式 strict 可以同时使用。
- `implement` 增加 Write/Edit，owned_files 必须是仓内精确相对文件路径，不能用目录/glob；此角色不提供 shell、代理、Workflow 或外部 MCP。
- 兼容入口 `workflow_review` 是保存 Workflow 的狭义只读模式：owned_files 必须为空，不能带 correction；每次都 fresh，不能用 `--resume-from`。它不代表完整的 Dynamic Workflow Review，也不验收多席位策略或实施型 Workflow。
- implement 的第一次运行要求干净 Git 工作区。已有合法脏改动由协调者安排合适候选目录，不得 stash 或覆盖。
- 纠正续跑的工作区、要求源内容与文件范围必须仍匹配上一轮快照，避免沿旧依据继续工作。保持 task/cwd/role/model、baseline、constraints、acceptance 和 owned/protected 范围不变；把局部补充放 objective/correction。这些边界需要变更时重新派 fresh 任务；effort 可以按方法需要调整。
- requirement_sources 是原始约束入口，也是保护对象。protected_files 按当前任务实际列出，不能把所有新测试自动称为正式 oracle。
- MCP Runtime 自行管理状态根下每轮的独立运行目录；在项目 PROGRESS 中引用它，不将运行日志写进被编辑仓库。开发夹具也使用隔离目录，不产生待审工作区的额外脏文件。
- `model` 和 `effort` 必须为非空字符串。模型目录用 `claude_models(cwd)` 从本机 CLI 的 initialize 响应发现（`identity_id` 已退役），返回 `value`、`resolvedModel` 及 effort 信息；只保留模型字段，不输出账号信息，不发送模型提示词。按类别跟随最新时原样传官方 alias；完整模型 ID 按用户显式要求原样传递。类别默认值不是固定版本映射，实际解析由 Claude CLI/提供方/组织/环境配置决定。目录声明、请求值和 provider 实际回报分别记述。

### 可选 findings 与逐项核验

结果仍含 status/summary/evidence/checks/unresolved。`findings` 是可选数组，不要求每轮或每个维度都有问题。category 可为 `defect`、`design`、`maintainability`、`test`、`documentation`；confidence 可为 `reproduced`、`code_confirmed`、`probable`、`subjective`。置信度是报告声明，Codex 仍须核对证据；设计建议不强制运行时触发条件。

`claude_decide` 可带 `finding_decisions`，逐项记录 finding_id、disposition（`accepted`、`downgraded`、`rejected`）和 reason；降级/拒绝必须说明原因。原 finding 与核验失败项保留，不能过滤后变成无发现。接口适用状态与整轮 accepted/returned 规则不变。

### 工作副本生命周期

isolated 的 cwd 是 Runtime 管理的独立 clone；输入来自原仓当前工作树，包括受覆盖的 dirty/untracked 文件，refs/config 不与原仓共享。启动前核对输入和 OS 保护，结束后保存报告与副本变更供 Codex 核验。副本随 run 保留用于局部纠正/resume，returned 后也保留；只有任务已处置且停止事实明确时才按显式清理操作移除。unknown 时不清理仍可能写入的副本。不要推测 ignored 依赖自动复制，也不能把副本上的 commit/test 当作原仓修改或真实设备验收。

### isolated 的完整报告与可选 Workflow

本轮执行 packet 给出 `review_report_path`，固定为副本 `.codex-review/<run_id>.json`；Claude 写完整 structured JSON，Bridge 停止确认后采集到 run 的 `review-report.json`，保存字节数/sha256与采集状态。`claude_result(run_id, artifact="review_report")` 读取完整报告，父级 structured 另保留，不以简报覆盖完整 findings。文件缺失或校验失败是未交付；正常退出或合法父级结果不替代完整报告与真实执行/覆盖核验。

`assets/review-workflow.js` 是可选 codex-full-review 脚本，放入独立副本供审查者使用；参数包含 user_request、review_scope、来源与本轮 report_path。isolated 不要求保存名称/hash 门禁，可自行选择 Agent/Workflow；Bridge 对已观察的每次 Workflow 调用核对完成及结束顺序；Agent 和其他检查的完成、覆盖充分性由 Codex 对完整报告与实际证据核验，不能把启动回执当完成，失败/未覆盖项保留。legacy workflow_review 的 inventory、精确绑定和私有交付要求见下节，不能将它们套到一般 isolated review。

### 普通资料目录

`workspace_kind="artifacts"` 仅支持真实非 Git 目录的 `role="review"`，模式必须 strict。必须给非空 `input_files`（cwd 内精确文件路径）与 `requirement_sources`（精确绝对文件路径，可在 cwd 外）；仅接收 UTF-8 `.md`、`.txt`、`.csv`、`.json`。拒绝符号链接、目录和二进制内容；只可读取清单中的 canonical 文件，不允许目录扫描或 glob 发现其他资料。

该模式拒绝 `baseline_commit`、`protocol_binding`、`workflow_review`、implement 与 resume。输出 `workspace_before.json` / `workspace_after.json`，包含每项来源身份、内容 SHA-256、目录元数据变化摘要和 workspace_digest。未声明内容不被读取；目录摘要也不证明任意工作区外无副作用。

### 可选预算

```json
"budget": {"max_turns": 12, "max_budget_usd": 1.0}
```

数值是本轮显式预算示例，不是默认阈值。正整数 max_turns 和有限正数 max_budget_usd 仅在本机 CLI help 列出对应 flag 时传入；无该 flag 则 blocked（`budget_capability_unavailable`），不忽略预算。结果保留 provider 原始 usage、主代理分项 usage_summary、原始 model_usage 和 provider_enforced 标记。usage_report 将主代理最终回报与含子代理的 CLI 会话累计分开；累计 token 按 modelUsage 的模型分项汇总，费用取 CLI 的 total_cost_usd，不把主代理再加入累计，也不把多个 result 相加。缺少 modelUsage 时整任务 token 未知；续跑累计可能包含此前轮次。费用是客户端估算，不是实际账单。CLI 的 API 计价预算不同于订阅账单或剩余额度。Runtime wall timeout 独立执行。

### 兼容只读 workflow_review：保存 Workflow 的精确绑定

协调者先调用 `claude_saved_workflows(cwd)` 取得 inventory，再把其中一个有效条目的 `name`、绝对 `path` 和 `sha256` 原样放入 `workflow_review` packet；如果该条目定义了调用参数，也可放入精确 JSON `args`。示意：

```json
{
  "role": "workflow_review",
  "owned_files": [],
  "workflow": {
    "name": "workflow_review",
    "path": "/absolute/project/.claude/workflows/workflow_review.js",
    "sha256": "<inventory 返回的 64 位小写 SHA-256>",
    "args": {"scope": "changed-files"}
  }
}
```

inventory 从 `cwd` 向 Git 根枚举 `.claude/workflows`，并读取个人 Claude 配置目录的 `workflows`。项目条目按 Claude 的项目优先级覆盖同名个人条目；同一项目名歧义、符号链接、非普通文件或 hash 不匹配都会拒绝启动。历史 Claude session、历史 run 和详情页记录不是保存 Workflow，也不能用来绕过 inventory。

此兼容入口仅允许 packet 精确绑定的 `Workflow(name[, args])`，不接受 inline script、`scriptPath`、其他 Workflow 或普通 review 作为替代。Workflow 脚本同样纳入 requirement source 身份核对。该模式仍需观察到匹配的 Workflow 工具调用、成功工具结果、完成事件和完整报告产物，才可把 Claude 的结果交给主协调者核验；`reported` 仍不是 `accepted`。完成事件须是同一会话、同一 tool_use_id 的父会话通知（`task_started` 已绑定 task_id 时还须同一 task；带 `task_type` 且不是 `local_workflow` 的 task_started 不绑定）；被采用的结果必须出现在该完成事件之后，之前的结构化结果只算启动阶段的中间结果，即使随后出现完成事件也判为 blocked（见 `workflow_final_after_completion`、`workflow_interim_result_count`）。bridge 只在该子进程环境设置 `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` 为 `--timeout` 的毫秒数（最小 1000，不为 0），避免 `claude -p` 默认 10 分钟空闲上限提前停止 Workflow；外层超时、取消与进程组清理不变。

此兼容入口保留以下交付、结果选择与 hook 复核；它们不限制 isolated review 的一般工具选择：

- **最终报告只取父级 result**：只有期望会话中、无 `parent_tool_use_id` 的 `type=result` 事件可成为最终报告；随后出现的子代理 result 或其他事件不会覆盖它。其他会话的事件照常记为 `session_error`，`result_selection` 记录所选行号与被忽略的子代理/异会话 result 数。
- **结构化事件优先**：同一调用有 `system/task_notification` 时以它为准。旧式父会话文本通知只读其开头的 `<task-notification>` 头部元素（task-id、tool-use-id、output-file、status），遇到 summary/result 等正文即停止；重复头部视为歧义，不跨通知拼字段，也不读报告正文里的标签。文本通知只能证明完成，不能提供报告产物。
- **每个调用一条证据**：`workflow_delivery.invocations` 逐个记录 tool_use_id、确认结果、task_id、终止状态/来源/行号、output 引用与收集结果；既有布尔字段由它派生。终止状态为 failed/stopped 等、同一调用的重复通知内容冲突、通知早于启动确认，都不算完成。
- **完整报告产物**：bridge 为本轮 Claude 子进程单独创建私有临时根，仅通过文档化的 `CLAUDE_CODE_TMPDIR` 传给该子进程，不改账号、配置或登录。只接受 `<根>/claude-<uid>/<cwd 编码>/<本会话>/tasks/<本 task>.output` 这一观察到的布局；逐级拒绝符号链接、非本用户所有、硬链接别名、非普通文件和超过 8 MiB 的文件，读取前后核对文件身份与大小不变，要求 UTF-8 JSON 对象且 `result` 为非空白字符串、对象或数组。进程组确认停止后，原样快照整份信封到 `workflow-output/<n>.envelope.json`、`result` 的文本表示到 `workflow-output/<n>.report.txt`（只读；字符串标记 `representation=exact_text`，对象/数组标记 `representation=json_value` 并用有限 JSON 编码，原始信封始终逐字节保留），记录 SHA-256、字节数、run/session/tool/task/Workflow 绑定与收集状态，再删除该临时根。布局不同时记明确的 unsupported/missing 状态，不猜其他路径；模型不需要也不允许读取这些临时文件。
- **未交付不等于其他失败**：任一绑定调用缺少完成、产物缺失/无效/不匹配，或父级 result 不在全部完成之后，`workflow_delivery.status=not_delivered` 并给出稳定原因码（如 `workflow_invocation_missing`、`workflow_acknowledgement_failed`、`workflow_notification_before_acknowledgement`、`workflow_terminal_pending`、`workflow_terminal_failed`、`workflow_final_before_completion`、`workflow_report_transport_missing`、`workflow_report_not_collected` 加具体收集码）。它只把原本 completed 的 run 改为 blocked，且 `blocked_by=workflow_evidence`（不是 executor）；failed、cancelled、timeout 保持原状态，已取得的产物照样保留为证据。父级 summary 不能代替完整报告。
- **读取完整报告**：`claude_result(run_id, artifact="workflow_report"|"workflow_envelope", index, offset, limit)` 与详情页按 UTF-8 字节偏移分页（每页 4..262144 字节，不切断字符），每页重新核对整份文件的大小和 SHA-256，不一致时不返回内容；沿 `next_offset_bytes` 拼接即可还原报告文本；`representation` 与 `value_type` 区分原始字符串和结构化 JSON 表示，后者可解码后与原始信封的 result 值核对。旧 run 和普通角色没有该记录，读取返回 `not_recorded`。
- **执行前的格式化拒绝**：只有完整无效 JSON 包装、同会话父级唯一调用和随后唯一 error tool_result 共同证明的 `StructuredOutput` 才单列为 `rejected_formatter_tool_uses`，不计作已执行但缺失 hook 的调用；保留字节长、摘要与 stream 行号。已有 hook 记录或 provider 拒绝优先，其他工具、歧义事件、缺少身份或错误证明仍要求审计，最终结构化报告仍独立校验。
- **hook 复核分层**：每次文件类 hook 只复核本轮已冻结 packet 与绑定脚本（仍在有效 workflow 目录、非符号链接、同一字节 hash 与 meta.name），不再枚举无关脚本；派单时与实际 `Workflow` 调用时仍做完整 inventory，因此新出现的同名遮蔽脚本或其他 inventory 问题会拒绝该调用。
- **meta 解析**：inventory 只解析文件开头（可有注释）的第一个 `export const meta = {...}` 纯字面量，支持单行/多行、注释和字符串中的括号；拒绝重复键、计算键、展开、简写、方法、调用/引用、模板插值和非法名称，从不扫描脚本正文找替代名称，也从不执行脚本。

完整报告的收集只证明来源、形状与字节完整，不证明 Workflow 的推理正确或覆盖充分；内容仍由 Codex 核验。若子进程停止无法确认（unknown），临时根保留以免删除仍在写入的文件，`workflow-temp-root.json` 记录其位置。

### 文件边界说明

提示中的 `scope` 由已验证 packet 推出：Git 模式下 cwd 内可用 Read/Glob/Grep；cwd 外只有精确的 requirement source 文件，可用 Read 或把 Grep 的 path 设为该文件；artifacts 模式只列出精确文件；`workflow_review` 另给出唯一允许的 `Workflow` 输入（含精确 args）。搜索这些文件的父目录会被拒绝，拒绝原因列出至多 3 个应改用的精确文件，但不授予父目录、不改写搜索目标、不删除拒绝记录，拒绝仍使本轮 failed。说明只是提示，不能保证模型或已保存 Workflow 的子代理不犯错；保存脚本的子代理提示应自行传递这些边界。作者须把 cwd 与 external_exact_files 清单传入各子代理提示；缺少边界传递时先报告风险，再按授权处理脚本。

历史记录：早期版本曾在 Claude CLI 2.1.276、2.1.277 和 2.1.278 上用真实 MCP 只读夹具观察过该模式：放行结构校验后的输出工具，并以同一 session、同一 Workflow tool_use_id 的 system/task_notification completed 作为完成证据。这不是当前版本的准入名单；其他版本同样以每轮观察到的完成证据为准。启动回执不算完成。仅涉及该受限模式，不覆盖任意脚本或完整审查策略。

## MCP 入口与底层开发参数

正常委派使用 `claude_start`、`claude_status`、`claude_cancel`、`claude_recovery` 等 MCP 工具。下列 CLI 入口用于开发接口说明和有界诊断/离线夹具；独立 Bridge 仍保留这些参数，但不等同受监督 Runtime 的外置启动交接，也不是用户任务的旁路执行方案。


```text
python3 <skill>/scripts/bridge.py doctor --cwd /absolute/project
python3 <skill>/scripts/bridge.py doctor --cwd /absolute/project --verify --model sonnet --timeout 60
python3 <skill>/scripts/bridge.py run --packet /absolute/packet.json --run-dir /absolute/runs/001 --timeout 300
python3 <skill>/scripts/bridge.py status --run-dir /absolute/runs/001
python3 <skill>/scripts/bridge.py cancel --run-dir /absolute/runs/001 --reason "用户调整该部分方向"
```

### 安装与认证预检

### 本机 CLI 准入

桥接器只使用用户本机的 Claude CLI：`CLAUDE_BIN`，其次插件 settings 的 `claude_bin`，最后 MCP 进程 PATH 中的 `claude`（保留 nvm/npm shim 的原路径，让同目录的 `node` 可用）。旧版本插件留下的受管选择、私有副本和资格回执只作历史保留，不参与选择、不阻断派单，也不会被删除。插件不下载、安装、更新、回退、复制或切换 CLI，不修改 shell、PATH 持久配置、CLI 自动更新策略、登录或账号。

每轮按任务检查：`--help` 必须以完整选项形式列出所需 flag（`-p` 不能由 `--print` 代替；续跑另需 `--resume`），并且 `claude auth status --json` 通过。版本号只写入诊断，不作白名单。缺项时报告具体 flag，状态为 `cli_incompatible`。help 只是声明的语法（`compatibility.evidence=cli_help_syntax`、`behavior_verified=false`），行为由每轮的 hook 自检、hook 覆盖、实际工具集核对（strict、implement 与兼容 workflow_review 按各自工具集核对）、会话身份和结构化结果校验确认。

`claude_cli_status(cwd, job_id?)` 分别报告本机 CLI、安装、登录、最近真实 provider 调用、历史受管记录和旧验证任务。`claude_cli_update` 已退役：除 `cancel`（请求停止旧版本留下的验证任务）外只返回说明，不改变任何文件或选择。

创建 run 时固定可执行文件的路径解析与 SHA-256，启动前再核对，变化即失败且不回退。续跑要求本机 CLI 与上一轮仍是同一文件；用户升级或更换后返回 `resume_cli_identity_changed`，旧版本插件的私有身份返回 `resume_cli_identity_retired`，都应改用 fresh 新一轮。

`doctor` 检查实际可执行路径、版本、所需参数与官方 `claude auth status --json`。`--version` 或 `--help` 失败时，报告中的 `cli.version_probe` / `cli.help_probe` 记录退出码、信号或超时类型，以及限长、脱敏后的输出摘要。`--cwd` 应与目标任务相同，使项目配置与认证来源一致；未指定时使用当前目录。API key/云平台认证不强制要求存在账号邮箱。

| 状态 | 含义与处理 |
|---|---|
| `cli_not_found` / `cli_not_executable` | CLI 未找到或不可执行。提示安装/修复或 PATH；不自动安装 |
| `cli_unavailable` | 版本或 help 探测失败；查看 `version_probe`/`help_probe` 的退出码或超时，核对本机权限与安装 |
| `cli_incompatible` | 本机 CLI 的 help 未列出本任务所需 flag；按报告的具体 flag 由用户升级或更换自己的 CLI |
| `not_logged_in` | 官方 CLI 明确回报未登录。用户运行 `claude auth login`，或配置自己选择的 API/云平台认证 |
| `auth_check_failed` | 状态命令异常、超时或无法解析。不能断言凭证过期，也不能当已登录 |
| `local_checks_passed` | CLI 回报已配置认证，远端有效性尚未验证 |
| `verified` | `--verify` 的实际请求成功；仅证明当前认证来源与指定模型在本次检查时可用 |
| `authentication_failed` | 在线请求明确拒绝认证；重新认证后再检查 |
| `quota_or_rate_limited` / `network_error` / `access_denied` | 额度/网络/访问权限问题，不能仅据此要求重新登录 |
| `verification_timeout` / `verification_failed` | 在线探测未能证明可用；保留未知，不盲目重试 |

若超时同时回报 `cleanup_status: unconfirmed`，表示宿主未允许确认探测进程已结束；先核对进程和权限，不重派。超时原因与清理状态分别保留，不能把这种情况说成凭证无效。

`--verify` 禁用 agent 工具、MCP 和 slash commands，最多一个 agent turn，不保存可续跑会话；保留宿主权限和原有 hook，会使用少量模型额度。CLI 可按自身正常机制刷新认证；桥不直接读取或保存密钥。输出仅含认证方式、provider、脱敏账号等状态字段，不保存原始认证响应/在线探测输出；不把本地状态、一次在线成功、额度与会话持久化混为一个承诺。

每次 `run` 在启动任务请求前自动执行本地预检。失败写入 `environment.json` 与 blocked 回执，返回非零，不启动 Claude 任务。确认预检失败回执并处理问题后，经原生 MCP 派发新的 fresh 轮次；unknown 仍须先核对恢复条件。已成功的在线检查只作当前会话的证据，不缓存为长期有效的凭证证明。

示例的 300 秒只是小夹具的运行上限；实际任务通过 `claude_start(timeout_seconds=...)` 按范围设置，不是质量或验收阈值。用户任务的 Bridge 由 MCP Runtime 监督，通过原 run_id 等待和核验；启动记录不等于完成。

局部纠正写新 packet、递增 revision、增加：

```json
"correction": {
  "finding_id": "F-01",
  "kind": "local",
  "reason": "补查未覆盖的端点相接场景，原始规则仍以 SPEC 为准。",
  "attempt": 1
}
```

```text
python3 <skill>/scripts/bridge.py run --packet /absolute/packet-002.json --run-dir /absolute/runs/002 --resume-from /absolute/runs/001 --timeout 300
```

只对已完成且身份匹配的会话使用 resume，结构/方向改变使用 fresh（不传 resume-from）。需要先处理旧执行和残留改动；新 revision 本身不构成需求变更授权。若涉及新决定，保存原有明确授权或取得必要决定后继续。

成功拿到结果不代表 Claude 已成功持久化会话。若 `--resume` 返回 `No conversation found`，该续跑应记失败；检查本机会话保存及宿主权限。不要猜另一个 session ID，也不要把新会话当成原会话续接。沙箱限制应走宿主的正常权限升级流程，随后经原生 MCP 用新 revision 重试；保留原失败证据。

## 受监督启动记录

Runtime 先验证执行模块身份，再在 Bridge Popen 前写执行目录之外的 `runtime_pre_spawn` 回执，并通过 `--startup-nonce` 交接。Bridge 核对 nonce、代码身份、输入和 lane 后接管，再记录 `pre_dispatch`、`launch_intent`、`executing` 与 `terminal`。这些是 Runtime/Bridge 产生的证据，不是协调者可手填的 packet 字段。

当前行为：hook 策略预检、guard 自检、CLI 描述符与代码身份核对之后、发布 launch intent 之前，再次检查 run_dir 与 Runtime 两处取消标记；此时已有取消即按“启动前取消”记录（`child_started=false`），不启动 Claude。Popen 成功后立即持久化 PID/PGID（`child.json` 与 lifecycle，进程身份随后补写），之后在同一个非阻塞监督循环里分块写入提示词并读取 stdout；执行时限从启动起算，提示词尚未送完时超时与取消同样生效。提示词未完整送达（子进程提前关闭 stdin 或退出）记 `prompt_delivery.state=incomplete` 并使 run failed；取消、超时保持原状态。启动后才到达的取消走原有进程组清理。

Bridge 从有效的 Runtime 状态目录启动；Claude 仍使用任务 cwd。Runtime 的 `bridge_spawn_cwd` 和诊断的 `bridge_startup` 可分别检查启动位置与代码身份。`doctor` 仅在未指定 `--cwd` 时解析当前目录；父目录已删除时，显式 cwd 和其他子命令不因无关的当前目录求值提前崩溃。

## 阅读回执

- `packet.json`：本轮输入快照。
- `environment.json`：派单前的安装/认证检查，账号脱敏，不含原始凭证。
- `stream.jsonl`、`stderr`：实际 provider 输出和诊断；不把原始大日志全部塞回协调者上下文。
- `workspace_before.json`、`workspace_after.json`：通用输入身份与 workspace_digest；Git 模式同时保留 `git_before.json`、`git_after.json`。`requirements.json` 保存要求内容身份。
- `plugin-identity.json`：每轮 bridge 创建 run-dir 后立即冻结、之后不再改写的插件身份，由 `plugin_identity.py` 按自身文件位置（不是被审仓 cwd）解析。含插件 `plugin_version`、`bridge_contract_id`、来源 `source` 和 `code_digest`。`source.kind=git` 时给 HEAD revision，`state` 为 clean / dirty / unknown（范围是插件根目录，dirty 时附至多 20 个路径；git status 失败则 unknown，不推断）；`release_manifest`（ZIP 分发的 RELEASE-MANIFEST.json）只作来源说明：`provenance_only=true`、`verification=not_performed`、`state=unknown`，不说明当前文件等于当时构建的内容；既非 Git 也无 manifest 时为 `kind=unknown`、revision 为空。`code_digest` 是插件根下可分发后缀文件的路径 + 字节 SHA-256（`scope` 字段写明范围，含被 Git 忽略的同后缀文件），不能用 revision 代替。
- `result.json`：provider 结束状态、实际 session/model、结构化内容、权限拒绝，以及同一份 `plugin_identity`。最终报告与 run 成败分开解析：失败、取消、超时的 run 只要有最终报告，仍保留 `structured`（或 `result_validation_error`），不会因此改变失败状态；`report_evidence` 给出 `state`（structured / validation_error / absent）、模型自述的 `claimed_status`、`run_status` 和 `accepted=false`。`permission_denials` 只保存实际被拒绝的条目（工具、tool_use_id、原因、路径类输入，长度有界），不嵌套整条 result、报告或 usage，原始事件仍在 `stream.jsonl`；还包含本 run `activity.jsonl` 中 PreToolUse hook 记录的拒绝（`source: bridge_pretooluse_hook`，含工具、tool_use_id、原因及是否出现在父级 stream）；Workflow 子代理的调用可能不进父级 stream，任何一条 hook 拒绝都会使 run 失败（`hook_denial_error`）。`hook_guard_coverage` 中，与 provider tool_use 的 id 和工具名都匹配的 allowed、denied 事件都算已审计，分别列出 `allowed_*`、`denied_*` 与 `missing_tool_use_ids`；未知 status 或工具名不符的事件不计。denied 不放宽 guard，仍使 run failed。结构化报告的形状规则是 summary 非空白、evidence/checks/unresolved 中没有空白项（同一规则也写进 `--json-schema`）；简短的“无发现”报告合法，内容是否充分仍由 Codex 判断。另记 `prompt_delivery`（提示词字节数、已写字节、complete/incomplete）、`result_selection`（最终报告的选取规则与被忽略的 result 计数），`workflow_review` 另有 `workflow_delivery`、`workflow_error_codes`；非兼容 Workflow 入口的交付证据按实际记录读取，不补造旧字段。
- `receipt.json`、`state.json`：运行结果，receipt 同样直接带 `plugin_identity`（预检 blocked、取消、unknown、bridge 失败回执也带；旧 run 没有该字段，读取方按缺失处理，冻结文件不可读时写 `status=unavailable` 而不掩盖原运行错误）。`reported` 需要主协调者核验，不是 accepted；`claude_decide` 仅适用于未被 superseded 的 `reported` run，其他终态由 Codex 在既有 PROGRESS 独立记录处置。

structured 内容中的 `checks` 是 Claude 自报。isolated 可运行命令，须核对实际公开工具/输出与结果；strict/implement 没有 shell，构建/测试由 Codex 执行和登记。

出现 unknown、cancelled、timeout、failed：先核真实进程与改动，不自动重派。cancel 先提交停止请求，待原进程终止与工作区核对后才交接。取消/超时留下的输出可以诊断，不能验收。

## unknown 的恢复

通过 MCP `claude_recovery(run_id)` 查看恢复条件、进程组、当前工作区快照与 digest。主 Codex 核查文件与真实进程证据后调用 `claude_reconcile(run_id, reason, evidence, expected_workspace_digest)`；该工具在同一 worktree lane 锁内重新检查，任一进程存活、身份不明、权限不足或快照变化均不能恢复。

执行目录存在时，成功恢复沿用目录内 `reconciliation.json`。0.6.1 新协议记录若在整个执行目录尚未生成时失败，只有 nonce/代码/lane、外置 packet 和 CLI 身份、停止事实及工作区均可核对，才允许把恢复回执写到 `<state_root>/recovery-receipts/<run_id>.json`；不创建假的执行目录，也不追加伪造的运行活动。目录存在但输入副本缺失或不符时，不使用外置 packet 兜底。

先持久化恢复证据，再提交 registry，最后仅清理本轮匹配的 marker。外置回执已经写入而 registry 提交失败时，重试必须匹配同一事实，不能覆写第一次原因与证据。缺少 child.json 本身不证明未启动；损坏绑定、launch_intent 启动不确定或历史证据不足时仍阻断。

恢复只解除已核验占用，不把旧 unknown 改为 accepted/reported。后续必须 fresh 且新 revision，不自动重试。保留旧回执与恢复证据，不通过人工删锁或 marker 绕过判断。

当前行为：已经以可信终态（reported/blocked/failed/cancelled/timeout）结束的 run，若清理自己的 lane marker 时遇到瞬时 I/O 或权限错误，终态保持不变，registry 记录 `admission_cleanup.state=pending` 与错误，不再另发 unknown marker，也不为套用 reconcile 而改成 unknown。同一 worktree 的下一次派单或 Runtime 重启会在独占 lane 下重试：只有 packet/CLI 绑定、lifecycle 终态仍可信且 bridge 与 Claude 进程组确认停止时，才删除该 run 自己的 marker 并记 `completed`；其他 run 或格式异常的 marker、证明缺失或进程组仍在时继续阻断。`claude_recovery` 对这类 run 仍报告 “run is not unknown”，并附 `admission_cleanup` 与说明。

bridge 在启动 Claude 之前，会先在系统临时目录的 `codex-claude-cwd-unknown/` 下发布本 run 的 launch intent marker，记录原 cwd、lane、run_id、run_dir 和 lifecycle 文件位置。只有 Claude 进程组确认停止或确认从未启动、且回执已写入后，bridge 才删除这个 marker。如果 Runtime 与 bridge 都已崩溃，marker 会继续阻止其他状态目录和独立 bridge 在同一 worktree 派发。这时到 marker 记录的原状态目录执行 `claude_recovery`/`claude_reconcile`；独立 bridge 的 run 需人工核实进程和工作区。marker 检查在 lane 锁内进行：lane 仍被占用时报告 active run；lane 空闲而 marker 仍在时，新 run-dir 记录 failed 回执，Claude 不会启动。

## 已知边界

工具名单与 PreToolUse hook 是工具入口约束，不能代替 OS 沙箱，也不保证任意 hook、外部进程或脱离进程组的后台任务都已隔离/停止。脚本不会为了执行而关闭原有 hook、绕过宿主沙箱或复制凭证。

插件 Runtime 与随附 bridge 共用按 Git worktree（真实 toplevel）计算的排他锁与 unknown marker，同一 worktree 的根目录与各子目录跨 task 也不能同时监督执行，linked worktree 与 artifacts 目录各自独立；未接入插件的外部工具写入仍由 Codex 协调。正式判据保护按原任务要求另外核对；本版不是 v2.9 四层保护的自动部署器。

Git 快照覆盖 tracked 与非忽略的 untracked 文件；显式 owned/protected 文件另作内容核对。未列出的 ignored 文件和工作区外的副作用不在 Git 差异证据覆盖内，不能据此声称整个文件系统无变化。工具约束的效果与实际 Git/显式文件证据分开报告。

strict review 与 implement 不调用 Workflow；兼容 workflow_review 只调用 hash 绑定的已保存只读脚本。isolated review 可以选择 Workflow，工具可用不证明实际调用或完成，也不自动验收某种多席策略。外部 MCP 默认关闭，插件不自动建 PR、发布或合并；完整审查验收见 runtime-design.md。

### 协调内容快照

Runtime 在 packet 外保存 `content-binding.json`，result/receipt 记录摘要；MCP 的 `claude_result(artifact="content_binding")` 可读取。新 fresh 仅固定随插件发布的 bundled 内容；历史内容可按原 digest 通过 `claude_content_read` 阅读。resume 保留原快照并核对当前代码兼容性。在线 check/review/switch 已退役，不下载或激活内容；expected_content_digest 可用于核对已读的固定版本。内容参考不改 formal protocol_binding、project_workflow 或用户验收。
