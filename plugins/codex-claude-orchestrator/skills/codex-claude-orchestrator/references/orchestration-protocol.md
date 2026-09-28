# 执行协议（Orchestration Protocol）v2.9

> 唯一权威副本，与任何项目无关；不含项目名、项目路径、阈值数值。仓内 `docs/internal/orchestration-protocol.md` 只是同步件。
> 每个 plan 在 `delta.md` 钉住采用的协议版本与 sha，仓内副本随 plan 基线 commit 入库；执行中不换版本。
> 恢复时用 `git show <基线 commit>:docs/internal/orchestration-protocol.md` 取回正文并核对 sha，对不上则停问。同步仓内副本只在新 plan 的 Boss 过目时一并确认。
> 规则分 [M] 机器可判定 / [R] 需判断。每条规则都经过"删掉会怎样"一问；删不掉的才留下。

## 0. 两份输入文件

| 文件 | 性质 | 内容 |
|---|---|---|
| `delta.md` | **批准后冻结**（进 oracle-manifest） | 目标；不变量（每条给检查命令，或列出允许例外）；验收 oracle 清单；事实清单（本 plan 触及的常量、枚举、提示词句、能力声明、语言表项，每条写 owner 文件）；已知红测试名单；基线分支；采用的协议版本+sha |
| `dispatch.md` | **可调**（不进 oracle-manifest） | lane 表：类型（MOVE / EDIT / FEATURE）、owned files、模型、预算及推导来源、gate 命令、worktree?、依赖。协调者按证据调整，PROGRESS 记一行，不重新确认。**边界**：可调的是命令、顺序、实现方式；不得减少 delta 已确认的必需行为、场景与覆盖，未执行不算通过。必需覆盖写在 delta，命令写在这里 |

state 目录 = delta 同目录（`docs/internal/orchestrator/<slug>/`）：PROGRESS / DECISIONS / DEBT / findings.jsonl / final.md。
一 plan 一目录一 task branch；不读写其他 plan 的 state。

冻结依据是**明确批准的工作目录、完整基线 commit、该 commit 中的清单**，不是 git add、HEAD 或工作区里出现了清单。草稿可改；清单中的 glob 只展开批准基线里已有的文件，新测试不会因命名或暂存自动成为正式判据。批准后登记方式见启用说明；授权变更更新原登记，其他已确认约束继续有效。

## 1. 开工 [M]

1. 读 delta。基线分支**只**来自 delta（项目事实或 Boss 指定）；缺 → 停下问，不猜。
2. 工作区脏：**不 stash**。脏文件与 owned files 或 oracle 清单相交 → 停下问；不相交 → 本 plan 用一个独立 worktree（每 plan 一个，不是每 lane 一个），主工作区不动。任务若依赖主工作区未提交内容，在 dispatch.md 写明采用的输入，不得在新 worktree 里漏掉。
3. 跑基线 gate。红且不在已知红名单 → 停。已知红名单进 DEBT 带期限，不得长期作基线。
4. delta / dispatch 未给的取舍，lane 不自选——停下回报，协调者补进 dispatch.md 再派。

## 2. 闸 [M]

**波次闸**（每 lane 合入后，按类型）：

| 类型 | 定义 | 必跑 |
|---|---|---|
| MOVE | 纯搬迁/重命名/删除，语句不变 | `git diff --name-status -M100% <base>..HEAD` **每行**为 R100 或在预登记例外内，否则 FAIL；全模块编译；层规则检查；冻结与 oracle diff-guard |
| EDIT | 行为不变的代码改动 | 单元测试（不强制重跑，以结果文件时间戳校验）；其他平台编译；仓库已配置的静态检查；diff-guard |
| FEATURE | 语义变化，每项独立 commit | 同 EDIT，加本 lane 新增/改动测试全绿 |

R100 只证明内容一致；移动对资源发现、打包、入口的影响由编译与打包步骤检查。

**阶段收官闸**（每阶段一次）：候选项 = 强制重跑全量单测、其他平台/模拟器测试、产物构建与发布、下游消费方闸、对账表（§5）、DEBT 逐条过、硬件/端到端验证。
**按适用选择，不是默认全跑**：跨平台、发布、下游、硬件各按本项目约定与当前产物/风险启用，适用条件写在 delta；项目已正式要求的完整闸照跑。适用的硬件与端到端项**不得推迟到全部阶段完成后**。

**下游闸的产物身份**：在线刷新失败可离线继续，但只有当下游解析到的产物身份（版本 + 哈希或构建标识）等于本次候选时才算通过；对不上记"下游验证未完成"，不算 FAIL 也不算 PASS。

## 3. review [M]

输入：**成品优先**（提示词、文本常量、发布产物先渲染或发布再审）；**全仓只读**（含下游消费方仓，Allowed files 只限执行席的写面）；**不采信台账**（"已修 / 已知 / 已登记"须在 HEAD 以 file:line 重核）。

每条 finding 三个独立字段：
- `nature`：CODE（行为、契约、安全；**契约性文本如提示词规则、接口文档、schema 也算 CODE**）/ REG（台账、注释、命名、commit 措辞）。
- `blocks`：是否阻断本 lane 目标。CODE Blocker 阻断且不可合并；CODE Major 阻断，协调者可决定带债合入 task branch，但**不等于验收通过**，final 必列；REG 不阻断。
- `scope`：lane 内 / lane 外。scope 外**不改变 blocks**，只改变谁修（路由给 owner lane 或新增 lane）。
未分类 = 阻断，直到分类。DEBT 只记"谁、何时修"，不参与是否通过的判断。

每条 CODE finding 带 file:line 与复现命令；关闭必填 `same_kind_scan`（搜索命令、全仓命中数、处置）。"可疑"是 Info 不是 finding。
数字类结论协调者复算一次再入 log；外部报告数字标来源。

派席：MOVE 不派（协调者抽查 name-status 输出）；EDIT 一席；FEATURE 一席 + oracle 完整性检查（正式判据按 §6 的同一批准基线与具体保护集核对；测试目录的工作区 diff 逐处定性：正式判据的行为性重写需已有授权依据 / 期望平移需根因 / 放宽·删断言·新增 @Ignore 默认 Blocker）。高风险 lane 由 dispatch.md 标注并加席。

红灯：自修**一轮**（写明与上次的差异）→ 协调者裁决三选一：另派一次 / 回退该 lane / park 进 DECISIONS 问 Boss。不再多轮，不等人工卷宗。
执行席认为已批准的 oracle 有错 → 沿已有授权变更；缺少授权则进 DECISIONS，仅暂停依赖该决定的工作。不得迎合实现改正式判据。未批准的临时假设或新测试可依据正式要求纠正，性质不由暂存、轮次或执行者决定。

## 4. 并行与合并 [M]

- worktree 只给**同轮并行的写 lane**；串行 lane 在 plan 的 worktree/branch 直接做，一 lane 一 commit；只读席不开 worktree。
- 未过闸的 lane 只回退其 owned files 中自 lane 起点以来的改动；其他改动不动。
- 合入 fast-forward 优先；cherry-pick 视为例外，记原因并重跑该 lane 波次闸。
- 唯一人工合并点 = task → 基线分支的 PR。协调者永不 push 基线分支、永不 merge PR。
- commit 格式 `{Jira-ID}:描述` 或 `{type}:描述`，冒号后无空格，禁 AI 字样。

## 5. 落盘 [M]

- PROGRESS：每 lane 状态（pending / in-progress / in-review / merged-to-task / parked）+ 类型 + 实测时长与 token；状态变更即写。
- DECISIONS：需要 Boss 的事项，`- [ ]` 现象 / 证据 / 选项 / 推荐 / 默认动作；任务内容级的取舍（改不改业务行为、阶段顺序、归属争议）一律进这里，不进执行规格。
- DEBT：id / 阶段 / 类型（REG · DEBT · PLAN-DRIFT · KNOWN-RED）/ 位置 / 期限（阶段数）/ owner / 状态。到期未处置升 DECISIONS。**"登记"不是终态**：finding 终态只能是 fixed / DEBT 带期限 / DECISIONS；注释和 review 输出里不得作结。
- 阶段收官必出：对账表（delta 原句 vs 落地；模块职责声明 vs 内容；不变量 vs 实况，未列出的例外 = 不变量失效）；单源性审计（事实清单每条全仓出现位置与措辞计数，无唯一 owner 的进 DEBT）。
- final.md：已合并 lane 与 diffstat；决策与债务摘要；未验证假设；阶段汇总表（lane × 类型 × 时长 × token × finding 计数）；对账与审计表。兼作 PR 描述。
- findings.jsonl 每行：`{ts, lane, type, nature, blocks, scope, severity, resolution, same_kind_scan, cost}`。
- 证据只留能复现裁决的最小集合；构建全量日志失败时才留。

## 6. 元规则 [M]

- 阈值只写推导来源（本仓同类型历史 lane 实测），数值进 dispatch.md；协议与 skill 不写数值。
- 任何"冻结 / 不动 / 后续集中做"必须带解冻条件与 owner。
- 同一事实只有一个 owner 文件；提示词、文本常量、能力声明视同代码，成品可渲染可 lint。
- 规则按实测增删：从不被违反 → 降为 lint/hook 后删除；反复被违反 → 修规格不加重措辞。
- 判据保护按职责分四层，不用 shell 文本推断写入效果，也不把事后恢复当事前阻止：
  ① 执行权限：Bash 及子进程的写入由实际启用的 OS 沙箱 `denyWrite` 拒绝。配置须覆盖同一批准基线导出的具体保护集；探针被拒只证明该探针路径，不能证明全清单已覆盖。记录有效配置、路径覆盖及排除命令；`excludedCommands` 不受 `denyWrite` 约束。试写只用专用探针，不碰真判据。
  ② 工具入口：Write/Edit hook 读取明确登记的批准基线与其中的清单，按 `file_path` 拒绝；不读当前索引或草稿清单推断批准。未登记的项目保护未启用，不能宣称已冻结。登记、输入或判断程序出错则拒绝并说明原因；其他项目的 Git 故障不扩散到本项目。hook 本身未被调用或被宿主终止不在它能保证的范围。
  ③ 验收：Gate-M 使用同一登记中的基线 commit 与具体路径，运行 `git --literal-pathspecs diff <基线 commit> -- <oracle 路径>` 和 `git --literal-pathspecs status --porcelain -- <oracle 路径>`，两条均须执行成功且输出为空。检查真实工作区，发现偏离阻止相应验收，受影响结果标不可用；不自动还原，也不把最终相等当作执行期间从未改动的证明。
  ④ 授权变更：负责人沿已有批准更新基线登记、沙箱覆盖和验收引用，更新必须限于获准差异，不能借换基线收下其他判据变更；执行代理不能自造授权。任务记录写明所需及实际保护层；必需的保护缺失只暂停依赖它的工作，记 DEBT 不能替代；原已批准的较轻模式可继续，不重复审批。

## 7. 修订记录

- v2.9（2026-09-09）：保护身份改为明确登记的不可变批准基线与清单，移除索引/草稿推断；工具错误统一阻断；同源导出具体保护集；探针结论与范围覆盖分开；缺失必需保护不得自动降级验收。dispatch skill 需按本版启用说明更新。
- v2.8（2026-09-09）：验收 diff 改为对工作区比基线 commit（原 `<base>...HEAD` 漏暂存与未暂存改动）；明确 `excludedCommands` 不受 `denyWrite` 约束；hook 查不清即按受保护处理；保护集生效用 probe 验证；oracle 清单进沙箱保护集归授权变更层负责。
- v2.7（2026-09-09）：§6 改为四层分工（执行权限 / 工具入口 / 验收 / 授权变更）；撤回 v2.6 的"调用后快照比对还原"——事后还原不等于事前阻止，且可能覆盖并行方的合法改动；文件系统只读位降为防误操作。

- v2.6（2026-09-09）：保护改为效果层——hook 停止解析命令文本，改快照比对加还原；全局协议文件系统只读；协议版本可从基线 commit 取回；dispatch.md 不得削减必需覆盖；收官项按适用选择而非默认全跑；worktree 依赖输入需声明。

- v2.5（2026-09-09）：按外部评审收窄——finding 拆 nature / blocks / scope，未分类即阻断，DEBT 不参与验收；协议副本同步只在 Boss 过目时做，执行中的 plan 钉版本；开工不 stash 不猜基线；delta 拆冻结/可调两份；MOVE 用 name-status 逐行判 R100；下游离线通过须核产物身份；梯子压成一轮自修加裁决；篇幅减半。
- v2.4（2026-09-09）：lane 分型、闸分级、审成品、单源性审计、DEBT、阈值只写推导、worktree 只给并行写 lane。
- v2.3（2026-08-21）：oracle 分权与 oracle-tamper 记账。
