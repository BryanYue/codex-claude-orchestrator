# 规格与执行的关联

随包 [orchestration-protocol.md](orchestration-protocol.md) 是用户提供的 v2.9 原文副本，用作可携带的参考，不修改其内容。项目 enable 优先绑定项目已有协议，否则复制此版本，记录 SHA-256。这个动作没有批准任何 delta、oracle 或基线。

## 采用完整协议的任务

协调者必须读取该任务实际批准的协议全文；本页只是接入映射，不是规则的替代品。

| 正式材料 | 本插件如何接入 |
| --- | --- |
| delta.md / 协议版本与 sha / 批准基线 | requirement_sources 包含明确原始约束；正式模式的 packet 携带 protocol_binding。MCP/bridge 核对批准基线中的协议和 delta 内容身份；授权来源仍由主代理核验，不能由工具生成批准 |
| dispatch.md | 主代理按证据选 lane / 模型 / owned_files / gate / 上限，变更记录原因；不减少 delta 必需覆盖 |
| PROGRESS / DECISIONS / DEBT / findings.jsonl / final.md | 主代理为唯一记录写入者，沿用任务现有目录；添加 run_id/run_dir 和裁决证据，不能另造 Claude 专属正式状态 |
| 波次闸 / review / oracle 保护 | 按原文 lane 类型和项目约定执行。Claude 此桥没有 shell，构建/测试由 Codex 运行；普通单席 review 不能替代要求的完整审查或保护层 |
| 一轮自修后的裁决 | finding 身份贯穿 revision，主代理另派/回退/park；仅暂停依赖新决定的工作 |
| 收官 | 对照原句、职责、不变量与事实 owner，完成适用闸和单源性审计；不能只看工具 success |

### 正式身份字段

采用完整协议时，由主代理从已有批准记录构造，不由用户手工填写：

```json
{
  "protocol_binding": {
    "baseline_commit": "完整的40位批准基线commit",
    "protocol_path": "docs/internal/orchestration-protocol.md",
    "protocol_sha256": "该基线正文的64位sha256",
    "delta_path": "docs/internal/orchestrator/任务/delta.md",
    "delta_sha256": "该基线delta正文的64位sha256",
    "dispatch_path": "docs/internal/orchestrator/任务/dispatch.md",
    "progress_path": "docs/internal/orchestrator/任务/PROGRESS.md",
    "approval_source": "能回溯明确授权的任务消息或批准记录"
  }
}
```

这里的 baseline_commit 是正式需求批准身份，可以早于当前 HEAD；packet 顶层 baseline_commit 是本轮代码输入身份，若给出则要求当前 HEAD 相同。两者不能混用。上述检查证明文件身份，不能证明 approval_source 文本是真的、业务验收正确、oracle 全集已受 OS 保护。

新增任务尚无批准 delta 时先建立可审阅草稿，沿用已有授权能确定的内容；只针对缺失的必要决定提问。既有 plan 恢复时从明确基线取回原协议，不能因升级插件/修改项目默认版本而替换它。

## 能力边界

本插件提供执行约束、真实事件、版本身份和结果证据。v2.9 所要求的正式 oracle 清单登记、实际 OS sandbox、各项目构建/下游/设备闸，由对应项目和运行宿主提供。缺失必需保护时停止依赖它的执行，不把项目 enable、hook 或一次探针说成完整保护。不自动安装整套全局 hook，不自动合入、发布，不把所有任务强制升级为完整协议。
