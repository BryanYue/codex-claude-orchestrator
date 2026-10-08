"""Render the Markdown brief that is sent to Claude as its prompt, byte for byte.

User words appear once, verbatim, inside tags that their own text cannot close.
The brief states where each part came from and which part wins a conflict.
It carries no review checklist, packet dump or bridge bookkeeping.
"""
from __future__ import annotations

import time
from typing import Any

ORIGIN_LABELS = {"user": "用户", "doc": "原件", "coordinator": "协调者"}


def _tag(base: str, texts: list[str]) -> str:
    """A tag name whose closing form occurs in none of the wrapped texts."""
    tag, counter = base, 1
    while any(f"</{tag}" in text for text in texts):
        counter += 1
        tag = f"{base}_{counter}"
    return tag


def _stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(epoch))


def _labelled(items: list[dict[str, str]]) -> list[str]:
    return [f"- [{ORIGIN_LABELS[item['origin']]}] {item['text']}" for item in items]


def _user_words(packet: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    messages = packet["user_messages"]
    if not messages:
        return ["## 用户原话", "",
                "本任务没有用户原话。协调者给出的原因：" + str(packet["no_user_words_reason"]),
                "需求只来自下方的原件和协调者说明；不确定的地方写进 questions。", ""]
    tag = _tag("user_message", [message["text"] for message in messages])
    new_from = (plan.get("continued") or {}).get("message_count", 0)
    lines = ["## 用户原话", "",
             "下面是用户的原话，逐字照录。source=relayed 表示由其他代理转交，并非用户直接发给 Codex。", ""]
    for index, message in enumerate(messages, start=1):
        attributes = f' index="{index}" source="{message["source"]}"'
        if message.get("at"):
            attributes += f' at="{message["at"]}"'
        if plan.get("continued") and index > new_from:
            attributes += ' new_this_round="true"'
        lines += [f"<{tag}{attributes}>", message["text"], f"</{tag}>", ""]
    return lines


def _inputs(plan: dict[str, Any]) -> list[str]:
    frozen = plan.get("inputs") or []
    skipped = plan.get("inputs_skipped") or []
    if not frozen and not skipped:
        return []
    lines = ["## 先读的原件", "",
             "开工时按字节复制的只读副本。先读完再动手；它们的地位等同用户原话。", ""]
    for entry in frozen:
        size = f"{entry['bytes']} 字节" if entry.get("kind") == "file" else f"目录，{entry.get('files', 0)} 个文件"
        lines.append(f"- `{entry['copy']}`（原路径 `{entry['original']}`，{size}）")
    if skipped:
        lines += ["", "以下内容按敏感文件规则未复制：", *[f"- `{path}`" for path in skipped[:50]]]
    return lines + [""]


def _environment(packet: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    copy = plan.get("profile") == "copy"
    lines = ["## 环境", ""]
    if copy:
        lines += [f"- 工作目录：`{plan['execution_cwd']}`。这是原仓库当前状态的独立 Git 副本（含未提交改动），可以自由编辑和运行命令。",
                  f"- 原仓库 `{plan['source_root']}`、它的 Git 元数据和插件运行目录受操作系统写保护；写入会失败，这是预期行为。",
                  "- 凭证、网络和其他本机路径没有隔离；不要读取或打印凭证。",
                  "- 可用工具：Bash、文件读写、Agent、Skill、Workflow 等内置工具；外部 MCP 关闭。"]
    else:
        lines += [f"- 工作目录：`{plan['execution_cwd']}`（原目录，只读）。",
                  "- 只有 Read、Glob、Grep" + ("、WebFetch、WebSearch" if packet["web"] else "") + "；没有写文件和执行命令的工具。"]
    lines.append("- 联网：已获用户授权。" if packet["web"] else "- 联网：未获授权。不要访问网络；需要外部资料时写进 not_verified 或 questions。")
    lines.append(f"- 截止时间：{_stamp(plan['deadline'])}。到点进程会被终止，"
                 + ("未写入交付文件的内容不会被收回。" if copy else "未提交的结构化结果不会被收回。"))
    layer = plan.get("instruction_layer") or {}
    files = layer.get("files") or []
    hooks = layer.get("hooks") or []
    if files or hooks:
        lines += ["- 宿主指令层：本会话还会加载下列用户/项目指令文件和 hooks。它们是通用偏好或项目规则；"
                  "与本任务书的用户原话、原件或约束冲突时以本任务书为准，并把冲突写进 disputes。"]
        lines += [f"  - `{entry['path']}`（{entry['scope']}）" for entry in files[:30]]
        events = sorted({hook["event"] for hook in hooks})
        if events:
            lines.append("  - hooks：" + "、".join(events))
    return lines + [""]


def _task(packet: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    lines = ["## 你的任务", ""]
    if packet["kind"] == "implement":
        lines += ["在工作目录里实现上述需求：",
                  "- 自己运行相关的测试、构建和 lint，并记录真实命令与结果；没跑或跑不了的写进 not_verified。",
                  "- 不要 git commit、push 或发布；插件会从副本生成 patch，由 Codex 审核后决定是否合入。",
                  "- 不要留下后台常驻进程。"]
        if packet["verify"]:
            lines += ["- 协调者建议的验证命令：", *[f"  - `{command}`" for command in packet["verify"]]]
        if packet["write_hint"]:
            lines.append("- 预计改动范围（仅供参考，超出不会被拦截，但要在报告里说明原因）："
                         + "、".join(f"`{item}`" for item in packet["write_hint"]))
        if packet["protected"]:
            lines.append("- 改动这些路径需要在报告里逐条说明理由：" + "、".join(f"`{item}`" for item in packet["protected"]))
    else:
        lines += ["先直接回答用户原话里的问题或完成其中要求的分析，再给依据。"]
        if packet["focus"]:
            lines += ["- 关注点（协调者从原话整理，原话优先）：", *[f"  - {item}" for item in packet["focus"]]]
        else:
            lines.append("- 范围按用户原话判断，不要套用固定的审查清单。")
        lines += ["- 区分已核实（读过源码、运行过命令）和推断，推断要写明。",
                  "- 发现的问题或建议写进 result 的 items（每条 id、title、confidence，尽量附 evidence 和 location）；没有就省略。"]
        if plan.get("profile") == "copy":
            lines.append("- 可以在副本里复现问题、运行测试；你在副本里写出的文件会随 patch 交回，作为交付物或证据。")
            if plan.get("workflow"):
                workflow = plan["workflow"]
                lines.append(f"- 可选：内置 Workflow `{workflow['name']}` 会按关注点分别审查再交叉核验；"
                             f"参数为 {{\"brief_path\": \"{plan['brief_path']}\", \"focus\": [...]}}。"
                             "是否使用由你根据原话判断；用了就等它完成再写交付文件。")
    return lines + [""]


def _deliver(plan: dict[str, Any]) -> list[str]:
    fields = ("{\n"
              '  "status": "completed | partial | blocked",\n'
              '  "summary": "一两句话的结论",\n'
              '  "acceptance": [{"criterion": "完成标准或原话要求", "self_check": "met | not_met | unverified", "evidence": "..."}],\n'
              '  "tests_run": [{"command": "...", "exit_code": 0, "outcome": "..."}],\n'
              '  "not_verified": ["没能核实或没运行的内容"],\n'
              '  "disputes": ["与原话、原件或约束的冲突，以及对协调者设定的异议"],\n'
              '  "questions": ["只有用户能决定的问题"],\n'
              '  "items": [{"id": "F1", "title": "...", "confidence": "verified | inferred", "evidence": "...", "location": "...", "severity": "..."}]\n'
              "}")
    report = ("第一节必须是“需求对照”：逐条列出用户原话里的要求和完成标准，标明已做 / 部分 / 未做并附证据。"
              "之后依次写：结论或改动说明、实际运行的验证（命令、退出码、结果）、未验证项、偏离与异议、需要用户决定的问题。")
    if plan.get("profile") == "copy":
        return ["## 交付", "",
                "边做边写，超时或中断时已经写下的内容也会被收回。只写这两个文件：",
                f"1. `{plan['outbox']}/report.md`：{report}",
                f"2. `{plan['outbox']}/result.json`，结构如下（只填有内容的字段；status 和 summary 必填）：",
                "", "```json", fields, "```", "",
                "最后一条回复用一两句话说明交付位置即可。", ""]
    return ["## 交付", "",
            "用 StructuredOutput 一次性交回结果：report_markdown 放完整报告（" + report + "），其余字段结构如下：",
            "", "```json", fields, "```", ""]


VERDICT_LABELS = {"accepted": "采纳", "accepted_with_corrections": "修正后采纳", "rejected": "退回"}
NEXT_LABELS = {"done": "完成", "next_round": "再做一轮", "codex_finishes": "由 Codex 完成剩余部分"}
DISPOSITION_LABELS = {"accepted": "成立", "downgraded": "降级", "rejected": "不成立"}


def _decision(entry: dict[str, Any]) -> list[str]:
    decision = entry["decision"]
    lines = [f"- 第 {entry.get('round')} 轮（{entry['run_id']}，`{entry['file']}`）："
             f"{VERDICT_LABELS.get(decision.get('verdict'), decision.get('verdict'))}，"
             f"下一步：{NEXT_LABELS.get(decision.get('next'), decision.get('next'))}"
             + ("；Codex 已把这一轮的 delivery.patch 应用到原仓库" if decision.get("applied") else "")]
    note = decision.get("note")
    if note:
        tag = _tag("codex_note", [note])
        lines += [f"  <{tag}>", "  " + note.replace("\n", "\n  "), f"  </{tag}>"]
    for item in decision.get("item_decisions") or []:
        reason = f"：{item['reason']}" if item.get("reason") else ""
        lines.append(f"  - {item['id']} {DISPOSITION_LABELS.get(item.get('disposition'), item.get('disposition'))}{reason}")
    return lines


def _continuation(plan: dict[str, Any], continued: dict[str, Any]) -> list[str]:
    lines = ["## 本轮是续接", "",
             f"这是同一任务的第 {plan['round']} 轮，接着上一轮（{continued['run_id']}）的工作继续。"
             "标有 new_this_round 的原话是本轮新增的；以上全部内容是本轮完整的任务，按它执行。"]
    if plan.get("profile") == "copy":
        carried = plan.get("carried") or {}
        lines.append("工作目录按原仓库的当前状态重新建立（包括 Codex 合入或修正过的代码）"
                     + (f"，并放上了此前尚未交付的改动（来自 {carried.get('from_run')}）" if carried.get("mode") == "replayed" else "")
                     + "。以当前工作目录的代码为准，不要按上一轮报告里的旧代码推断。")
    if continued.get("report"):
        lines.append(f"上一轮的报告：`{continued['report']}`（只读，供参考）。")
    decisions = continued.get("decisions") or []
    if decisions:
        lines += ["", "Codex 对此前各轮的裁决如下，效力高于这些轮次的报告：被判为不成立或降级的条目不要再当作成立的问题处理，"
                  "除非你有新的证据（写进 disputes）。"]
        for entry in decisions:
            lines += _decision(entry)
    return lines + [""]


def render(packet: dict[str, Any], plan: dict[str, Any]) -> str:
    kind = "实现" if packet["kind"] == "implement" else "分析"
    lines = [f"# 委派任务（{kind}）", "",
             "你是 Codex 委派的 Claude Code。Codex 负责规划和最终验收；你交回的结果不等于通过，Codex 会逐项核验。",
             "下文的“你”指你（Claude）；用户原话中的“你”“它”通常是用户在对 Codex 说话。", "",
             "## 优先级", "",
             "1. 用户原话决定做什么、问什么；用户提供的原件（见“先读的原件”）与原话同级。",
             "2. 约束是权限与安全边界，必须遵守。标为“协调者”的约束如果与原话冲突，仍先照做，并把冲突写进 disputes。",
             "3. 协调者说明是 Codex 的理解，可能有误；与原话或原件冲突时以原话为准，并写进 disputes。",
             "4. 本任务书里的做法建议优先级最低。",
             "5. 遇到冲突就写出来，不要自行取舍；只有用户能决定的事写进 questions，不要替用户做决定。", ""]
    lines += _user_words(packet, plan)
    lines += _inputs(plan)
    if packet["brief"]:
        tag = _tag("coordinator_note", [packet["brief"]])
        lines += ["## 协调者说明（可能有误）", "", f"<{tag}>", packet["brief"], f"</{tag}>", ""]
    if packet["constraints"]:
        lines += ["## 约束", "", *_labelled(packet["constraints"]), ""]
    if packet["done_when"]:
        lines += ["## 完成标准", "", *_labelled(packet["done_when"]),
                  "", "在 result 的 acceptance 里逐条自评；最终是否通过由 Codex 判定。", ""]
    continued = plan.get("continued")
    if continued:
        lines += _continuation(plan, continued)
    lines += _task(packet, plan)
    lines += _environment(packet, plan)
    lines += _deliver(plan)
    return "\n".join(lines)
