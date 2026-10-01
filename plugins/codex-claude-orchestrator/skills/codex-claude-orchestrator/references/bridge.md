# Claude bridge 使用

路径中的 `<skill>` 是本技能目录；由 Codex 展开为实际绝对路径。本页描述当前 0.6.1 的 Bridge；它要求 Python >=3.11 与本机已安装且可正常认证的 `claude`，沿用本机认证，不读取或复制 token。用户任务经原生 `claude_*` MCP 执行，本页底层 CLI 参数是开发参考，不能当作 MCP 不可用时的真实委派后备。仅更新 Markdown 不会升级已加载的 Bridge。

## 任务包

每轮由协调者写一个 JSON 文件，示意：

```json
{
  "task_id": "example-L1",
  "revision": 1,
  "role": "review",
  "cwd": "/absolute/project",
  "objective": "对照 SPEC.md 审查目标实现，给出具体反例和代码证据。",
  "requirement_sources": ["/absolute/project/SPEC.md"],
  "constraints": ["只读；不改文件；不运行构建；不要把测试名当成行为证据"],
  "acceptance": ["每个成立的问题有文件位置、触发输入和违反的规格"],
  "owned_files": [],
  "protected_files": ["SPEC.md", "test_example.py"],
  "model": "sonnet",
  "effort": "medium"
}
```

`workspace_kind` 默认为 `git`，保持原 packet 兼容。`baseline_commit` 可选；传入时为明确的完整 commit，当前 HEAD 必须一致，不能猜分支。此字段标识本轮执行输入，不能替代正式 oracle 的批准登记。所有 run 保存真实 HEAD/status/diff 身份。

- `review` 仅文件读取和搜索；`implement` 增加 Write/Edit，owned_files 必须是仓内精确相对文件路径，不能用目录/glob。
- `workflow_review` 是保存 Workflow 的狭义只读模式：owned_files 必须为空，不能带 correction；每次都 fresh，不能用 `--resume-from`。它不代表完整的 Dynamic Workflow Review，也不验收多席位策略或实施型 Workflow。
- implement 的第一次运行要求干净 Git 工作区。已有合法脏改动由协调者安排合适候选目录，不得 stash 或覆盖。
- 纠正续跑的工作区、要求源内容与文件范围必须仍匹配上一轮快照，避免沿旧依据继续工作。保持 task/cwd/role/model、baseline、constraints、acceptance 和 owned/protected 范围不变；把局部补充放 objective/correction。这些边界需要变更时重新派 fresh 任务；effort 可以按方法需要调整。
- requirement_sources 是原始约束入口，也是保护对象。protected_files 按当前任务实际列出，不能把所有新测试自动称为正式 oracle。
- MCP Runtime 自行管理状态根下每轮的独立运行目录；在项目 PROGRESS 中引用它，不将运行日志写进被编辑仓库。开发夹具也使用隔离目录，不产生待审工作区的额外脏文件。
- `model` 和 `effort` 必须为非空字符串。模型目录用 `claude_models(cwd)` 从本机 CLI 的 initialize 响应发现（`identity_id` 已退役），返回 `value`、`resolvedModel` 及 effort 信息；只保留模型字段，不输出账号信息，不发送模型提示词。按类别跟随最新时原样传官方 alias；完整模型 ID 按用户显式要求原样传递。类别默认值不是固定版本映射，实际解析由 Claude CLI/提供方/组织/环境配置决定。目录声明、请求值和 provider 实际回报分别记述。

### 普通资料目录

`workspace_kind="artifacts"` 仅支持真实非 Git 目录的 `role="review"`。必须给非空 `input_files`（cwd 内精确文件路径）与 `requirement_sources`（精确绝对文件路径，可在 cwd 外）；仅接收 UTF-8 `.md`、`.txt`、`.csv`、`.json`。拒绝符号链接、目录和二进制内容；只可读取清单中的 canonical 文件，不允许目录扫描或 glob 发现其他资料。

该模式拒绝 `baseline_commit`、`protocol_binding`、`workflow_review`、implement 与 resume。输出 `workspace_before.json` / `workspace_after.json`，包含每项来源身份、内容 SHA-256、目录元数据变化摘要和 workspace_digest。未声明内容不被读取；目录摘要也不证明任意工作区外无副作用。

### 可选预算

```json
"budget": {"max_turns": 12, "max_budget_usd": 1.0}
```

数值是本轮显式预算示例，不是默认阈值。正整数 max_turns 和有限正数 max_budget_usd 仅在本机 CLI help 列出对应 flag 时传入；无该 flag 则 blocked（`budget_capability_unavailable`），不忽略预算。结果保留 provider 原始 usage、主代理分项 usage_summary、原始 model_usage 和 provider_enforced 标记。usage_report 将主代理最终回报与含子代理的 CLI 会话累计分开；累计 token 按 modelUsage 的模型分项汇总，费用取 CLI 的 total_cost_usd，不把主代理再加入累计，也不把多个 result 相加。缺少 modelUsage 时整任务 token 未知；续跑累计可能包含此前轮次。费用是客户端估算，不是实际账单。CLI 的 API 计价预算不同于订阅账单或剩余额度。Runtime wall timeout 独立执行。

### 保存 Workflow 的精确绑定

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

bridge 仅允许 packet 精确绑定的 `Workflow(name[, args])`，不接受 inline script、`scriptPath`、其他 Workflow 或普通 review 作为替代。Workflow 脚本同样纳入 requirement source 身份核对。该模式仍需观察到匹配的 Workflow 工具调用、成功工具结果及完成事件，才可把 Claude 的结果交给主协调者核验；`reported` 仍不是 `accepted`。完成事件须是同一会话、同一 tool_use_id 的父会话通知（`task_started` 已绑定 task_id 时还须同一 task）；被采用的结果必须出现在该完成事件之后，之前的结构化结果只算启动阶段的中间结果，即使随后出现完成事件也判为 blocked（见 `workflow_final_after_completion`、`workflow_interim_result_count`）。bridge 只在该子进程环境设置 `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` 为 `--timeout` 的毫秒数（最小 1000，不为 0），避免 `claude -p` 默认 10 分钟空闲上限提前停止 Workflow；外层超时、取消与进程组清理不变。

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

每轮按任务检查：`--help` 必须以完整选项形式列出所需 flag（`-p` 不能由 `--print` 代替；续跑另需 `--resume`），并且 `claude auth status --json` 通过。版本号只写入诊断，不作白名单。缺项时报告具体 flag，状态为 `cli_incompatible`。help 只是声明的语法（`compatibility.evidence=cli_help_syntax`、`behavior_verified=false`），行为由每轮的 hook 自检、hook 覆盖、实际工具集核对（普通角色出现 `--tools` 以外的工具即 `tool_policy_error` 失败）、会话身份和结构化结果校验确认。

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

## 0.6.1 受监督启动记录

Runtime 先验证执行模块身份，再在 Bridge Popen 前写执行目录之外的 `runtime_pre_spawn` 回执，并通过 `--startup-nonce` 交接。Bridge 核对 nonce、代码身份、输入和 lane 后接管，再记录 `pre_dispatch`、`launch_intent`、`executing` 与 `terminal`。这些是 Runtime/Bridge 产生的证据，不是协调者可手填的 packet 字段。

Bridge 从有效的 Runtime 状态目录启动；Claude 仍使用任务 cwd。Runtime 的 `bridge_spawn_cwd` 和诊断的 `bridge_startup` 可分别检查启动位置与代码身份。`doctor` 仅在未指定 `--cwd` 时解析当前目录；父目录已删除时，显式 cwd 和其他子命令不因无关的当前目录求值提前崩溃。

## 阅读回执

- `packet.json`：本轮输入快照。
- `environment.json`：派单前的安装/认证检查，账号脱敏，不含原始凭证。
- `stream.jsonl`、`stderr`：实际 provider 输出和诊断；不把原始大日志全部塞回协调者上下文。
- `workspace_before.json`、`workspace_after.json`：通用输入身份与 workspace_digest；Git 模式同时保留 `git_before.json`、`git_after.json`。`requirements.json` 保存要求内容身份。
- `plugin-identity.json`：每轮 bridge 创建 run-dir 后立即冻结、之后不再改写的插件身份，由 `plugin_identity.py` 按自身文件位置（不是被审仓 cwd）解析。含插件 `plugin_version`、`bridge_contract_id`、来源 `source` 和 `code_digest`。`source.kind=git` 时给 HEAD revision，`state` 为 clean / dirty / unknown（范围是插件根目录，dirty 时附至多 20 个路径；git status 失败则 unknown，不推断）；`release_manifest`（ZIP 分发的 RELEASE-MANIFEST.json）只作来源说明：`provenance_only=true`、`verification=not_performed`、`state=unknown`，不说明当前文件等于当时构建的内容；既非 Git 也无 manifest 时为 `kind=unknown`、revision 为空。`code_digest` 是插件根下可分发后缀文件的路径 + 字节 SHA-256（`scope` 字段写明范围，含被 Git 忽略的同后缀文件），不能用 revision 代替。
- `result.json`：provider 结束状态、实际 session/model、结构化内容、权限拒绝，以及同一份 `plugin_identity`。最终报告与 run 成败分开解析：失败、取消、超时的 run 只要有最终报告，仍保留 `structured`（或 `result_validation_error`），不会因此改变失败状态；`report_evidence` 给出 `state`（structured / validation_error / absent）、模型自述的 `claimed_status`、`run_status` 和 `accepted=false`。`permission_denials` 只保存实际被拒绝的条目（工具、tool_use_id、原因、路径类输入，长度有界），不嵌套整条 result、报告或 usage，原始事件仍在 `stream.jsonl`；还包含本 run `activity.jsonl` 中 PreToolUse hook 记录的拒绝（`source: bridge_pretooluse_hook`，含工具、tool_use_id、原因及是否出现在父级 stream）；Workflow 子代理的调用可能不进父级 stream，任何一条 hook 拒绝都会使 run 失败（`hook_denial_error`）。`hook_guard_coverage` 中，与 provider tool_use 的 id 和工具名都匹配的 allowed、denied 事件都算已审计，分别列出 `allowed_*`、`denied_*` 与 `missing_tool_use_ids`；未知 status 或工具名不符的事件不计。denied 不放宽 guard，仍使 run failed。
- `receipt.json`、`state.json`：运行结果，receipt 同样直接带 `plugin_identity`（预检 blocked、取消、unknown、bridge 失败回执也带；旧 run 没有该字段，读取方按缺失处理，冻结文件不可读时写 `status=unavailable` 而不掩盖原运行错误）。`reported` 需要主协调者核验，不是 accepted；`claude_decide` 仅适用于未被 superseded 的 `reported` run，其他终态由 Codex 在既有 PROGRESS 独立记录处置。

structured 内容中的 `checks` 是 Claude 自报；本工具集没有 shell，因此不能凭自报说真实构建/测试已运行。由 Codex 执行和登记实际检查。

出现 unknown、cancelled、timeout、failed：先核真实进程与改动，不自动重派。cancel 先提交停止请求，待原进程终止与工作区核对后才交接。取消/超时留下的输出可以诊断，不能验收。

## unknown 的恢复

通过 MCP `claude_recovery(run_id)` 查看恢复条件、进程组、当前工作区快照与 digest。主 Codex 核查文件与真实进程证据后调用 `claude_reconcile(run_id, reason, evidence, expected_workspace_digest)`；该工具在同一 worktree lane 锁内重新检查，任一进程存活、身份不明、权限不足或快照变化均不能恢复。

执行目录存在时，成功恢复沿用目录内 `reconciliation.json`。0.6.1 新协议记录若在整个执行目录尚未生成时失败，只有 nonce/代码/lane、外置 packet 和 CLI 身份、停止事实及工作区均可核对，才允许把恢复回执写到 `<state_root>/recovery-receipts/<run_id>.json`；不创建假的执行目录，也不追加伪造的运行活动。目录存在但输入副本缺失或不符时，不使用外置 packet 兜底。

先持久化恢复证据，再提交 registry，最后仅清理本轮匹配的 marker。外置回执已经写入而 registry 提交失败时，重试必须匹配同一事实，不能覆写第一次原因与证据。缺少 child.json 本身不证明未启动；损坏绑定、launch_intent 启动不确定或历史证据不足时仍阻断。

恢复只解除已核验占用，不把旧 unknown 改为 accepted/reported。后续必须 fresh 且新 revision，不自动重试。保留旧回执与恢复证据，不通过人工删锁或 marker 绕过判断。

bridge 在启动 Claude 之前，会先在系统临时目录的 `codex-claude-cwd-unknown/` 下发布本 run 的 launch intent marker，记录原 cwd、lane、run_id、run_dir 和 lifecycle 文件位置。只有 Claude 进程组确认停止或确认从未启动、且回执已写入后，bridge 才删除这个 marker。如果 Runtime 与 bridge 都已崩溃，marker 会继续阻止其他状态目录和独立 bridge 在同一 worktree 派发。这时到 marker 记录的原状态目录执行 `claude_recovery`/`claude_reconcile`；独立 bridge 的 run 需人工核实进程和工作区。marker 检查在 lane 锁内进行：lane 仍被占用时报告 active run；lane 空闲而 marker 仍在时，新 run-dir 记录 failed 回执，Claude 不会启动。

## 已知边界

工具名单与 PreToolUse hook 是工具入口约束，不能代替 OS 沙箱，也不保证任意 hook、外部进程或脱离进程组的后台任务都已隔离/停止。脚本不会为了执行而关闭原有 hook、绕过宿主沙箱或复制凭证。

插件 Runtime 与随附 bridge 共用按 Git worktree（真实 toplevel）计算的排他锁与 unknown marker，同一 worktree 的根目录与各子目录跨 task 也不能同时监督执行，linked worktree 与 artifacts 目录各自独立；未接入插件的外部工具写入仍由 Codex 协调。正式判据保护按原任务要求另外核对；本版不是 v2.9 四层保护的自动部署器。

Git 快照覆盖 tracked 与非忽略的 untracked 文件；显式 owned/protected 文件另作内容核对。未列出的 ignored 文件和工作区外的副作用不在 Git 差异证据覆盖内，不能据此声称整个文件系统无变化。工具约束的效果与实际 Git/显式文件证据分开报告。

普通 `review` 与 `implement` 不调用 Claude Workflow；只有上述 hash 绑定、fresh-only 的 `workflow_review` 可调用一个已保存的只读 Workflow。bridge 不调用外部连接器，也不自动建 PR、发布或合并。完整 Dynamic Workflow Review 的接入边界见 runtime-design.md。

### 协调内容快照

Runtime 在 packet 外保存 `content-binding.json`，result/receipt 记录摘要；MCP 的 `claude_result(artifact="content_binding")` 可读取。fresh 使用 `claude_content_read` 所选 digest，派单传 `expected_content_digest` 防止并发切换。resume 保留原快照；内容参考不改 formal protocol_binding、project_workflow 或用户验收。
