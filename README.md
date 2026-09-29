# Codex–Claude 协作 · macOS 插件 0.5.0

把项目规格、Skill、Codex 原生监督子代理、Claude CLI、MCP、持久 Runtime 和简洁详情装在同一个包里。你在 Codex 主任务中正常提需求，Codex 负责选择执行者、同步进度、纠正方向和核验结果。

## 从个人 Git marketplace 安装

维护者：**BryanYue**。仓库：[BryanYue/codex-claude-orchestrator](https://github.com/BryanYue/codex-claude-orchestrator)，现已公开，可通过 HTTPS 匿名读取源码并添加 Git marketplace，无需仓库邀请。

```bash
codex plugin marketplace add https://github.com/BryanYue/codex-claude-orchestrator.git --ref v0.5.0
codex plugin add codex-claude-orchestrator@codex-claude-team
```

已有同名本地市场时，先按[迁移与回退说明](docs/git-marketplace.md#change-a-fixed-ref-or-migrate-from-zip)切换来源。Git 安装后先预热锁定依赖，再开新 Codex 任务；不要执行下方 ZIP 安装器来修复 Git 安装，否则会切回本地 catalog。仓库访问、Python 依赖、Claude CLI 及其登录分别准备，完整步骤见 [Git 安装说明](docs/git-marketplace.md)。

## 使用 ZIP 安装包开始

1. 安装并登录 **Codex 桌面客户端**、**Claude Code CLI**；安装 `uv`（[官方说明](https://docs.astral.sh/uv/getting-started/installation/)，已有 Homebrew 可用 `brew install uv`）。
2. 解压完整安装包，在终端运行 `bash /解压目录/Install.command`；也可双击 **Install.command**。若下载后的 Gatekeeper 阻止双击，使用上述 bash 入口，不必关闭系统安全设置。安装器校验包内哈希并检查环境，使用官方 Codex plugin 命令安装。Claude 尚未安装或登录时也可以先安装插件，再由诊断提示补齐；实际委派仍会被预检阻止。
3. 安装后打开一个新的 Codex 任务，让客户端加载插件。项目已经有采用入口时，直接提任务；首次采用一个项目只需说：

   > 这个项目以后采用 Codex–Claude 协作流程。

4. 此后照常说“按计划继续”“实现这个需求”“这里理解错了”“停止这个方向”。**不用每轮点名插件或粘贴预检、派单、详情、验收的长口令。**

项目采用会给现有 `AGENTS.md` 追加独立的插件块，并保存 `.agents/codex-claude/` 中的参考协议与配置。保留原有规则，可随项目代码共享；新同事无需重新采用。不会修改全局规则，也不表示任何 plan、基线或 oracle 已获批准。采用后的项目文件是否提交仍按项目授权处理。

安装源自动保存到 `~/.codex/claude-orchestrator/catalog`（尊重 CODEX_HOME）；下载包可以移走。使用独立 `codex-claude-team` marketplace，不占用同事的 personal。更新仍运行新包的 Install.command；旧安装源保留供回退。`bash Install.command --check` 只检查完整性/环境/冲突。

需要维护时，在解压目录运行：

```bash
bash Install.command --diagnose-json
bash Install.command --configure-claude-bin /绝对路径/claude
bash Install.command --list-backups
bash Install.command --rollback <上一步返回的备份ID>
```

诊断摘要不含账号身份、凭据、任务原文和原始 run/session 标识；不会发送遥测。回退只接受哈希校验通过的本插件备份，并保留当前版本，随后通过官方插件命令重新安装。更新或回退后开一个新 Codex 任务加载相应版本；旧任务已加载的工具不会自动热替换。

每个人使用自己的 Claude 本机认证和额度。安装器准备插件私有的已验证执行版本，保留 Claude 原有安装；不自动登录或切换账号。Skill 会在首次实际需要委派时调用在线凭证检查（bridge 每轮的自动预检仅查本地配置），会使用少量 Claude 额度；近期成功调用可复用。

首发在 macOS Apple Silicon 实测；Intel 尚未单独实测，Windows 不在本版范围。安装器要求在标准 /Applications 目录中检测到 Codex.app 或 ChatGPT.app 内置且支持 plugin 命令的 Codex CLI。仅安装 PATH Codex CLI 的宿主当前不在支持范围，不能作为桌面兼容性证据；--check 不执行安装，但条件不满足会返回非零退出码。

## 0.5.0：协作工作台与 Git 分发准备

本版提供按任务组织的只读协作工作台，并把源码整理为独立 Git 仓库，新增可复现构建和首次安装说明。实际验证与限制见 [发布验证](RELEASE-VERIFICATION.md)。

- 按任务和项目聚合执行轮次；执行、报告、Codex 核验分别显示。历史轮、详情未载入、过时记录和失败但保留报告内容各有明确提示。
- 同一主任务首次派发时请求打开一次工作台；后续执行通过工作台列表查看。queued 也记为已发出请求，监督子代理不再另外开页。显式重开仍指向原执行；不承诺跨 MCP 进程或跨 Codex 任务自动复用同一浏览器标签。
- 新增响应式明暗主题、窄面板轮次选择、搜索筛选、只读下一步指令、维护抽屉和键盘焦点处理。保留原 HTTP 权限与执行核心。

- 新增 `tools/build_distribution.py`：从干净、已提交的 Git commit 构建分发包；校验根包版本（`.codex-plugin/plugin.json` 基础版本号与 `pyproject.toml`、`uv.lock` 中的 `codex-claude-orchestrator` 版本一致，第三方依赖锁定内容不受影响）；拒绝覆盖已有输出、拒绝 symlink 和未登记的文件类型；生成确定性排序/时间戳/权限的 ZIP，使同一 Git 提交、同一输出文件名的重复构建可核对哈希；随包写出 `FILE-SHA256.json` 与 `RELEASE-MANIFEST.json`（版本、源提交、执行契约摘要）。也校验 `launch.sh`、`Install.command` 在 Git 中记录为可执行模式（100755），因为 Git 安装路径依赖这个位，ZIP 安装路径依赖打包时写入的权限位。用法（Python >=3.11）：`uv run --project plugins/codex-claude-orchestrator --frozen python tools/build_distribution.py --source-root <repo> --output <目标目录>`（默认输出到 `<repo>/dist/codex-claude-orchestrator-macos-<version>`）。
- 新增 [Git marketplace 说明](docs/git-marketplace.md) 与 [安装与恢复说明](docs/install-and-recovery.md)：区分 ZIP 与 Git 两条首次安装路径；uv/Python/依赖/CLI/认证的分步准备；本机同名市场迁移的具体步骤与失败回退；并明确区分"本地 Git-ready"（可克隆、可复现构建）与"真实远端已验证"（实际执行过 Git 取源与安装）两种不同结论，不把前者当后者宣称。
- `plugins/codex-claude-orchestrator/scripts/launch.sh` 新增 `--prepare-dependencies` 显式模式：只执行 `uv sync --frozen --no-dev` 做冷启动依赖预热，不启动 MCP server、不发起任何模型调用、不修改 global/PATH/profile/认证；`.mcp.json` 的默认启动路径（不带该参数）行为不变。
- 插件与根包版本号同步为 0.5.0；`uv.lock` 中第三方依赖的锁定内容未改动。

## 0.4.7：审查后的稳定性修复

- 自动资格验证启动与用户暂停策略同步；仅在能够证明未启动时延后重试，启动结果不明保留原任务，不重复消耗额度。同一维护故障确认后不反复提醒；新版发现失败不再被“已是最新”遮盖。
- 分开显示配置的 active CLI 与基础任务预计使用的版本。具体任务仍以执行记录为准；模型初始化声明、实际响应及一轮中的多个模型分别记录。
- 自然语言让 Claude 审查时，经插件建立执行和详情。自动打开为 queued 时说明排队并给出可点击链接；重连继续查询同一 run。非 Git 源码可先生成带原始路径、行号、哈希的明确文本快照，再经现有只读资料入口审查。
- 修复终态刷新/取消竞态、已证明未启动的恢复出口及受监督进程清理；守卫处理异常和歧义路径更明确。已发现 hooks 被关闭时阻止派单并解释，不改写用户或组织设置。
- 安装注册失败时先核实宿主实际登记，必要时恢复已验证旧 catalog；只有宿主也核对成功才说完全恢复。运行中的 MCP 使用独立版本缓存。

本机验收及企业策略、Workflow 等尚未确认的边界见[发布验证](RELEASE-VERIFICATION.md)。

## 0.4.6：官方最新版与模型发现

- 可一次开启“自动跟进 Claude 官方最新版”。后台发现官方发布、验证来源和文件完整性，在私有目录准备候选；兼容验证通过后才切换新任务。下载和资格失败保留可用版本，进度、原因与未通过能力可查。
- 模型信息直接从实际 CLI 读取，包括新出现的类别、解析后的模型 ID 和 effort 选项。按类别选择时使用 Claude 官方别名，不维护固定模型版本表；显式模型 ID 保留。目录信息和实际模型调用结果分别展示。
- 部分能力通过时明确列出缺失能力；已有按能力选择机制可使用此前保留的有效版本。旧任务与续跑不随全局升级更换执行文件。
- 官方最新频道需要本机 GnuPG（`gpg`）核验发布签名；已有 Homebrew 的同事可运行 `brew install gnupg`。缺少时会保留可用执行版本并给出提示，插件不会跳过验签。
- 旧配置仍按随包清单维护。想启用新模式，告诉 Codex：**“自动跟进 Claude 官方最新版，允许每个新候选做一次有界兼容验证。”** 这会使用本人 Claude 额度；不希望自动验证时可只自动获取候选。

## 0.4.5：实际使用细节修复

- 项目检查返回明确的绑定目标和下一步。多仓库工作区先保留工作区约定，进入实际 Git 仓库时按已有授权完成绑定；检查失败、尚未采用、明确停用分别处理。
- Claude 报告退回后，如果 Codex 已独立补齐并核验，可以显示“原报告未采纳 · Codex 已完成”。尚未补齐时仍显示待修正；不从旧记录的自由文本推测完成。原报告、退回原因、补齐摘要和历史均可查看。
- 文件影响比较前后快照，分开展示开始前已有改动与执行期间观察到的变化。缺少证据显示未确认，不能把整个工作区脏文件列表归给 Claude；忽略文件和中途改动不在快照统计内。
- 简洁页显示最近有意义的活动及其真实时间，原始公开活动在完整页保留；工具许可显示“已允许读取”，不冒充读取成功。
- Skill 的常规进度调用使用 compact 摘要；完整结果与核验历史按需读取。旧 MCP 调用省略 compact 参数仍返回完整结构。
- 审查要求明确：搜索无命中不能证明文件不存在；命令说明须核对实际默认值、产物与消费路径。该规则帮助减少错误，结果仍必须由 Codex 核验。

上述 0.4.5 修复当时没有扩展定制 plan 或完整 Workflow Review，使用隔离回归与历史证据副本，没有为页面和状态修复重复调用付费 Claude。0.4.6 的 CLI 资格验证涉及真实调用，当前覆盖与未验证项见 [发布验证](RELEASE-VERIFICATION.md)。

## Claude 更新与执行版本

**插件默认自动维护自己的 Claude 执行版本。** MCP 启动后在后台检查，并在服务运行期间按已配置频道定期检查。发现可升级版本时，优先复用本机内容完全一致的原生文件；否则从官方源下载。完整性、签名和所需兼容资格全部核验成功后才原子切换，旧版本保留供回退和已有任务使用。下载或校验失败会说明原因，保留原来的执行选择。

下载会保留与版本、平台和 SHA256 绑定的断点；再次获取时请求 HTTP Range。若服务器忽略 Range，将在独立文件中重新下载，完整成功前保留原断点。安装终端显示百分比和 MiB，后台详情显示已收字节。默认连续 120 秒无数据才判无进展，同时保留单次 3600 秒资源上限；同目标下载锁最多等待 30 秒。失败保留断点和旧选择；自动重试间隔 3600 秒，手动刷新或重跑安装可立即重试。最终大小、哈希和签名全部通过前不会执行下载文件。

显式切换/回退会把执行选择与 manual 策略一起提交；资格检查或并发校验失败不暂停 automatic。崩溃恢复只处理能够证明属于本事务的写入；检测到旧版本进程后续修改时保留现值，并显示需要核对的冲突通知，避免覆盖用户的新选择。

**两种更新来源分别标明。** `bundled` 使用已安装插件随包审计清单，作为旧配置默认和首次安装的可用基线；`latest` 查询 Claude 官方发布频道及该版本元数据，不要求每次发布插件才能发现新 CLI。通过 `claude_cli_update(action="policy", policy="automatic", channel="latest", auto_qualify=true)` 一次启用全流程；设 `auto_qualify=false` 则只获取候选并提示待验证。每次验证最多10场景/900秒，无USD硬上限。已运行任务仍使用原身份，新任务只使用有当前资格的能力；缺组可回退到保留版本。这里自动维护的是Claude CLI，插件包自身仍通过新包安装或宿主插件更新机制更新。

官方发现每六小时检查一次；MCP 服务运行时后台每五分钟检查是否到期及接续维护，新任务也可触发维护。关闭 Codex 后不会另装常驻系统服务；重开后继续。资格验证完成到后续切换可能等待下一次后台检查。升级插件后请打开新 Codex 任务加载新版工具；已经运行的 MCP 进程不热替换代码，不能假定旧任务已获得本版修复。

Codex 会简要说明当前版本、目标版本、保留或升级的原因，以及对本次任务的影响；默认简洁详情显示更新进度与待说明的结果，完整模式可查清单来源。正常“已是支持的最新版”检查保持安静。用户主动回退或指定执行版本后，自动维护暂停，直到用户明确恢复；刷新检查不会取消这个暂停。

系统 Claude 自动更新后，插件保留的可用版本仍可处理任务；陌生版本可作为候选，按已授权策略通过隔离验证后再使用。无需因系统更新全局降级或关闭 Claude 全局自动更新。原生安装的更新行为见 [Claude 官方说明](https://code.claude.com/docs/en/setup#auto-updates)。

私有版本位于 `~/.codex/claude-orchestrator/cli/versions`（尊重 CODEX_HOME），以平台、版本和内容 SHA256 标识，校验原生签名和文件内容。安装时优先保留本机已验证的原生版本；需要时从官方分发源准备精确的已验证基线。复制对象不含凭据。只有插件启动的私有执行进程禁用自动更新，全局 launcher、账号和设置保持原样。

每次派单固定执行文件身份，运行证据与详情中可查。切换和回退影响后续新任务；续跑沿用原轮次身份，不追随新的默认版本。文件损坏或丢失时明确阻断，不临时换 PATH。0.4.2/0.4.3 的完整执行描述符可迁移到续跑协议 1；后续兼容补丁沿用该协议，不再仅因源码哈希变化而拒绝续跑。仍要求原执行文件完整、任务契约不变、当前能力资格有效，并在新回执记录首次与当前实现契约。真正不兼容的续跑协议明确要求 fresh。0.4.1 及更早的历史 run 没有完整身份凭据，需核对旧结果后另起 fresh 轮次。

版本管理入口已整合到 Skill，日常仍正常提需求。需要维护时可以直接说：

| 需求 | 工具行为 |
| --- | --- |
| 查看 Claude 版本和兼容情况 | `claude_cli_status`，分别列出系统版本、当前执行版本、候选、登录与实际调用记录 |
| 检查支持版本的更新进度 | `claude_cli_update(action="refresh")`，接续后台维护；已授权 auto_qualify 时可能启动有界资格调用，尊重已暂停的策略 |
| 暂停自动更新 / 恢复自动更新 | `claude_cli_update(action="policy", policy="manual"/"automatic")`；不改变运行中任务 |
| 准备可用的 Claude 执行版本 | `claude_cli_update(action="prepare")`，准备私有文件，不发模型请求 |
| 验证系统新版，通过后切换 | `claude_cli_update(action="validate")`，返回验证 job_id，可查询进度与报告 |
| 先验证，不切换 | 上述工具加 `activate_on_success=false` |
| 停止这次版本验证 | `claude_cli_update(action="cancel", job_id=...)`，等确认进程已停止 |
| 回退 Claude 执行版本 | `claude_cli_update(action="rollback")`，核对保留版本后原子切换，并暂停自动更新以保留选择 |

验证分为核心调用/取消、只读、写入、续跑和 Workflow 能力组；必须取得实际证据，参数出现在 help 里不算通过。新版本可取得与执行文件和当前桥接契约绑定的本地资格，不需要改内置列表。可选组未通过时，依赖该能力的任务选择仍被保留、资格有效的版本。认证、网络、配额或没有实际观察到测试工具调用会标成“未确认”，不会误报登录失效或兼容通过。

陌生版本的资格验证会消耗Claude额度：手动validate或用户一次开启latest+auto_qualify后，才可发起。已启用时每个候选身份/当前桥接契约只自动尝试一次；未确认、失败不会在每次轮询或重启后重复付费。状态查询永远不发模型请求，模型目录只用initialize元数据查询；远端模型实际可用性以真实调用为准。验证失败、取消或与另一切换发生冲突时，保留原执行选择。已确认的能力违规会阻断对应身份的能力。历史文件保留以支持回退与续跑，本版不自动清理这些执行版本；版本状态的 storage_summary 显示已核验保留版本字节数，未计入下载断点和运行证据。清理工具尚未实现，不能删除被旧任务引用的执行文件。新任务若需要当前默认版不支持的已验证可选限制或能力，可选择资格有效的保留版本；详情记录本轮实际选择。

### 模型升级

`opus`、`sonnet`、`haiku` 等是官方类别别名，不是固定模型版本。`claude_models(cwd)` 查询当前执行CLI公布的模型选择项、`resolvedModel`和effort选项，也支持指定保留的CLI身份做比较。新模型类别会自动出现在返回目录中；插件不会自行切到更贵的类别，或把目录声明当作账户调用成功。组织、提供方和官方模型环境覆盖仍由Claude处理。

模型升级可能同时要求CLI升级。以2026-09-23官方说明为例，Opus5.5要求CLI>=2.1.280；旧CLI仍可用不代表已经使用新模型。请求类别别名与实际provider回报分别保存；历史任务不会被新目录改写。[官方模型配置](https://code.claude.com/docs/en/model-config)

### 显式使用外部安装

下面的 `Install.command` 命令仅用于 ZIP 安装。Git marketplace 用户不要执行它或其配置参数；优先使用既有的受管 `prepare` 路径，保留所选 Git source/ref。

需要自行管理 npm/custom 安装时，可显式选择外部模式。它按 `CLAUDE_BIN` → 插件 `settings.json` 的 `claude_bin` → MCP PATH 选择，仍有兼容预检；不享有插件私有版本的保留保证。若终端能找到 Claude、桌面找不到，在终端用 `command -v claude` 确认具体路径，再运行：

```bash
bash Install.command --configure-claude-bin /你确认的绝对路径/claude
bash Install.command --diagnose-json
```

该配置只影响插件，不 source `.zshrc`、不切换 Node、不改全局 PATH。npm/nvm 的 `bin/claude` 符号链接会保留原路径，让旧式 Node shim 找到同一 bin 目录的 Node。更新配置后在新 Codex 任务加载插件。使用 prepare 可重新准备托管执行；状态页会清楚区分系统候选发现与实际派单的选择。

## 检查状态与执行详情

配置检查、环境检查、路由选择都不启动业务 Claude 任务。工具摘要会说明本次操作；配置检查失败时采用状态为 unknown（adopted=null），不能当作确认未采用。停用、未采用、采用但待修复、采用且就绪分别显示。只读检查支持项目目录别名，返回请求目录和真实目录；派单使用经过检查的真实目录。同一仓库的别名共用执行互斥，嵌套 Git 仓库独立检查。配置、协议等受保护路径仍拒绝符号链接；enable/disable 等写入使用真实项目根。

派单后先显示具体任务 ID、目标、范围、请求模型与真实执行状态，并提供可点击工作台链接。同一 Codex 任务内，仅主代理在确认尚未请求打开时自动请求一次，发出前即记账；queued 只表示面板排队，失败不自动重开，上下文不明时只给链接。后续执行通过工作台任务列表查看；用户显式要求时可重新取得同一 run 的链接并打开，这不会新增 Claude 执行。主代理从自己的 MCP 取得 Viewer 链接，不直接依赖监督席的临时服务。详情失败不会重新派单。单页范围是同一任务内存活的 Viewer，跨 MCP/重启的统一入口和宿主深链复用标签页不作保证。主 Codex 用同一 run_id 等待增量并核验结束结果。插件工具标题是否出现在宿主汇总卡片，由 Codex 客户端决定。

简洁页同时显示最近公开活动及其时间、页面最后同步时间和报告核验。页面不断刷新不代表 Claude 有新活动；连接中断也不证明任务停止。完整页分开显示请求模型、初始化解析与实际响应模型；实际模型来自 assistant.message.model 或 result.modelUsage，可能有多个，只有初始化信息时保持待确认。reported 是模型交回报告，accepted 是 Codex 核验决定；两者保持分开。

## 自动选择和手动指定

| 你说的话 | Codex 的动作 |
| --- | --- |
| 按已确认计划继续 | 读取项目约定/现有进度，按任务适配和偏好选执行者 |
| 提高 Claude 的执行偏好 | 调整该项目的路由权重；不改全局模型 |
| 先改成仅手动调用 Claude | 切换 manual，日常由 Codex 处理 |
| 这一次让 Claude 执行 | 单次覆盖，项目默认保持原样 |
| 这次由 Codex 处理 | 单次指定 Codex |
| 用 Claude 的某个 Workflow 跑这次审查 | 核对指定保存脚本、名称、哈希和权限；只有真实 Workflow 调用才算启动 |
| 这里理解错了，按这个反例修正 | 保留原验收，选择局部续接或停止后重新派单 |
| 打开执行详情 | 打开同一执行的简洁卡片，可展开或切完整模式 |

偏好配置在 `.agents/codex-claude/routing.json`：auto/manual，Codex/Claude 各自 0..100。提供“按任务选择”“仅手动”“Claude 偏好”三种预设；高级设置省略的字段保留当前值。默认双方50，加任务适配分；是可调的启发式偏好，不是调用配额、概率或模型能力排名。0表示不自动选择该执行者，手动仍可指定。澄清、小改动和架构裁决默认倾向 Codex；明确方案实现与独立审查默认倾向 Claude。路由同时考虑范围是否明确、独立性、主 Codex 已有上下文、响应时效、所需工具和已知可用性。需要外部连接器或超出当前能力时会解释阻断原因；手动指定保留原选择，不悄悄换执行者。路由建议不产生执行授权。

Claude 自身的 Workflow 属于单独的显式入口，不能因提高 Claude 权重自动启用。完整多席 Workflow Review 与普通单席 review 的证据分别报告。

## 命名保存 Workflow：受限只读审查

`workflow_review` 是单独、受限的保存 Workflow 入口，只用于一次新的只读审查。它先从 `claude_saved_workflows` inventory 中取得有效条目，再在 packet 中绑定**完全相同**的 `name`、绝对 `path` 和 `sha256`；如该脚本需要参数，`args` 也必须精确匹配。不能把名称相同、路径不同或内容已变的脚本替代进去。

inventory 从当前 `cwd` 向 Git 根查找 `.claude/workflows`，并合并个人 Claude 配置目录下的 `workflows`；项目脚本按 Claude 的项目优先级覆盖同名个人脚本。它只盘点磁盘上的保存脚本，不把旧的 Claude session、历史 run 或详情页记录当作“已安装 Workflow”。`workflow_review` 是 fresh-only：不能携带 correction，也不支持 resume。

这项能力只允许 `Workflow(<已绑定名称>)` 和只读工具，执行后还要观察到对应 Workflow 的工具调用、成功工具结果与完成事件，才可进入报告核验。它不是完整的 **Claude Dynamic Workflow Review**：多席位编排策略、实施型 Workflow 以及其成本/并行/写入边界尚未验收，不能把这个单席只读模式写成完整 Workflow Review。

## 你能看到什么

- **Codex 主任务**：选择执行者的简短原因、阶段进度、需你决定的问题、最终核验结论。
- **原生子代理入口**：适合独立委派且主代理能并行核验时，看到负责本次 Claude 执行的 **Codex 监督子代理**；可查看其调用和摘要。Claude 本身不是 Codex 原生代理。
- **默认简洁详情**：一句任务目标、统一状态、时长/最近同步、可读报告、核验结论与直接证据入口；可切轮次，展开查看公开过程。失败、未知和断连说明原因与下一步。
- **完整模式**：文件筛选、工作区 diff、执行轮次、实际模型/会话、输入与结果证据。

工具活动取自真实公开事件，不展示私有思考，不伪造完成百分比。没有新事件不等于停止。`reported` 是 Claude 已交回报告；主 Codex 核对真实文件与检查后才记录 `accepted` 或 `returned`。原生卡片样式和宿主 CLI 界面由 Codex 客户端决定，插件不能直接修改；插件详情页只提供相近的简洁阅读方式。

## 规格、纠正与恢复

主 Codex 保留目标、技术裁决和验收责任。监督席负责预检/启动/进度/回传，以及主代理批准的纠正；不直接修改业务文件或自批结果。

随包包含 v2.9 协议原文；项目已有协议优先。项目采用绑定参考协议 SHA；完整协议任务仍从已批准 delta/dispatch/基线恢复。正式 packet 的 protocol_binding 核对批准基线中的协议与 delta 身份，不能凭安装或一次模型 success 宣称已完成协议验收。

局部完成后的纠正使用精确 run_id 续接，结构/方向变化在确认旧写入结束后 fresh。重连时优先核对绑定本轮输入的执行回执，并确认原进程已停止：完整证据可恢复到实际 reported/failed/cancelled 等终态；已结束状态不会被旧轮询写回运行中。启动前失败有独立回执，缺少 child.json 本身不能证明从未启动。未知状态先核实，不盲目重派。对于仍缺执行证据的 unknown，有“检查恢复”入口：Codex 对照记录检查执行进程组已停止、cwd 无执行者、当前文件快照一致，附实际证据后登记恢复。人工解除 unknown 占用不会把旧 unknown 改成成功，也不恢复旧会话；仅解除明确核验过的占用，允许另起 fresh 轮次。缺少进程身份或权限时仍保持阻断。重连后的非 owner 也可发送持久取消请求，仍须等待停止确认。多次验收/退回保留 decision_history。同一 finding 次数跨轮次保留；v2.9 只允许一轮自修后由协调者裁决。项目现有 PROGRESS/DECISIONS/DEBT/findings/final 是正式进度来源，插件记录仅作运行证据索引。

## 普通文档与资料审校

在不属于 Git 项目的资料目录，可直接说“让 Claude 对照这份要求审校这些文档”。Codex 指明输入文件与要求来源，Claude 只读声明清单里的 UTF-8 `.md`、`.txt`、`.csv`、`.json`，结果按来源位置核查；执行前后保存声明文件内容哈希，并检查目录项新增、删除、重命名和类型变化；未声明文件内容不会被读取。Finder 的精确 `.DS_Store` 元数据不触发内容失败，并在快照中单列留痕。不会为这项任务创建 Git 仓库。

适合规格对照、文案审校、文本资料一致性检查、已有数据摘要的独立复核。此模式不编辑文件，不支持会话 resume 或 Workflow；新的纠正使用 fresh 审校。Word/Excel/PPT/PDF、联网研究和外部业务操作需相应工具准备资料、并另行核验，本版不把文本入口称为这些完整能力。

## 执行与数据边界

- 普通 review 只读；implement 仅允许精确 owned_files 的 Edit/Write。构建/测试由 Codex 执行，Claude 此桥没有 shell。
- 原有 Claude 配置/hook 保留；文件 hook 不是 OS 沙箱。完整协议要求的 oracle 登记、OS 保护、项目构建/下游/设备闸需由项目真实提供，缺失必需保护时停止依赖它的工作。
- 同 cwd 排他防止接入桥的重复写入；其他工具仍由主代理协调。取消请求与确认停止分别记录。
- `claude_start` 的执行时限默认 300 秒，可按任务显式设置为 1..14400 秒；它与每次最长 25 秒的进度等待不同。长任务在开始前选合适的有界时限，不到时自动续费重跑。可设置 Claude 最大轮数与 API 计价预算。轮数/预算仅在已测试 CLI profile 且参数实际可用时启用；不支持时拒绝带该控制的派单。费用与 token 来自 provider 回报，不等同订阅剩余额度或账单保证。
- 详情页按游标增量读取；历史分页，终态降低刷新频率，页面不可见时减少查询。状态与验收结论使用同一规则，历史轮次被替代后不能显示为当前已完成。
- 本地运行记录默认 `~/.codex/claude-orchestrator`，含项目资料，不随分发包发送。详情服务仅绑定127.0.0.1，随机端口/访问令牌，只读；重连后取新详情链接。
- cwd 的跨版本锁/unknown marker 暂沿用系统临时目录，与 0.4.0 共用同一协调协议。不能手工删除活动锁或 marker 来解锁；系统清理临时目录仍是已知局限，持久协调目录迁移尚未交付。
- 本版不自动删除运行证据，也不设无限后台采集。原始日志可能含任务资料；按团队留存规则在确认无活动执行后归档，分发只发安装包。诊断摘要与运行证据分开。
- 不自动合并/发布，不实现无人值守协调者替换。停用项目采用不会自动停止正在运行的 Claude，先正常取消并等待终态。

## 可复用来源

- 实际依赖：[官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)，锁定 `mcp==2.2.0`，由 `uv.lock` 固定依赖；不自写 JSON-RPC/MCP 协议。
- 调用基础：[Claude Code 官方 CLI](https://code.claude.com/docs/en/cli-reference) 的 `-p`、stream-json、JSON schema、session/resume 和 hook。
- 生命周期体验参考：[OpenAI 官方 codex-plugin-cc](https://github.com/openai/codex-plugin-cc)。它是 Claude 调用 Codex 的反向插件；借鉴 status/result/cancel 等接口分工，没有直接当作本插件后端。
- 调研了 [agent-bridge](https://github.com/teamnebula-ai/agent-bridge)、[Claude-Bridge](https://github.com/constripacity/Claude-Bridge) 的任务文件、事件与本地查看思路。这些项目不是本插件的依赖，也没有复制其源码。

## 开发与验证

进入 `plugins/codex-claude-orchestrator`：

```bash
uv sync --frozen
uv run python scripts/run_tests.py --suite all
```

回归入口为测试子进程提供独立 TMPDIR，结束后清理测试自己的 lock/marker，不触碰真实运行记录。可用 `--suite plugin` 或 `--suite bridge` 做定向回归。

`tools/build_distribution.py` 只用 Python >=3.11 标准库；macOS 的系统 `python3` 可能更旧。下面使用项目锁定环境中的 Python，测试用标准 `unittest`，会在临时目录里创建最小 Git 夹具，不触碰当前工作树：

```bash
uv run --project plugins/codex-claude-orchestrator --frozen --no-dev python tools/tests/test_build_distribution.py
```

自动回归、真实 Claude 调用、原生宿主接入和浏览器观察分别记录；小夹具通过不证明生产长任务零偏差。整体结构为：用户 → 主 Codex + Skill/规格 → 原生 Codex 监督席 → MCP Runtime → Claude；证据原路返回，由主 Codex 核验。

发布验证同时覆盖隔离夹具、MCP 协议、浏览器和安装。具体版本与覆盖以随包 RELEASE-VERIFICATION.md 为准；不将历史版本的通过数当作本版证据。普通请求在新桌面任务中从路由到委派的完整自动入口，需要在目标宿主正常权限下走查；本机 MCP 直连不替代这项验收。

源码与 Git marketplace 由个人仓库 BryanYue/codex-claude-orchestrator 维护，使用固定发布 ref 安装。具体取源、升级、回退步骤见 [Git marketplace 说明](docs/git-marketplace.md)，实际验证范围见 [发布验证](RELEASE-VERIFICATION.md)。问题请通过仓库 Issues 提交，并去除凭据、viewer token 与业务代码。当前未指定开源许可证。

要停用项目入口，向 Codex 说“这个项目不再自动采用该协作流程”。插件只移除精确未改的自有块，保留其他规则和证据。要卸载客户端插件，在 Codex 插件界面卸载；任务记录按团队规则另行保留。
