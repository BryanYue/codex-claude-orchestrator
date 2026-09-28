# Claude bridge 使用

路径中的 `<skill>` 是本技能目录；由 Codex 展开为实际绝对路径。桥接器只依赖 Python 3 标准库，要求本机已安装且可正常认证的 `claude`。它沿用本机认证，不读取或复制 token。

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
- 运行目录放项目任务记录目录中的独立运行区，或者授权的本地任务目录；不要放进当前被编辑的仓而产生未知脏文件。
- `model` 和 `effort` 必须为非空字符串。模型目录用 `claude_models(cwd, identity_id?)` 从当前 CLI 的 initialize 响应发现，返回 `value`、`resolvedModel` 及 effort 信息；只保留模型字段，不输出账号信息，不发送模型提示词。按类别跟随最新时原样传官方 alias；完整模型 ID 按用户显式要求原样传递。类别默认值不是固定版本映射，实际解析由 Claude CLI/提供方/组织/环境配置决定。目录声明、请求值和 provider 实际回报分别记述。

### 普通资料目录

`workspace_kind="artifacts"` 仅支持真实非 Git 目录的 `role="review"`。必须给非空 `input_files`（cwd 内精确文件路径）与 `requirement_sources`（精确绝对文件路径，可在 cwd 外）；仅接收 UTF-8 `.md`、`.txt`、`.csv`、`.json`。拒绝符号链接、目录和二进制内容；只可读取清单中的 canonical 文件，不允许目录扫描或 glob 发现其他资料。

该模式拒绝 `baseline_commit`、`protocol_binding`、`workflow_review`、implement 与 resume。输出 `workspace_before.json` / `workspace_after.json`，包含每项来源身份、内容 SHA-256、目录元数据变化摘要和 workspace_digest。未声明内容不被读取；目录摘要也不证明任意工作区外无副作用。

### 可选预算

```json
"budget": {"max_turns": 12, "max_budget_usd": 1.0}
```

数值是本轮显式预算示例，不是默认阈值。正整数 max_turns 和有限正数 max_budget_usd 仅在 profile 与 CLI help 均确认相应能力时传入；无该能力则 blocked，不忽略预算。结果保留 provider 原始 usage、分项 usage_summary 和 provider_enforced 标记，不把 cache 项重复加进总数。CLI 的 API 计价预算不同于订阅账单或剩余额度。Runtime wall timeout 独立执行。

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

bridge 仅允许 packet 精确绑定的 `Workflow(name[, args])`，不接受 inline script、`scriptPath`、其他 Workflow 或普通 review 作为替代。Workflow 脚本同样纳入 requirement source 身份核对。该模式仍需观察到匹配的 Workflow 工具调用、成功工具结果及完成事件，才可把 Claude 的结果交给主协调者核验；`reported` 仍不是 `accepted`。

已通过 Claude CLI 2.1.276、2.1.277 和 2.1.278 的真实 MCP 只读夹具验证：放行结构校验后的输出工具，并以同一 session、同一 Workflow tool_use_id 的 system/task_notification completed 作为完成证据。启动回执不算完成。仅证明该受限模式，不覆盖任意脚本或完整审查策略。

## 命令

```text
python3 <skill>/scripts/bridge.py doctor --cwd /absolute/project
python3 <skill>/scripts/bridge.py doctor --cwd /absolute/project --verify --model sonnet --timeout 60
python3 <skill>/scripts/bridge.py run --packet /absolute/packet.json --run-dir /absolute/runs/001 --timeout 300
python3 <skill>/scripts/bridge.py status --run-dir /absolute/runs/001
python3 <skill>/scripts/bridge.py cancel --run-dir /absolute/runs/001 --reason "用户调整该部分方向"
```

### 安装与认证预检

### CLI 生命周期管理

通过 MCP `claude_cli_status(cwd, job_id?)` 区分系统发现的 CLI、配置的 active CLI、基础只读任务的派单预览以及 candidate 验证阶段。编辑或 Workflow 的能力要求可能选择不同保留版本；本轮实际版本以 run 的固定证据为准。状态还分别报告安装、登录和最近真实 provider 调用；不能把通用预览当成任意 packet 已完成派单。候选失败不使仍有资格的执行版本变成不可用。

已有配置默认保留 `channel=bundled`，来源是随包支持清单。用户授权自动跟进官方最新版后，`claude_cli_update(action="policy", policy="automatic", channel="latest", auto_qualify=true)` 开启后台官方发现、私有下载、隔离资格验证和按能力切换；付费验证每个候选身份/当前契约仅自动尝试一次，10场景/900秒上限，无USD硬上限。仅自动获取而暂不付费验证可设 `auto_qualify=false`。状态查询只读；`cli_maintenance.channel` 标明当前来源/范围/检查时间。部分能力通过不等于完整资格通过，查看 `requested_groups_outcome`/`missing_groups`；缺组继续使用已验证保留版本。普通 `up_to_date` 不反复提示。

当 `cli_maintenance.notice_pending=true`，Codex 先说明当前/目标版本、保留或切换/失败原因和任务影响，再调用 `claude_cli_update(action='acknowledge', notice_id=<状态返回值>)` 确认已说明。已有 run 的固定 identity 不因全局维护而改变。用户询问版本维护时用 `claude_cli_update(action='refresh')`；`policy='automatic'` 或 `policy='manual'` 控制后续维护。手动 rollback 会暂停自动维护，直到显式恢复 automatic。

用户明确请求升级时使用 `claude_cli_update`：`prepare` 自动复用健康的 native active；自动发现到 unknown 或 non-native candidate 时保留它，并从具备来源和完整性证明的官方分发 bootstrap 已测基线。它不发起模型请求，也不改全局 launcher、登录或 profile。`validate` 最多运行 10 个场景、总时限 900 秒并做有限 cleanup，没有 USD 硬上限；`activate` 原子切换已验证 candidate；`rollback` 还原先前 active CLI；`cancel` 请求停止仍在运行的验证。验证报告路径和 job 状态由 `claude_cli_status` 返回。不能通过 flags、help 输出或 fake CLI 让 candidate 获得已验证 profile；验证失败、取消或 capability 不足时仍使用之前 eligible active CLI。

可选 capability（例如 `max_budget_usd`）按 packet 实际请求判断。缺少它只会阻止请求该能力的派单，不能把整个 active CLI 或不使用该能力的普通任务显示为不可用。CLI executable 的 rollback 与安装器管理的插件 catalog rollback 相互独立。

`doctor` 检查实际可执行路径、版本、所需参数与官方 `claude auth status --json`。`--cwd` 应与目标任务相同，使项目配置与认证来源一致；未指定时使用当前目录。API key/云平台认证不强制要求存在账号邮箱。

| 状态 | 含义与处理 |
|---|---|
| `cli_not_found` / `cli_not_executable` | CLI 未找到或不可执行。提示安装/修复或 PATH；不自动安装 |
| `cli_unavailable` / `cli_incompatible` | 启动失败或必要参数无法确认。核对本机权限/版本 |
| `cli_profile_unverified` | CLI 已安装但候选尚未取得当前契约的本地资格；用显式本地验证取得所需 groups。已有 eligible active CLI 可继续处理不需要候选能力的任务；不能凭 help flags 放行 |
| `not_logged_in` | 官方 CLI 明确回报未登录。用户运行 `claude auth login`，或配置自己选择的 API/云平台认证 |
| `auth_check_failed` | 状态命令异常、超时或无法解析。不能断言凭证过期，也不能当已登录 |
| `local_checks_passed` | CLI 回报已配置认证，远端有效性尚未验证 |
| `verified` | `--verify` 的实际请求成功；仅证明当前认证来源与指定模型在本次检查时可用 |
| `authentication_failed` | 在线请求明确拒绝认证；重新认证后再检查 |
| `quota_or_rate_limited` / `network_error` / `access_denied` | 额度/网络/访问权限问题，不能仅据此要求重新登录 |
| `verification_timeout` / `verification_failed` | 在线探测未能证明可用；保留未知，不盲目重试 |

若超时同时回报 `cleanup_status: unconfirmed`，表示宿主未允许确认探测进程已结束；先核对进程和权限，不重派。超时原因与清理状态分别保留，不能把这种情况说成凭证无效。

`--verify` 禁用 agent 工具、MCP 和 slash commands，最多一个 agent turn，不保存可续跑会话；保留宿主权限和原有 hook，会使用少量模型额度。CLI 可按自身正常机制刷新认证；桥不直接读取或保存密钥。输出仅含认证方式、provider、脱敏账号等状态字段，不保存原始认证响应/在线探测输出；不把本地状态、一次在线成功、额度与会话持久化混为一个承诺。

每次 `run` 在启动任务请求前自动执行本地预检。失败写入 `environment.json` 与 blocked 回执，返回非零，不启动 Claude 任务。处理问题后使用新的 run-dir 重试。已成功的在线检查只作当前会话的证据，不缓存为长期有效的凭证证明。

示例的 300 秒只是小夹具的运行上限；实际任务按范围设置，不是质量或验收阈值。run 是前台受监督进程，应由 Codex 的终端工具保留 session 并等待，不能在启动后直接汇报完成。

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

成功拿到结果不代表 Claude 已成功持久化会话。若 `--resume` 返回 `No conversation found`，该续跑应记失败；检查本机会话保存及宿主权限。不要猜另一个 session ID，也不要把新会话当成原会话续接。沙箱限制应走宿主的正常权限升级流程，随后用新的 run-dir 重试；保留原失败证据。

## 阅读回执

- `packet.json`：本轮输入快照。
- `environment.json`：派单前的安装/认证检查，账号脱敏，不含原始凭证。
- `stream.jsonl`、`stderr`：实际 provider 输出和诊断；不把原始大日志全部塞回协调者上下文。
- `workspace_before.json`、`workspace_after.json`：通用输入身份与 workspace_digest；Git 模式同时保留 `git_before.json`、`git_after.json`。`requirements.json` 保存要求内容身份。
- `result.json`：provider 结束状态、实际 session/model、结构化内容、权限拒绝。
- `receipt.json`、`state.json`：运行结果。`reported` 需要主协调者核验，不是 accepted。

structured 内容中的 `checks` 是 Claude 自报；本工具集没有 shell，因此不能凭自报说真实构建/测试已运行。由 Codex 执行和登记实际检查。

出现 unknown、cancelled、timeout、failed：先核真实进程与改动，不自动重派。cancel 先提交停止请求，待原进程终止与工作区核对后才交接。取消/超时留下的输出可以诊断，不能验收。

## unknown 的恢复

通过 MCP `claude_recovery(run_id)` 查看恢复条件、进程组、当前工作区快照与 digest。主 Codex 核查文件与真实进程证据后调用 `claude_reconcile(run_id, reason, evidence, expected_workspace_digest)`；该工具在 cwd 锁内重新检查，任一进程存活、身份不明、权限不足或快照变化均不能恢复。

成功只登记 `reconciliation.json` 和解除本轮占用，不把旧 unknown 改为 accepted/reported。后续必须 fresh 且新 revision，不自动重试。保留旧回执与恢复证据，不通过人工删锁或 marker 绕过判断。

## 已知边界

工具名单与 PreToolUse hook 是工具入口约束，不能代替 OS 沙箱，也不保证任意 hook、外部进程或脱离进程组的后台任务都已隔离/停止。脚本不会为了执行而关闭原有 hook、绕过宿主沙箱或复制凭证。

插件 Runtime 与随附 bridge 共用 cwd 排他锁，跨 task 也不能同时监督写入同一目录；未接入插件的外部工具写入仍由 Codex 协调。正式判据保护按原任务要求另外核对；本版不是 v2.9 四层保护的自动部署器。

Git 快照覆盖 tracked 与非忽略的 untracked 文件；显式 owned/protected 文件另作内容核对。未列出的 ignored 文件和工作区外的副作用不在 Git 差异证据覆盖内，不能据此声称整个文件系统无变化。工具约束的效果与实际 Git/显式文件证据分开报告。

普通 `review` 与 `implement` 不调用 Claude Workflow；只有上述 hash 绑定、fresh-only 的 `workflow_review` 可调用一个已保存的只读 Workflow。bridge 不调用外部连接器，也不自动建 PR、发布或合并。完整 Dynamic Workflow Review 的接入边界见 runtime-design.md。
