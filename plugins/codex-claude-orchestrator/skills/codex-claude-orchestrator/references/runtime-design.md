# Runtime、审查与恢复边界

本文解释随包 Runtime 的职责。实际加载版本、代码身份、run 回执与发布验收决定可用能力；升级 Markdown 不升级旧进程，也不把历史夹具变成当前真实验收。

## 职责与证据

链路为 Codex → MCP Runtime → Bridge → 本机 Claude CLI。Codex 拥有目标、写入协调和验收；Runtime 管理 packet、CLI/插件身份、生命周期、进程组、取消与状态；Bridge 执行工具政策并保存公开 stream、结果、回执和工作区证据。工作台区分执行、报告和核验，公开活动不包含私有思考。无人值守 daemon、自动更换协调者、自动合并/发布不属于本版。

`reported` 是交回结果，accepted/returned 是 Codex 核验决定；只允许未 superseded 的 reported 写决定。returned 可继续 revision_requested，或由 Codex 补齐并核验后记录 completed_by_codex。失败、取消、超时、阻断、unknown 的报告保存为未验收证据，独立处置写原任务 PROGRESS，不改原回执。findings 是可选的追溯材料，逐项裁决保留原发现与理由；数量不是完成判据。

contract 身份定义兼容性，启动 loaded-code 身份防止磁盘与已加载模块混用，plugin code_digest 描述整包实际内容；三者用途不同，不以一个哈希替代。plugin-identity 在 run 开始固定；Git 来源记录 revision/dirty，ZIP manifest 只证明来源声明，缺少验证时明确 unknown。详见 [bridge.md](bridge.md) 的回执字段。

工作区快照说明观察到的变化和原有 dirty 状态，不证明写入者归因、全文件系统无变化或真实设备效果。compact 仅减少显示，原始事件/报告仍可读取；游标继续对应原流。恢复与验收不得依据摘要补造缺失证据。

## 审查工具与资源保护

review_scope 定义 defects/quality/full 的覆盖；review_mode 定义 strict/isolated 的执行方式，两者独立。带 user_request 的新 MCP Git review 默认 isolated；旧调用缺原话或原始 packet 省略模式保持 strict，artifacts 仅 strict。原始 user_request 与来源不可由 objective 替代。

isolated 提供独立可写 clone，用于复现、测试、脚本、Agent/Skill/Workflow；OS 写保护限制原仓库、相关 Git 目录与外部 requirement_sources。删除 remote、hook 和前后快照都不能单独证明预防写入。所需保护不可用时明确拒绝，不降级为假隔离。此边界不是网络、凭证或任意外部副作用沙箱；外部 MCP 默认不继承，行为仍须符合用户授权和宿主权限。

副本和原仓分开记录；副本随 run 保留供纠正/resume，returned 后仍保留。只有处置完成且停止确认后才显式清理；unknown 不清理。续跑保持要求、输入、文件/资源范围、模型与 CLI 可执行身份，边界变化则 fresh。OS 保护不代替全部项目 oracle/构建/下游/设备闸。

strict 只读工具与精确来源守卫、implement 的 owned_files 政策继续保留。外部源的父目录搜索不在精确授权内；保存 Workflow 的作者须向每个子代理传递相同边界。Runtime 锁仅约束接入本插件的执行者；Codex 仍需协调外部写入者。

## Workflow：能力、执行与交付分开

isolated 可以选择 Workflow，但工具可用、合法父级结果与非空报告都不证明 Workflow 实际执行、完成或覆盖充分。评估实际工具调用、关联完成与完整输出，保留未完成项和核验失败 findings；单席报告不冒称多席策略已验收。工作流使用运行时注入能力，不能把脚本交普通 Node 就当作真实 Workflow。

兼容 `workflow_review` 保持 inventory 的 name/path/sha256/args 精确绑定、只读、fresh-only，不接受 inline、其他 Workflow 或 correction/resume。完成需要同一调用的确认、关联通知、最终父级结果和完整报告证据；每次真实调用分别记录。旧私有临时输出布局及稳定原因码仅服务此兼容入口，细节集中在 bridge，父级 summary 不替代完整报告。沿 workflow_report 的 next_offset_bytes 读完整产物并核哈希；取到字节仍不等于推理正确。

历史 2.1.276–2.1.278 的单席只读夹具、2.1.287 的长短报告交付验收只证明当时场景。旧负面夹具观察到子代理 Read 被 hook 拒绝，Bash/Write 未实际调用；不能写成执行过 Bash 拒绝。本版不按 CLI 历史版本白名单准入，以本轮真实证据为准。

## 启动、停止与 unknown

Runtime 从有效 state_root 启动 Bridge，Claude 使用核对后的任务 cwd（isolated 为副本 cwd）。创建 run 前核对已加载执行代码与磁盘一致；升级后旧连接可能失败，应新连接核身份，保留原 run_id。

执行目录外的 runtime_pre_spawn 绑定 nonce、代码、packet、CLI 与 lane；Bridge 接管后记录 pre_dispatch → launch_intent → executing → terminal。launch_intent、child_started=null 或缺 child.json 不能证明未启动。Popen 后立即记录 PID/PGID，再在非阻塞监督循环写提示词并读 stdout；超时从启动计算，预检结束仍重查取消标记。提示词未完整送达保存失败证据，取消/超时保持自身状态。

Claude 进程组不继承 lane 锁。Bridge 启动前发布与 run/lane/lifecycle 绑定的 marker；只有进程组确认停止或明确未启动，且回执/终态已落盘后才删除自己的 marker。崩溃和停止不明保留它，阻止跨状态目录重复派发。终态清理遇瞬时错误只记录 admission_cleanup，不把可信终态变 unknown；下一次派单/重启在独占 lane 和停止证据下重试，不删除外来或异常 marker。

重连先核 packet、回执、代码/CLI 绑定、Bridge/Claude 停止事实和 lane，再采纳可信终态。证据不足保持 unknown，用 recovery/reconcile 核查；人工恢复只解除占用，后续新 revision fresh，不创造 reported/accepted。带有效 lane_identity 且原子目录消失的 Git run 可在仍属同 lane 的真实根目录取证；symlink、越界、嵌套仓库、身份不符或缺绑定的历史记录不能推测恢复资格。

整个 run-dir 尚未生成时，只有新启动绑定完整才可使用外置 packet，恢复回执存 state_root/recovery-receipts；目录存在时必须核对目录内 packet/CLI 副本。先持久化恢复事实，再提交 registry，最后清理匹配 marker；重试保留第一次绑定原因/证据，不补造目录或活动日志。恢复条件与 API 详见 bridge。

## 协调内容随发布固定

新任务固定随插件发布的 bundled 说明；在线抓取、候选审查和激活已退役。历史 run 的不可变 content_binding 与快照保留可读，resume 核对原绑定和代码兼容性；旧 active 配置不改变新任务说明。新文案不替换正式 protocol_binding、已批准要求或验收。损坏历史快照须报告缺口，不静默换成新 bundled 来假装原会话延续。
